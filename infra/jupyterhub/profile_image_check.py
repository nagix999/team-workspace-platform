#!/usr/bin/env python3
"""Validate every enabled managed profile against its actual container image."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from profile_policy import (
    accelerator_contract,
    is_managed_runtime_profile,
    kernel_runtime_environment,
    load_profile_policy,
)


# This is deliberately narrower than Docker's complete reference grammar. The
# local policy only needs ordinary registry/repository/tag/digest references,
# and rejecting a leading option-like value makes the CLI boundary unambiguous.
IMAGE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,254}$")
NVIDIA_GPU_UUID_RE = re.compile(r"^GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
MAX_NVIDIA_GPU_COUNT = 64
MAX_EXHAUSTIVE_GPU_POOL_SIZE = 8


def _assigned_gpu_ids(
    value: str | Sequence[str] | None, *, required_count: int
) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif value is None:
        values = ()
    else:
        values = tuple(value)
    if (
        type(required_count) is not int
        or not 1 <= required_count <= MAX_NVIDIA_GPU_COUNT
        or len(values) != required_count
        or any(not isinstance(item, str) for item in values)
        or any(not NVIDIA_GPU_UUID_RE.fullmatch(item) for item in values)
        or values != tuple(sorted(set(values)))
    ):
        raise ValueError(
            "NVIDIA profile image verification requires the exact canonical GPU UUID set"
        )
    return values


def _gpu_pool(value: Sequence[str] | None) -> tuple[str, ...]:
    values = tuple(value or ())
    if (
        len(values) > MAX_NVIDIA_GPU_COUNT
        or any(not isinstance(item, str) for item in values)
        or any(not NVIDIA_GPU_UUID_RE.fullmatch(item) for item in values)
        or values != tuple(sorted(set(values)))
    ):
        raise ValueError("NVIDIA GPU pool must contain sorted unique canonical UUIDs")
    return values


def _gpu_verification_assignments(
    pool: tuple[str, ...], *, required_count: int
) -> tuple[tuple[str, ...], ...]:
    """Return exact-size assignments that exercise every physical GPU.

    Testing every possible subset grows combinatorially and does not add useful
    image-compatibility coverage. Keep ``required_count - 1`` anchor devices and
    rotate every remaining pool member through the last slot instead. This
    yields ``len(pool) - required_count + 1`` probes, keeps every assignment
    canonical, and makes every physical UUID execute the image's tensor probe.
    """

    if len(pool) < required_count:
        raise ValueError(
            "enabled NVIDIA profile history exceeds the verified GPU pool"
        )
    anchors = pool[: required_count - 1]
    return tuple(
        _assigned_gpu_ids((*anchors, device_id), required_count=required_count)
        for device_id in pool[required_count - 1 :]
    )


def _bounded_gpu_verification_assignments(
    pool: tuple[str, ...], *, required_count: int
) -> tuple[tuple[str, ...], ...]:
    """Bound image probes while retaining exhaustive coverage on ordinary hosts.

    The host GPU preflight already tensor-tests every UUID separately and the
    complete pool with the deployment image. For pools up to eight devices we
    retain per-profile physical-device coverage. Larger pools use one exact-size
    assignment per GPU-count profile, including the highest sorted UUID, so a
    64-device policy performs 64 rather than 2,080 profile tensor launches.
    """

    assignments = _gpu_verification_assignments(pool, required_count=required_count)
    return (
        assignments
        if len(pool) <= MAX_EXHAUSTIVE_GPU_POOL_SIZE
        else (assignments[-1],)
    )


def docker_verification_command(
    profile: dict[str, Any],
    *,
    nvidia_gpu_device_id: str | Sequence[str] | None = None,
    docker_binary: str = "docker",
) -> list[str]:
    """Build the live CUDA probe, or the ordinary wrapper probe for CPU."""

    accelerator = accelerator_contract(profile)
    if accelerator is None or accelerator["kind"] != "nvidia":
        return docker_kernel_verification_command(
            profile,
            nvidia_gpu_device_id=nvidia_gpu_device_id,
            docker_binary=docker_binary,
        )
    image, command = _docker_command_prefix(
        profile,
        nvidia_gpu_device_id=nvidia_gpu_device_id,
        attach_gpu=True,
        docker_binary=docker_binary,
    )
    default = next(
        kernel
        for kernel in profile["kernels"]
        if kernel["name"] == profile["default_kernel"]
    )
    # A GPU profile remains restartable even after it is no longer selectable.
    # Run that image's own fail-closed verifier with the exact physical UUID,
    # without setting WORKSPACE_ID (which would incorrectly require real
    # private/shared volume mounts).
    command.extend(
        (
            "--entrypoint",
            default["executable"],
            image,
            "/usr/local/libexec/verify_cuda_runtime.py",
            "--require-gpu",
        )
    )
    return command


def _docker_command_prefix(
    profile: dict[str, Any],
    *,
    nvidia_gpu_device_id: str | Sequence[str] | None,
    attach_gpu: bool,
    docker_binary: str,
) -> tuple[str, list[str]]:
    image = profile["image"]
    if not isinstance(image, str) or not IMAGE_REFERENCE_RE.fullmatch(image):
        raise ValueError("profile image is not safe for the Docker CLI boundary")
    environment = kernel_runtime_environment(profile)
    accelerator = accelerator_contract(profile)
    gpu_arguments: list[str] = []
    if accelerator is not None and accelerator["kind"] == "nvidia":
        device_ids = _assigned_gpu_ids(
            nvidia_gpu_device_id, required_count=accelerator["count"]
        )
        serialized_device_ids = ",".join(device_ids)
        environment["PLATFORM_NVIDIA_GPU_DEVICE_IDS"] = serialized_device_ids
        if attach_gpu:
            # Docker's --gpus value is CSV. Preserve a multi-device request as
            # one `device=` field by retaining literal quotes in direct argv.
            gpu_request = (
                f'"device={serialized_device_ids}"'
                if len(device_ids) > 1
                else f"device={serialized_device_ids}"
            )
            gpu_arguments = ["--gpus", gpu_request]
    command = [
        docker_binary,
        "run",
        "--rm",
        "--pull",
        "never",
        *gpu_arguments,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--user",
        "1000:100",
    ]
    for name in sorted(environment):
        command.extend(("--env", f"{name}={environment[name]}"))
    return image, command


def docker_kernel_verification_command(
    profile: dict[str, Any],
    *,
    nvidia_gpu_device_id: str | Sequence[str] | None = None,
    docker_binary: str = "docker",
) -> list[str]:
    """Build the real-entrypoint kernel and image-metadata probe."""

    image, command = _docker_command_prefix(
        profile,
        nvidia_gpu_device_id=nvidia_gpu_device_id,
        attach_gpu=False,
        docker_binary=docker_binary,
    )
    # Keep the image's real ENTRYPOINT. DockerSpawner supplies this command to
    # that entrypoint, so the preflight must include upstream activation hooks
    # instead of bypassing them with --entrypoint. For CUDA images the wrapper
    # checks the profile's PLATFORM_KERNEL_CONTRACT and image metadata without
    # requiring a device because WORKSPACE_ID is intentionally absent.
    command.extend((image, "/usr/local/bin/platform-singleuser", "--version"))
    return command


def check_profile_images(
    policy_path: str | Path,
    *,
    allow_unsafe_policy: bool,
    execute: bool,
    nvidia_gpu_device_id: str | None = None,
    nvidia_gpu_device_ids: Sequence[str] | None = None,
    docker_binary: str = "docker",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[int, int]:
    if nvidia_gpu_device_id is not None and nvidia_gpu_device_ids is not None:
        raise ValueError("legacy singular and plural NVIDIA GPU inputs conflict")
    pool = _gpu_pool(
        (nvidia_gpu_device_id,)
        if nvidia_gpu_device_id is not None
        else nvidia_gpu_device_ids
    )
    policy = load_profile_policy(policy_path, allow_unsafe_images=allow_unsafe_policy)
    managed = []
    skipped_legacy = 0
    for key, profile in sorted(policy["profiles"].items()):
        if profile["enabled"] is not True:
            continue
        if not is_managed_runtime_profile(profile):
            skipped_legacy += 1
            continue
        managed.append((key, profile))

    if not managed:
        raise ValueError("policy contains no enabled managed runtime profiles")
    if execute:
        seen_gpu_kernel_commands: set[tuple[str, ...]] = set()
        seen_gpu_commands: set[tuple[str, ...]] = set()
        for (profile_id, version), profile in managed:
            accelerator = accelerator_contract(profile)
            if accelerator is not None and accelerator["kind"] == "nvidia":
                count = accelerator["count"]
                assignments = _bounded_gpu_verification_assignments(
                    pool, required_count=count
                )
                assigned = assignments[0]
                kernel_command = docker_kernel_verification_command(
                    profile,
                    nvidia_gpu_device_id=assigned,
                    docker_binary=docker_binary,
                )
                kernel_command_key = tuple(kernel_command)
                if kernel_command_key not in seen_gpu_kernel_commands:
                    seen_gpu_kernel_commands.add(kernel_command_key)
                    print(
                        "verifying GPU profile kernel/image contract: "
                        f"{profile_id}@{version}"
                    )
                    runner(kernel_command, check=True, timeout=120)
                for assigned in assignments:
                    command = docker_verification_command(
                        profile,
                        nvidia_gpu_device_id=assigned,
                        docker_binary=docker_binary,
                    )
                    command_key = tuple(command)
                    if command_key in seen_gpu_commands:
                        continue
                    seen_gpu_commands.add(command_key)
                    print(
                        "verifying GPU tensor contract: "
                        f"{profile_id}@{version} devices={','.join(assigned)}"
                    )
                    # A broken container runtime or CUDA initialization must not hold
                    # the operator lock indefinitely during production preflight.
                    runner(command, check=True, timeout=120)
            else:
                command = docker_verification_command(
                    profile,
                    nvidia_gpu_device_id=nvidia_gpu_device_id,
                    docker_binary=docker_binary,
                )
                print(f"verifying profile image contract: {profile_id}@{version}")
                # A broken container runtime or image initialization must not hold
                # the operator lock indefinitely during production preflight.
                runner(command, check=True, timeout=120)
    return len(managed), skipped_legacy


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate policy schema and managed runtime image contracts"
    )
    parser.add_argument("--policy", required=True)
    parser.add_argument(
        "--allow-unsafe-policy",
        action="store_true",
        help="allow mutable images and unenforced disk quota for isolated local dev",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate policy/digests without invoking Docker",
    )
    parser.add_argument("--docker-binary", default="docker")
    gpu = parser.add_mutually_exclusive_group()
    gpu.add_argument("--nvidia-gpu-device-id")
    gpu.add_argument("--nvidia-gpu-device-ids")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    managed, skipped = check_profile_images(
        args.policy,
        allow_unsafe_policy=args.allow_unsafe_policy,
        execute=not args.validate_only,
        nvidia_gpu_device_id=args.nvidia_gpu_device_id,
        nvidia_gpu_device_ids=(
            args.nvidia_gpu_device_ids.split(",")
            if args.nvidia_gpu_device_ids is not None
            else None
        ),
        docker_binary=args.docker_binary,
    )
    action = "validated" if args.validate_only else "verified"
    print(
        f"profile policy {action}: managed={managed}, "
        f"enabled_legacy_skipped={skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
