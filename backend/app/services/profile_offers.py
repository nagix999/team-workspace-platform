from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import ResourcePolicy, WorkspaceProfile, WorkspaceProfileOffer
from ..serialization import iso
from .resource_policy import profile_is_allowed


def _current_runtime_profiles(db: Session) -> list[WorkspaceProfile]:
    rows = db.scalars(
        select(WorkspaceProfile)
        .where(
            WorkspaceProfile.enabled.is_(True),
            WorkspaceProfile.selectable.is_(True),
            WorkspaceProfile.python_version != "legacy",
        )
        .order_by(WorkspaceProfile.id, WorkspaceProfile.version.desc())
    ).all()
    current: dict[str, WorkspaceProfile] = {}
    for row in rows:
        current.setdefault(row.id, row)
    return list(current.values())


def ensure_default_offers(db: Session) -> None:
    existing_ids = set(db.scalars(select(WorkspaceProfileOffer.id)).all())
    now = datetime.utcnow()
    for profile in _current_runtime_profiles(db):
        if profile.id in existing_ids:
            continue
        db.add(
            WorkspaceProfileOffer(
                id=profile.id,
                row_version=1,
                name=profile.name[:80],
                runtime_profile_id=profile.id,
                runtime_profile_version=profile.version,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )


def runtime_template_dict(profile: WorkspaceProfile) -> dict[str, object]:
    return {
        "id": profile.id,
        "version": profile.version,
        "kernel_name": profile.kernel_name,
        "kernel_display_name": profile.kernel_display_name,
        "python_version": profile.python_version,
        "cpu_limit": profile.cpu_limit,
        "memory_limit_mb": profile.memory_limit_mb,
    }


def offer_dict(
    offer: WorkspaceProfileOffer,
    runtime: WorkspaceProfile,
    *,
    resource_policy: ResourcePolicy | None = None,
) -> dict[str, object]:
    effective = offer.enabled and (
        profile_is_allowed(runtime, resource_policy)
        if resource_policy is not None
        else runtime.enabled and runtime.selectable
    )
    reason = None
    if not offer.enabled:
        reason = "OFFER_DISABLED"
    elif not runtime.enabled or not runtime.selectable:
        reason = "RUNTIME_UNAVAILABLE"
    elif not effective:
        reason = "RESOURCE_POLICY_EXCLUDED"
    return {
        "id": offer.id,
        "version": offer.row_version,
        "name": offer.name,
        "description": offer.description,
        "enabled": offer.enabled,
        "effective_selectable": effective,
        "unavailable_reason": reason,
        "runtime_profile": runtime_template_dict(runtime),
        "created_at": iso(offer.created_at),
        "updated_at": iso(offer.updated_at),
    }


def public_offer_dict(
    offer: WorkspaceProfileOffer, runtime: WorkspaceProfile
) -> dict[str, object]:
    effective_disk_limit_mb = (
        runtime.private_disk_limit_mb if runtime.private_disk_quota_enforced else None
    )
    return {
        "id": offer.id,
        "version": offer.row_version,
        "name": offer.name,
        "description": offer.description,
        "kernel_name": runtime.kernel_name,
        "kernel_display_name": runtime.kernel_display_name,
        "python_version": runtime.python_version,
        "cpu_limit": runtime.cpu_limit,
        "memory_limit_mb": runtime.memory_limit_mb,
        "pids_limit": runtime.pids_limit,
        "private_disk_limit_mb": effective_disk_limit_mb,
        "private_disk_quota_enforced": runtime.private_disk_quota_enforced,
    }


def resolve_offer(
    db: Session, offer_id: str, offer_version: int, *, require_enabled: bool = True
) -> tuple[WorkspaceProfileOffer, WorkspaceProfile]:
    offer = db.get(WorkspaceProfileOffer, offer_id)
    if offer is None or (require_enabled and not offer.enabled):
        raise AppError(404, "PROFILE_NOT_FOUND", "Workspace profile was not found")
    if offer.row_version != offer_version:
        raise AppError(
            409,
            "PROFILE_VERSION_STALE",
            "Workspace profile version is no longer current",
        )
    runtime = db.get(
        WorkspaceProfile,
        (offer.runtime_profile_id, offer.runtime_profile_version),
    )
    if runtime is None or not runtime.enabled:
        raise AppError(409, "PROFILE_DISABLED", "Runtime profile is disabled")
    return offer, runtime


def create_offer(
    db: Session,
    *,
    actor_user_id: str,
    name: str,
    description: str | None,
    runtime_profile_id: str,
    runtime_profile_version: int,
    enabled: bool,
) -> tuple[WorkspaceProfileOffer, WorkspaceProfile]:
    runtime = db.get(WorkspaceProfile, (runtime_profile_id, runtime_profile_version))
    if (
        runtime is None
        or not runtime.enabled
        or not runtime.selectable
        or runtime.python_version == "legacy"
    ):
        raise AppError(
            422,
            "RUNTIME_PROFILE_NOT_ALLOWED",
            "Offer must reference an enabled managed runtime template",
        )
    now = datetime.utcnow()
    offer = WorkspaceProfileOffer(
        id=f"offer-{uuid.uuid4().hex}",
        row_version=1,
        name=name,
        description=description,
        runtime_profile_id=runtime.id,
        runtime_profile_version=runtime.version,
        enabled=enabled,
        created_by_user_id=actor_user_id,
        created_at=now,
        updated_at=now,
        disabled_at=None if enabled else now,
    )
    db.add(offer)
    db.flush()
    return offer, runtime


def update_offer(
    db: Session,
    *,
    offer_id: str,
    expected_version: int,
    name: str,
    description: str | None,
    enabled: bool,
) -> tuple[WorkspaceProfileOffer, WorkspaceProfile]:
    offer = db.get(WorkspaceProfileOffer, offer_id)
    if offer is None:
        raise AppError(404, "PROFILE_NOT_FOUND", "Workspace profile was not found")
    if offer.row_version != expected_version:
        raise AppError(
            409,
            "PROFILE_VERSION_CONFLICT",
            "Workspace profile was changed by another administrator",
        )
    runtime = db.get(
        WorkspaceProfile, (offer.runtime_profile_id, offer.runtime_profile_version)
    )
    if runtime is None:  # pragma: no cover - guarded by FK
        raise AppError(500, "INVARIANT_VIOLATION", "Runtime profile is missing")
    if enabled and (
        not runtime.enabled
        or not runtime.selectable
        or runtime.python_version == "legacy"
    ):
        raise AppError(
            422,
            "RUNTIME_PROFILE_NOT_ALLOWED",
            "Disabled or unmanaged runtime profile cannot be offered",
        )
    now = datetime.utcnow()
    offer.name = name
    offer.description = description
    offer.enabled = enabled
    offer.disabled_at = None if enabled else now
    offer.row_version += 1
    offer.updated_at = now
    return offer, runtime


def disable_offer(
    db: Session, *, offer_id: str, expected_version: int
) -> tuple[WorkspaceProfileOffer, WorkspaceProfile]:
    offer = db.get(WorkspaceProfileOffer, offer_id)
    if offer is None:
        raise AppError(404, "PROFILE_NOT_FOUND", "Workspace profile was not found")
    if offer.row_version != expected_version:
        raise AppError(
            409,
            "PROFILE_VERSION_CONFLICT",
            "Workspace profile was changed by another administrator",
        )
    return update_offer(
        db,
        offer_id=offer_id,
        expected_version=expected_version,
        name=offer.name,
        description=offer.description,
        enabled=False,
    )


def admin_profile_catalog(
    db: Session, *, resource_policy: ResourcePolicy | None = None
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    offers = db.scalars(
        select(WorkspaceProfileOffer).order_by(
            WorkspaceProfileOffer.created_at, WorkspaceProfileOffer.id
        )
    ).all()
    items: list[dict[str, object]] = []
    for offer in offers:
        runtime = db.get(
            WorkspaceProfile,
            (offer.runtime_profile_id, offer.runtime_profile_version),
        )
        if runtime is not None:
            items.append(offer_dict(offer, runtime, resource_policy=resource_policy))
    templates = [runtime_template_dict(item) for item in _current_runtime_profiles(db)]
    return items, templates
