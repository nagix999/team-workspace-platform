from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import AppError
from ..models import ResourcePolicy, WorkspaceProfile
from ..profile_values import cpu_limit_to_millicores


@dataclass(frozen=True)
class ResourceCatalog:
    cpu_millicores: tuple[int, ...]
    memory_mb: tuple[int, ...]


def resource_catalog(db: Session, settings: Settings) -> ResourceCatalog:
    rows = db.scalars(
        select(WorkspaceProfile).where(
            WorkspaceProfile.enabled.is_(True),
            WorkspaceProfile.selectable.is_(True),
        )
    ).all()
    cpu_values: set[int] = set()
    memory_values: set[int] = set()
    for profile in rows:
        try:
            cpu = cpu_limit_to_millicores(profile.cpu_limit)
        except ValueError as exc:
            raise AppError(
                500,
                "PROFILE_RESOURCE_INVALID",
                "An enabled profile has invalid CPU configuration",
            ) from exc
        if (
            0 < cpu <= settings.workspace_cpu_budget_millicores
            and 0 < profile.memory_limit_mb <= settings.workspace_memory_budget_mb
        ):
            cpu_values.add(cpu)
            memory_values.add(profile.memory_limit_mb)
    if not cpu_values or not memory_values:
        raise AppError(
            503,
            "RESOURCE_CATALOG_EMPTY",
            "No workspace resource profile fits the configured hard ceiling",
        )
    return ResourceCatalog(tuple(sorted(cpu_values)), tuple(sorted(memory_values)))


def get_resource_policy(
    db: Session,
    settings: Settings,
    *,
    create: bool = False,
    enforce_hard_ceiling: bool = True,
) -> ResourcePolicy:
    policy = db.get(ResourcePolicy, 1)
    if policy is not None:
        # Deployment settings are a hard host ceiling, not just bootstrap
        # defaults. A lower ceiling on a later rollout must fail closed until an
        # administrator deliberately lowers the persisted policy; silently
        # clamping it would make the approved policy and admission state diverge.
        if enforce_hard_ceiling and (
            policy.cpu_budget_millicores > settings.workspace_cpu_budget_millicores
            or policy.memory_budget_mb > settings.workspace_memory_budget_mb
        ):
            raise AppError(
                503,
                "RESOURCE_POLICY_EXCEEDS_HARD_CEILING",
                "Persisted resource policy exceeds the deployment hard ceiling",
            )
        return policy
    if not create:
        raise AppError(
            503, "RESOURCE_POLICY_MISSING", "Resource policy is not initialized"
        )
    catalog = resource_catalog(db, settings)
    now = datetime.utcnow()
    policy = ResourcePolicy(
        id=1,
        version=1,
        cpu_budget_millicores=settings.workspace_cpu_budget_millicores,
        memory_budget_mb=settings.workspace_memory_budget_mb,
        selectable_cpu_millicores_json=json.dumps(list(catalog.cpu_millicores)),
        selectable_memory_mb_json=json.dumps(list(catalog.memory_mb)),
        created_at=now,
        updated_at=now,
    )
    db.add(policy)
    db.flush()
    return policy


def selected_resource_values(policy: ResourcePolicy) -> tuple[set[int], set[int]]:
    try:
        cpus = json.loads(policy.selectable_cpu_millicores_json)
        memories = json.loads(policy.selectable_memory_mb_json)
    except json.JSONDecodeError as exc:  # pragma: no cover - database corruption
        raise AppError(
            500, "RESOURCE_POLICY_INVALID", "Resource policy JSON is invalid"
        ) from exc
    if (
        not isinstance(cpus, list)
        or not isinstance(memories, list)
        or any(type(value) is not int or value <= 0 for value in cpus + memories)
    ):
        raise AppError(500, "RESOURCE_POLICY_INVALID", "Resource policy is invalid")
    return set(cpus), set(memories)


def profile_is_allowed(profile: WorkspaceProfile, policy: ResourcePolicy) -> bool:
    cpus, memories = selected_resource_values(policy)
    try:
        cpu = cpu_limit_to_millicores(profile.cpu_limit)
    except ValueError:
        return False
    return (
        profile.enabled
        and profile.selectable
        and cpu in cpus
        and profile.memory_limit_mb in memories
        and cpu <= policy.cpu_budget_millicores
        and profile.memory_limit_mb <= policy.memory_budget_mb
    )


