from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select

from .config import Settings
from .db import begin_immediate, create_database_engine, create_session_factory
from .domain import UserProvisioningStatus
from .models import User, UserProvisioningJob, WorkspaceProfile
from .profile_values import cpu_limit_to_millicores
from .services.provisioning import activate_user_from_manifest
from .services.profile_offers import ensure_default_offers
from .services.resource_policy import get_resource_policy, selected_resource_values
from .services.resource_profiles import (
    ensure_resource_profile_matrix,
    is_dynamic_resource_profile,
)


PROFILE_EXECUTION_FIELDS_V1 = tuple(
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
        }
    )
)
PROFILE_EXECUTION_FIELDS_V2 = tuple(
    sorted(
        {
            *PROFILE_EXECUTION_FIELDS_V1,
            "python_version",
            "kernels",
            "default_kernel",
            "private_disk_quota_enforced",
        }
    )
)
PROFILE_EXECUTION_FIELDS = PROFILE_EXECUTION_FIELDS_V2
PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
KERNEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PYTHON_VERSION_RE = re.compile(
    r"^(?:[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
VOLUME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
DIGEST_IMAGE_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PYTHON_EXECUTABLE_RE = re.compile(
    r"^/opt/conda(?:/envs/[a-z][a-z0-9_-]{0,63})?/bin/python$"
)
PRIVATE_MOUNT_PATH = "/home/jovyan/work"
SHARED_MOUNT_PATH = "/home/jovyan/shared"


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"profile {field} must be a positive integer")
    return value


