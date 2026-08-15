#!/usr/bin/env python3
"""Create labelled Docker volumes for the explicit non-XFS local-dev path.

These labels say quota.enforced=false and are accepted only when the Hub itself is
started with both PLATFORM_ENV=local-dev and ALLOW_UNSAFE_LOCAL_DEV=true.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "jupyterhub"))
from local_volume_policy import (  # noqa: E402
    DEFAULT_PROJECT_ID_START,
    USERNAME_RE,
    atomic_json,
    build_local_manifest,
    local_profile_runtime,
    private_volume_labels,
    reserve_project_id_block,
    shared_volume_labels,
)
from profile_policy import load_profile_policy  # noqa: E402


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def validate_volume_inspect(name: str, labels: dict[str, str], raw: str) -> None:
    value = json.loads(raw)
    if not isinstance(value, list) or len(value) != 1:
        raise RuntimeError(f"unexpected Docker inspect response for {name}")
    volume = value[0]
    actual = volume.get("Labels") or {}
    if (
        volume.get("Name") != name
        or volume.get("Driver") != "local"
        or (volume.get("Options") or {}) != {}
        or actual != labels
    ):
        raise RuntimeError(f"existing local volume {name} has conflicting labels")


def ensure_volume(name: str, labels: dict[str, str]) -> None:
    inspected = docker("volume", "inspect", name, check=False)
    if inspected.returncode == 0:
        validate_volume_inspect(name, labels, inspected.stdout)
        return
    command = ["volume", "create"]
    for key, value in sorted(labels.items()):
        command.extend(["--label", f"{key}={value}"])
    command.append(name)
    if docker(*command).stdout.strip() != name:
        raise RuntimeError(f"unexpected Docker response while creating {name}")
    validate_volume_inspect(
        name,
        labels,
        docker("volume", "inspect", name).stdout,
    )


def initialize_volume(name: str, image: str, *, uid: int, gid: int, mode: str) -> None:
    """Set only the named-volume root metadata with a tightly constrained helper."""
    docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "FOWNER",
        "--cap-add",
        "FSETID",
        "--cap-add",
        "DAC_OVERRIDE",
        "--security-opt",
        "no-new-privileges:true",
        "--mount",
        f"type=volume,src={name},dst=/volume",
        "--entrypoint",
        "/bin/sh",
        image,
        "-ceu",
        'chown "$1:$2" /volume; chmod "$3" /volume',
        "volume-init",
        str(uid),
        str(gid),
        mode,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-policy", required=True, type=Path)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--project-id-start",
        type=int,
        default=DEFAULT_PROJECT_ID_START,
        help="lowest automatic five-ID allocation block (default: 10000)",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--i-understand-this-is-unsafe-local-dev", action="store_true", required=True
    )
    args = parser.parse_args()
    if not USERNAME_RE.fullmatch(args.username):
        raise RuntimeError("invalid username")
    policy = load_profile_policy(args.profile_policy, allow_unsafe_images=True)
    runtime = local_profile_runtime(policy)
    # Persist the reservation only after static policy validation, but before the
    # first Docker mutation. A failed Docker operation can therefore be retried
    # without allowing another user to claim the same block.
    project_id_base = reserve_project_id_block(
        output=args.output,
        user_id=args.user_id,
        username=args.username,
        project_id_start=args.project_id_start,
    )
    manifest = build_local_manifest(
        user_id=args.user_id,
        username=args.username,
        uid=runtime["uid"],
        gid=runtime["gid"],
        hard_limit_bytes=runtime["hard_limit_bytes"],
        project_id_base=project_id_base,
    )

    shared = policy["shared_volume"]
    ensure_volume(
        shared["name"],
        shared_volume_labels(),
    )
    initialize_volume(
        shared["name"],
        runtime["initializer_image"],
        uid=0,
        gid=shared["gid"],
        mode="2770",
    )
    for slot in manifest["slots"]:
        ensure_volume(
            slot["volume_name"],
            private_volume_labels(
                user_id=manifest["user_id"],
                username=manifest["username"],
                slot=slot,
            ),
        )
        initialize_volume(
            slot["volume_name"],
            runtime["initializer_image"],
            uid=runtime["uid"],
            gid=runtime["gid"],
            mode="0700",
        )
    atomic_json(args.output, manifest)
    print(args.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"ERROR: {exc}")
