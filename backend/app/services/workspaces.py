from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import begin_immediate
from ..domain import (
    DesiredState,
    ObservedState,
    OperationStatus,
    OperationType,
    ProvisionStatus,
    UserStatus,
)
from ..errors import AppError
from ..hub.base import HubAuthError, HubUnavailableError, JupyterHubProvider
from ..models import (
    AuditEvent,
    Operation,
    SpawnAuthorization,
    User,
    UserSession,
    Workspace,
    WorkspaceDeletionJob,
    WorkspaceProfile,
    WorkspaceVolumeSlot,
)
from ..profile_values import cpu_limit_to_millicores
from ..security import TokenCipher, json_dumps_safe, keyed_hash
from .profile_offers import resolve_offer
from .provisioning import active_inventory_is_valid
from .resource_policy import get_resource_policy, profile_is_allowed


@dataclass(frozen=True)
class WorkspaceOperationResult:
    workspace: Workspace
    operation: Operation
    reused: bool = False


@dataclass(frozen=True)
class ActiveReservations:
    count: int
    cpu_millicores: int
    memory_mb: int


def _audit(
    db: Session,
    *,
    actor_id: str,
    workspace_id: str | None,
    action: str,
    result: str,
    request_id: str,
    metadata: dict[str, object] | None = None,
) -> None:
    db.add(
        AuditEvent(
            id=str(uuid.uuid4()),
            actor_user_id=actor_id,
            workspace_id=workspace_id,
            action=action,
            result=result,
            request_id=request_id,
            safe_metadata_json=json_dumps_safe(metadata or {}),
        )
    )


def owned_workspace(db: Session, user_id: str, workspace_id: str) -> Workspace:
    workspace = db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.owner_user_id == user_id,
            Workspace.archived_at.is_(None),
        )
    )
    if workspace is None:
        raise AppError(404, "WORKSPACE_NOT_FOUND", "Workspace was not found")
    return workspace


def active_workspace(db: Session, workspace_id: str) -> Workspace:
    workspace = db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.archived_at.is_(None),
        )
    )
    if workspace is None:
        raise AppError(404, "WORKSPACE_NOT_FOUND", "Workspace was not found")
    return workspace


def _cancel_active_lifecycle_operations(
    db: Session, *, workspace_id: str, now: datetime
) -> None:
    """Maintain one authoritative active lifecycle operation per workspace."""

    db.execute(
        update(Operation)
        .where(
            Operation.workspace_id == workspace_id,
            Operation.operation_type.in_(
                [
                    OperationType.CREATE.value,
                    OperationType.START.value,
                    OperationType.STOP.value,
                    OperationType.RESTART.value,
                ]
            ),
            Operation.status.in_(
                [OperationStatus.PENDING.value, OperationStatus.RUNNING.value]
            ),
        )
        .values(
            status=OperationStatus.CANCELLED.value,
            error_code="SUPERSEDED",
            error_summary="Operation was superseded by a newer lifecycle request",
            completed_at=now,
            next_attempt_at=None,
            lease_owner=None,
            lease_expires_at=None,
        )
    )


