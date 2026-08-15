from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import WorkspaceProfile
from ..profile_values import cpu_limit_to_millicores


DYNAMIC_BASE_KEY = "platform_resource_base"
_SAFE_COMPONENT_RE = re.compile(r"[^a-z0-9-]+")
_DIGEST_FIELDS = tuple(
    sorted(
        {
            "id",
            "version",
            "image",
            "cpu_limit",
            "memory_limit_bytes",
            "pids_limit",
            "private_disk_hard_limit_bytes",
            "writable_layer_size_bytes",
            "tmpfs_size_bytes",
            "shm_size_bytes",
            "log_max_size_bytes",
            "log_max_files",
            "private_mount_path",
            "uid",
            "gid",
            "python_version",
            "kernels",
            "default_kernel",
            "private_disk_quota_enforced",
        }
    )
)


def _provider_options(profile: WorkspaceProfile) -> dict[str, Any]:
    try:
        value = json.loads(profile.provider_options_json)
    except json.JSONDecodeError as exc:
        raise AppError(
            500, "PROFILE_RESOURCE_INVALID", "Runtime profile metadata is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise AppError(
            500, "PROFILE_RESOURCE_INVALID", "Runtime profile metadata is invalid"
        )
    return value


def dynamic_base(profile: WorkspaceProfile) -> dict[str, object] | None:
    value = _provider_options(profile).get(DYNAMIC_BASE_KEY)
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"id", "version", "config_digest"}
        or not isinstance(value["id"], str)
        or type(value["version"]) is not int
        or value["version"] <= 0
        or not isinstance(value["config_digest"], str)
    ):
        raise AppError(
            500, "PROFILE_RESOURCE_INVALID", "Derived profile binding is invalid"
        )
    return value


def is_dynamic_resource_profile(profile: WorkspaceProfile) -> bool:
    return dynamic_base(profile) is not None


def _runtime_signature(raw: dict[str, Any]) -> str:
    execution = {
        key: raw[key]
        for key in _DIGEST_FIELDS
        if key not in {"id", "version", "cpu_limit", "memory_limit_bytes"}
    }
    if len(execution) != len(_DIGEST_FIELDS) - 4:
        raise AppError(
            500,
            "PROFILE_RESOURCE_INVALID",
            "A selectable runtime profile cannot be used as a resource template",
        )
    canonical = json.dumps(
        execution, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def derived_profile_id(
    *, kernel_name: str, runtime_signature: str, cpu_millicores: int, memory_mb: int
) -> str:
    kernel = _SAFE_COMPONENT_RE.sub("-", kernel_name.lower()).strip("-")[:16]
    value = f"runtime-{kernel}-{runtime_signature[:8]}-c{cpu_millicores}-m{memory_mb}"
    if not kernel or len(value) > 63:
        raise AppError(
            500, "PROFILE_RESOURCE_INVALID", "Derived profile identity is invalid"
        )
    return value


def _profile_digest(raw: dict[str, Any]) -> str:
    canonical = json.dumps(
        {key: raw[key] for key in _DIGEST_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _cpu_json_value(cpu_millicores: int) -> int | float:
    return (
        cpu_millicores // 1000 if cpu_millicores % 1000 == 0 else cpu_millicores / 1000
    )


def ensure_resource_profile_matrix(
    db: Session,
    *,
    cpu_millicores: list[int],
    memory_mb: list[int],
) -> None:
    """Materialize every verified Python runtime × approved resource pair.

    Images, commands, kernels, mounts, identity, and containment limits are
    copied from a policy-imported immutable runtime. Only CPU and memory vary.
    JupyterHub independently reconstructs and checks this derivation at spawn.
    """

    rows = db.scalars(
        select(WorkspaceProfile).where(
            WorkspaceProfile.enabled.is_(True),
            WorkspaceProfile.selectable.is_(True),
            WorkspaceProfile.python_version != "legacy",
        )
    ).all()
    static_rows = [row for row in rows if not is_dynamic_resource_profile(row)]
    by_kernel: dict[tuple[str, str], list[WorkspaceProfile]] = {}
    for row in static_rows:
        by_kernel.setdefault((row.kernel_name, row.python_version), []).append(row)
    if not by_kernel:
        raise AppError(
            503, "RESOURCE_CATALOG_EMPTY", "No verified Python runtime is available"
        )
    existing = {
        (
            row.kernel_name,
            row.python_version,
            cpu_limit_to_millicores(row.cpu_limit),
            row.memory_limit_mb,
        ): row
        for row in rows
    }
    for (kernel_name, python_version), bases in sorted(by_kernel.items()):
        missing = [
            (cpu, memory)
            for cpu in cpu_millicores
            for memory in memory_mb
            if (kernel_name, python_version, cpu, memory) not in existing
        ]
        if not missing:
            continue
        families: dict[str, tuple[WorkspaceProfile, dict[str, Any]]] = {}
        for candidate in bases:
            raw = _provider_options(candidate)
            families.setdefault(_runtime_signature(raw), (candidate, raw))
        if len(families) != 1:
            raise AppError(
                500,
                "PROFILE_RESOURCE_AMBIGUOUS",
                "A Python kernel maps to multiple incompatible runtime contracts",
            )
        signature, (base, base_raw) = next(iter(families.items()))
        for cpu, memory in missing:
            key = (kernel_name, python_version, cpu, memory)
            profile_id = derived_profile_id(
                kernel_name=kernel_name,
                runtime_signature=signature,
                cpu_millicores=cpu,
                memory_mb=memory,
            )
            raw = dict(base_raw)
            raw.update(
                {
                    "id": profile_id,
                    "version": 1,
                    "enabled": True,
                    "selectable": True,
                    "cpu_limit": _cpu_json_value(cpu),
                    "memory_limit_bytes": memory * 1024 * 1024,
                }
            )
            raw["config_digest"] = _profile_digest(raw)
            raw[DYNAMIC_BASE_KEY] = {
                "id": base.id,
                "version": base.version,
                "config_digest": base.config_digest,
            }
            profile = WorkspaceProfile(
                id=profile_id,
                version=1,
                name=(
                    f"{base.kernel_display_name} · CPU {cpu / 1000:g} · "
                    f"Memory {memory} MB"
                )[:80],
                kernel_name=base.kernel_name,
                kernel_display_name=base.kernel_display_name,
                python_version=base.python_version,
                image_ref=base.image_ref,
                cpu_limit=str(_cpu_json_value(cpu)),
                memory_limit_mb=memory,
                pids_limit=base.pids_limit,
                private_disk_limit_mb=base.private_disk_limit_mb,
                private_disk_quota_enforced=base.private_disk_quota_enforced,
                idle_timeout_seconds=base.idle_timeout_seconds,
                provider_options_json=json.dumps(
                    raw, sort_keys=True, separators=(",", ":")
                ),
                config_digest=raw["config_digest"],
                enabled=True,
                selectable=True,
            )
            db.add(profile)
            existing[key] = profile
    db.flush()
