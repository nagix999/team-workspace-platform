from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import begin_immediate
from ..domain import ProvisionStatus, UserProvisioningStatus, UserStatus
from ..errors import AppError
from ..models import (
    AuditEvent,
    User,
    UserProvisioningJob,
    WorkspaceProfile,
    WorkspaceVolumeSlot,
)
from ..security import json_dumps_safe


GENERIC_FAILURE_CODE = "HOST_PROVISIONING_FAILED"
GENERIC_FAILURE_SUMMARY = "개인 저장공간을 준비하지 못했습니다. 다시 요청해 주세요."
ATTEMPTS_EXHAUSTED_CODE = "PROVISIONING_ATTEMPTS_EXHAUSTED"
ATTEMPTS_EXHAUSTED_SUMMARY = "개인 저장공간 준비 재시도 한도에 도달했습니다."


class ProvisioningManifestError(ValueError):
    """A non-sensitive manifest/policy validation failure."""


@dataclass(frozen=True)
class ProvisioningClaim:
    user_id: str
    username: str
    attempt_no: int
    lease_expires_at: datetime


def _audit(
    db: Session,
    *,
    actor_id: str,
    action: str,
    result: str,
    request_id: str,
    metadata: dict[str, object] | None = None,
) -> None:
    db.add(
        AuditEvent(
            id=str(uuid.uuid4()),
            actor_user_id=actor_id,
            workspace_id=None,
            action=action,
            result=result,
            request_id=request_id,
            safe_metadata_json=json_dumps_safe(metadata or {}),
        )
    )


def _require_feature(settings: Settings) -> None:
    if not settings.web_provisioning_enabled:
        raise AppError(
            409,
            "WEB_PROVISIONING_DISABLED",
            "Web self-service provisioning is not enabled",
        )


def provisioning_dict(
    job: UserProvisioningJob | None, *, max_attempts: int
) -> dict[str, object] | None:
    if job is None:
        return None

    def iso(value: datetime | None) -> str | None:
        return f"{value.isoformat()}Z" if value else None

    return {
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": max_attempts,
        "error_code": job.error_code,
        "error_summary": job.error_summary,
        "requested_at": iso(job.requested_at),
        "started_at": iso(job.started_at),
        "completed_at": iso(job.completed_at),
        "updated_at": iso(job.updated_at),
    }


def provisioning_view(
    *,
    db: Session,
    user: User,
    job: UserProvisioningJob | None,
    settings: Settings,
) -> dict[str, object]:
    if user.status == UserStatus.ACTIVE.value and not active_inventory_is_valid(
        db, user
    ):
        value = (
            provisioning_dict(job, max_attempts=settings.provisioning_max_attempts)
            if job is not None
            else {
                "attempts": 0,
                "max_attempts": settings.provisioning_max_attempts,
                "requested_at": None,
                "started_at": None,
                "completed_at": None,
                "updated_at": None,
            }
        )
        assert value is not None
        return {
            **value,
            "status": UserProvisioningStatus.FAILED.value,
            "error_code": "PROVISIONING_INVARIANT_FAILED",
            "error_summary": "개인 저장공간 상태를 확인할 수 없습니다.",
        }
    if job is not None:
        value = provisioning_dict(job, max_attempts=settings.provisioning_max_attempts)
        assert value is not None
        return value
    error_code = None
    error_summary = None
    if user.status == UserStatus.ACTIVE.value:
        status = UserProvisioningStatus.SUCCEEDED.value
    elif (
        user.status == UserStatus.PROVISIONING.value
        and settings.web_provisioning_enabled
    ):
        status = "NOT_REQUESTED"
    else:
        status = "MANUAL_REQUIRED"
    return {
        "status": status,
        "attempts": 0,
        "max_attempts": settings.provisioning_max_attempts,
        "error_code": error_code,
        "error_summary": error_summary,
        "requested_at": None,
        "started_at": None,
        "completed_at": None,
        "updated_at": None,
    }


