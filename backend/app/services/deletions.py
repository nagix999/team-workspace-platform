from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import begin_immediate
from ..domain import (
    DesiredState,
    ObservedState,
    OperationStatus,
    OperationType,
    ProvisionStatus,
)
from ..errors import AppError
from ..models import (
    AuditEvent,
    EnvironmentVariable,
    Operation,
    SpawnAuthorization,
    User,
    Workspace,
    WorkspaceDeletionJob,
    WorkspaceProfile,
    WorkspaceVolumeSlot,
)
from ..security import json_dumps_safe


@dataclass(frozen=True)
class DeletionClaim:
    deletion_id: str
    workspace_id: str
    operation_id: str
    owner_user_id: str
    username: str
    server_name: str
    workspace_spec_version: int
    private_volume_slot_id: str
    private_volume_slot_number: int
    private_volume_name: str
    attempt_no: int


def _require_enabled(settings: Settings) -> None:
    if not settings.workspace_deletion_enabled:
        raise AppError(
            503,
            "WORKSPACE_DELETION_DISABLED",
            "Workspace deletion agent is not enabled",
        )


def _bound_rows(
    db: Session, job: WorkspaceDeletionJob
) -> tuple[Workspace, Operation, User, WorkspaceVolumeSlot, WorkspaceProfile]:
    workspace = db.get(Workspace, job.workspace_id)
    operation = db.get(Operation, job.operation_id)
    owner = db.get(User, workspace.owner_user_id) if workspace else None
    slot = (
        db.get(WorkspaceVolumeSlot, workspace.private_volume_slot_id)
        if workspace
        else None
    )
    profile = (
        db.get(WorkspaceProfile, (workspace.profile_id, workspace.profile_version))
        if workspace
        else None
    )
    if not all((workspace, operation, owner, slot, profile)):
        raise AppError(
            409, "DELETION_INVARIANT_FAILED", "Deletion binding is incomplete"
        )
    assert workspace and operation and owner and slot and profile
    if not (
        operation.operation_type == OperationType.DELETE.value
        and operation.status == OperationStatus.WAITING_EXTERNAL.value
        and operation.workspace_id == workspace.id
        and workspace.owner_user_id == owner.id == slot.owner_user_id
        and workspace.desired_state == DesiredState.DELETED.value
        and workspace.deletion_started_at is not None
        and workspace.deletion_checkpoint in {"DELETION_PENDING", "DELETION_FAILED"}
        and workspace.archived_at is None
        and workspace.observed_state == ObservedState.NOT_FOUND.value
        and workspace.spec_version == job.expected_spec_version
        and slot.provision_status == ProvisionStatus.WIPING.value
    ):
        raise AppError(409, "DELETION_INVARIANT_FAILED", "Deletion binding changed")
    return workspace, operation, owner, slot, profile


def _expire_exhausted_job(
    db: Session,
    *,
    job: WorkspaceDeletionJob,
    now: datetime,
) -> None:
    workspace, operation, _owner, slot, _profile = _bound_rows(db, job)
    job.status = "FAILED"
    job.error_code = "VOLUME_REPROVISION_FAILED"
    job.error_summary = "Private volume wipe and reprovision did not complete"
    job.completed_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now
    workspace.deletion_checkpoint = "DELETION_FAILED"
    workspace.last_error_code = job.error_code
    workspace.last_error_summary = job.error_summary
    workspace.row_version += 1
    workspace.updated_at = now
    slot.provision_status = ProvisionStatus.WIPING.value
    operation.status = OperationStatus.FAILED.value
    operation.completed_at = now
    operation.error_code = job.error_code
    operation.error_summary = job.error_summary
    operation.lifecycle_checkpoint = "DELETION_FAILED"
    db.add(
        AuditEvent(
            id=str(uuid.uuid4()),
            actor_user_id=operation.actor_user_id,
            workspace_id=workspace.id,
            action="WORKSPACE_DELETION_ATTEMPT",
            result="FAILED",
            request_id=f"deletion-expired:{job.deletion_id}"[:64],
            safe_metadata_json=json_dumps_safe(
                {
                    "attempt": job.attempts,
                    "deletion_id": job.deletion_id,
                    "reason": "LEASE_EXPIRED",
                }
            ),
        )
    )