def resource_policy_dict(
    db: Session, settings: Settings, policy: ResourcePolicy
) -> dict[str, object]:
    from ..serialization import iso

    catalog = resource_catalog(db, settings)
    cpus, memories = selected_resource_values(policy)
    return {
        "version": policy.version,
        "cpu_budget_millicores": policy.cpu_budget_millicores,
        "memory_budget_mb": policy.memory_budget_mb,
        "selectable_cpu_millicores": sorted(cpus),
        "selectable_memory_mb": sorted(memories),
        "available_cpu_millicores": list(catalog.cpu_millicores),
        "available_memory_mb": list(catalog.memory_mb),
        "hard_ceiling": {
            "cpu_millicores": settings.workspace_cpu_budget_millicores,
            "memory_mb": settings.workspace_memory_budget_mb,
        },
        "updated_at": iso(policy.updated_at),
    }


def update_resource_policy(
    db: Session,
    *,
    settings: Settings,
    actor_user_id: str,
    expected_version: int,
    cpu_budget_millicores: int,
    memory_budget_mb: int,
    selectable_cpu_millicores: list[int],
    selectable_memory_mb: list[int],
    reserved_cpu_millicores: int,
    reserved_memory_mb: int,
) -> ResourcePolicy:
    # Keep the administrative recovery path usable after a deployment lowers a
    # hard ceiling. Admission reads remain strict, while this mutation may only
    # replace the drifted value with one that passes the new ceiling checks below.
    policy = get_resource_policy(db, settings, enforce_hard_ceiling=False)
    if policy.version != expected_version:
        raise AppError(
            409,
            "RESOURCE_POLICY_VERSION_CONFLICT",
            "Resource policy was changed by another administrator",
        )
    if (
        cpu_budget_millicores > settings.workspace_cpu_budget_millicores
        or memory_budget_mb > settings.workspace_memory_budget_mb
    ):
        raise AppError(
            422,
            "RESOURCE_BUDGET_EXCEEDS_HARD_CEILING",
            "Runtime resource budget cannot exceed the deployment hard ceiling",
        )
    if (
        cpu_budget_millicores < reserved_cpu_millicores
        or memory_budget_mb < reserved_memory_mb
    ):
        raise AppError(
            409,
            "RESOURCE_BUDGET_BELOW_RESERVED",
            "Runtime resource budget cannot be lower than current reservations",
        )
    selected_cpu = sorted(set(selectable_cpu_millicores))
    selected_memory = sorted(set(selectable_memory_mb))
    if (
        selected_cpu != selectable_cpu_millicores
        or selected_memory != selectable_memory_mb
        or not selected_cpu
        or not selected_memory
        or any(type(value) is not int or value <= 0 for value in selected_cpu)
        or any(type(value) is not int or value <= 0 for value in selected_memory)
        or any(value > cpu_budget_millicores for value in selected_cpu)
        or any(value > memory_budget_mb for value in selected_memory)
        or any(
            value > settings.workspace_cpu_budget_millicores for value in selected_cpu
        )
        or any(value > settings.workspace_memory_budget_mb for value in selected_memory)
    ):
        raise AppError(
            422,
            "RESOURCE_SELECTION_INVALID",
            "Selectable CPU and memory values must be sorted positive values within budget",
        )

    # Materialize every selected CPU × memory pair from the immutable Python
    # runtime families imported from the deployment policy. This keeps images,
    # commands, kernels, mounts and containment settings administrator-proof
    # while allowing a larger host to expose new numeric resource choices.
    from .profile_offers import ensure_default_offers
    from .resource_profiles import ensure_resource_profile_matrix

    ensure_resource_profile_matrix(
        db, cpu_millicores=selected_cpu, memory_mb=selected_memory
    )
    ensure_default_offers(db)
    candidates = db.scalars(
        select(WorkspaceProfile).where(
            WorkspaceProfile.enabled.is_(True),
            WorkspaceProfile.selectable.is_(True),
        )
    ).all()
    if not any(
        cpu_limit_to_millicores(profile.cpu_limit) in selected_cpu
        and profile.memory_limit_mb in selected_memory
        for profile in candidates
    ):
        raise AppError(
            422,
            "RESOURCE_SELECTION_EMPTY",
            "Resource selection exposes no workspace profile",
        )
    policy.version += 1
    policy.cpu_budget_millicores = cpu_budget_millicores
    policy.memory_budget_mb = memory_budget_mb
    policy.selectable_cpu_millicores_json = json.dumps(selected_cpu)
    policy.selectable_memory_mb_json = json.dumps(selected_memory)
    policy.updated_by_user_id = actor_user_id
    policy.updated_at = datetime.utcnow()
    return policy
