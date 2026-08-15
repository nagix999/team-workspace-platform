from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import begin_immediate
from ..domain import DesiredState, OperationStatus, ProvisionStatus, UserStatus
from ..errors import AppError
from ..models import (
    Operation,
    SpawnAuthorization,
    User,
    Workspace,
    WorkspaceProfile,
    WorkspaceVolumeSlot,
)
from ..profile_values import cpu_limit_to_millicores
from ..schemas import (
    SpawnAuthorizationBinding,
    SpawnAuthorizationPayload,
    SpawnCheckRequest,
    SpawnConsumeRequest,
)
from ..security import TokenCipher, sha256_hex
from .environment import (
    environment_map_digest,
    spawn_snapshot_purpose,
    validate_environment_name,
    validate_environment_value,
)
from .resource_profiles import dynamic_base


@dataclass(frozen=True)
class SpawnApproval:
    authorization: SpawnAuthorizationPayload | SpawnAuthorizationBinding


def _execution_identity(profile: WorkspaceProfile) -> tuple[int, int, int]:
    try:
        options = json.loads(profile.provider_options_json)
        uid = int(options["uid"])
        gid = int(options["gid"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AppError(
            403, "SPAWN_PROFILE_INVALID", "Profile execution identity is missing"
        ) from exc
    hard_bytes = profile.private_disk_limit_mb * 1024 * 1024
    configured_hard_bytes = options.get("private_disk_hard_limit_bytes", hard_bytes)
    if uid <= 0 or gid <= 0 or configured_hard_bytes != hard_bytes:
        raise AppError(
            403,
            "SPAWN_PROFILE_INVALID",
            "Profile execution identity or disk limit is invalid",
        )
    return uid, gid, hard_bytes


def _validate_mutable_invariants(
    db: Session, authorization: SpawnAuthorization, *, require_consumed: bool
) -> tuple[Operation, Workspace, User, WorkspaceVolumeSlot, WorkspaceProfile]:
    now = datetime.utcnow()
    if authorization.revoked_at is not None or authorization.expires_at <= now:
        raise AppError(
            403, "SPAWN_NOT_AUTHORIZED", "Spawn authorization expired or was revoked"
        )
    if require_consumed and authorization.consumed_at is None:
        raise AppError(
            403, "SPAWN_NOT_AUTHORIZED", "Spawn authorization was not consumed"
        )

    operation = db.get(Operation, authorization.operation_id)
    workspace = db.get(Workspace, authorization.workspace_id)
    user = db.get(User, authorization.owner_user_id)
    slot = db.get(WorkspaceVolumeSlot, authorization.private_volume_slot_id)
    profile = db.get(
        WorkspaceProfile, (authorization.profile_id, authorization.profile_version)
    )
    invariant_ok = all((operation, workspace, user, slot, profile))
    if invariant_ok:
        assert operation and workspace and user and slot and profile
        invariant_ok = (
            operation.status == OperationStatus.RUNNING.value
            and operation.attempts == authorization.attempt_no
            and workspace.owner_user_id == user.id == slot.owner_user_id
            and workspace.private_volume_slot_id == slot.id
            and workspace.hub_server_name == authorization.hub_server_name
            and workspace.profile_id == profile.id
            and workspace.profile_version == profile.version
            and workspace.spec_version == authorization.workspace_spec_version
            and workspace.desired_state == DesiredState.RUNNING.value
            and workspace.deletion_started_at is None
            and workspace.archived_at is None
            and user.status == UserStatus.ACTIVE.value
            and user.hub_username == authorization.hub_username
            and slot.provision_status == ProvisionStatus.PROVISIONED.value
            and slot.hard_limit_mb == profile.private_disk_limit_mb
            and profile.enabled
            and profile.config_digest == authorization.profile_config_digest
            and authorization.environment_digest is not None
            and user.environment_generation == authorization.user_environment_generation
            and workspace.environment_generation
            == authorization.workspace_environment_generation
        )
    if not invariant_ok:
        raise AppError(403, "SPAWN_INVARIANT_FAILED", "Spawn invariants no longer hold")
    assert operation and workspace and user and slot and profile
    return operation, workspace, user, slot, profile


def _binding(
    authorization: SpawnAuthorization,
    operation: Operation,
    workspace: Workspace,
    user: User,
    slot: WorkspaceVolumeSlot,
    profile: WorkspaceProfile,
) -> SpawnAuthorizationBinding:
    uid, gid, hard_bytes = _execution_identity(profile)
    base = dynamic_base(profile) or {
        "id": profile.id,
        "version": profile.version,
        "config_digest": profile.config_digest,
    }
    if authorization.environment_digest is None:
        raise AppError(403, "SPAWN_INVARIANT_FAILED", "Environment digest is missing")
    return SpawnAuthorizationBinding(
        spawn_authorization_id=authorization.id,
        workspace_id=workspace.id,
        operation_id=operation.id,
        attempt_no=authorization.attempt_no,
        workspace_spec_version=authorization.workspace_spec_version,
        username=user.hub_username,
        server_name=workspace.hub_server_name,
        profile_id=profile.id,
        profile_version=profile.version,
        profile_config_digest=profile.config_digest,
        runtime_base_profile_id=str(base["id"]),
        runtime_base_profile_version=int(base["version"]),
        runtime_base_profile_config_digest=str(base["config_digest"]),
        cpu_limit_millicores=cpu_limit_to_millicores(profile.cpu_limit),
        memory_limit_bytes=profile.memory_limit_mb * 1024 * 1024,
        private_volume_slot_id=slot.id,
        private_volume_slot_number=slot.slot_no,
        private_volume_name=slot.volume_name,
        private_disk_hard_limit_bytes=hard_bytes,
        uid=uid,
        gid=gid,
        environment_digest=authorization.environment_digest,
        user_environment_generation=authorization.user_environment_generation,
        workspace_environment_generation=authorization.workspace_environment_generation,
        valid_until_unix=int(
            authorization.expires_at.replace(tzinfo=timezone.utc).timestamp()
        ),
    )


def consume_spawn_authorization(
    db: Session,
    request: SpawnConsumeRequest,
    *,
    cipher: TokenCipher,
    environment_hmac_key: str,
) -> SpawnApproval:
    now = datetime.utcnow()
    begin_immediate(db)
    authorization = db.scalar(
        select(SpawnAuthorization).where(
            SpawnAuthorization.ticket_hash == sha256_hex(request.spawn_ticket)
        )
    )
    if authorization is None:
        db.rollback()
        raise AppError(403, "SPAWN_NOT_AUTHORIZED", "Spawn authorization was not found")
    if authorization.consumed_at is not None:
        db.rollback()
        raise AppError(
            403, "SPAWN_NOT_AUTHORIZED", "Spawn authorization was already consumed"
        )
    if (
        authorization.hub_username != request.username
        or authorization.hub_server_name != request.server_name
        or authorization.profile_id != request.profile_id
        or authorization.profile_version != request.profile_version
    ):
        db.rollback()
        raise AppError(
            403, "SPAWN_BINDING_MISMATCH", "Spawn authorization binding does not match"
        )
    try:
        operation, workspace, user, slot, profile = _validate_mutable_invariants(
            db, authorization, require_consumed=False
        )
        binding = _binding(authorization, operation, workspace, user, slot, profile)
        if authorization.environment_snapshot_cipher is None:
            raise AppError(
                403, "SPAWN_INVARIANT_FAILED", "Environment snapshot is missing"
            )
        plaintext = cipher.decrypt(
            authorization.environment_snapshot_cipher,
            purpose=spawn_snapshot_purpose(
                authorization.id, workspace.id, binding.environment_digest
            ),
        )
        raw_environment = json.loads(plaintext)
        if not isinstance(raw_environment, dict) or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in raw_environment.items()
        ):
            raise AppError(
                403, "SPAWN_INVARIANT_FAILED", "Environment snapshot is invalid"
            )
        environment = dict(raw_environment)
        for name, value in environment.items():
            validate_environment_name(name)
            validate_environment_value(value)
        if (
            environment_map_digest(environment, environment_hmac_key)
            != binding.environment_digest
        ):
            raise AppError(
                403, "SPAWN_INVARIANT_FAILED", "Environment digest does not match"
            )
        payload = SpawnAuthorizationPayload(
            **binding.model_dump(), environment=environment
        )
    except (AppError, ValueError, json.JSONDecodeError) as exc:
        db.rollback()
        if isinstance(exc, AppError):
            raise
        raise AppError(
            403, "SPAWN_INVARIANT_FAILED", "Environment snapshot is invalid"
        ) from exc
    authorization.consumed_at = now
    # The one-time response now owns the in-memory plaintext. Durable snapshot
    # ciphertext is erased immediately to minimize retained secret material.
    authorization.environment_snapshot_cipher = None
    db.commit()
    return SpawnApproval(payload)


def check_spawn_authorization(db: Session, request: SpawnCheckRequest) -> SpawnApproval:
    begin_immediate(db)
    authorization = db.get(SpawnAuthorization, request.spawn_authorization_id)
    if authorization is None:
        db.rollback()
        raise AppError(403, "SPAWN_NOT_AUTHORIZED", "Spawn authorization was not found")
    try:
        operation, workspace, user, slot, profile = _validate_mutable_invariants(
            db, authorization, require_consumed=True
        )
        expected = _binding(authorization, operation, workspace, user, slot, profile)
    except AppError:
        db.rollback()
        raise
    supplied = request.model_dump(exclude={"schema_version"})
    if supplied != expected.model_dump():
        db.rollback()
        raise AppError(
            403, "SPAWN_BINDING_MISMATCH", "Spawn authorization facts changed"
        )
    db.commit()
    return SpawnApproval(expected)
