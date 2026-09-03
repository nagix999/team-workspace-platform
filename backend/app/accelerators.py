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
        or spec.count != 1
        or spec.sharing != "exclusive"
        or not isinstance(spec.cuda_version, str)
        or not CUDA_VERSION_RE.fullmatch(spec.cuda_version)
        or spec.framework != "pytorch"
        or not isinstance(spec.framework_version, str)
        or not FRAMEWORK_VERSION_RE.fullmatch(spec.framework_version)
    ):
        raise ValueError(
            "only one exclusive NVIDIA GPU with pinned PyTorch is supported"
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
    if len(values) != 1 or not NVIDIA_GPU_DEVICE_ID_RE.fullmatch(values[0]):
        raise RuntimeError(
            "PLATFORM_NVIDIA_GPU_DEVICE_IDS must contain exactly one canonical physical GPU UUID"
        )
    return values


def gpu_inventory_digest(device_ids: tuple[str, ...]) -> str:
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