def claim_deletion_job(
    db: Session, *, settings: Settings, worker_id: str
) -> DeletionClaim | None:
    _require_enabled(settings)
    now = datetime.utcnow()
    begin_immediate(db)
    job = db.scalar(
        select(WorkspaceDeletionJob)
        .where(
            or_(
                WorkspaceDeletionJob.status == "PENDING",
                (
                    (WorkspaceDeletionJob.status == "RUNNING")
                    & (WorkspaceDeletionJob.lease_expires_at < now)
                ),
            ),
        )
        .order_by(WorkspaceDeletionJob.requested_at, WorkspaceDeletionJob.workspace_id)
        .limit(1)
    )
    if job is None:
        db.rollback()
        return None
    if job.attempts >= settings.deletion_max_attempts:
        _expire_exhausted_job(db, job=job, now=now)
        db.commit()
        return None
    workspace, _operation, owner, slot, _profile = _bound_rows(db, job)
    job.status = "RUNNING"
    job.attempts += 1
    job.started_at = job.started_at or now
    job.lease_owner = worker_id
    job.lease_expires_at = now + timedelta(seconds=settings.deletion_lease_seconds)
    job.updated_at = now
    workspace.deletion_checkpoint = "DELETION_PENDING"
    db.commit()
    return DeletionClaim(
        deletion_id=job.deletion_id,
        workspace_id=workspace.id,
        operation_id=job.operation_id,
        owner_user_id=owner.id,
        username=owner.hub_username,
        server_name=workspace.hub_server_name,
        workspace_spec_version=workspace.spec_version,
        private_volume_slot_id=slot.id,
        private_volume_slot_number=slot.slot_no,
        private_volume_name=slot.volume_name,
        attempt_no=job.attempts,
    )