def _absolute_mount(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute POSIX path")
    path = PurePosixPath(value)
    normalized = str(path)
    if ".." in path.parts or normalized in {"/", "/home", "/home/jovyan"}:
        raise ValueError(f"{field} is too broad or contains traversal")
    return normalized


def _mount_paths_overlap(left: str, right: str) -> bool:
    left_path = PurePosixPath(left)
    right_path = PurePosixPath(right)
    return (
        left_path == right_path
        or left_path in right_path.parents
        or right_path in left_path.parents
    )


def _read_json(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value


def _profile_digest(
    profile: dict[str, Any],
    execution_fields: tuple[str, ...] | None = None,
) -> str:
    fields = execution_fields or (
        PROFILE_EXECUTION_FIELDS_V2
        if all(field in profile for field in PROFILE_EXECUTION_FIELDS_V2)
        else PROFILE_EXECUTION_FIELDS_V1
    )
    canonical = json.dumps(
        {key: profile[key] for key in fields},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def import_profiles(settings: Settings, policy_path: str) -> None:
    policy = _read_json(policy_path)
    if (
        set(policy) != {"schema_version", "shared_volume", "profiles"}
        or type(policy["schema_version"]) is not int
        or policy["schema_version"] not in {1, 2}
    ):
        raise ValueError("profile policy top-level schema mismatch")
    schema_version = policy["schema_version"]
    shared = policy["shared_volume"]
    if not isinstance(shared, dict) or set(shared) != {"name", "mount_path", "gid"}:
        raise ValueError("shared volume schema mismatch")
    if not isinstance(shared["name"], str) or not VOLUME_RE.fullmatch(shared["name"]):
        raise ValueError("shared volume name is invalid")
    shared_mount_path = _absolute_mount(
        shared["mount_path"], "shared volume mount_path"
    )
    if shared_mount_path != SHARED_MOUNT_PATH:
        raise ValueError(f"shared volume mount_path must be {SHARED_MOUNT_PATH}")
    _positive_int(shared["gid"], "shared volume gid")
    v1_legacy_profile_keys = set(PROFILE_EXECUTION_FIELDS_V1).union(
        {"enabled", "config_digest"}
    )
    v2_legacy_profile_keys = v1_legacy_profile_keys.union({"selectable"})
    v2_extended_profile_keys = set(PROFILE_EXECUTION_FIELDS_V2).union(
        {"enabled", "selectable", "config_digest"}
    )
    profiles = policy["profiles"]
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("profile policy contains no profiles")
    declared_profile_keys: set[tuple[str, int]] = set()
    for raw in profiles:
        if not isinstance(raw, dict):
            raise ValueError("profile execution schema mismatch")
        profile_id = raw.get("id")
        version = raw.get("version")
        if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError("profile id is invalid")
        if type(version) is not int or version <= 0:
            raise ValueError("profile version must be a positive integer")
        key = (profile_id, version)
        if key in declared_profile_keys:
            raise ValueError(f"duplicate profile key {profile_id}@{version}")
        declared_profile_keys.add(key)
    disk_limits: set[int] = set()
    enabled_count = 0
    selectable_count = 0
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    with factory() as db:
        begin_immediate(db)
        enabled_profile_keys = {
            (profile.id, profile.version)
            for profile in db.scalars(
                select(WorkspaceProfile).where(WorkspaceProfile.enabled.is_(True))
            ).all()
            if not is_dynamic_resource_profile(profile)
        }
        missing_enabled_keys = enabled_profile_keys - declared_profile_keys
        if missing_enabled_keys:
            missing = ", ".join(
                f"{profile_id}@{version}"
                for profile_id, version in sorted(missing_enabled_keys)
            )
            db.rollback()
            raise ValueError(
                "profile policy omits enabled profile(s): "
                f"{missing}; include each tuple explicitly and set enabled=false "
                "to retire it"
            )

        # The imported document is authoritative for new selection. Disabled rows
        # may be omitted and remain unavailable, but every enabled tuple must stay
        # in the Hub's matching allowlist so pinned workspaces remain restartable.
        for stored_profile in db.scalars(select(WorkspaceProfile)).all():
            if not is_dynamic_resource_profile(stored_profile):
                stored_profile.selectable = False
        seen_profile_keys: set[tuple[str, int]] = set()
        for raw in profiles:
            if not isinstance(raw, dict):
                raise ValueError("profile execution schema mismatch")
            raw_keys = set(raw)
            if schema_version == 1 and raw_keys == v1_legacy_profile_keys:
                execution_fields = PROFILE_EXECUTION_FIELDS_V1
                extended_profile = False
            elif schema_version == 2 and raw_keys == v2_legacy_profile_keys:
                execution_fields = PROFILE_EXECUTION_FIELDS_V1
                extended_profile = False
            elif schema_version == 2 and raw_keys == v2_extended_profile_keys:
                execution_fields = PROFILE_EXECUTION_FIELDS_V2
                extended_profile = True
            else:
                # This also rejects a partial v2 extension and any extended profile
                # inside a v1 document.
                raise ValueError("profile execution schema mismatch")
            profile_id = raw["id"]
            version = raw["version"]
            if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(
                profile_id
            ):
                raise ValueError("profile id is invalid")
            if type(version) is not int or version <= 0:
                raise ValueError("profile version must be a positive integer")
            key = (profile_id, version)
            if key in seen_profile_keys:
                raise ValueError(f"duplicate profile key {profile_id}@{version}")
            seen_profile_keys.add(key)
            if type(raw["enabled"]) is not bool:
                raise ValueError("profile enabled must be boolean")
            selectable = raw["selectable"] if schema_version == 2 else raw["enabled"]
            if type(selectable) is not bool:
                raise ValueError("profile selectable must be boolean")
            if selectable and not raw["enabled"]:
                raise ValueError("selectable profile must be enabled")
            if selectable and not extended_profile:
                raise ValueError(
                    "selectable v2 profile requires exact runtime metadata"
                )

            image = raw["image"]
            if (
                not isinstance(image, str)
                or not image
                or any(char.isspace() for char in image)
            ):
                raise ValueError("profile image is invalid")
            if not settings.unsafe_local_runtime and not DIGEST_IMAGE_RE.fullmatch(
                image
            ):
                raise ValueError("production profile image must be digest pinned")
            cpu_limit = raw["cpu_limit"]
            if isinstance(cpu_limit, bool) or not isinstance(cpu_limit, (int, float)):
                raise ValueError("profile cpu_limit must be numeric")
            try:
                cpu_millicores = cpu_limit_to_millicores(cpu_limit)
            except ValueError as exc:
                raise ValueError(f"profile {exc}") from exc
            for field in (
                "memory_limit_bytes",
                "pids_limit",
                "private_disk_hard_limit_bytes",
                "writable_layer_size_bytes",
                "tmpfs_size_bytes",
                "shm_size_bytes",
                "log_max_size_bytes",
                "log_max_files",
                "uid",
                "gid",
            ):
                _positive_int(raw[field], field)
            memory_bytes = raw["memory_limit_bytes"]
            disk_bytes = raw["private_disk_hard_limit_bytes"]
            if memory_bytes % (1024 * 1024) or disk_bytes % (1024 * 1024):
                raise ValueError("profile memory/disk bytes must be whole MiB")
            raw["private_mount_path"] = _absolute_mount(
                raw["private_mount_path"], "profile private_mount_path"
            )
            if _mount_paths_overlap(raw["private_mount_path"], shared_mount_path):
                raise ValueError("private and shared mount paths overlap")
            if raw["private_mount_path"] != PRIVATE_MOUNT_PATH:
                raise ValueError(
                    f"profile private_mount_path must be {PRIVATE_MOUNT_PATH}"
                )
            memory_mb = memory_bytes // (1024 * 1024)
            if selectable and (
                cpu_millicores > settings.workspace_cpu_budget_millicores
                or memory_mb > settings.workspace_memory_budget_mb
            ):
                raise ValueError(
                    "selectable profile exceeds aggregate workspace resource budget"
                )

            if extended_profile:
                kernel_name, kernel_display_name, python_version = (
                    _validate_runtime_selection(raw)
                )
                quota_enforced = raw["private_disk_quota_enforced"]
                if type(quota_enforced) is not bool:
                    raise ValueError(
                        "profile private_disk_quota_enforced must be boolean"
                    )
            else:
                kernel_name = "python3"
                kernel_display_name = "Python 3"
                python_version = "legacy"
                # Legacy profile digests cannot acquire the new execution fact.
                # False is the conservative disclosure and legacy rows are not
                # selectable in a v2 policy.
                quota_enforced = False
            if (
                raw["enabled"]
                and not settings.unsafe_local_runtime
                and not extended_profile
            ):
                raise ValueError(
                    "production profile requires schema v2 exact runtime metadata"
                )
            configured_digest = raw["config_digest"]
            if not isinstance(configured_digest, str) or not SHA256_RE.fullmatch(
                configured_digest
            ):
                raise ValueError("profile config_digest is invalid")
            digest = _profile_digest(raw, execution_fields)
            if configured_digest != digest:
                raise ValueError(
                    f"profile digest mismatch for {raw.get('id')}@{raw.get('version')}"
                )
            disk_limits.add(disk_bytes)
            enabled_count += int(raw["enabled"])
            selectable_count += int(selectable)
            values = {
                "name": str(raw["id"]),
                "kernel_name": kernel_name,
                "kernel_display_name": kernel_display_name,
                "python_version": python_version,
                "image_ref": str(image),
                "cpu_limit": str(raw["cpu_limit"]),
                "memory_limit_mb": memory_mb,
                "pids_limit": int(raw["pids_limit"]),
                "private_disk_limit_mb": disk_bytes // (1024 * 1024),
                "private_disk_quota_enforced": quota_enforced,
                "idle_timeout_seconds": None,
                "provider_options_json": json.dumps(
                    raw, sort_keys=True, separators=(",", ":")
                ),
                "config_digest": digest,
                "enabled": bool(raw.get("enabled")),
                "selectable": selectable,
            }
            existing = db.get(WorkspaceProfile, key)
            if existing is None:
                db.add(WorkspaceProfile(id=key[0], version=key[1], **values))
            else:
                # config_digest already binds the canonical execution subset.
                # Keep the original JSON for legacy rows because a v2 document adds
                # only the mutable `selectable` metadata to that raw object.
                immutable = {
                    field: getattr(existing, field)
                    for field in values
                    if field not in {"enabled", "selectable", "provider_options_json"}
                }
                expected = {
                    field: value
                    for field, value in values.items()
                    if field not in {"enabled", "selectable", "provider_options_json"}
                }
                if immutable != expected:
                    raise ValueError(
                        f"refusing in-place profile mutation for {key[0]}@{key[1]}"
                    )
                existing.enabled = values["enabled"]
                existing.selectable = values["selectable"]
        if len(disk_limits) != 1:
            raise ValueError("all MVP profiles must have one private disk hard limit")
        if enabled_count == 0:
            raise ValueError("profile policy must enable at least one profile")
        if selectable_count == 0:
            raise ValueError("profile policy must expose at least one profile")
        db.flush()
        # Offer creation is an explicit bootstrap mutation. Read-only catalog
        # endpoints never create or reactivate offers.
        ensure_default_offers(db)
        resource_policy = get_resource_policy(db, settings, create=True)
        selected_cpu, selected_memory = selected_resource_values(resource_policy)
        ensure_resource_profile_matrix(
            db,
            cpu_millicores=sorted(selected_cpu),
            memory_mb=sorted(selected_memory),
        )
        ensure_default_offers(db)
        db.commit()
    engine.dispose()


def _validate_runtime_selection(profile: dict[str, Any]) -> tuple[str, str, str]:
    python_version = profile["python_version"]
    if not isinstance(python_version, str) or not PYTHON_VERSION_RE.fullmatch(
        python_version
    ):
        raise ValueError("profile python_version must be exact X.Y.Z")
    kernels = profile["kernels"]
    if not isinstance(kernels, list) or not kernels:
        raise ValueError("profile kernels must be a non-empty array")
    names: list[str] = []
    by_name: dict[str, dict[str, str]] = {}
    for kernel in kernels:
        if not isinstance(kernel, dict) or set(kernel) != {
            "name",
            "display_name",
            "language",
            "python_version",
            "executable",
        }:
            raise ValueError("profile kernel schema mismatch")
        name = kernel["name"]
        display_name = kernel["display_name"]
        language = kernel["language"]
        kernel_python_version = kernel["python_version"]
        executable = kernel["executable"]
        if not isinstance(name, str) or not KERNEL_NAME_RE.fullmatch(name):
            raise ValueError("profile kernel name is invalid")
        if (
            not isinstance(display_name, str)
            or not 1 <= len(display_name) <= 128
            or any(ord(char) < 32 or ord(char) == 127 for char in display_name)
        ):
            raise ValueError("profile kernel display name is invalid")
        if language != "python":
            raise ValueError("profile kernel language must be python")
        if not isinstance(
            kernel_python_version, str
        ) or not PYTHON_VERSION_RE.fullmatch(kernel_python_version):
            raise ValueError("profile kernel python_version must be exact X.Y.Z")
        if not isinstance(executable, str) or not PYTHON_EXECUTABLE_RE.fullmatch(
            executable
        ):
            raise ValueError("profile kernel executable is not allowlisted")
        names.append(name)
        by_name[name] = kernel
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("profile kernels must be unique and name-sorted")
    default_kernel = profile["default_kernel"]
    if not isinstance(default_kernel, str) or default_kernel not in by_name:
        raise ValueError("profile default_kernel is not allowlisted")
    if by_name[default_kernel]["python_version"] != python_version:
        raise ValueError("profile python_version must match the default kernel version")
    return (
        default_kernel,
        by_name[default_kernel]["display_name"],
        python_version,
    )


def list_users(settings: Settings) -> None:
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    with factory() as db:
        users = db.scalars(select(User).order_by(User.hub_username)).all()
        print(
            json.dumps(
                [
                    {
                        "id": user.id,
                        "username": user.hub_username,
                        "status": user.status,
                        "role": user.role,
                    }
                    for user in users
                ],
                indent=2,
                sort_keys=True,
            )
        )
    engine.dispose()


def provision_user(
    settings: Settings,
    *,
    username: str,
    manifest_path: str | None,
    local_project_id_base: int | None,
) -> None:
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    with factory() as db:
        begin_immediate(db)
        user = db.scalar(select(User).where(User.hub_username == username))
        if user is None:
            raise ValueError("user has not completed the first portal OAuth login")
        if manifest_path:
            manifest = _read_json(manifest_path)
        else:
            if not settings.unsafe_local_runtime:
                raise ValueError(
                    "production activation requires a root-provisioner manifest"
                )
            if local_project_id_base is None or local_project_id_base <= 0:
                raise ValueError("local dev requires a positive --project-id-base")
            enabled_profiles = db.scalars(
                select(WorkspaceProfile).where(WorkspaceProfile.enabled.is_(True))
            ).all()
            limits = {profile.private_disk_limit_mb for profile in enabled_profiles}
            identities = {
                (
                    int(json.loads(profile.provider_options_json)["uid"]),
                    int(json.loads(profile.provider_options_json)["gid"]),
                )
                for profile in enabled_profiles
            }
            if len(limits) != 1 or len(identities) != 1:
                raise ValueError(
                    "enabled profiles must share one disk limit and runtime identity"
                )
            hard_limit_mb = limits.pop()
            expected_uid, expected_gid = identities.pop()
            manifest = {
                "schema_version": 1,
                "unsafe_local_dev": True,
                "user_id": user.id,
                "username": user.hub_username,
                "uid": expected_uid,
                "gid": expected_gid,
                "slots": [
                    {
                        "slot_id": str(
                            uuid.uuid5(
                                uuid.UUID(user.id),
                                f"workspace-volume-slot-{number}",
                            )
                        ),
                        "slot_number": number,
                        "volume_name": (f"jupyter-user-{username}-slot-{number}"),
                        "project_id": local_project_id_base + number - 1,
                        "hard_limit_bytes": hard_limit_mb * 1024 * 1024,
                    }
                    for number in range(1, 6)
                ],
            }

        activate_user_from_manifest(
            db,
            settings=settings,
            user=user,
            manifest=manifest,
            # Preserve the legacy --project-id-base DB-only local path. A real
            # local host manifest uses deterministic synthetic DB sentinels.
            use_local_synthetic_project_ids=manifest_path is not None,
        )
        job = db.get(UserProvisioningJob, user.id)
        if job is not None:
            now = datetime.utcnow()
            job.status = UserProvisioningStatus.SUCCEEDED.value
            job.error_code = None
            job.error_summary = None
            job.completed_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
        db.commit()
    engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fail-closed platform bootstrap administration"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-users")
    profiles = sub.add_parser("import-profiles")
    profiles.add_argument("--policy", required=True)
    provision = sub.add_parser("provision-user")
    provision.add_argument("--username", required=True)
    source = provision.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument("--project-id-base", type=int)
    args = parser.parse_args()

    settings = Settings.from_env()
    settings.validate()
    if args.command == "list-users":
        list_users(settings)
    elif args.command == "import-profiles":
        import_profiles(settings, args.policy)
    elif args.command == "provision-user":
        provision_user(
            settings,
            username=args.username,
            manifest_path=args.manifest,
            local_project_id_base=args.project_id_base,
        )


if __name__ == "__main__":
    main()
