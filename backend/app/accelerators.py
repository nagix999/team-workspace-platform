from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


NVIDIA_GPU_DEVICE_ID_RE = re.compile(
    r"^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
CUDA_VERSION_RE = re.compile(r"^(?:[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
FRAMEWORK_VERSION_RE = re.compile(
    r"^(?:[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
ACCELERATOR_KEYS = {
    "kind",
    "count",
    "sharing",
    "cuda_version",
    "framework",
    "framework_version",
}
MAX_NVIDIA_GPU_COUNT = 64


@dataclass(frozen=True)
class AcceleratorSpec:
    kind: str
    count: int
    sharing: str
    cuda_version: str | None
    framework: str | None
    framework_version: str | None

    @property
    def is_gpu(self) -> bool:
        return self.kind == "nvidia"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "count": self.count,
            "sharing": self.sharing,
            "cuda_version": self.cuda_version,
            "framework": self.framework,
            "framework_version": self.framework_version,
        }


CPU_ACCELERATOR = AcceleratorSpec(
    kind="none",
    count=0,
    sharing="none",
    cuda_version=None,
    framework=None,
    framework_version=None,
)


def validate_accelerator(value: Any) -> AcceleratorSpec:
    if not isinstance(value, dict) or set(value) != ACCELERATOR_KEYS:
        raise ValueError("accelerator schema is invalid")
    spec = AcceleratorSpec(
        kind=value.get("kind"),
        count=value.get("count"),
        sharing=value.get("sharing"),
        cuda_version=value.get("cuda_version"),
        framework=value.get("framework"),
        framework_version=value.get("framework_version"),
    )
    if spec == CPU_ACCELERATOR:
        return spec
    if (
        spec.kind != "nvidia"
        or type(spec.count) is not int
        or not 1 <= spec.count <= MAX_NVIDIA_GPU_COUNT
        or spec.sharing != "exclusive"
        or not isinstance(spec.cuda_version, str)
        or not CUDA_VERSION_RE.fullmatch(spec.cuda_version)
        or spec.framework != "pytorch"
        or not isinstance(spec.framework_version, str)
        or not FRAMEWORK_VERSION_RE.fullmatch(spec.framework_version)
    ):
        raise ValueError(
            "one or more exclusive NVIDIA GPUs with pinned PyTorch are required"
        )
    return spec


def profile_accelerator_options(options: dict[str, Any]) -> AcceleratorSpec:
    value = options.get("accelerator")
    # Schema v2 profiles predate accelerators and remain immutable CPU history.
    return CPU_ACCELERATOR if value is None else validate_accelerator(value)


def stored_profile_accelerator(profile: Any) -> AcceleratorSpec:
    spec = AcceleratorSpec(
        kind=profile.accelerator_kind,
        count=profile.gpu_count,
        sharing="exclusive" if profile.accelerator_kind == "nvidia" else "none",
        cuda_version=profile.cuda_version,
        framework=profile.gpu_framework,
        framework_version=profile.gpu_framework_version,
    )
    return validate_accelerator(spec.as_dict())


def configured_gpu_device_ids(raw: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        return ()
    if (
        len(values) > MAX_NVIDIA_GPU_COUNT
        or any(not NVIDIA_GPU_DEVICE_ID_RE.fullmatch(value) for value in values)
        or values != tuple(sorted(set(values)))
    ):
        raise RuntimeError(
            "PLATFORM_NVIDIA_GPU_DEVICE_IDS must contain canonical, unique, "
            "lexicographically sorted physical GPU UUIDs"
        )
    return values


def gpu_device_ids_json(device_ids: tuple[str, ...]) -> str:
    """Serialize an exact, canonical physical-GPU assignment."""

    if not device_ids or configured_gpu_device_ids(",".join(device_ids)) != device_ids:
        raise ValueError("GPU device assignment is not canonical")
    return json.dumps(list(device_ids), separators=(",", ":"), ensure_ascii=True)


def parse_gpu_device_ids_json(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("GPU device assignment JSON is invalid") from exc
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError("GPU device assignment JSON is invalid")
    device_ids = tuple(value)
    try:
        validated = configured_gpu_device_ids(",".join(device_ids))
    except RuntimeError as exc:
        raise ValueError("GPU device assignment JSON is invalid") from exc
    if validated != device_ids:
        raise ValueError("GPU device assignment JSON is invalid")
    return device_ids


def gpu_inventory_digest(device_ids: tuple[str, ...]) -> str:
    if not device_ids:
        raise ValueError("GPU inventory cannot be empty")
    try:
        canonical_ids = configured_gpu_device_ids(",".join(device_ids))
    except RuntimeError as exc:
        raise ValueError("GPU inventory is not canonical") from exc
    if canonical_ids != device_ids:
        raise ValueError("GPU inventory is not canonical")
    canonical = json.dumps(
        {"schema_version": 1, "device_ids": list(device_ids)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return (
        "sha256:"
        + hashlib.sha256(b"platform-nvidia-gpu-inventory-v1\0" + canonical).hexdigest()
    )
