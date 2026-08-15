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
    is_managed_runtime_profile,
    kernel_runtime_environment,
    load_profile_policy,
)


# This is deliberately narrower than Docker's complete reference grammar. The
# local policy only needs ordinary registry/repository/tag/digest references,
# and rejecting a leading option-like value makes the CLI boundary unambiguous.
IMAGE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,254}$")


def docker_verification_command(
    profile: dict[str, Any], *, docker_binary: str = "docker"
) -> list[str]:
    image = profile["image"]
    if not isinstance(image, str) or not IMAGE_REFERENCE_RE.fullmatch(image):
        raise ValueError("profile image is not safe for the Docker CLI boundary")
    environment = kernel_runtime_environment(profile)
    command = [
        docker_binary,
        "run",
        "--rm",
        "--pull",
        "never",
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
    for name in (
        "PLATFORM_DEFAULT_KERNEL",
        "PLATFORM_PYTHON_EXECUTABLE",
        "PATH",
        "PLATFORM_KERNEL_CONTRACT",
    ):
        command.extend(("--env", f"{name}={environment[name]}"))
    # Keep the image's real ENTRYPOINT. DockerSpawner supplies this command to
    # that entrypoint, so the preflight must include upstream activation hooks
    # instead of bypassing them with --entrypoint.
    command.extend((image, "/usr/local/bin/platform-singleuser", "--version"))
    return command


def check_profile_images(
    policy_path: str | Path,
    *,
    allow_unsafe_policy: bool,
    execute: bool,
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
        for (profile_id, version), profile in managed:
            print(f"verifying profile image contract: {profile_id}@{version}")
            runner(
                docker_verification_command(profile, docker_binary=docker_binary),
                check=True,
            )
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    managed, skipped = check_profile_images(
        args.policy,
        allow_unsafe_policy=args.allow_unsafe_policy,
        execute=not args.validate_only,
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