def request_self_provisioning(
    db: Session,
    *,
    settings: Settings,
    user: User,
    request_id: str,
) -> UserProvisioningJob:
    _require_feature(settings)
    begin_immediate(db)
    now = datetime.utcnow()
    job = db.get(UserProvisioningJob, user.id)

    if user.status == UserStatus.DISABLED.value:
        db.rollback()
        raise AppError(403, "USER_NOT_ACTIVE", "Disabled users cannot be provisioned")
    if user.status == UserStatus.ACTIVE.value:
        if not active_inventory_is_valid(db, user):
            db.rollback()
            raise AppError(
                409,
                "PROVISIONING_INVARIANT_FAILED",
                "Active user volume inventory is incomplete",
            )
        reconciled = job is None or job.status != UserProvisioningStatus.SUCCEEDED.value
        if job is None:
            job = UserProvisioningJob(
                user_id=user.id,
                status=UserProvisioningStatus.SUCCEEDED.value,
                attempts=0,
                requested_at=now,
                started_at=now,
                completed_at=now,
                updated_at=now,
            )
            db.add(job)
        elif job.status != UserProvisioningStatus.SUCCEEDED.value:
            job.status = UserProvisioningStatus.SUCCEEDED.value
            job.error_code = None
            job.error_summary = None
            job.completed_at = job.completed_at or now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
        if reconciled:
            _audit(
                db,
                actor_id=user.id,
                action="USER_PROVISIONING_SUCCEEDED",
                result="SUCCEEDED",
                request_id=request_id,
                metadata={"source": "active-reconciliation"},
            )
        db.commit()
        return job
    if user.status != UserStatus.PROVISIONING.value:
        db.rollback()
        raise AppError(403, "USER_NOT_ACTIVE", "The platform account is not available")

    requested = False
    if job is None:
        job = UserProvisioningJob(
            user_id=user.id,
            status=UserProvisioningStatus.PENDING.value,
            attempts=0,
            requested_at=now,
            updated_at=now,
        )
        db.add(job)
        requested = True
    elif job.status == UserProvisioningStatus.FAILED.value:
        job.status = UserProvisioningStatus.PENDING.value
        job.attempts = 0
        job.error_code = None
        job.error_summary = None
        job.requested_at = now
        job.started_at = None
        job.completed_at = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        requested = True
    elif job.status == UserProvisioningStatus.SUCCEEDED.value:
        db.rollback()
        raise AppError(
            500,
            "PROVISIONING_STATE_INVALID",
            "Provisioning state is inconsistent with the user account",
        )

    if requested:
        _audit(
            db,
            actor_id=user.id,
            action="USER_PROVISIONING_REQUESTED",
            result="ACCEPTED",
            request_id=request_id,
        )
    db.commit()
    return job