def complete_deletion_job(
    db: Session,
    *,
    settings: Settings,
    worker_id: str,
    workspace_id: str,
    attempt_no: int,
    manifest: dict[str, object],
    request_id: str,
) -> None:
    _require_enabled(settings)
    now = datetime.utcnow()
    begin_immediate(db)
    job = db.get(WorkspaceDeletionJob, workspace_id)
    if (
        job is None
        or job.status != "RUNNING"
        or job.lease_owner != worker_id
        or job.attempts != attempt_no
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        db.rollback()
        raise AppError(409, "DELETION_LEASE_LOST", "Deletion lease is not current")
    workspace, operation, owner, slot, profile = _bound_rows(db, job)
    try:
        options = json.loads(profile.provider_options_json)
        uid = int(options["uid"])
        gid = int(options["gid"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        db.rollback()
        raise AppError(
            409, "DELETION_INVARIANT_FAILED", "Runtime identity is invalid"
        ) from exc
    manifest_project_id = manifest.get("project_id")
    if (
        type(manifest_project_id) is not int
        or manifest_project_id <= 0
        or (
            profile.private_disk_quota_enforced
            and manifest_project_id != slot.quota_project_id
        )
    ):
        db.rollback()
        raise AppError(
            409, "DELETION_MANIFEST_MISMATCH", "Deletion manifest does not match"
        )
    expected = {
        "schema_version": 1,
        "workspace_id": workspace.id,
        "owner_user_id": owner.id,
        "username": owner.hub_username,
        "workspace_spec_version": workspace.spec_version,
        "private_volume_slot_id": slot.id,
        "private_volume_slot_number": slot.slot_no,
        "private_volume_name": slot.volume_name,
        "hard_limit_bytes": slot.hard_limit_mb * 1024 * 1024,
        # Unlimited Docker volumes retain the allocator's real project ID in
        # their labels, while local activation stores a collision-resistant DB
        # sentinel.  The privileged agent proves those exact labels before
        # removal and reports the real positive ID here. Enforced quota
        # profiles still require exact agreement with DB inventory above.
        "project_id": manifest_project_id,
        "uid": uid,
        "gid": gid,
        "mode": "0700",
        "volume_recreated": True,
    }
    if manifest != expected:
        db.rollback()
        raise AppError(
            409, "DELETION_MANIFEST_MISMATCH", "Deletion manifest does not match"
        )
    db.execute(
        update(EnvironmentVariable)
        .where(
            EnvironmentVariable.workspace_id == workspace.id,
            EnvironmentVariable.deleted_at.is_(None),
        )
        .values(
            value_cipher=None,
            plain_value=None,
            value_fingerprint=None,
            deleted_at=now,
            updated_at=now,
            row_version=EnvironmentVariable.row_version + 1,
        )
    )
    db.execute(
        update(SpawnAuthorization)
        .where(SpawnAuthorization.workspace_id == workspace.id)
        .values(revoked_at=now, environment_snapshot_cipher=None)
    )
    slot.provision_status = ProvisionStatus.PROVISIONED.value
    slot.verified_at = now
    workspace.observed_state = ObservedState.NOT_FOUND.value
    workspace.stale = False
    workspace.hub_server_url = None
    workspace.archived_at = now
    workspace.deletion_checkpoint = "ARCHIVED"
    workspace.last_error_code = None
    workspace.last_error_summary = None
    workspace.row_version += 1
    workspace.updated_at = now
    operation.status = OperationStatus.SUCCEEDED.value
    operation.completed_at = now
    operation.error_code = None
    operation.error_summary = None
    operation.lifecycle_checkpoint = "ARCHIVED"
    job.status = "SUCCEEDED"
    job.completed_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now
    db.add(
        AuditEvent(
            id=str(uuid.uuid4()),
            actor_user_id=operation.actor_user_id,
            workspace_id=workspace.id,
            action="WORKSPACE_DELETION_COMPLETED",
            result="SUCCEEDED",
            request_id=request_id,
            safe_metadata_json=json_dumps_safe(
                {"deletion_id": job.deletion_id, "slot_number": slot.slot_no}
            ),
        )
    )
    db.commit()


def fail_deletion_job(
    db: Session,
    *,
    settings: Settings,
    worker_id: str,
    workspace_id: str,
    attempt_no: int,
    request_id: str,
) -> None:
    _require_enabled(settings)
    now = datetime.utcnow()
    begin_immediate(db)
    job = db.get(WorkspaceDeletionJob, workspace_id)
    if (
        job is None
        or job.status != "RUNNING"
        or job.lease_owner != worker_id
        or job.attempts != attempt_no
    ):
        db.rollback()
        raise AppError(409, "DELETION_LEASE_LOST", "Deletion lease is not current")
    workspace, operation, _owner, slot, _profile = _bound_rows(db, job)
    terminal = job.attempts >= settings.deletion_max_attempts
    job.status = "FAILED" if terminal else "PENDING"
    job.error_code = "VOLUME_REPROVISION_FAILED"
    job.error_summary = "Private volume wipe and reprovision did not complete"
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now
    workspace.deletion_checkpoint = (
        "DELETION_FAILED" if terminal else "DELETION_PENDING"
    )
    workspace.last_error_code = job.error_code
    workspace.last_error_summary = job.error_summary
    workspace.row_version += 1
    workspace.updated_at = now
    slot.provision_status = ProvisionStatus.WIPING.value
    if terminal:
        job.completed_at = now
        operation.status = OperationStatus.FAILED.value
        operation.completed_at = now
        operation.error_code = job.error_code
        operation.error_summary = job.error_summary
        operation.lifecycle_checkpoint = "DELETION_FAILED"
    db.add(
        AuditEvent(
            id=str(uuid.uuid4()),
            actor_user_id=operation.actor_user_id,
            workspace_id=workspace.id,
            action="WORKSPACE_DELETION_ATTEMPT",
            result="FAILED" if terminal else "RETRY",
            request_id=request_id,
            safe_metadata_json=json_dumps_safe(
                {"attempt": job.attempts, "deletion_id": job.deletion_id}
            ),
        )
    )
    db.commit()
