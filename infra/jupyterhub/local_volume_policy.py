"""Deterministic local-development volume allocation and manifest policy.

This module deliberately contains no Docker calls.  Both the host CLI and the
Hub-managed local provisioner use it so user identities, five-slot allocation,
labels, and persisted project-ID reservations cannot drift between entrypoints.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any


USERNAME_RE = re.compile(r"^(?!.*--)[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")
SLOT_COUNT = 5
DEFAULT_PROJECT_ID_START = 10_000
MAX_PROJECT_ID = 2**31 - 1
ALLOCATION_REGISTRY_NAME = ".local-project-id-allocations.json"
ALLOCATION_LOCK_NAME = ".local-project-id-allocations.lock"
STATE_DIRECTORY_MODE = 0o2770
STATE_FILE_MODE = 0o660


def validate_web_provisioning_mode(
    *,
    platform_env: str,
    unsafe_local_dev: bool,
    unsafe_domain_test: bool = False,
    enabled: bool,
) -> None:
    explicitly_local = (
        platform_env == "local-dev"
        and unsafe_local_dev is True
        and unsafe_domain_test is False
    ) or (
        platform_env == "domain-test"
        and unsafe_local_dev is False
        and unsafe_domain_test is True
    )
    if enabled and not explicitly_local:
        raise RuntimeError(
            "PLATFORM_WEB_PROVISIONING_ENABLED is forbidden outside an explicit local test mode"
        )


def validate_workspace_deletion_mode(
    *, enabled: bool, storage_policy_mode: str
) -> None:
    """Allow the wipe agent only for the verified unlimited local-volume mode.

    The legacy XFS mode requires project-quota-specific recreation that this
    Docker named-volume agent cannot prove. Production is otherwise permitted:
    user signup provisioning remains a separate, local-test-only capability.
    """

    if enabled and storage_policy_mode != "docker-volume-unlimited-v1":
        raise RuntimeError(
            "workspace deletion requires docker-volume-unlimited-v1 storage policy"
        )


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Durably replace one private JSON file without following a temp symlink."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, STATE_FILE_MODE)
        payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def canonical_user_id(value: object, *, where: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{where} user_id is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise RuntimeError(f"{where} user_id is invalid") from None
    if str(parsed) != value:
        raise RuntimeError(f"{where} user_id must be a canonical lowercase UUID")
    return str(parsed)


def validate_username(value: object, *, where: str = "requested") -> str:
    if not isinstance(value, str) or not USERNAME_RE.fullmatch(value):
        raise RuntimeError(f"{where} username is invalid")
    return value


def _validate_project_id_base(value: object, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{where} project ID is invalid")
    if value <= 0 or value + SLOT_COUNT - 1 > MAX_PROJECT_ID:
        raise RuntimeError(f"{where} project ID block is outside the supported range")
    return value


def validate_local_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"cannot read existing local manifest {path}: {exc}"
        ) from exc
    return validate_local_manifest_value(manifest, where=str(path))


def validate_local_manifest_value(
    manifest: object, *, where: str = "local manifest"
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise RuntimeError(f"{where} is not an object")
    if set(manifest) != {
        "schema_version",
        "unsafe_local_dev",
        "user_id",
        "username",
        "uid",
        "gid",
        "slots",
    }:
        raise RuntimeError(f"{where} schema is invalid")
    if manifest["schema_version"] != 1 or manifest["unsafe_local_dev"] is not True:
        raise RuntimeError(f"{where} has an invalid schema version/mode")
    user_id = canonical_user_id(manifest["user_id"], where=where)
    username = validate_username(manifest["username"], where=where)
    for field in ("uid", "gid"):
        value = manifest[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"{where} has an invalid {field}")
    slots = manifest["slots"]
    if not isinstance(slots, list) or len(slots) != SLOT_COUNT:
        raise RuntimeError(f"{where} must contain five slots")
    ordered: dict[int, dict[str, Any]] = {}
    for slot in slots:
        if not isinstance(slot, dict) or set(slot) != {
            "slot_id",
            "slot_number",
            "volume_name",
            "hard_limit_bytes",
            "project_id",
        }:
            raise RuntimeError(f"{where} has an invalid slot schema")
        number = slot["slot_number"]
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number not in range(1, SLOT_COUNT + 1)
            or number in ordered
        ):
            raise RuntimeError(f"{where} has an invalid or duplicate slot number")
        expected_slot_id = str(
            uuid.uuid5(uuid.UUID(user_id), f"workspace-volume-slot-{number}")
        )
        if slot["slot_id"] != expected_slot_id:
            raise RuntimeError(f"{where} has an invalid slot ID")
        if slot["volume_name"] != f"jupyter-user-{username}-slot-{number}":
            raise RuntimeError(f"{where} has an invalid volume name")
        for field in ("hard_limit_bytes", "project_id"):
            value = slot[field]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RuntimeError(f"{where} has an invalid slot {field}")
        ordered[number] = slot
    project_ids = [ordered[number]["project_id"] for number in range(1, 6)]
    project_id_base = _validate_project_id_base(project_ids[0], where=where)
    if project_ids != list(range(project_id_base, project_id_base + SLOT_COUNT)):
        raise RuntimeError(f"{where} project IDs are not one contiguous block")
    hard_limits = {ordered[number]["hard_limit_bytes"] for number in range(1, 6)}
    if len(hard_limits) != 1:
        raise RuntimeError(f"{where} slot hard limits differ")
    return {
        "user_id": user_id,
        "username": username,
        "project_id_base": project_id_base,
    }


def _load_allocation_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"cannot read local project ID registry {path}: {exc}"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "allocations"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("allocations"), list)
    ):
        raise RuntimeError("local project ID allocation registry schema is invalid")
    allocations: list[dict[str, Any]] = []
    for raw in value["allocations"]:
        if not isinstance(raw, dict) or set(raw) != {
            "user_id",
            "username",
            "project_id_base",
        }:
            raise RuntimeError("local project ID allocation registry entry is invalid")
        candidate = {
            "user_id": canonical_user_id(raw["user_id"], where="allocation registry"),
            "username": validate_username(raw["username"], where="allocation registry"),
            "project_id_base": _validate_project_id_base(
                raw["project_id_base"], where="allocation registry"
            ),
        }
        before = len(allocations)
        _merge_allocation(allocations, candidate)
        if len(allocations) == before:
            raise RuntimeError("local project ID allocation registry has duplicates")
    return allocations


def _merge_allocation(
    allocations: list[dict[str, Any]], candidate: dict[str, Any]
) -> None:
    candidate_ids = set(
        range(candidate["project_id_base"], candidate["project_id_base"] + SLOT_COUNT)
    )
    for existing in allocations:
        if existing["user_id"] == candidate["user_id"]:
            if existing != candidate:
                raise RuntimeError(
                    "existing local project ID allocation conflicts with user identity"
                )
            return
        if existing["username"] == candidate["username"]:
            raise RuntimeError(
                "Hub username is already allocated to another platform user"
            )
        existing_ids = set(
            range(existing["project_id_base"], existing["project_id_base"] + SLOT_COUNT)
        )
        if candidate_ids & existing_ids:
            raise RuntimeError("local project ID allocation blocks overlap")
    allocations.append(candidate)


def reserve_project_id_block(
    *,
    output: Path,
    user_id: str,
    username: str,
    project_id_start: int = DEFAULT_PROJECT_ID_START,
) -> int:
    """Atomically reserve and return a stable five-ID block for one local user."""

    canonical_id = canonical_user_id(user_id, where="requested")
    safe_username = validate_username(username)
    project_id_start = _validate_project_id_base(
        project_id_start, where="project-id-start"
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=STATE_DIRECTORY_MODE)
    directory_stat = output.parent.stat()
    directory_mode = directory_stat.st_mode
    if (
        directory_mode & 0o007 or directory_mode & 0o2070 != 0o2070
    ) and directory_stat.st_uid == os.geteuid():
        os.chmod(output.parent, STATE_DIRECTORY_MODE)
        directory_mode = output.parent.stat().st_mode
    if (
        not stat.S_ISDIR(directory_mode)
        or directory_mode & 0o007
        or directory_mode & 0o2070 != 0o2070
    ):
        raise RuntimeError(
            "local provisioning state directory must be setgid/group-rwx and private"
        )
    lock_path = output.parent / ALLOCATION_LOCK_NAME
    lock_path.touch(mode=STATE_FILE_MODE, exist_ok=True)
    lock_stat = lock_path.stat()
    if stat.S_IMODE(lock_stat.st_mode) != STATE_FILE_MODE:
        if lock_stat.st_uid != os.geteuid():
            raise RuntimeError("local provisioning allocator lock mode is unsafe")
        os.chmod(lock_path, STATE_FILE_MODE)
    registry_path = output.parent / ALLOCATION_REGISTRY_NAME
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        allocations = _load_allocation_registry(registry_path)

        manifest_paths = set(output.parent.glob("local-user-*.json"))
        if output.exists():
            manifest_paths.add(output)
        for manifest_path in sorted(manifest_paths):
            allocation = validate_local_manifest(manifest_path)
            if manifest_path == output and (
                allocation["user_id"] != canonical_id
                or allocation["username"] != safe_username
            ):
                raise RuntimeError(
                    "existing output manifest conflicts with requested user identity"
                )
            match = re.fullmatch(r"local-user-([0-9a-f-]+)\.json", manifest_path.name)
            if match and match.group(1) != allocation["user_id"]:
                raise RuntimeError(
                    f"existing local manifest {manifest_path} filename/user mismatch"
                )
            _merge_allocation(allocations, allocation)

        requested = next(
            (item for item in allocations if item["user_id"] == canonical_id), None
        )
        if requested is not None:
            if requested["username"] != safe_username:
                raise RuntimeError(
                    "existing local project ID allocation conflicts with requested username"
                )
            project_id_base = requested["project_id_base"]
        else:
            if any(item["username"] == safe_username for item in allocations):
                raise RuntimeError(
                    "Hub username is already allocated to another platform user"
                )
            used_ids = {
                project_id
                for item in allocations
                for project_id in range(
                    item["project_id_base"], item["project_id_base"] + SLOT_COUNT
                )
            }
            project_id_base = project_id_start
            while set(range(project_id_base, project_id_base + SLOT_COUNT)) & used_ids:
                project_id_base += SLOT_COUNT
                _validate_project_id_base(project_id_base, where="allocated")
            allocations.append(
                {
                    "user_id": canonical_id,
                    "username": safe_username,
                    "project_id_base": project_id_base,
                }
            )

        allocations.sort(key=lambda item: item["user_id"])
        atomic_json(
            registry_path,
            {"schema_version": 1, "allocations": allocations},
        )
        return project_id_base


def local_profile_runtime(policy: dict[str, Any]) -> dict[str, Any]:
    enabled = [
        profile for profile in policy["profiles"].values() if profile["enabled"] is True
    ]
    if not enabled:
        raise RuntimeError("local profile policy has no enabled profile")
    disk_limits = {profile["private_disk_hard_limit_bytes"] for profile in enabled}
    if len(disk_limits) != 1:
        raise RuntimeError("local profiles do not share one disk-limit label")
    identities = {(profile["uid"], profile["gid"]) for profile in enabled}
    if len(identities) != 1:
        raise RuntimeError("local profiles must share one runtime UID/GID")
    uid, gid = identities.pop()
    return {
        "hard_limit_bytes": disk_limits.pop(),
        "uid": uid,
        "gid": gid,
        "initializer_image": enabled[0]["image"],
    }


def build_local_manifest(
    *,
    user_id: str,
    username: str,
    uid: int,
    gid: int,
    hard_limit_bytes: int,
    project_id_base: int,
) -> dict[str, Any]:
    canonical_id = canonical_user_id(user_id, where="requested")
    safe_username = validate_username(username)
    _validate_project_id_base(project_id_base, where="allocated")
    for field, value in (
        ("uid", uid),
        ("gid", gid),
        ("hard_limit_bytes", hard_limit_bytes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"{field} is invalid")
    owner_uuid = uuid.UUID(canonical_id)
    manifest = {
        "schema_version": 1,
        "unsafe_local_dev": True,
        "user_id": canonical_id,
        "username": safe_username,
        "uid": uid,
        "gid": gid,
        "slots": [
            {
                "slot_id": str(
                    uuid.uuid5(owner_uuid, f"workspace-volume-slot-{number}")
                ),
                "slot_number": number,
                "volume_name": f"jupyter-user-{safe_username}-slot-{number}",
                "hard_limit_bytes": hard_limit_bytes,
                "project_id": project_id_base + number - 1,
            }
            for number in range(1, SLOT_COUNT + 1)
        ],
    }
    validate_local_manifest_value(manifest)
    return manifest


def shared_volume_labels() -> dict[str, str]:
    return {
        "platform.managed": "true",
        "platform.provisioned": "true",
        "platform.shared": "true",
        "platform.quota.enforced": "false",
    }


def private_volume_labels(
    *, user_id: str, username: str, slot: dict[str, Any]
) -> dict[str, str]:
    return {
        "platform.managed": "true",
        "platform.provisioned": "true",
        "platform.owner.user_id": user_id,
        "platform.owner.username": username,
        "platform.volume.slot": str(slot["slot_number"]),
        "platform.volume.slot_id": str(slot["slot_id"]),
        "platform.quota.hard_bytes": str(slot["hard_limit_bytes"]),
        "platform.quota.enforced": "false",
        "platform.quota.project_id": str(slot["project_id"]),
    }
