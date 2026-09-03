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


def docker_verification_command(
    profile: dict[str, Any],
    *,
    nvidia_gpu_device_id: str | None = None,
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
    nvidia_gpu_device_id: str | None,
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
        if not isinstance(
            nvidia_gpu_device_id, str
        ) or not NVIDIA_GPU_UUID_RE.fullmatch(nvidia_gpu_device_id):
            raise ValueError(
                "NVIDIA profile image verification requires one exact GPU UUID"
            )
        environment["PLATFORM_NVIDIA_GPU_DEVICE_IDS"] = nvidia_gpu_device_id
        if attach_gpu:
            gpu_arguments = ["--gpus", f"device={nvidia_gpu_device_id}"]
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
    nvidia_gpu_device_id: str | None = None,
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
    docker_binary: str = "docker",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[int, int]:
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
                kernel_command = docker_kernel_verification_command(
                    profile,
                    nvidia_gpu_device_id=nvidia_gpu_device_id,
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
                command = docker_verification_command(
                    profile,
                    nvidia_gpu_device_id=nvidia_gpu_device_id,
                    docker_binary=docker_binary,
                )
                command_key = tuple(command)
                if command_key in seen_gpu_commands:
                    continue
                seen_gpu_commands.add(command_key)
                print(f"verifying GPU tensor contract: {profile_id}@{version}")
            else:
                command = docker_verification_command(
                    profile,
                    nvidia_gpu_device_id=nvidia_gpu_device_id,
                    docker_binary=docker_binary,
                )
                print(f"verifying profile image contract: {profile_id}@{version}")
            # A broken container runtime or CUDA initialization must not hold
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
    parser.add_argument("--nvidia-gpu-device-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    managed, skipped = check_profile_images(
        args.policy,
        allow_unsafe_policy=args.allow_unsafe_policy,
        execute=not args.validate_only,
        nvidia_gpu_device_id=args.nvidia_gpu_device_id,
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
