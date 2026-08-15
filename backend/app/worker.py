from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import socket
import stat
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .db import begin_immediate, create_database_engine, create_session_factory
from .domain import (
    DesiredState,
    HubServerState,
    ObservedState,
    OperationStatus,
    OperationType,
    ProvisionStatus,
    UserRole,
    UserStatus,
)
from .errors import AppError
from .hub import (
    ApprovedProfile,
    HTTPJupyterHubProvider,
    HubAuthError,
    HubCapacityError,
    HubPrincipal,
    HubRequestError,
    HubServer,
    HubUnavailableError,
    JupyterHubProvider,
)
from .models import (
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
from .security import TokenCipher, json_dumps_safe, random_token, sha256_hex
from .services.environment import effective_environment, spawn_snapshot_purpose


def read_admin_lifecycle_token(path: Path) -> str:
    """Read the cross-user Hub credential without following or trusting paths."""

    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("admin lifecycle token no-follow support is unavailable")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        file_stat = os.fstat(descriptor)
        permission_mode = stat.S_IMODE(file_stat.st_mode)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or permission_mode not in {0o400, 0o440, 0o600, 0o640}
            or not 32 <= file_stat.st_size <= 4098
        ):
            raise ValueError("admin lifecycle token file permissions are unsafe")
        with os.fdopen(descriptor, encoding="ascii") as input_file:
            descriptor = None
            value = input_file.read(4099).rstrip("\r\n")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(
                "admin lifecycle token file permissions are unsafe"
            ) from exc
        raise ValueError("admin lifecycle token file is unavailable") from exc
    except UnicodeError as exc:
        raise ValueError("admin lifecycle token file is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not 32 <= len(value) <= 4096 or any(character.isspace() for character in value):
        raise ValueError("admin lifecycle token is invalid")
    return value


class OperationWorker:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        hub: JupyterHubProvider,
        cipher: TokenCipher,
        *,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.hub = hub
        self.cipher = cipher
        self.worker_id = (
            worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )

    def _claim(self) -> str | None:
        now = datetime.utcnow()
        with self.session_factory() as db:
            begin_immediate(db)
            operation = db.scalar(
                select(Operation)
                .where(
                    or_(
                        (
                            (Operation.status == OperationStatus.PENDING.value)
                            & or_(
                                Operation.next_attempt_at.is_(None),
                                Operation.next_attempt_at <= now,
                            )
                        ),
                        (
                            (Operation.status == OperationStatus.RUNNING.value)
                            & (Operation.lease_expires_at < now)
                        ),
                    )
                )
                .order_by(Operation.requested_at, Operation.id)
                .limit(1)
            )
            if operation is None:
                db.rollback()
                return None
            operation.status = OperationStatus.RUNNING.value
            operation.started_at = operation.started_at or now
            operation.lease_owner = self.worker_id
            operation.lease_expires_at = now + timedelta(
                seconds=self.settings.worker_lease_seconds
            )
            operation.next_attempt_at = None
            db.commit()
            return operation.id

    async def process_next(self) -> bool:
        operation_id = self._claim()
        if operation_id is None:
            return False
        await self._process(operation_id)
        return True

    async def _process(self, operation_id: str) -> None:
        with self.session_factory() as db:
            operation = db.get(Operation, operation_id)
            if operation is None or operation.lease_owner != self.worker_id:
                return
            workspace = db.get(Workspace, operation.workspace_id)
            user = db.get(User, operation.requested_by_user_id)
            actor = db.get(User, operation.actor_user_id)
            portal_session = (
                db.get(UserSession, operation.auth_session_id_hash)
                if operation.auth_session_id_hash
                else None
            )
            profile = (
                db.get(
                    WorkspaceProfile, (workspace.profile_id, workspace.profile_version)
                )
                if workspace
                else None
            )
            invariant_ok = bool(workspace and user and actor and profile)
            if operation.credential_mode == "USER_DELEGATED":
                invariant_ok = bool(
                    invariant_ok
                    and portal_session is not None
                    and actor is not None
                    and user is not None
                    and actor.id == user.id
                )
            elif operation.credential_mode == "ADMIN_SERVICE":
                invariant_ok = bool(
                    invariant_ok and portal_session is None and actor is not None
                )
            else:
                invariant_ok = False
            if not invariant_ok:
                self._finish(
                    db,
                    operation,
                    None,
                    OperationStatus.FAILED,
                    "INVARIANT_VIOLATION",
                    "Required operation binding is missing",
                )
                return
            assert workspace and user and actor and profile
            if operation.credential_mode == "ADMIN_SERVICE" and (
                actor.role != UserRole.ADMIN.value
                or actor.hub_username not in self.settings.admin_usernames
            ):
                self._finish(
                    db,
                    operation,
                    workspace,
                    OperationStatus.FAILED,
                    "ADMIN_AUTHORITY_REVOKED",
                    "Administrator authority was revoked",
                )
                return
            target_running = operation.operation_type in {
                OperationType.CREATE.value,
                OperationType.START.value,
                OperationType.RESTART.value,
            }
            delete_operation = operation.operation_type == OperationType.DELETE.value
            if target_running and user.status != UserStatus.ACTIVE.value:
                self._finish(
                    db,
                    operation,
                    workspace,
                    OperationStatus.AUTH_REQUIRED,
                    "USER_NOT_ACTIVE",
                    "User is not active",
                )
                return
            expected_desired = (
                DesiredState.DELETED.value
                if delete_operation
                else (
                    DesiredState.RUNNING.value
                    if target_running
                    else DesiredState.STOPPED.value
                )
            )
            if (
                workspace.desired_state != expected_desired
                or (not delete_operation and workspace.deletion_started_at is not None)
                or (delete_operation and workspace.deletion_started_at is None)
            ):
                self._finish(
                    db,
                    operation,
                    workspace,
                    OperationStatus.FAILED,
                    "SUPERSEDED",
                    "Operation was superseded by newer desired state",
                )
                return
            if target_running and (
                not profile.enabled or not self.settings.execution_host_healthy
            ):
                code = (
                    "PROFILE_DISABLED"
                    if not profile.enabled
                    else "EXECUTION_HOST_UNHEALTHY"
                )
                self._finish(
                    db,
                    operation,
                    workspace,
                    OperationStatus.FAILED,
                    code,
                    "Execution preflight failed",
                )
                return
            try:
                token = (
                    self._decrypt_token(portal_session)
                    if portal_session is not None
                    else self._admin_lifecycle_token()
                )
            except (OSError, ValueError, AppError):
                if operation.credential_mode == "ADMIN_SERVICE":
                    self._finish(
                        db,
                        operation,
                        workspace,
                        OperationStatus.FAILED,
                        "ADMIN_LIFECYCLE_UNAVAILABLE",
                        "Administrator lifecycle service is unavailable",
                    )
                else:
                    self._finish(
                        db,
                        operation,
                        workspace,
                        OperationStatus.AUTH_REQUIRED,
                        "AUTH_REQUIRED",
                        "A fresh login is required",
                    )
                return
            username = user.hub_username
            server_name = workspace.hub_server_name

        try:
            principal = await self.hub.resolve_principal(token)
            if operation.credential_mode == "USER_DELEGATED":
                if (
                    principal.username != username
                    or not principal.can_manage_own_servers()
                ):
                    raise HubAuthError("delegated token owner or scope mismatch")
            elif not principal.can_admin_servers():
                raise HubAuthError("admin lifecycle token scope mismatch")
            current = await self.hub.get_server(
                principal, server_name, token, target_username=username
            )
            if operation.operation_type == OperationType.RESTART.value:
                await self._drive_restart(
                    operation_id,
                    workspace.id,
                    profile,
                    principal,
                    token,
                    current,
                    username,
                )
            elif delete_operation:
                await self._drive_delete(
                    operation_id,
                    workspace.id,
                    principal,
                    token,
                    current,
                    username,
                )
            elif target_running:
                await self._drive_start(
                    operation_id,
                    workspace.id,
                    profile,
                    principal,
                    token,
                    current,
                    username,
                )
            else:
                await self._drive_stop(
                    operation_id,
                    workspace.id,
                    principal,
                    token,
                    current,
                    username,
                )
        except HubAuthError:
            if operation.credential_mode == "ADMIN_SERVICE":
                self._finish_by_id(
                    operation_id,
                    OperationStatus.FAILED,
                    "ADMIN_LIFECYCLE_UNAVAILABLE",
                    "Administrator lifecycle service is unavailable",
                )
            else:
                self._finish_by_id(
                    operation_id,
                    OperationStatus.AUTH_REQUIRED,
                    "AUTH_REQUIRED",
                    "A fresh JupyterHub login is required",
                )
        except HubCapacityError:
            self._finish_by_id(
                operation_id,
                OperationStatus.FAILED,
                "CAPACITY_LIMIT",
                "JupyterHub active capacity is full",
            )
        except HubRequestError:
            self._finish_by_id(
                operation_id,
                OperationStatus.FAILED,
                "HUB_REQUEST_REJECTED",
                "JupyterHub rejected the workspace operation",
            )
        except HubUnavailableError:
            self._retry_or_fail(
                operation_id, "HUB_UNAVAILABLE", "JupyterHub is temporarily unavailable"
            )
        except AppError as exc:
            self._finish_by_id(
                operation_id,
                OperationStatus.FAILED,
                exc.code,
                exc.message,
            )

    async def _drive_start(
        self,
        operation_id: str,
        workspace_id: str,
        profile: WorkspaceProfile,
        principal: HubPrincipal,
        token: str,
        current: HubServer,
        target_username: str,
    ) -> None:
        if current.state == HubServerState.RUNNING and current.ready:
            self._apply_server_and_finish(operation_id, workspace_id, current)
            return
        if self._fail_if_lifecycle_timed_out(operation_id, workspace_id):
            return
        if current.state == HubServerState.STARTING:
            current = await self.hub.get_spawn_progress(
                principal,
                self._server_name(workspace_id),
                token,
                target_username=target_username,
            )
            if current.state == HubServerState.RUNNING and current.ready:
                self._apply_server_and_finish(operation_id, workspace_id, current)
                return
            if current.state == HubServerState.STARTING:
                self._apply_server_and_requeue(operation_id, workspace_id, current)
                return
            # A failed/stopped/not-found result may be retried with a fresh,
            # one-time ticket, subject to the durable command-attempt limit.
        elif current.state == HubServerState.STOPPING:
            self._apply_server_and_requeue(operation_id, workspace_id, current)
            return

        if self._fail_if_start_attempts_exhausted(operation_id, workspace_id, current):
            return

        ticket, server_name = self._create_spawn_authorization(
            operation_id, workspace_id, profile
        )
        result = await self.hub.request_start(
            principal,
            server_name,
            ApprovedProfile(profile.id, profile.version, profile.config_digest),
            ticket,
            token,
            target_username=target_username,
        )
        if result.state == HubServerState.RUNNING and result.ready:
            self._apply_server_and_finish(operation_id, workspace_id, result)
        elif result.state in {
            HubServerState.FAILED,
            HubServerState.STOPPED,
            HubServerState.NOT_FOUND,
        }:
            if not self._fail_if_start_attempts_exhausted(
                operation_id, workspace_id, result
            ):
                self._apply_server_and_requeue(
                    operation_id,
                    workspace_id,
                    result,
                    error_code="SPAWN_FAILED",
                    error_summary=(
                        result.failure_summary
                        or "JupyterHub did not start the workspace"
                    ),
                )
        else:
            self._apply_server_and_requeue(operation_id, workspace_id, result)

    async def _drive_stop(
        self,
        operation_id: str,
        workspace_id: str,
        principal: HubPrincipal,
        token: str,
        current: HubServer,
        target_username: str,
    ) -> None:
        if current.state in {HubServerState.NOT_FOUND, HubServerState.STOPPED}:
            stopped = HubServer(state=HubServerState.STOPPED, progress_percent=100)
            self._apply_server_and_finish(operation_id, workspace_id, stopped)
            return
        if self._fail_if_lifecycle_timed_out(operation_id, workspace_id):
            return
        if current.state == HubServerState.STOPPING:
            self._apply_server_and_requeue(operation_id, workspace_id, current)
            return
        if self._begin_lifecycle_command(operation_id, workspace_id):
            return
        result = await self.hub.request_stop(
            principal,
            self._server_name(workspace_id),
            token,
            target_username=target_username,
        )
        if result.state in {HubServerState.NOT_FOUND, HubServerState.STOPPED}:
            self._apply_server_and_finish(operation_id, workspace_id, result)
        else:
            self._apply_server_and_requeue(operation_id, workspace_id, result)

    async def _drive_restart(
        self,
        operation_id: str,
        workspace_id: str,
        profile: WorkspaceProfile,
        principal: HubPrincipal,
        token: str,
        current: HubServer,
        target_username: str,
    ) -> None:
        with self.session_factory() as db:
            operation = db.get(Operation, operation_id)
            if operation is None or operation.lease_owner != self.worker_id:
                return
            checkpoint = operation.lifecycle_checkpoint
        if checkpoint == "RESTART_STOPPING":
            if current.state == HubServerState.NOT_FOUND:
                with self.session_factory() as db:
                    operation = db.get(Operation, operation_id)
                    if operation is None or operation.lease_owner != self.worker_id:
                        return
                    operation.lifecycle_checkpoint = "RESTART_STARTING"
                    operation.attempts = 0
                    db.commit()
                await self._drive_start(
                    operation_id,
                    workspace_id,
                    profile,
                    principal,
                    token,
                    current,
                    target_username,
                )
                return
            if self._fail_if_lifecycle_timed_out(operation_id, workspace_id):
                return
            if current.state == HubServerState.STOPPED:
                # A stopped named-server record still retains the old spawner
                # state. Remove it explicitly and only start after Hub confirms
                # NOT_FOUND so environment changes cannot be hot-applied to the
                # old container/spawner.
                if self._begin_lifecycle_command(operation_id, workspace_id):
                    return
                removed = await self.hub.request_remove(
                    principal,
                    self._server_name(workspace_id),
                    token,
                    target_username=target_username,
                )
                if removed.state == HubServerState.NOT_FOUND:
                    with self.session_factory() as db:
                        operation = db.get(Operation, operation_id)
                        if operation is None or operation.lease_owner != self.worker_id:
                            return
                        operation.lifecycle_checkpoint = "RESTART_STARTING"
                        operation.attempts = 0
                        db.commit()
                    await self._drive_start(
                        operation_id,
                        workspace_id,
                        profile,
                        principal,
                        token,
                        removed,
                        target_username,
                    )
                else:
                    self._apply_server_and_requeue(operation_id, workspace_id, removed)
                return
            if current.state == HubServerState.STOPPING:
                self._apply_server_and_requeue(operation_id, workspace_id, current)
                return
            if self._begin_lifecycle_command(operation_id, workspace_id):
                return
            result = await self.hub.request_stop(
                principal,
                self._server_name(workspace_id),
                token,
                target_username=target_username,
            )
            if result.state == HubServerState.STOPPED:
                if self._begin_lifecycle_command(operation_id, workspace_id):
                    return
                removed = await self.hub.request_remove(
                    principal,
                    self._server_name(workspace_id),
                    token,
                    target_username=target_username,
                )
                if removed.state == HubServerState.NOT_FOUND:
                    with self.session_factory() as db:
                        operation = db.get(Operation, operation_id)
                        if operation is None or operation.lease_owner != self.worker_id:
                            return
                        operation.lifecycle_checkpoint = "RESTART_STARTING"
                        operation.attempts = 0
                        db.commit()
                    await self._drive_start(
                        operation_id,
                        workspace_id,
                        profile,
                        principal,
                        token,
                        removed,
                        target_username,
                    )
                else:
                    self._apply_server_and_requeue(operation_id, workspace_id, removed)
            else:
                self._apply_server_and_requeue(operation_id, workspace_id, result)
            return
        if checkpoint != "RESTART_STARTING":
            self._finish_by_id(
                operation_id,
                OperationStatus.FAILED,
                "INVARIANT_VIOLATION",
                "Restart checkpoint is invalid",
            )
            return
        await self._drive_start(
            operation_id,
            workspace_id,
            profile,
            principal,
            token,
            current,
            target_username,
        )

    async def _drive_delete(
        self,
        operation_id: str,
        workspace_id: str,
        principal: HubPrincipal,
        token: str,
        current: HubServer,
        target_username: str,
    ) -> None:
        if current.state == HubServerState.NOT_FOUND:
            self._queue_deletion_job(operation_id, workspace_id)
            return
        if self._fail_if_lifecycle_timed_out(operation_id, workspace_id):
            return
        if current.state == HubServerState.STOPPED:
            if self._begin_lifecycle_command(operation_id, workspace_id):
                return
            result = await self.hub.request_remove(
                principal,
                self._server_name(workspace_id),
                token,
                target_username=target_username,
            )
            if result.state == HubServerState.NOT_FOUND:
                self._queue_deletion_job(operation_id, workspace_id)
            else:
                self._apply_server_and_requeue(operation_id, workspace_id, result)
            return
        if current.state == HubServerState.STOPPING:
            self._apply_server_and_requeue(operation_id, workspace_id, current)
            return
        if self._begin_lifecycle_command(operation_id, workspace_id):
            return
        result = await self.hub.request_stop(
            principal,
            self._server_name(workspace_id),
            token,
            target_username=target_username,
        )
        if result.state == HubServerState.NOT_FOUND:
            self._queue_deletion_job(operation_id, workspace_id)
        elif result.state == HubServerState.STOPPED:
            if self._begin_lifecycle_command(operation_id, workspace_id):
                return
            removed = await self.hub.request_remove(
                principal,
                self._server_name(workspace_id),
                token,
                target_username=target_username,
            )
            if removed.state == HubServerState.NOT_FOUND:
                self._queue_deletion_job(operation_id, workspace_id)
            else:
                self._apply_server_and_requeue(operation_id, workspace_id, removed)
        else:
            self._apply_server_and_requeue(operation_id, workspace_id, result)

    def _queue_deletion_job(self, operation_id: str, workspace_id: str) -> None:
        now = datetime.utcnow()
        with self.session_factory() as db:
            begin_immediate(db)
            operation = db.get(Operation, operation_id)
            workspace = db.get(Workspace, workspace_id)
            slot = (
                db.get(WorkspaceVolumeSlot, workspace.private_volume_slot_id)
                if workspace
                else None
            )
            if (
                operation is None
                or workspace is None
                or slot is None
                or operation.lease_owner != self.worker_id
                or operation.operation_type != OperationType.DELETE.value
                or workspace.desired_state != DesiredState.DELETED.value
                or workspace.deletion_started_at is None
                or workspace.archived_at is not None
            ):
                db.rollback()
                return
            removed = HubServer(state=HubServerState.NOT_FOUND, progress_percent=100)
            self._apply_server(workspace, removed)
            workspace.deletion_checkpoint = "DELETION_PENDING"
            slot.provision_status = ProvisionStatus.WIPING.value
            job = db.get(WorkspaceDeletionJob, workspace.id)
            if job is None:
                job = WorkspaceDeletionJob(
                    workspace_id=workspace.id,
                    deletion_id=str(uuid.uuid4()),
                    operation_id=operation.id,
                    expected_spec_version=workspace.spec_version,
                    status="PENDING",
                    attempts=0,
                    requested_at=now,
                    updated_at=now,
                )
                db.add(job)
            elif job.status == "FAILED":
                if (
                    job.operation_id != operation.id
                    or job.expected_spec_version != workspace.spec_version
                ):
                    db.rollback()
                    return
                job.status = "PENDING"
                job.attempts = 0
                job.error_code = None
                job.error_summary = None
                job.completed_at = None
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
            elif job.status != "PENDING":
                db.rollback()
                return
            operation.status = OperationStatus.WAITING_EXTERNAL.value
            operation.lifecycle_checkpoint = "DELETION_PENDING"
            operation.lease_owner = None
            operation.lease_expires_at = None
            operation.next_attempt_at = None
            db.add(
                AuditEvent(
                    id=str(uuid.uuid4()),
                    actor_user_id=operation.actor_user_id,
                    workspace_id=workspace.id,
                    action="WORKSPACE_DELETION_WIPE_QUEUED",
                    result="ACCEPTED",
                    request_id=f"worker:{operation.id}",
                    safe_metadata_json="{}",
                )
            )
            db.commit()

    def _decrypt_token(self, portal_session: UserSession) -> str:
        now = datetime.utcnow()
        if (
            portal_session.revoked_at is not None
            or portal_session.hub_oauth_token_cipher is None
            or portal_session.hub_oauth_expires_at <= now
            or portal_session.absolute_expires_at <= now
        ):
            raise AppError(401, "AUTH_REQUIRED", "Session token is unavailable")
        return self.cipher.decrypt(
            portal_session.hub_oauth_token_cipher,
            purpose=f"hub-oauth:{portal_session.id_hash}",
        )

    def _admin_lifecycle_token(self) -> str:
        path = self.settings.admin_lifecycle_token_file
        if not path:
            raise AppError(
                503,
                "ADMIN_LIFECYCLE_UNAVAILABLE",
                "Admin lifecycle credential is not configured",
            )
        return read_admin_lifecycle_token(Path(path))

    def _server_name(self, workspace_id: str) -> str:
        with self.session_factory() as db:
            workspace = db.get(Workspace, workspace_id)
            if workspace is None:
                raise RuntimeError("workspace disappeared")
            return workspace.hub_server_name

    def _fail_if_lifecycle_timed_out(
        self, operation_id: str, workspace_id: str
    ) -> bool:
        now = datetime.utcnow()
        with self.session_factory() as db:
            operation = db.get(Operation, operation_id)
            workspace = db.get(Workspace, workspace_id)
            if (
                operation is None
                or workspace is None
                or operation.lease_owner != self.worker_id
            ):
                return True
            if (
                now - operation.requested_at
            ).total_seconds() < self.settings.lifecycle_timeout_seconds:
                return False
            self._finish(
                db,
                operation,
                workspace,
                OperationStatus.FAILED,
                "LIFECYCLE_TIMEOUT",
                "Workspace lifecycle operation timed out",
            )
            return True

    def _begin_lifecycle_command(self, operation_id: str, workspace_id: str) -> bool:
        with self.session_factory() as db:
            begin_immediate(db)
            operation = db.get(Operation, operation_id)
            workspace = db.get(Workspace, workspace_id)
            if (
                operation is None
                or workspace is None
                or operation.lease_owner != self.worker_id
            ):
                db.rollback()
                return True
            if operation.attempts >= self.settings.worker_max_attempts:
                self._finish(
                    db,
                    operation,
                    workspace,
                    OperationStatus.FAILED,
                    "LIFECYCLE_ATTEMPTS_EXHAUSTED",
                    "Workspace lifecycle command attempts were exhausted",
                )
                return True
            operation.attempts += 1
            db.commit()
            return False

    def _create_spawn_authorization(
        self, operation_id: str, workspace_id: str, profile_snapshot: WorkspaceProfile
    ) -> tuple[str, str]:
        ticket = random_token(48)
        now = datetime.utcnow()
        with self.session_factory() as db:
            begin_immediate(db)
            operation = db.get(Operation, operation_id)
            workspace = db.get(Workspace, workspace_id)
            user = db.get(User, workspace.owner_user_id) if workspace else None
            profile = (
                db.get(
                    WorkspaceProfile, (workspace.profile_id, workspace.profile_version)
                )
                if workspace
                else None
            )
            if (
                operation is None
                or workspace is None
                or user is None
                or profile is None
                or operation.lease_owner != self.worker_id
                or operation.status != OperationStatus.RUNNING.value
                or workspace.desired_state != DesiredState.RUNNING.value
                or workspace.deletion_started_at is not None
                or user.status != UserStatus.ACTIVE.value
                or profile.config_digest != profile_snapshot.config_digest
                or not profile.enabled
            ):
                db.rollback()
                raise HubAuthError("spawn binding changed before authorization")
            environment = effective_environment(
                db,
                cipher=self.cipher,
                hmac_key=self.settings.internal_hmac_key,
                owner_user_id=user.id,
                workspace_id=workspace.id,
            )
            db.execute(
                update(SpawnAuthorization)
                .where(
                    SpawnAuthorization.operation_id == operation.id,
                    SpawnAuthorization.consumed_at.is_(None),
                    SpawnAuthorization.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            operation.attempts += 1
            authorization_id = str(uuid.uuid4())
            environment_snapshot_cipher = self.cipher.encrypt(
                json.dumps(
                    environment.values,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                purpose=spawn_snapshot_purpose(
                    authorization_id, workspace.id, environment.digest
                ),
            )
            db.add(
                SpawnAuthorization(
                    id=authorization_id,
                    ticket_hash=sha256_hex(ticket),
                    operation_id=operation.id,
                    attempt_no=operation.attempts,
                    workspace_id=workspace.id,
                    owner_user_id=user.id,
                    workspace_spec_version=workspace.spec_version,
                    private_volume_slot_id=workspace.private_volume_slot_id,
                    hub_username=user.hub_username,
                    hub_server_name=workspace.hub_server_name,
                    profile_id=profile.id,
                    profile_version=profile.version,
                    profile_config_digest=profile.config_digest,
                    user_environment_generation=user.environment_generation,
                    workspace_environment_generation=workspace.environment_generation,
                    environment_digest=environment.digest,
                    environment_snapshot_cipher=environment_snapshot_cipher,
                    expires_at=now
                    + timedelta(seconds=self.settings.spawn_ticket_seconds),
                )
            )
            server_name = workspace.hub_server_name
            db.commit()
            return ticket, server_name

    def _observed_from_hub(self, state: HubServerState) -> str:
        return {
            HubServerState.NOT_FOUND: ObservedState.NOT_FOUND.value,
            HubServerState.STARTING: ObservedState.STARTING.value,
            HubServerState.RUNNING: ObservedState.RUNNING.value,
            HubServerState.STOPPING: ObservedState.STOPPING.value,
            HubServerState.STOPPED: ObservedState.STOPPED.value,
            HubServerState.FAILED: ObservedState.FAILED.value,
        }[state]

    def _fail_if_start_attempts_exhausted(
        self, operation_id: str, workspace_id: str, server: HubServer
    ) -> bool:
        with self.session_factory() as db:
            operation = db.get(Operation, operation_id)
            workspace = db.get(Workspace, workspace_id)
            if (
                not operation
                or not workspace
                or operation.lease_owner != self.worker_id
            ):
                return True
            if operation.attempts < self.settings.worker_max_attempts:
                return False
            self._apply_server(workspace, server)
            self._finish(
                db,
                operation,
                workspace,
                OperationStatus.FAILED,
                "SPAWN_ATTEMPTS_EXHAUSTED",
                "Workspace failed to start after the maximum number of attempts",
            )
            return True

    def _apply_server(self, workspace: Workspace, server: HubServer) -> None:
        workspace.observed_state = self._observed_from_hub(server.state)
        if (
            server.progress_percent is not None
            or server.state != HubServerState.STARTING
        ):
            workspace.progress_percent = server.progress_percent
        workspace.hub_server_url = server.full_url if server.ready else None
        workspace.hub_started_at = server.started_at
        workspace.hub_last_activity_at = server.last_activity_at
        workspace.last_reconciled_at = datetime.utcnow()
        workspace.stale = False
        workspace.last_error_code = None
        workspace.last_error_summary = None
        workspace.row_version += 1
        workspace.updated_at = datetime.utcnow()

    def _apply_server_and_finish(
        self, operation_id: str, workspace_id: str, server: HubServer
    ) -> None:
        with self.session_factory() as db:
            operation = db.get(Operation, operation_id)
            workspace = db.get(Workspace, workspace_id)
            if (
                not operation
                or not workspace
                or operation.lease_owner != self.worker_id
            ):
                return
            self._apply_server(workspace, server)
            if server.state == HubServerState.RUNNING and server.ready:
                authorization = db.scalar(
                    select(SpawnAuthorization)
                    .where(
                        SpawnAuthorization.operation_id == operation.id,
                        SpawnAuthorization.consumed_at.is_not(None),
                    )
                    .order_by(SpawnAuthorization.attempt_no.desc())
                    .limit(1)
                )
                if authorization is not None:
                    # Bind exactly what the consumed ticket carried. Environment
                    # edits racing after container creation must remain visible as
                    # restart_required rather than being marked applied.
                    workspace.applied_user_environment_generation = (
                        authorization.user_environment_generation
                    )
                    workspace.applied_workspace_environment_generation = (
                        authorization.workspace_environment_generation
                    )
            self._finish(
                db, operation, workspace, OperationStatus.SUCCEEDED, None, None
            )

    def _apply_server_and_requeue(
        self,
        operation_id: str,
        workspace_id: str,
        server: HubServer,
        *,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        with self.session_factory() as db:
            operation = db.get(Operation, operation_id)
            workspace = db.get(Workspace, workspace_id)
            if (
                not operation
                or not workspace
                or operation.lease_owner != self.worker_id
            ):
                return
            self._apply_server(workspace, server)
            operation.error_code = error_code
            operation.error_summary = error_summary
            if error_code:
                workspace.last_error_code = error_code
                workspace.last_error_summary = error_summary
            operation.status = OperationStatus.PENDING.value
            operation.lease_owner = None
            operation.lease_expires_at = None
            operation.next_attempt_at = datetime.utcnow() + timedelta(
                seconds=self.settings.worker_retry_seconds
            )
            db.commit()

    def _retry_or_fail(self, operation_id: str, code: str, summary: str) -> None:
        with self.session_factory() as db:
            operation = db.get(Operation, operation_id)
            workspace = db.get(Workspace, operation.workspace_id) if operation else None
            if not operation or operation.lease_owner != self.worker_id:
                return
            operation.transient_failures += 1
            if operation.transient_failures >= self.settings.worker_max_attempts:
                self._finish(
                    db, operation, workspace, OperationStatus.FAILED, code, summary
                )
                return
            if workspace:
                workspace.stale = True
                workspace.last_error_code = code
                workspace.last_error_summary = summary
                workspace.row_version += 1
            operation.status = OperationStatus.PENDING.value
            operation.error_code = code
            operation.error_summary = summary
            operation.lease_owner = None
            operation.lease_expires_at = None
            operation.next_attempt_at = datetime.utcnow() + timedelta(
                seconds=self.settings.worker_retry_seconds
            )
            db.commit()

    def _finish_by_id(
        self,
        operation_id: str,
        status: OperationStatus,
        code: str | None,
        summary: str | None,
    ) -> None:
        with self.session_factory() as db:
            operation = db.get(Operation, operation_id)
            workspace = db.get(Workspace, operation.workspace_id) if operation else None
            if not operation or operation.lease_owner != self.worker_id:
                return
            self._finish(db, operation, workspace, status, code, summary)

    def _finish(
        self,
        db: Session,
        operation: Operation,
        workspace: Workspace | None,
        status: OperationStatus,
        code: str | None,
        summary: str | None,
    ) -> None:
        now = datetime.utcnow()
        operation.status = status.value
        operation.error_code = code
        operation.error_summary = summary
        operation.completed_at = now
        operation.lease_owner = None
        operation.lease_expires_at = None
        operation.next_attempt_at = None
        if workspace and status != OperationStatus.SUCCEEDED:
            workspace.last_error_code = code
            workspace.last_error_summary = summary
            workspace.stale = code == "HUB_UNAVAILABLE"
            workspace.row_version += 1
        db.execute(
            update(SpawnAuthorization)
            .where(
                SpawnAuthorization.operation_id == operation.id,
                SpawnAuthorization.consumed_at.is_(None),
                SpawnAuthorization.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        db.add(
            AuditEvent(
                id=str(uuid.uuid4()),
                actor_user_id=operation.actor_user_id,
                workspace_id=operation.workspace_id,
                action=f"OPERATION_{operation.operation_type}",
                result=status.value,
                request_id=f"worker:{operation.id}",
                safe_metadata_json=json_dumps_safe(
                    {"error_code": code} if code else {}
                ),
            )
        )
        db.commit()


async def run_forever(worker: OperationWorker, poll_seconds: float) -> None:
    while True:
        processed = await worker.process_next()
        if not processed:
            await asyncio.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the single durable operation worker"
    )
    parser.add_argument(
        "--once", action="store_true", help="Process at most one ready operation"
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.validate()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    cipher = TokenCipher(
        settings.token_encryption_key_id, settings.token_encryption_key
    )
    hub = HTTPJupyterHubProvider(settings)
    worker = OperationWorker(settings, factory, hub, cipher)

    async def _run() -> None:
        try:
            if args.once:
                await worker.process_next()
            else:
                await run_forever(worker, args.poll_seconds)
        finally:
            await hub.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
