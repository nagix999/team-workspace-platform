"""Load and validate the immutable local execution-profile allowlist.

This module intentionally has no JupyterHub dependency so the same digest logic
can be used by CI and by the control plane when profile rows are seeded.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any


class ProfilePolicyError(ValueError):
    """The local execution profile policy is invalid."""


TOP_LEVEL_KEYS = {"schema_version", "shared_volume", "profiles"}
SHARED_VOLUME_KEYS = {"name", "mount_path", "gid"}
LEGACY_PROFILE_KEYS = {
    "id",
    "version",
    "enabled",
    "config_digest",
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
PROFILE_METADATA_KEYS = {"enabled", "selectable", "config_digest"}
V2_RUNTIME_KEYS = {
    "python_version",
    "kernels",
    "default_kernel",
    "private_disk_quota_enforced",
}
V2_LEGACY_PROFILE_KEYS = LEGACY_PROFILE_KEYS | {"selectable"}
V2_PROFILE_KEYS = V2_LEGACY_PROFILE_KEYS | V2_RUNTIME_KEYS
ACCELERATOR_KEYS = {
    "kind",
    "count",
    "sharing",
    "cuda_version",
    "framework",
    "framework_version",
}
MAX_NVIDIA_GPU_COUNT = 64
V3_PROFILE_KEYS = V2_PROFILE_KEYS | {"accelerator"}
LEGACY_EXECUTION_FIELDS = tuple(
    sorted(LEGACY_PROFILE_KEYS - {"enabled", "config_digest"})
)
V2_EXECUTION_FIELDS = tuple(sorted(V2_PROFILE_KEYS - PROFILE_METADATA_KEYS))
V3_EXECUTION_FIELDS = tuple(sorted(V3_PROFILE_KEYS - PROFILE_METADATA_KEYS))
PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
VOLUME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
IMMUTABLE_IMAGE_RE = re.compile(r"^(?:\S+@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PYTHON_VERSION_RE = re.compile(
    r"^(?:[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
CUDA_VERSION_RE = re.compile(r"^(?:[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
KERNEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PYTHON_EXECUTABLE_RE = re.compile(
    r"^/opt/conda(?:/envs/[a-z][a-z0-9_-]{0,63})?/bin/python$"
)
PRIVATE_MOUNT_PATH = "/home/jovyan/work"
SHARED_MOUNT_PATH = "/home/jovyan/shared"


def _exact_keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = set(value) - allowed
    missing = allowed - set(value)
    if extra or missing:
        raise ProfilePolicyError(
            f"{where} keys mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
        )


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProfilePolicyError(f"{where} must be a positive integer")
    return value


def _absolute_mount(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ProfilePolicyError(f"{where} must be an absolute POSIX path")
    path = PurePosixPath(value)
    if ".." in path.parts or str(path) in {"/", "/home", "/home/jovyan"}:
        raise ProfilePolicyError(f"{where} is too broad or contains traversal")
    return str(path)


def _mount_paths_overlap(left: str, right: str) -> bool:
    left_path = PurePosixPath(left)
    right_path = PurePosixPath(right)
    return (
        left_path == right_path
        or left_path in right_path.parents
        or right_path in left_path.parents
    )


def canonical_profile(profile: dict[str, Any]) -> bytes:
    """Return the canonical bytes hashed into config_digest."""

    keys = set(profile)
    if keys == LEGACY_PROFILE_KEYS or keys == V2_LEGACY_PROFILE_KEYS:
        fields = LEGACY_EXECUTION_FIELDS
    elif keys == V2_PROFILE_KEYS:
        fields = V2_EXECUTION_FIELDS
    elif keys == V3_PROFILE_KEYS:
        fields = V3_EXECUTION_FIELDS
    else:
        raise ProfilePolicyError("profile keys do not match a supported digest schema")
    document = {field: profile[field] for field in fields}
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def profile_digest(profile: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_profile(profile)).hexdigest()


def resource_runtime_signature(profile: dict[str, Any]) -> str:
    """Identify immutable runtime facts while excluding CPU and memory."""

    if frozenset(profile) not in {
        frozenset(V2_PROFILE_KEYS),
        frozenset(V3_PROFILE_KEYS),
    }:
        raise ProfilePolicyError("resource base must be a managed runtime profile")
    excluded = {
        "id",
        "version",
        "enabled",
        "selectable",
        "config_digest",
        "cpu_limit",
        "memory_limit_bytes",
    }
    canonical = json.dumps(
        {key: profile[key] for key in sorted(set(profile) - excluded)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def derived_resource_profile_id(
    profile: dict[str, Any], *, cpu_millicores: int, memory_mb: int
) -> str:
    if type(cpu_millicores) is not int or cpu_millicores <= 0:
        raise ProfilePolicyError("derived CPU must be positive whole millicores")
    if type(memory_mb) is not int or memory_mb <= 0:
        raise ProfilePolicyError("derived memory must be positive whole MiB")
    kernel = re.sub(r"[^a-z0-9-]+", "-", profile["default_kernel"].lower())
    kernel = kernel.strip("-")[:16]
    profile_id = (
        f"runtime-{kernel}-{resource_runtime_signature(profile)[:8]}-"
        f"c{cpu_millicores}-m{memory_mb}"
    )
    if not kernel or not PROFILE_ID_RE.fullmatch(profile_id):
        raise ProfilePolicyError("derived profile identity is invalid")
    return profile_id


def derive_resource_profile(
    base: dict[str, Any],
    *,
    cpu_millicores: int,
    memory_mb: int,
    gpu_count: int | None = None,
) -> dict[str, Any]:
    profile = dict(base)
    accelerator = accelerator_contract(base)
    if gpu_count is not None:
        if type(gpu_count) is not int:
            raise ProfilePolicyError("derived GPU count must be an integer")
        if accelerator is None or accelerator["kind"] == "none":
            expected_gpu_count = 0
        elif accelerator["kind"] == "nvidia":
            expected_gpu_count = accelerator["count"]
        else:  # pragma: no cover - loaded policies reject this first
            raise ProfilePolicyError("derived accelerator contract is unsupported")
        # GPU count is part of the immutable runtime identity. The production
        # generator emits one reviewed base per count; authorization may select
        # a base, but must never synthesize a different accelerator contract.
        if gpu_count != expected_gpu_count:
            raise ProfilePolicyError(
                "derived GPU count does not match the immutable runtime base"
            )
    profile["id"] = derived_resource_profile_id(
        profile, cpu_millicores=cpu_millicores, memory_mb=memory_mb
    )
    profile["version"] = 1
    profile["enabled"] = True
    profile["selectable"] = True
    profile["cpu_limit"] = (
        cpu_millicores // 1000 if cpu_millicores % 1000 == 0 else cpu_millicores / 1000
    )
    profile["memory_limit_bytes"] = memory_mb * 1024 * 1024
    profile["config_digest"] = profile_digest(profile)
    return profile


def is_managed_runtime_profile(profile: dict[str, Any]) -> bool:
    """Return whether a profile declares the complete managed-kernel contract."""

    return V2_RUNTIME_KEYS.issubset(profile)


def accelerator_contract(profile: dict[str, Any]) -> dict[str, Any] | None:
    """Return the explicit v3 accelerator contract, if the profile has one.

    Schema-v2 profiles predate accelerator support and are always CPU-only.
    Keeping that fact implicit preserves their historical digest so stopped
    workspaces can still restart after a schema-v3 rollout.
    """

    accelerator = profile.get("accelerator")
    if accelerator is None:
        return None
    if not isinstance(accelerator, dict) or set(accelerator) != ACCELERATOR_KEYS:
        raise ProfilePolicyError("profile accelerator contract is invalid")
    return accelerator


def kernel_runtime_environment(profile: dict[str, Any]) -> dict[str, str]:
    """Build the exact environment consumed by the single-user image wrapper.

    The policy loader validates every value before this helper is called. Keeping
    this serialization beside the policy schema prevents deployment preflight and
    the live DockerSpawner path from constructing subtly different contracts.
    """

    if not is_managed_runtime_profile(profile):
        raise ProfilePolicyError("profile does not declare a managed runtime")
    default = next(
        kernel
        for kernel in profile["kernels"]
        if kernel["name"] == profile["default_kernel"]
    )
    executable = default["executable"]
    python_bin = str(PurePosixPath(executable).parent)
    contract = {
        "schema_version": 1,
        "python_version": profile["python_version"],
        "kernels": profile["kernels"],
        "default_kernel": profile["default_kernel"],
    }
    environment = {
        "PLATFORM_DEFAULT_KERNEL": profile["default_kernel"],
        "PLATFORM_PYTHON_EXECUTABLE": executable,
        "PATH": (
            f"{python_bin}:/opt/conda/bin:/usr/local/sbin:/usr/local/bin:"
            "/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "PLATFORM_KERNEL_CONTRACT": json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ),
    }
    accelerator = accelerator_contract(profile)
    if accelerator is not None and accelerator["kind"] == "nvidia":
        environment.update(
            {
                "PLATFORM_ACCELERATOR_CONTRACT": json.dumps(
                    {"schema_version": 1, **accelerator},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
            }
        )
    return environment


def _validate_v2_runtime(profile: dict[str, Any], where: str) -> None:
    if not isinstance(profile["private_disk_quota_enforced"], bool):
        raise ProfilePolicyError(f"{where}.private_disk_quota_enforced must be boolean")
    version = profile["python_version"]
    if not isinstance(version, str) or not PYTHON_VERSION_RE.fullmatch(version):
        raise ProfilePolicyError(f"{where}.python_version must be exact X.Y.Z")

    kernels = profile["kernels"]
    if not isinstance(kernels, list) or not kernels:
        raise ProfilePolicyError(f"{where}.kernels must be a non-empty array")
    normalized: list[str] = []
    for kernel_index, kernel in enumerate(kernels):
        kernel_where = f"{where}.kernels[{kernel_index}]"
        if not isinstance(kernel, dict):
            raise ProfilePolicyError(f"{kernel_where} must be an object")
        _exact_keys(
            kernel,
            {
                "name",
                "display_name",
                "language",
                "python_version",
                "executable",
            },
            kernel_where,
        )
        name = kernel["name"]
        if not isinstance(name, str) or not KERNEL_NAME_RE.fullmatch(name):
            raise ProfilePolicyError(f"{kernel_where}.name is invalid")
        display_name = kernel["display_name"]
        if (
            not isinstance(display_name, str)
            or not 1 <= len(display_name) <= 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in display_name
            )
        ):
            raise ProfilePolicyError(f"{kernel_where}.display_name is invalid")
        if kernel["language"] != "python":
            raise ProfilePolicyError(f"{kernel_where}.language must be python")
        kernel_version = kernel["python_version"]
        if not isinstance(kernel_version, str) or not PYTHON_VERSION_RE.fullmatch(
            kernel_version
        ):
            raise ProfilePolicyError(
                f"{kernel_where}.python_version must be exact X.Y.Z"
            )
        executable = kernel["executable"]
        if not isinstance(executable, str) or not PYTHON_EXECUTABLE_RE.fullmatch(
            executable
        ):
            raise ProfilePolicyError(f"{kernel_where}.executable is not allowlisted")
        normalized.append(name)
    if normalized != sorted(set(normalized)):
        raise ProfilePolicyError(f"{where}.kernels must be unique and sorted by name")
    default_kernel = profile["default_kernel"]
    if not isinstance(default_kernel, str) or default_kernel not in normalized:
        raise ProfilePolicyError(f"{where}.default_kernel is not declared in kernels")
    default = next(kernel for kernel in kernels if kernel["name"] == default_kernel)
    if default["python_version"] != version:
        raise ProfilePolicyError(
            f"{where}.python_version must match the default kernel version"
        )


def _validate_v3_accelerator(profile: dict[str, Any], where: str) -> None:
    accelerator = profile["accelerator"]
    if not isinstance(accelerator, dict):
        raise ProfilePolicyError(f"{where}.accelerator must be an object")
    _exact_keys(accelerator, ACCELERATOR_KEYS, f"{where}.accelerator")

    kind = accelerator["kind"]
    count = accelerator["count"]
    sharing = accelerator["sharing"]
    cuda_version = accelerator["cuda_version"]
    framework = accelerator["framework"]
    framework_version = accelerator["framework_version"]
    if type(count) is not int:
        raise ProfilePolicyError(f"{where}.accelerator.count must be an integer")

    if kind == "none":
        if (
            count != 0
            or sharing != "none"
            or cuda_version is not None
            or framework is not None
            or framework_version is not None
        ):
            raise ProfilePolicyError(
                f"{where}.accelerator CPU contract must use zero/null values"
            )
        return

    if kind != "nvidia":
        raise ProfilePolicyError(f"{where}.accelerator.kind is unsupported")
    if not 1 <= count <= MAX_NVIDIA_GPU_COUNT or sharing != "exclusive":
        raise ProfilePolicyError(
            f"{where}.accelerator NVIDIA contract must request 1-64 exclusive GPUs"
        )
    if not isinstance(cuda_version, str) or not CUDA_VERSION_RE.fullmatch(cuda_version):
        raise ProfilePolicyError(f"{where}.accelerator.cuda_version must be exact X.Y")
    if framework != "pytorch":
        raise ProfilePolicyError(f"{where}.accelerator.framework must be pytorch")
    if not isinstance(framework_version, str) or not PYTHON_VERSION_RE.fullmatch(
        framework_version
    ):
        raise ProfilePolicyError(
            f"{where}.accelerator.framework_version must be exact X.Y.Z"
        )
    if len(profile["kernels"]) != 1:
        raise ProfilePolicyError(
            f"{where} NVIDIA profile must expose exactly one verified kernel"
        )


def load_profile_policy(
    path: str | Path, *, allow_unsafe_images: bool = False
) -> dict[str, Any]:
    policy_path = Path(path)
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfilePolicyError(
            f"cannot read profile policy {policy_path}: {exc}"
        ) from exc

    if not isinstance(policy, dict):
        raise ProfilePolicyError("profile policy must be a JSON object")
    _exact_keys(policy, TOP_LEVEL_KEYS, "profile policy")
    schema_version = policy["schema_version"]
    if type(schema_version) is not int or schema_version not in {1, 2, 3}:
        raise ProfilePolicyError("profile policy schema_version must be 1, 2 or 3")

    shared = policy["shared_volume"]
    if not isinstance(shared, dict):
        raise ProfilePolicyError("shared_volume must be an object")
    _exact_keys(shared, SHARED_VOLUME_KEYS, "shared_volume")
    if not VOLUME_RE.fullmatch(str(shared["name"])):
        raise ProfilePolicyError("shared_volume.name is not a safe Docker volume name")
    shared["mount_path"] = _absolute_mount(
        shared["mount_path"], "shared_volume.mount_path"
    )
    if shared["mount_path"] != SHARED_MOUNT_PATH:
        raise ProfilePolicyError(
            f"shared_volume.mount_path must be {SHARED_MOUNT_PATH}"
        )
    _positive_int(shared["gid"], "shared_volume.gid")

    profiles = policy["profiles"]
    if not isinstance(profiles, list) or not profiles:
        raise ProfilePolicyError("profiles must be a non-empty array")

    by_key: dict[tuple[str, int], dict[str, Any]] = {}
    enabled_count = 0
    selectable_count = 0
    disk_limits: set[int] = set()
    for index, profile in enumerate(profiles):
        where = f"profiles[{index}]"
        if not isinstance(profile, dict):
            raise ProfilePolicyError(f"{where} must be an object")
        profile_keys = set(profile)
        if schema_version == 1:
            _exact_keys(profile, LEGACY_PROFILE_KEYS, where)
            is_extended = False
            has_accelerator = False
        elif profile_keys == V2_LEGACY_PROFILE_KEYS:
            is_extended = False
            has_accelerator = False
        elif profile_keys == V2_PROFILE_KEYS:
            is_extended = True
            has_accelerator = False
        elif schema_version == 3 and profile_keys == V3_PROFILE_KEYS:
            is_extended = True
            has_accelerator = True
        else:
            raise ProfilePolicyError(
                f"{where} keys do not match the policy profile schema"
            )
        if not PROFILE_ID_RE.fullmatch(str(profile["id"])):
            raise ProfilePolicyError(f"{where}.id is invalid")
        version = _positive_int(profile["version"], f"{where}.version")
        if not isinstance(profile["enabled"], bool):
            raise ProfilePolicyError(f"{where}.enabled must be boolean")
        if schema_version in {2, 3}:
            if not isinstance(profile["selectable"], bool):
                raise ProfilePolicyError(f"{where}.selectable must be boolean")
            if profile["selectable"] and not profile["enabled"]:
                raise ProfilePolicyError(f"{where} selectable profile must be enabled")
            if profile["selectable"] and not is_extended:
                raise ProfilePolicyError(
                    f"{where} legacy profile may remain enabled but cannot be selectable"
                )
            if schema_version == 3 and profile["selectable"] and not has_accelerator:
                raise ProfilePolicyError(
                    f"{where} selectable schema-v3 profile requires accelerator metadata"
                )
        if is_extended:
            _validate_v2_runtime(profile, where)
        if has_accelerator:
            _validate_v3_accelerator(profile, where)
        if profile["enabled"] and not allow_unsafe_images and not is_extended:
            raise ProfilePolicyError(
                f"{where} production profile requires schema v2 exact runtime metadata"
            )
        cpu_limit = profile["cpu_limit"]
        cpu_decimal = (
            Decimal(str(cpu_limit))
            if isinstance(cpu_limit, (int, float)) and not isinstance(cpu_limit, bool)
            else Decimal(0)
        )
        millicores = cpu_decimal * 1000
        if (
            not isinstance(cpu_limit, (int, float))
            or isinstance(cpu_limit, bool)
            or not cpu_decimal.is_finite()
            or cpu_decimal <= 0
            or millicores != millicores.to_integral_value()
        ):
            raise ProfilePolicyError(
                f"{where}.cpu_limit must be finite positive whole millicores"
            )
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
            _positive_int(profile[field], f"{where}.{field}")
        profile["private_mount_path"] = _absolute_mount(
            profile["private_mount_path"], f"{where}.private_mount_path"
        )
        if _mount_paths_overlap(profile["private_mount_path"], shared["mount_path"]):
            raise ProfilePolicyError(f"{where} private/shared mount paths overlap")
        if profile["private_mount_path"] != PRIVATE_MOUNT_PATH:
            raise ProfilePolicyError(
                f"{where}.private_mount_path must be {PRIVATE_MOUNT_PATH}"
            )

        image = profile["image"]
        if not isinstance(image, str) or not image or any(ch.isspace() for ch in image):
            raise ProfilePolicyError(f"{where}.image is invalid")
        if not allow_unsafe_images and not IMMUTABLE_IMAGE_RE.fullmatch(image):
            raise ProfilePolicyError(
                f"{where}.image must be a registry digest or exact local image ID"
            )

        digest = profile["config_digest"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ProfilePolicyError(
                f"{where}.config_digest must be sha256:<64 lowercase hex>"
            )
        computed = profile_digest(profile)
        if digest != computed:
            raise ProfilePolicyError(
                f"{where}.config_digest mismatch: configured={digest}, computed={computed}"
            )

        key = (str(profile["id"]), version)
        if key in by_key:
            raise ProfilePolicyError(f"duplicate profile key {key}")
        by_key[key] = profile
        disk_limits.add(profile["private_disk_hard_limit_bytes"])
        enabled_count += int(profile["enabled"])
        selectable_count += int(profile.get("selectable", profile["enabled"]))

    if enabled_count == 0:
        raise ProfilePolicyError("at least one profile must be enabled")
    if selectable_count == 0:
        raise ProfilePolicyError("at least one profile must be selectable")
    if len(disk_limits) != 1:
        raise ProfilePolicyError(
            "all MVP profiles must use the same private disk hard limit"
        )

    return {
        "schema_version": schema_version,
        "shared_volume": shared,
        "profiles": by_key,
    }
