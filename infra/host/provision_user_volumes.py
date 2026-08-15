#!/usr/bin/env python3
"""Root-only, exact-target XFS project-quota and Docker-volume provisioner."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from hostlib import (
    HostConfigError,
    atomic_json,
    canonical_sha256,
    load_config,
    read_json,
    require_commands,
    run,
)


USERNAME_RE = re.compile(r"^(?!.*--)[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def size_mib(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HostConfigError(f"{where} must be a positive byte count")
    if value % (1024 * 1024):
        raise HostConfigError(f"{where} must be an exact MiB multiple")
    return value // (1024 * 1024)


def inventory_digest(inventory: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in inventory.items() if key != "inventory_sha256"}
    return canonical_sha256(unsigned)


def load_inventory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 1,
            "shared": None,
            "users": [],
            "inventory_sha256": "",
        }
    value = read_json(path)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "shared",
        "users",
        "inventory_sha256",
    }:
        raise HostConfigError("volume inventory schema mismatch")
    if value["schema_version"] != 1 or not isinstance(value["users"], list):
        raise HostConfigError("volume inventory is invalid")
    if value["inventory_sha256"] != inventory_digest(value):
        raise HostConfigError("volume inventory digest mismatch")
    return value


def configure_project(mount: Path, path: Path, project_id: int, hard_bytes: int) -> None:
    hard_mib = size_mib(hard_bytes, "hard quota")
    if project_id <= 0:
        raise HostConfigError("project ID must be nonzero")
    if " " in str(path):
        raise HostConfigError("quota paths containing spaces are not supported")
    run(
        [
            "xfs_quota",
            "-x",
            "-c",
            f"project -s -p {path} {project_id}",
            str(mount),
        ]
    )
    run(
        [
            "xfs_quota",
            "-x",
            "-c",
            f"limit -p bsoft={hard_mib}m bhard={hard_mib}m {project_id}",
            str(mount),
        ]
    )


def ensure_volume(name: str, path: Path, labels: dict[str, str]) -> None:
    inspected = run(["docker", "volume", "inspect", name], check=False)
    if inspected.returncode == 0:
        value = json.loads(inspected.stdout)
        if not isinstance(value, list) or len(value) != 1:
            raise HostConfigError(f"unexpected inspect response for volume {name}")
        volume = value[0]
        options = volume.get("Options") or {}
        actual_labels = volume.get("Labels") or {}
        if (
            options.get("type") != "none"
            or options.get("o") != "bind"
            or Path(options.get("device", "")) != path
            or any(actual_labels.get(key) != value for key, value in labels.items())
        ):
            raise HostConfigError(f"existing Docker volume {name} does not match policy")
        return
    command = [
        "docker",
        "volume",
        "create",
        "--driver=local",
        "--opt",
        "type=none",
        "--opt",
        "o=bind",
        "--opt",
        f"device={path}",
    ]
    for key, value in sorted(labels.items()):
        command.extend(["--label", f"{key}={value}"])
    command.append(name)
    created = run(command).stdout.strip()
    if created != name:
        raise HostConfigError(f"Docker returned an unexpected volume name for {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--project-id-base", required=True, type=int)
    args = parser.parse_args()
    config = load_config(args.config)
    storage = config["storage"]
    if storage["enabled"] is not True:
        raise HostConfigError("XFS volume provisioning is disabled in this config")
    require_commands(["docker", "findmnt", "xfs_quota", "xfs_io", "lsattr"])
    try:
        user_uuid = uuid.UUID(args.user_id)
    except ValueError:
        raise HostConfigError("user-id must be a canonical UUID") from None
    if str(user_uuid) != args.user_id.lower():
        raise HostConfigError("user-id must use canonical lowercase UUID form")
    if not USERNAME_RE.fullmatch(args.username):
        raise HostConfigError("username is outside the approved Hub namespace")
    if storage["slot_count"] != 5:
        raise HostConfigError("MVP slot_count must be exactly 5")
    if args.project_id_base <= 0 or args.project_id_base + 4 >= 2**31:
        raise HostConfigError("project-id-base is outside the supported range")
    size_mib(storage["private_hard_limit_bytes"], "private_hard_limit_bytes")
    size_mib(storage["shared_hard_limit_bytes"], "shared_hard_limit_bytes")
    if storage["shared_project_id"] <= 0:
        raise HostConfigError("shared_project_id must be nonzero")

    mount = Path(storage["mount_path"])
    private_root = Path(storage["private_root"])
    shared_path = Path(storage["shared_path"])
    manifest_dir = Path(storage["manifest_dir"])
    inventory_path = Path(storage["inventory_file"])
    for path in (private_root, shared_path, manifest_dir, inventory_path.parent):
        if not within(path, mount):
            raise HostConfigError(f"configured path escapes storage mount: {path}")

    mounted = run(["findmnt", "-n", "-o", "FSTYPE,OPTIONS", "--target", str(mount)])
    fields = mounted.stdout.strip().split(maxsplit=1)
    if len(fields) != 2 or fields[0] != "xfs" or not {
        "pquota",
        "prjquota",
    }.intersection(fields[1].split(",")):
        raise HostConfigError("storage mount is not XFS with project quota enabled")

    mount.mkdir(parents=True, exist_ok=True, mode=0o755)
    private_root.mkdir(parents=True, exist_ok=True, mode=0o711)
    manifest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = manifest_dir / ".inventory.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        inventory = load_inventory(inventory_path)
        used_project_ids = {
            int(slot["project_id"])
            for user in inventory["users"]
            for slot in user["slots"]
        }
        if inventory["shared"] is not None:
            used_project_ids.add(int(inventory["shared"]["project_id"]))
        requested_ids = set(range(args.project_id_base, args.project_id_base + 5))
        existing_user = next(
            (item for item in inventory["users"] if item["user_id"] == str(user_uuid)),
            None,
        )
        existing_ids = (
            {int(slot["project_id"]) for slot in existing_user["slots"]}
            if existing_user
            else set()
        )
        if requested_ids & (used_project_ids - existing_ids):
            raise HostConfigError("one or more project IDs are already allocated")
        if existing_user and (
            existing_user["username"] != args.username or existing_ids != requested_ids
        ):
            raise HostConfigError("existing user inventory does not match requested identity/IDs")

        shared_path.mkdir(parents=True, exist_ok=True, mode=0o2770)
        os.chown(shared_path, 0, storage["shared_gid"])
        os.chmod(shared_path, 0o2770)
        configure_project(
            mount,
            shared_path,
            storage["shared_project_id"],
            storage["shared_hard_limit_bytes"],
        )
        shared_labels = {
            "platform.managed": "true",
            "platform.provisioned": "true",
            "platform.shared": "true",
            "platform.quota.hard_bytes": str(storage["shared_hard_limit_bytes"]),
            "platform.quota.project_id": str(storage["shared_project_id"]),
            "platform.quota.enforced": "true",
        }
        ensure_volume(storage["shared_volume_name"], shared_path, shared_labels)
        shared_record = {
            "volume_name": storage["shared_volume_name"],
            "path": str(shared_path),
            "project_id": storage["shared_project_id"],
            "hard_limit_bytes": storage["shared_hard_limit_bytes"],
            "gid": storage["shared_gid"],
        }
        if inventory["shared"] not in (None, shared_record):
            raise HostConfigError("shared inventory conflicts with host config")
        inventory["shared"] = shared_record

        user_root = private_root / args.username
        user_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(user_root, storage["uid"], storage["gid"])
        os.chmod(user_root, 0o700)
        slots: list[dict[str, Any]] = []
        for slot_number in range(1, 6):
            slot_id = str(uuid.uuid5(user_uuid, f"workspace-volume-slot-{slot_number}"))
            project_id = args.project_id_base + slot_number - 1
            slot_path = user_root / f"slot-{slot_number}"
            slot_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(slot_path, storage["uid"], storage["gid"])
            os.chmod(slot_path, 0o700)
            configure_project(
                mount,
                slot_path,
                project_id,
                storage["private_hard_limit_bytes"],
            )
            volume_name = f"jupyter-user-{args.username}-slot-{slot_number}"
            labels = {
                "platform.managed": "true",
                "platform.provisioned": "true",
                "platform.owner.user_id": str(user_uuid),
                "platform.owner.username": args.username,
                "platform.volume.slot": str(slot_number),
                "platform.volume.slot_id": slot_id,
                "platform.quota.project_id": str(project_id),
                "platform.quota.hard_bytes": str(storage["private_hard_limit_bytes"]),
                "platform.quota.enforced": "true",
            }
            ensure_volume(volume_name, slot_path, labels)
            slots.append(
                {
                    "slot_id": slot_id,
                    "slot_number": slot_number,
                    "volume_name": volume_name,
                    "path": str(slot_path),
                    "project_id": project_id,
                    "hard_limit_bytes": storage["private_hard_limit_bytes"],
                }
            )

        user_record = {
            "user_id": str(user_uuid),
            "username": args.username,
            "uid": storage["uid"],
            "gid": storage["gid"],
            "slots": slots,
        }
        inventory["users"] = [
            item for item in inventory["users"] if item["user_id"] != str(user_uuid)
        ] + [user_record]
        inventory["users"].sort(key=lambda item: item["user_id"])
        inventory["inventory_sha256"] = inventory_digest(inventory)
        atomic_json(inventory_path, inventory, mode=0o600)
        user_manifest = {
            "schema_version": 1,
            "inventory_sha256": inventory["inventory_sha256"],
            **user_record,
        }
        manifest_path = manifest_dir / f"user-{user_uuid}.json"
        atomic_json(manifest_path, user_manifest, mode=0o600)
        print(manifest_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HostConfigError as exc:
        raise SystemExit(f"ERROR: {exc}")