class WorkspaceService:
    def __init__(self, settings: Settings, cipher: TokenCipher) -> None:
        self.settings = settings
        self.cipher = cipher

    def _request_fingerprint(self, operation_type: str, payload: object) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        return keyed_hash(
            f"workspace-operation-v1\0{operation_type}\0{canonical}",
            self.settings.internal_hmac_key,
        )

    def _observation_is_fresh(self, workspace: Workspace, now: datetime) -> bool:
        return bool(
            not workspace.stale
            and workspace.last_reconciled_at is not None
            and workspace.last_reconciled_at
            >= now - timedelta(seconds=self.settings.reconciliation_freshness_seconds)
        )

    def _existing_operation(
        self,
        db: Session,
        actor_user_id: str,
        idempotency_key: str,
        *,
        operation_type: OperationType,
        workspace_id: str | None = None,
        request_fingerprint: str,
    ) -> WorkspaceOperationResult | None:
        operation = db.scalar(
            select(Operation).where(
                Operation.actor_user_id == actor_user_id,
                Operation.idempotency_key == idempotency_key,
            )
        )
        if operation is None:
            return None
        workspace = db.get(Workspace, operation.workspace_id)
        if workspace is None:  # pragma: no cover - guarded by foreign key
            raise AppError(500, "INVARIANT_VIOLATION", "Operation workspace is missing")
        binding_matches = (
            operation.operation_type == operation_type.value
            and operation.request_fingerprint == request_fingerprint
        )
        if workspace_id is not None:
            binding_matches = binding_matches and workspace.id == workspace_id
        if not binding_matches:
            raise AppError(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
            )
        return WorkspaceOperationResult(workspace, operation, reused=True)

    def _require_active(self, db: Session, user: User) -> None:
        self._require_active_status(user)
        if not active_inventory_is_valid(db, user):
            raise AppError(
                409,
                "PROVISIONING_INVARIANT_FAILED",
                "Active user volume inventory is incomplete",
            )

    @staticmethod
    def _require_active_status(user: User) -> None:
        if user.status == UserStatus.PROVISIONING.value:
            raise AppError(
                409,
                "PROVISIONING_REQUIRED",
                "Private workspace volume slots have not been provisioned",
            )
        if user.status != UserStatus.ACTIVE.value:
            raise AppError(403, "USER_NOT_ACTIVE", "The platform account is not active")

    def _require_execution_health(self) -> None:
        if not self.settings.execution_host_healthy:
            raise AppError(
                503,
                "EXECUTION_HOST_UNHEALTHY",
                "Execution-host network or storage health is not verified",
            )

    def _require_capacity(
        self,
        reservations: ActiveReservations,
        profile: WorkspaceProfile,
        *,
        cpu_budget_millicores: int,
        memory_budget_mb: int,
    ) -> None:
        if reservations.count >= self.settings.max_active_workspaces:
            raise AppError(
                429, "CAPACITY_LIMIT", "Active workspace capacity of 15 reached"
            )
        try:
            requested_cpu = cpu_limit_to_millicores(profile.cpu_limit)
        except ValueError as exc:
            raise AppError(
                500,
                "PROFILE_RESOURCE_INVALID",
                "Pinned workspace profile resource values are invalid",
            ) from exc
        if profile.memory_limit_mb <= 0:
            raise AppError(
                500,
                "PROFILE_RESOURCE_INVALID",
                "Pinned workspace profile resource values are invalid",
            )
        if (
            reservations.cpu_millicores + requested_cpu > cpu_budget_millicores
            or reservations.memory_mb + profile.memory_limit_mb > memory_budget_mb
        ):
            raise AppError(
                429,
                "RESOURCE_CAPACITY_LIMIT",
                "Aggregate workspace CPU or memory capacity would be exceeded",
            )

    def create(
        self,
        db: Session,
        *,
        user: User,
        portal_session: UserSession,
        profile_id: str,
        profile_version: int,
        display_name: str | None,
        idempotency_key: str,
        request_id: str,
    ) -> WorkspaceOperationResult:
        begin_immediate(db)
        self._require_active(db, user)
        request_fingerprint = self._request_fingerprint(
            OperationType.CREATE.value,
            {
                "profile_id": profile_id,
                "profile_version": profile_version,
                "display_name": display_name,
            },
        )
        existing = self._existing_operation(
            db,
            user.id,
            idempotency_key,
            operation_type=OperationType.CREATE,
            request_fingerprint=request_fingerprint,
        )
        if existing:
            db.commit()
            return existing
        used = int(
            db.scalar(
                select(func.count(Workspace.id)).where(
                    Workspace.owner_user_id == user.id, Workspace.archived_at.is_(None)
                )
            )
            or 0
        )
        if used >= self.settings.max_workspaces_per_user:
            db.rollback()
            raise AppError(
                409, "WORKSPACE_QUOTA_EXCEEDED", "Workspace limit of 5 reached"
            )
        offer, profile = resolve_offer(db, profile_id, profile_version)
        policy = get_resource_policy(db, self.settings)
        if not profile_is_allowed(profile, policy):
            db.rollback()
            raise AppError(
                409,
                "PROFILE_NOT_SELECTABLE",
                "Workspace profile is not allowed by the current resource policy",
            )
        used_slots = select(Workspace.private_volume_slot_id).where(
            Workspace.archived_at.is_(None)
        )
        slot = db.scalar(
            select(WorkspaceVolumeSlot)
            .where(
                WorkspaceVolumeSlot.owner_user_id == user.id,
                WorkspaceVolumeSlot.provision_status
                == ProvisionStatus.PROVISIONED.value,
                WorkspaceVolumeSlot.hard_limit_mb == profile.private_disk_limit_mb,
                ~WorkspaceVolumeSlot.id.in_(used_slots),
            )
            .order_by(WorkspaceVolumeSlot.slot_no)
            .limit(1)
        )
        if slot is None:
            db.rollback()
            raise AppError(
                409,
                "PROVISIONING_REQUIRED",
                "No verified private volume slot matches the profile",
            )

        workspace_id = str(uuid.uuid4())
        server_name = f"ws-{uuid.uuid4().hex}"
        now = datetime.utcnow()
        workspace = Workspace(
            id=workspace_id,
            owner_user_id=user.id,
            profile_id=profile.id,
            profile_version=profile.version,
            profile_offer_id=offer.id,
            profile_offer_version=offer.row_version,
            profile_offer_name_snapshot=offer.name,
            hub_target_key=f"jupyterhub:{user.hub_username}:{server_name}",
            hub_server_name=server_name,
            private_volume_slot_id=slot.id,
            display_name=display_name or f"환경-{slot.slot_no}",
            desired_state=DesiredState.STOPPED.value,
            observed_state=ObservedState.NOT_FOUND.value,
            progress_percent=100,
            stale=False,
            last_reconciled_at=now,
            environment_generation=1,
            applied_user_environment_generation=0,
            applied_workspace_environment_generation=0,
            spec_version=1,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        operation = Operation(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            requested_by_user_id=user.id,
            actor_user_id=user.id,
            auth_session_id_hash=portal_session.id_hash,
            credential_mode="USER_DELEGATED",
            operation_type=OperationType.CREATE.value,
            status=OperationStatus.SUCCEEDED.value,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            attempts=0,
            requested_at=now,
            completed_at=now,
        )
        # Explicit flushes keep SQLite FK ordering deterministic even though these
        # mappings intentionally avoid ORM relationships.
        db.add(workspace)
        db.flush()
        db.add(operation)
        db.flush()
        _audit(
            db,
            actor_id=user.id,
            workspace_id=workspace_id,
            action="WORKSPACE_CREATED",
            result="SUCCEEDED",
            request_id=request_id,
            metadata={
                "profile_offer_id": offer.id,
                "profile_offer_version": offer.row_version,
                "runtime_profile_id": profile.id,
                "runtime_profile_version": profile.version,
            },
        )
        db.commit()
        return WorkspaceOperationResult(workspace, operation)

    def action(
        self,
        db: Session,
        *,
        owner: User,
        actor: User,
        portal_session: UserSession | None,
        credential_mode: str,
        workspace_id: str,
        target: DesiredState,
        idempotency_key: str,
        request_id: str,
    ) -> WorkspaceOperationResult:
        begin_immediate(db)
        if target == DesiredState.RUNNING or credential_mode == "USER_DELEGATED":
            self._require_active(db, owner)
        if target == DesiredState.RUNNING:
            self._require_execution_health()
        operation_type = (
            OperationType.START
            if target == DesiredState.RUNNING
            else OperationType.STOP
        )
        request_fingerprint = self._request_fingerprint(
            operation_type.value,
            {"workspace_id": workspace_id, "target": target.value},
        )
        existing = self._existing_operation(
            db,
            actor.id,
            idempotency_key,
            operation_type=operation_type,
            workspace_id=workspace_id,
            request_fingerprint=request_fingerprint,
        )
        if existing:
            db.commit()
            return existing
        workspace = owned_workspace(db, owner.id, workspace_id)
        if (
            workspace.deletion_started_at is not None
            or workspace.desired_state == DesiredState.DELETED.value
        ):
            db.rollback()
            raise AppError(409, "WORKSPACE_DELETING", "Workspace deletion has started")
        now = datetime.utcnow()
        observation_fresh = self._observation_is_fresh(workspace, now)
        no_op = (
            target == DesiredState.RUNNING
            and workspace.observed_state == ObservedState.RUNNING.value
            and observation_fresh
        ) or (
            target == DesiredState.STOPPED
            and workspace.observed_state
            in {ObservedState.STOPPED.value, ObservedState.NOT_FOUND.value}
            and observation_fresh
        )
        if target == DesiredState.RUNNING and not no_op:
            profile = db.get(
                WorkspaceProfile, (workspace.profile_id, workspace.profile_version)
            )
            if profile is None:  # pragma: no cover - guarded by foreign key
                db.rollback()
                raise AppError(
                    500, "INVARIANT_VIOLATION", "Pinned workspace profile is missing"
                )
            reservations = active_reservations(db, exclude_workspace_id=workspace.id)
            policy = get_resource_policy(db, self.settings)
            try:
                self._require_capacity(
                    reservations,
                    profile,
                    cpu_budget_millicores=policy.cpu_budget_millicores,
                    memory_budget_mb=policy.memory_budget_mb,
                )
            except AppError:
                db.rollback()
                raise
        if workspace.desired_state != target.value:
            workspace.desired_state = target.value
            workspace.spec_version += 1
            workspace.row_version += 1
            workspace.updated_at = datetime.utcnow()
        _cancel_active_lifecycle_operations(db, workspace_id=workspace.id, now=now)
        operation = Operation(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            requested_by_user_id=owner.id,
            actor_user_id=actor.id,
            auth_session_id_hash=(portal_session.id_hash if portal_session else None),
            credential_mode=credential_mode,
            operation_type=operation_type.value,
            status=(
                OperationStatus.SUCCEEDED.value
                if no_op
                else OperationStatus.PENDING.value
            ),
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            attempts=0,
            requested_at=now,
            completed_at=now if no_op else None,
            next_attempt_at=None if no_op else now,
        )
        db.add(operation)
        _audit(
            db,
            actor_id=actor.id,
            workspace_id=workspace.id,
            action=f"WORKSPACE_{operation_type.value}_REQUESTED",
            result="NO_OP" if no_op else "ACCEPTED",
            request_id=request_id,
            metadata={"target_owner_user_id": owner.id},
        )
        db.commit()
        return WorkspaceOperationResult(workspace, operation)

    def restart(
        self,
        db: Session,
        *,
        owner: User,
        actor: User,
        portal_session: UserSession | None,
        credential_mode: str,
        workspace_id: str,
        idempotency_key: str,
        request_id: str,
    ) -> WorkspaceOperationResult:
        begin_immediate(db)
        self._require_active(db, owner)
        self._require_execution_health()
        request_fingerprint = self._request_fingerprint(
            OperationType.RESTART.value, {"workspace_id": workspace_id}
        )
        existing = self._existing_operation(
            db,
            actor.id,
            idempotency_key,
            operation_type=OperationType.RESTART,
            workspace_id=workspace_id,
            request_fingerprint=request_fingerprint,
        )
        if existing:
            db.commit()
            return existing
        workspace = owned_workspace(db, owner.id, workspace_id)
        if workspace.deletion_started_at is not None:
            db.rollback()
            raise AppError(409, "WORKSPACE_DELETING", "Workspace deletion has started")
        now = datetime.utcnow()
        if (
            workspace.observed_state != ObservedState.RUNNING.value
            or not self._observation_is_fresh(workspace, now)
        ):
            db.rollback()
            raise AppError(409, "WORKSPACE_NOT_RUNNING", "Workspace is not running")
        _cancel_active_lifecycle_operations(db, workspace_id=workspace.id, now=now)
        workspace.desired_state = DesiredState.RUNNING.value
        workspace.spec_version += 1
        workspace.row_version += 1
        workspace.updated_at = now
        db.execute(
            update(SpawnAuthorization)
            .where(
                SpawnAuthorization.workspace_id == workspace.id,
                SpawnAuthorization.consumed_at.is_(None),
                SpawnAuthorization.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        operation = Operation(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            requested_by_user_id=owner.id,
            actor_user_id=actor.id,
            auth_session_id_hash=(portal_session.id_hash if portal_session else None),
            credential_mode=credential_mode,
            operation_type=OperationType.RESTART.value,
            status=OperationStatus.PENDING.value,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            lifecycle_checkpoint="RESTART_STOPPING",
            attempts=0,
            requested_at=now,
            next_attempt_at=now,
        )
        db.add(operation)
        _audit(
            db,
            actor_id=actor.id,
            workspace_id=workspace.id,
            action="WORKSPACE_RESTART_REQUESTED",
            result="ACCEPTED",
            request_id=request_id,
            metadata={"target_owner_user_id": owner.id},
        )
        db.commit()
        return WorkspaceOperationResult(workspace, operation)

    def delete(
        self,
        db: Session,
        *,
        owner: User,
        actor: User,
        portal_session: UserSession | None,
        credential_mode: str,
        workspace_id: str,
        idempotency_key: str,
        request_id: str,
    ) -> WorkspaceOperationResult:
        if not self.settings.workspace_deletion_enabled:
            raise AppError(
                503,
                "WORKSPACE_DELETION_DISABLED",
                "Workspace deletion agent is not enabled",
            )
        begin_immediate(db)
        if credential_mode == "USER_DELEGATED":
            # A failed external wipe deliberately leaves the target slot WIPING.
            # Check account status here, but defer the five-slot inventory check
            # until we know whether this is that exact, safely-bound retry.
            self._require_active_status(owner)
        request_fingerprint = self._request_fingerprint(
            OperationType.DELETE.value, {"workspace_id": workspace_id}
        )
        existing = self._existing_operation(
            db,
            actor.id,
            idempotency_key,
            operation_type=OperationType.DELETE,
            workspace_id=workspace_id,
            request_fingerprint=request_fingerprint,
        )
        if existing:
            db.commit()
            return existing
        workspace = owned_workspace(db, owner.id, workspace_id)
        deletion_job = db.get(WorkspaceDeletionJob, workspace.id)
        failed_external = deletion_job is not None and deletion_job.status == "FAILED"
        failed_external_binding_valid = False
        if failed_external:
            slot = db.get(WorkspaceVolumeSlot, workspace.private_volume_slot_id)
            prior_delete = db.get(Operation, deletion_job.operation_id)
            failed_external_binding_valid = bool(
                slot is not None
                and prior_delete is not None
                and prior_delete.workspace_id == workspace.id
                and prior_delete.operation_type == OperationType.DELETE.value
                and prior_delete.status == OperationStatus.FAILED.value
                and deletion_job.expected_spec_version == workspace.spec_version
                and workspace.desired_state == DesiredState.DELETED.value
                and workspace.deletion_started_at is not None
                and workspace.deletion_checkpoint == "DELETION_FAILED"
                and slot.owner_user_id == owner.id
                and slot.provision_status == ProvisionStatus.WIPING.value
            )
            if not failed_external_binding_valid:
                db.rollback()
                raise AppError(
                    409,
                    "DELETION_INVARIANT_FAILED",
                    "Failed deletion binding changed",
                )
        retry_external = bool(
            failed_external_binding_valid
            and workspace.observed_state == ObservedState.NOT_FOUND.value
        )
        retry_external_lifecycle = bool(
            failed_external_binding_valid
            and workspace.observed_state != ObservedState.NOT_FOUND.value
        )
        latest_delete = db.scalar(
            select(Operation)
            .where(
                Operation.workspace_id == workspace.id,
                Operation.operation_type == OperationType.DELETE.value,
            )
            .order_by(Operation.requested_at.desc(), Operation.id.desc())
            .limit(1)
        )
        retry_lifecycle = retry_external_lifecycle or bool(
            deletion_job is None
            and workspace.deletion_started_at is not None
            and latest_delete is not None
            and latest_delete.status
            in {OperationStatus.FAILED.value, OperationStatus.AUTH_REQUIRED.value}
        )
        if credential_mode == "USER_DELEGATED" and not failed_external_binding_valid:
            # Initial deletion and control-plane retries still require the complete
            # provisioned inventory. Only the exact failed wipe binding above may
            # tolerate its own WIPING slot.
            self._require_active(db, owner)
        if workspace.deletion_started_at is not None and not (
            retry_external or retry_lifecycle
        ):
            db.rollback()
            raise AppError(
                409,
                "WORKSPACE_DELETION_PENDING",
                "Workspace deletion is already pending",
            )
        now = datetime.utcnow()
        if workspace.deletion_started_at is None:
            workspace.deletion_started_at = now
            workspace.deletion_checkpoint = "TOMBSTONED"
            workspace.desired_state = DesiredState.DELETED.value
            workspace.spec_version += 1
            workspace.row_version += 1
        workspace.last_error_code = None
        workspace.last_error_summary = None
        workspace.updated_at = now
        db.execute(
            update(SpawnAuthorization)
            .where(
                SpawnAuthorization.workspace_id == workspace.id,
                SpawnAuthorization.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        _cancel_active_lifecycle_operations(db, workspace_id=workspace.id, now=now)
        operation = Operation(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            requested_by_user_id=owner.id,
            actor_user_id=actor.id,
            auth_session_id_hash=(portal_session.id_hash if portal_session else None),
            credential_mode=credential_mode,
            operation_type=OperationType.DELETE.value,
            status=(
                OperationStatus.WAITING_EXTERNAL.value
                if retry_external
                else OperationStatus.PENDING.value
            ),
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            lifecycle_checkpoint=(
                "DELETION_PENDING" if retry_external else "DELETE_STOPPING"
            ),
            attempts=0,
            requested_at=now,
            next_attempt_at=None if retry_external else now,
        )
        db.add(operation)
        db.flush()
        if failed_external_binding_valid:
            assert deletion_job is not None
            deletion_job.operation_id = operation.id
            deletion_job.expected_spec_version = workspace.spec_version
            if retry_external:
                deletion_job.status = "PENDING"
                deletion_job.attempts = 0
                deletion_job.error_code = None
                deletion_job.error_summary = None
                deletion_job.completed_at = None
                deletion_job.lease_owner = None
                deletion_job.lease_expires_at = None
                deletion_job.updated_at = now
                workspace.deletion_checkpoint = "DELETION_PENDING"
            else:
                # Reconciliation can rediscover a stopped Hub named-server
                # record after a failed wipe. Keep the external job
                # unclaimable until the lifecycle worker removes that exact
                # record and observes NOT_FOUND again.
                workspace.deletion_checkpoint = "STOPPING"
        else:
            workspace.deletion_checkpoint = "STOPPING"
        _audit(
            db,
            actor_id=actor.id,
            workspace_id=workspace.id,
            action="WORKSPACE_DELETE_REQUESTED",
            result=("RETRY" if (retry_external or retry_lifecycle) else "ACCEPTED"),
            request_id=request_id,
            metadata={"target_owner_user_id": owner.id},
        )
        db.commit()
        return WorkspaceOperationResult(workspace, operation)


def _active_reservation_conditions(
    *, exclude_workspace_id: str | None = None
) -> list[object]:
    pending_lifecycle = exists(
        select(Operation.id).where(
            Operation.workspace_id == Workspace.id,
            Operation.operation_type.in_(
                [
                    OperationType.CREATE.value,
                    OperationType.START.value,
                    OperationType.RESTART.value,
                    # A stop/delete request can supersede an in-flight start
                    # before that start's RUNNING observation is persisted. Keep
                    # the pinned resources reserved until Hub confirms teardown;
                    # deletion releases at WAITING_EXTERNAL/NOT_FOUND.
                    OperationType.STOP.value,
                    OperationType.DELETE.value,
                ]
            ),
            Operation.status.in_(
                [OperationStatus.PENDING.value, OperationStatus.RUNNING.value]
            ),
        )
    )
    conditions: list[object] = [
        Workspace.archived_at.is_(None),
        or_(
            Workspace.observed_state.in_(
                [
                    ObservedState.STARTING.value,
                    ObservedState.RUNNING.value,
                    ObservedState.STOPPING.value,
                ]
            ),
            pending_lifecycle,
        ),
    ]
    if exclude_workspace_id is not None:
        conditions.append(Workspace.id != exclude_workspace_id)
    return conditions


def active_reservations(
    db: Session, *, exclude_workspace_id: str | None = None
) -> ActiveReservations:
    rows = db.execute(
        select(
            Workspace.id,
            WorkspaceProfile.cpu_limit,
            WorkspaceProfile.memory_limit_mb,
        )
        .select_from(Workspace)
        .outerjoin(
            WorkspaceProfile,
            and_(
                Workspace.profile_id == WorkspaceProfile.id,
                Workspace.profile_version == WorkspaceProfile.version,
            ),
        )
        .where(
            *_active_reservation_conditions(exclude_workspace_id=exclude_workspace_id)
        )
    ).all()
    cpu_millicores = 0
    memory_mb = 0
    for _workspace_id, cpu_limit, profile_memory_mb in rows:
        if cpu_limit is None or profile_memory_mb is None or profile_memory_mb <= 0:
            raise AppError(
                500,
                "PROFILE_RESOURCE_INVALID",
                "An active reservation has invalid pinned profile resources",
            )
        try:
            cpu_millicores += cpu_limit_to_millicores(cpu_limit)
        except ValueError as exc:
            raise AppError(
                500,
                "PROFILE_RESOURCE_INVALID",
                "An active reservation has invalid pinned profile resources",
            ) from exc
        memory_mb += profile_memory_mb
    return ActiveReservations(
        count=len(rows),
        cpu_millicores=cpu_millicores,
        memory_mb=memory_mb,
    )


def active_reservation_count(
    db: Session, *, exclude_workspace_id: str | None = None
) -> int:
    return active_reservations(db, exclude_workspace_id=exclude_workspace_id).count


def validate_launch_url(
    full_url: str, *, username: str, server_name: str, settings: Settings
) -> str:
    try:
        parsed = urlsplit(full_url)
        port = parsed.port
    except ValueError as exc:
        raise AppError(
            502, "HUB_URL_INVALID", "JupyterHub returned an invalid URL"
        ) from exc
    expected_label = username.encode("idna").decode("ascii")
    expected_host = f"{expected_label}.{settings.hub_user_domain}"
    expected_path = f"/user/{quote(username, safe='')}/{quote(server_name, safe='')}/"
    public_origin = urlsplit(settings.hub_public_url)
    expected_scheme = public_origin.scheme
    default_port = 443 if expected_scheme == "https" else 80
    expected_effective_port = public_origin.port or default_port
    parsed_effective_port = port or default_port
    expected_authority = expected_host
    if public_origin.port is not None:
        expected_authority = f"{expected_authority}:{public_origin.port}"
    if (
        parsed.scheme != expected_scheme
        or parsed.hostname != expected_host
        or parsed.netloc != expected_authority
        or parsed_effective_port != expected_effective_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
        or "%" in parsed.path
        or "\\" in parsed.path
        or any(ord(char) < 32 for char in parsed.path)
    ):
        raise AppError(
            502, "HUB_URL_INVALID", "JupyterHub URL failed exact origin/path validation"
        )
    return full_url


async def fresh_launch_url(
    *,
    db: Session,
    workspace: Workspace,
    user: User,
    portal_session: UserSession,
    token: str,
    hub: JupyterHubProvider,
    settings: Settings,
) -> str:
    del db, portal_session
    try:
        principal = await hub.resolve_principal(token)
        if principal.username != user.hub_username:
            raise AppError(
                403,
                "IDENTITY_MISMATCH",
                "Hub token owner does not match workspace owner",
            )
        server = await hub.get_server(principal, workspace.hub_server_name, token)
    except HubAuthError as exc:
        raise AppError(
            401, "AUTH_REQUIRED", "A fresh JupyterHub login is required"
        ) from exc
    except HubUnavailableError as exc:
        raise AppError(
            503, "HUB_UNAVAILABLE", "JupyterHub state could not be verified"
        ) from exc
    if not server.ready or not server.full_url:
        raise AppError(409, "WORKSPACE_NOT_RUNNING", "Workspace is not running")
    return validate_launch_url(
        server.full_url,
        username=user.hub_username,
        server_name=workspace.hub_server_name,
        settings=settings,
    )