def claim_provisioning_job(
    db: Session,
    *,
    settings: Settings,
    worker_id: str,
    request_id: str,
) -> ProvisioningClaim | None:
    _require_feature(settings)
    begin_immediate(db)
    now = datetime.utcnow()
    jobs = db.scalars(
        select(UserProvisioningJob)
        .where(
            or_(
                UserProvisioningJob.status == UserProvisioningStatus.PENDING.value,
                (
                    (UserProvisioningJob.status == UserProvisioningStatus.RUNNING.value)
                    & or_(
                        UserProvisioningJob.lease_expires_at.is_(None),
                        UserProvisioningJob.lease_expires_at <= now,
                    )
                ),
            )
        )
        .order_by(UserProvisioningJob.requested_at, UserProvisioningJob.user_id)
    ).all()

    claimed: ProvisioningClaim | None = None
    for job in jobs:
        user = db.get(User, job.user_id)
        if user is None:  # pragma: no cover - guarded by FK
            job.status = UserProvisioningStatus.FAILED.value
            job.error_code = "PROVISIONING_INVARIANT_FAILED"
            job.error_summary = "프로비저닝 사용자 정보를 확인할 수 없습니다."
            job.completed_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            continue
        if user.status == UserStatus.ACTIVE.value:
            valid_inventory = active_inventory_is_valid(db, user)
            job.status = (
                UserProvisioningStatus.SUCCEEDED.value
                if valid_inventory
                else UserProvisioningStatus.FAILED.value
            )
            job.error_code = (
                None if valid_inventory else "PROVISIONING_INVARIANT_FAILED"
            )
            job.error_summary = (
                None if valid_inventory else "개인 저장공간 상태를 확인할 수 없습니다."
            )
            job.completed_at = job.completed_at or now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            _audit(
                db,
                actor_id=user.id,
                action=(
                    "USER_PROVISIONING_SUCCEEDED"
                    if valid_inventory
                    else "USER_PROVISIONING_FAILED"
                ),
                result="SUCCEEDED" if valid_inventory else "FAILED",
                request_id=request_id,
                metadata={
                    "source": "active-reconciliation",
                    **({} if valid_inventory else {"error_code": job.error_code}),
                },
            )
            continue
        if user.status != UserStatus.PROVISIONING.value:
            job.status = UserProvisioningStatus.FAILED.value
            job.error_code = "USER_NOT_ACTIVE"
            job.error_summary = "현재 계정 상태에서는 저장공간을 준비할 수 없습니다."
            job.completed_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            _audit(
                db,
                actor_id=user.id,
                action="USER_PROVISIONING_FAILED",
                result="FAILED",
                request_id=request_id,
                metadata={"error_code": job.error_code},
            )
            continue
        if job.attempts >= settings.provisioning_max_attempts:
            job.status = UserProvisioningStatus.FAILED.value
            job.error_code = ATTEMPTS_EXHAUSTED_CODE
            job.error_summary = ATTEMPTS_EXHAUSTED_SUMMARY
            job.completed_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            _audit(
                db,
                actor_id=user.id,
                action="USER_PROVISIONING_FAILED",
                result="FAILED",
                request_id=request_id,
                metadata={"error_code": job.error_code},
            )
            continue

        job.status = UserProvisioningStatus.RUNNING.value
        job.attempts += 1
        job.started_at = job.started_at or now
        job.error_code = None
        job.error_summary = None
        job.lease_owner = worker_id
        job.lease_expires_at = now + timedelta(
            seconds=settings.provisioning_lease_seconds
        )
        job.updated_at = now
        claimed = ProvisioningClaim(
            user_id=user.id,
            username=user.hub_username,
            attempt_no=job.attempts,
            lease_expires_at=job.lease_expires_at,
        )
        break

    db.commit()
    return claimed


def _leased_job(
    db: Session,
    *,
    user_id: str,
    worker_id: str,
    attempt_no: int,
) -> UserProvisioningJob:
    job = db.get(UserProvisioningJob, user_id)
    now = datetime.utcnow()
    if (
        job is None
        or job.status != UserProvisioningStatus.RUNNING.value
        or job.attempts != attempt_no
        or job.lease_owner != worker_id
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        raise AppError(
            409,
            "PROVISIONING_LEASE_LOST",
            "The provisioning claim is no longer current",
        )
    return job


def _enabled_profile_policy(
    db: Session,
) -> tuple[int, int, int]:
    profiles = db.scalars(
        select(WorkspaceProfile).where(WorkspaceProfile.enabled.is_(True))
    ).all()
    disk_limits = {profile.private_disk_limit_mb for profile in profiles}
    if len(disk_limits) != 1:
        raise ProvisioningManifestError(
            "exactly one common enabled profile disk limit is required"
        )
    identities: set[tuple[int, int]] = set()
    try:
        for profile in profiles:
            options = json.loads(profile.provider_options_json)
            identities.add((int(options["uid"]), int(options["gid"])))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProvisioningManifestError(
            "enabled profile execution identity is invalid"
        ) from exc
    if len(identities) != 1:
        raise ProvisioningManifestError("enabled profiles do not share one UID/GID")
    uid, gid = identities.pop()
    return disk_limits.pop(), uid, gid


def active_inventory_is_valid(db: Session, user: User) -> bool:
    try:
        hard_limit_mb, _uid, _gid = _enabled_profile_policy(db)
    except ProvisioningManifestError:
        return False
    slots = db.scalars(
        select(WorkspaceVolumeSlot)
        .where(WorkspaceVolumeSlot.owner_user_id == user.id)
        .order_by(WorkspaceVolumeSlot.slot_no)
    ).all()
    return len(slots) == 5 and all(
        slot.slot_no == number
        and slot.id
        == str(uuid.uuid5(uuid.UUID(user.id), f"workspace-volume-slot-{number}"))
        and slot.provision_status == ProvisionStatus.PROVISIONED.value
        and slot.hard_limit_mb == hard_limit_mb
        and slot.volume_name == f"jupyter-user-{user.hub_username}-slot-{number}"
        for number, slot in enumerate(slots, start=1)
    )


def activate_user_from_manifest(
    db: Session,
    *,
    settings: Settings,
    user: User,
    manifest: dict[str, Any],
    use_local_synthetic_project_ids: bool = True,
) -> None:
    """Validate a host result and stage five slot rows plus ACTIVE in one transaction.

    The caller owns the transaction and commit so job completion can be atomic with
    the user/slot transition.
    """

    if user.status not in {
        UserStatus.PROVISIONING.value,
        UserStatus.ACTIVE.value,
    }:
        raise ProvisioningManifestError(
            "user state does not permit provisioning activation"
        )

    local_keys = {
        "schema_version",
        "unsafe_local_dev",
        "user_id",
        "username",
        "uid",
        "gid",
        "slots",
    }
    production_keys = local_keys.difference({"unsafe_local_dev"}).union(
        {"inventory_sha256"}
    )
    is_local_manifest = manifest.get("unsafe_local_dev") is True
    expected_keys = local_keys if is_local_manifest else production_keys
    if set(manifest) != expected_keys or manifest.get("schema_version") != 1:
        raise ProvisioningManifestError("provisioner manifest schema is invalid")
    if is_local_manifest and not settings.unsafe_local_runtime:
        raise ProvisioningManifestError(
            "unsafe local manifest is forbidden outside an explicit local test mode"
        )
    if not is_local_manifest and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", str(manifest.get("inventory_sha256", ""))
    ):
        raise ProvisioningManifestError("provisioner inventory digest is invalid")
    if (
        manifest.get("user_id") != user.id
        or manifest.get("username") != user.hub_username
    ):
        raise ProvisioningManifestError("provisioner manifest user binding mismatch")

    hard_limit_mb, expected_uid, expected_gid = _enabled_profile_policy(db)
    if manifest.get("uid") != expected_uid or manifest.get("gid") != expected_gid:
        raise ProvisioningManifestError(
            "provisioner manifest UID/GID does not match enabled profiles"
        )
    slots = manifest.get("slots")
    if not isinstance(slots, list) or len(slots) != 5:
        raise ProvisioningManifestError(
            "provisioner manifest must contain exactly five slots"
        )
    if {slot.get("slot_number") for slot in slots if isinstance(slot, dict)} != set(
        range(1, 6)
    ):
        raise ProvisioningManifestError("provisioner slot numbers must be exactly 1..5")

    supplied_project_ids: set[int] = set()
    project_id_by_slot: dict[int, int] = {}
    production_paths: set[str] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            raise ProvisioningManifestError("provisioner slot is invalid")
        expected_slot_keys = {
            "slot_id",
            "slot_number",
            "volume_name",
            "hard_limit_bytes",
            "project_id",
        }
        if not is_local_manifest:
            expected_slot_keys.add("path")
        if set(slot) != expected_slot_keys:
            raise ProvisioningManifestError("provisioner slot schema is invalid")
        if not is_local_manifest:
            raw_path = slot.get("path")
            if not isinstance(raw_path, str):
                raise ProvisioningManifestError("private volume path is invalid")
            parsed_path = PurePosixPath(raw_path)
            if (
                not parsed_path.is_absolute()
                or ".." in parsed_path.parts
                or str(parsed_path) != raw_path
                or raw_path in production_paths
            ):
                raise ProvisioningManifestError("private volume path is invalid")
            if (
                settings.production_docker_volume_provisioning
                and settings.storage_policy_mode == "docker-volume-unlimited-v1"
                and raw_path
                != f"/var/lib/docker/volumes/{slot.get('volume_name')}/_data"
            ):
                raise ProvisioningManifestError(
                    "private volume path does not match the Docker volume identity"
                )
            production_paths.add(raw_path)
        number = slot["slot_number"]
        if isinstance(number, bool) or not isinstance(number, int):
            raise ProvisioningManifestError("provisioner slot number is invalid")
        expected_slot_id = str(
            uuid.uuid5(uuid.UUID(user.id), f"workspace-volume-slot-{number}")
        )
        if slot.get("slot_id") != expected_slot_id:
            raise ProvisioningManifestError("private volume slot ID is invalid")
        expected_name = f"jupyter-user-{user.hub_username}-slot-{number}"
        if slot.get("volume_name") != expected_name:
            raise ProvisioningManifestError(
                "private volume name does not match username/slot"
            )
        expected_hard_bytes = hard_limit_mb * 1024 * 1024
        if slot.get("hard_limit_bytes") != expected_hard_bytes:
            raise ProvisioningManifestError(
                "private volume hard limit does not match enabled profiles"
            )
        supplied_project_id = slot.get("project_id")
        if (
            isinstance(supplied_project_id, bool)
            or not isinstance(supplied_project_id, int)
            or supplied_project_id <= 0
            or supplied_project_id in supplied_project_ids
        ):
            raise ProvisioningManifestError(
                "quota project IDs must be positive and unique"
            )
        supplied_project_ids.add(supplied_project_id)
        project_id_by_slot[number] = supplied_project_id

        project_id = (
            1_000_000_000 + (uuid.UUID(user.id).int % 90_000_000) * 10 + number
            if is_local_manifest and use_local_synthetic_project_ids
            else supplied_project_id
        )
        values = {
            "owner_user_id": user.id,
            "slot_no": number,
            "volume_name": expected_name,
            "quota_project_id": project_id,
            "hard_limit_mb": hard_limit_mb,
            "provision_status": ProvisionStatus.PROVISIONED.value,
            "verified_at": datetime.utcnow(),
        }
        existing = db.get(WorkspaceVolumeSlot, expected_slot_id)
        conflicting_slot = db.scalar(
            select(WorkspaceVolumeSlot).where(
                WorkspaceVolumeSlot.owner_user_id == user.id,
                WorkspaceVolumeSlot.slot_no == number,
                WorkspaceVolumeSlot.id != expected_slot_id,
            )
        )
        conflicting_project = db.scalar(
            select(WorkspaceVolumeSlot).where(
                WorkspaceVolumeSlot.quota_project_id == project_id,
                WorkspaceVolumeSlot.id != expected_slot_id,
            )
        )
        conflicting_volume = db.scalar(
            select(WorkspaceVolumeSlot).where(
                WorkspaceVolumeSlot.volume_name == expected_name,
                WorkspaceVolumeSlot.id != expected_slot_id,
            )
        )
        if (
            conflicting_slot is not None
            or conflicting_project is not None
            or conflicting_volume is not None
        ):
            raise ProvisioningManifestError(
                "volume-slot inventory conflicts with another allocation"
            )
        if existing is None:
            db.add(WorkspaceVolumeSlot(id=expected_slot_id, **values))
        elif any(
            getattr(existing, field) != value
            for field, value in values.items()
            if field != "verified_at"
        ):
            raise ProvisioningManifestError(
                "existing volume-slot inventory conflicts with manifest"
            )

    ordered_project_ids = [project_id_by_slot[number] for number in range(1, 6)]
    project_id_base = ordered_project_ids[0]
    if (
        ordered_project_ids != list(range(project_id_base, project_id_base + 5))
        or ordered_project_ids[-1] >= 2**31
    ):
        raise ProvisioningManifestError(
            "quota project IDs must be one contiguous supported block"
        )

    if not is_local_manifest:
        canonical_inventory = json.dumps(
            {
                "user_id": manifest["user_id"],
                "username": manifest["username"],
                "uid": manifest["uid"],
                "gid": manifest["gid"],
                "slots": slots,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_inventory_digest = (
            "sha256:" + hashlib.sha256(canonical_inventory).hexdigest()
        )
        if manifest["inventory_sha256"] != expected_inventory_digest:
            raise ProvisioningManifestError(
                "provisioner inventory digest does not match its contents"
            )

    db.flush()
    provisioned = db.scalars(
        select(WorkspaceVolumeSlot).where(
            WorkspaceVolumeSlot.owner_user_id == user.id,
            WorkspaceVolumeSlot.provision_status == ProvisionStatus.PROVISIONED.value,
        )
    ).all()
    if len(provisioned) != 5:
        raise ProvisioningManifestError(
            "user does not have exactly five verified slots"
        )
    if user.status == UserStatus.DISABLED.value:
        raise ProvisioningManifestError(
            "disabled user cannot be reactivated by provisioning"
        )
    if user.status == UserStatus.PROVISIONING.value:
        user.status = UserStatus.ACTIVE.value
        user.updated_at = datetime.utcnow()


def complete_provisioning_job(
    db: Session,
    *,
    settings: Settings,
    worker_id: str,
    user_id: str,
    attempt_no: int,
    manifest: dict[str, Any],
    request_id: str,
) -> None:
    _require_feature(settings)
    begin_immediate(db)
    job = _leased_job(
        db,
        user_id=user_id,
        worker_id=worker_id,
        attempt_no=attempt_no,
    )
    user = db.get(User, user_id)
    if user is None:  # pragma: no cover - guarded by FK
        db.rollback()
        raise AppError(
            409, "PROVISIONING_INVARIANT_FAILED", "Provisioning user is missing"
        )
    try:
        if user.status != UserStatus.PROVISIONING.value:
            raise ProvisioningManifestError(
                "user is no longer in the provisioning state"
            )
        activate_user_from_manifest(
            db,
            settings=settings,
            user=user,
            manifest=manifest,
        )
    except (ProvisioningManifestError, IntegrityError) as exc:
        db.rollback()
        raise AppError(
            409,
            "PROVISIONING_MANIFEST_REJECTED",
            "The provisioning manifest did not match platform policy",
        ) from exc

    now = datetime.utcnow()
    job.status = UserProvisioningStatus.SUCCEEDED.value
    job.error_code = None
    job.error_summary = None
    job.completed_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now
    _audit(
        db,
        actor_id=user.id,
        action="USER_PROVISIONING_SUCCEEDED",
        result="SUCCEEDED",
        request_id=request_id,
        metadata={"attempt_no": attempt_no},
    )
    db.commit()


def fail_provisioning_job(
    db: Session,
    *,
    settings: Settings,
    worker_id: str,
    user_id: str,
    attempt_no: int,
    request_id: str,
) -> None:
    _require_feature(settings)
    begin_immediate(db)
    job = _leased_job(
        db,
        user_id=user_id,
        worker_id=worker_id,
        attempt_no=attempt_no,
    )
    now = datetime.utcnow()
    terminal = job.attempts >= settings.provisioning_max_attempts
    job.status = (
        UserProvisioningStatus.FAILED.value
        if terminal
        else UserProvisioningStatus.PENDING.value
    )
    job.error_code = ATTEMPTS_EXHAUSTED_CODE if terminal else GENERIC_FAILURE_CODE
    job.error_summary = (
        ATTEMPTS_EXHAUSTED_SUMMARY if terminal else GENERIC_FAILURE_SUMMARY
    )
    job.completed_at = now if terminal else None
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now
    _audit(
        db,
        actor_id=user_id,
        action="USER_PROVISIONING_FAILED",
        result="FAILED" if terminal else "RETRYING",
        request_id=request_id,
        metadata={"attempt_no": attempt_no, "error_code": job.error_code},
    )
    db.commit()
