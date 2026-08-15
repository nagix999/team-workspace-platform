#!/usr/bin/env python3
"""Verify XFS quota trees/limits and Docker labels, then publish storage health."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from hostlib import (
    HostConfigError,
    canonical_sha256,
    load_config,
    read_json,
    require_commands,
    run,
    write_health_manifest,
)


CHECK_NAMES = (
    "filesystem_xfs",
    "docker_storage_driver_exact",
    "docker_data_root_project_quota",
    "project_quota_accounting",
    "project_quota_enforcement",
    "inventory_digest_exact",
    "project_ids_nonzero_unique",
    "projinherit_all",
    "hard_limits_exact",
    "docker_volume_labels_exact",
    "shared_quota_exact",
)
PROJID_RE = re.compile(r"projid\s*=\s*(\d+)")


def inventory_digest(inventory: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in inventory.items() if key != "inventory_sha256"}
    return canonical_sha256(unsigned)


def quota_report(mount: Path) -> dict[int, int]:
    """Return project ID -> hard limit bytes from xfs_quota's KiB report."""

    output = run(
        [
            "xfs_quota",
            "-x",
            "-c",
            "report -p -b -n -N",
            str(mount),
        ]
    ).stdout
    result: dict[int, int] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        identifier = fields[0].lstrip("#")
        if not identifier.isdigit() or not fields[3].isdigit():
            continue
        # xfs_quota block reports use KiB units unless a human-readable flag is set.
        result[int(identifier)] = int(fields[3]) * 1024
    return result


def project_id(path: Path) -> int:
    value = run(["xfs_io", "-c", "stat", str(path)]).stdout
    match = PROJID_RE.search(value)
    if match is None:
        raise HostConfigError(f"cannot read XFS project ID for {path}")
    return int(match.group(1))


def has_projinherit(path: Path) -> bool:
    output = run(["lsattr", "-d", str(path)]).stdout.strip()
    if not output:
        return False
    flags = output.split()[0]
    return "P" in flags


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    storage = config["storage"]
    if storage["enabled"] is not True:
        raise HostConfigError("storage health cannot be published while storage is disabled")
    require_commands(["docker", "findmnt", "xfs_quota", "xfs_io", "lsattr"])
    mount = Path(storage["mount_path"])
    inventory_path = Path(storage["inventory_file"])
    checks = {name: False for name in CHECK_NAMES}
    errors: list[str] = []

    def check(name: str, callback: Callable[[], bool]) -> None:
        try:
            checks[name] = callback() is True
            if not checks[name]:
                errors.append(f"{name}: condition is false")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    findmnt_output = ""

    def filesystem_xfs() -> bool:
        nonlocal findmnt_output
        findmnt_output = run(
            ["findmnt", "-n", "-o", "FSTYPE,OPTIONS", "--target", str(mount)]
        ).stdout.strip()
        fields = findmnt_output.split(maxsplit=1)
        return (
            len(fields) == 2
            and fields[0] == "xfs"
            and bool({"pquota", "prjquota"}.intersection(fields[1].split(",")))
        )

    quota_state = ""

    def load_quota_state() -> str:
        nonlocal quota_state
        if not quota_state:
            quota_state = run(
                ["xfs_quota", "-x", "-c", "state -p", str(mount)]
            ).stdout
        return quota_state

    check("filesystem_xfs", filesystem_xfs)

    def docker_storage_driver_exact() -> bool:
        actual = run(["docker", "info", "--format", "{{.Driver}}"]).stdout.strip()
        root = run(["docker", "info", "--format", "{{.DockerRootDir}}"]).stdout.strip()
        return (
            actual == storage["docker_storage_driver"]
            and Path(root).resolve() == Path(storage["docker_data_root"]).resolve()
        )

    check("docker_storage_driver_exact", docker_storage_driver_exact)

    def docker_data_root_project_quota() -> bool:
        data_root = Path(storage["docker_data_root"])
        output = run(
            ["findmnt", "-n", "-o", "FSTYPE,OPTIONS", "--target", str(data_root)]
        ).stdout.strip()
        fields = output.split(maxsplit=1)
        return (
            len(fields) == 2
            and fields[0] == "xfs"
            and bool({"pquota", "prjquota"}.intersection(fields[1].split(",")))
            and mount.stat().st_dev != data_root.stat().st_dev
        )

    check("docker_data_root_project_quota", docker_data_root_project_quota)
    check(
        "project_quota_accounting",
        lambda: bool(re.search(r"Accounting:\s*ON", load_quota_state(), re.IGNORECASE)),
    )
    check(
        "project_quota_enforcement",
        lambda: bool(re.search(r"Enforcement:\s*ON", load_quota_state(), re.IGNORECASE)),
    )

    inventory: dict[str, Any] | None = None

    def inventory_digest_exact() -> bool:
        nonlocal inventory
        value = read_json(inventory_path)
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "shared",
            "users",
            "inventory_sha256",
        }:
            return False
        if value["schema_version"] != 1 or not isinstance(value["users"], list):
            return False
        inventory = value
        return value["inventory_sha256"] == inventory_digest(value)

    check("inventory_digest_exact", inventory_digest_exact)

    def all_records() -> list[dict[str, Any]]:
        if inventory is None:
            raise HostConfigError("verified inventory is unavailable")
        if not isinstance(inventory.get("shared"), dict):
            raise HostConfigError("shared inventory is missing")
        records = [inventory["shared"]]
        for user in inventory["users"]:
            if not isinstance(user, dict) or len(user.get("slots", [])) != 5:
                raise HostConfigError("a user inventory row does not have five slots")
            records.extend(user["slots"])
        return records

    def project_ids_nonzero_unique() -> bool:
        identifiers = [int(item["project_id"]) for item in all_records()]
        return all(identifier > 0 for identifier in identifiers) and len(identifiers) == len(
            set(identifiers)
        )

    check("project_ids_nonzero_unique", project_ids_nonzero_unique)

    def projinherit_all() -> bool:
        return all(
            Path(item["path"]).is_dir()
            and project_id(Path(item["path"])) == int(item["project_id"])
            and has_projinherit(Path(item["path"]))
            for item in all_records()
        )

    check("projinherit_all", projinherit_all)

    def hard_limits_exact() -> bool:
        report = quota_report(mount)
        private_records = all_records()[1:]
        return bool(private_records) and all(
            report.get(int(item["project_id"])) == int(item["hard_limit_bytes"])
            and int(item["hard_limit_bytes"]) == storage["private_hard_limit_bytes"]
            for item in private_records
        )

    check("hard_limits_exact", hard_limits_exact)

    def inspect_volume(name: str) -> dict[str, Any]:
        value = json.loads(run(["docker", "volume", "inspect", name]).stdout)
        if not isinstance(value, list) or len(value) != 1:
            raise HostConfigError(f"unexpected volume inspect response for {name}")
        return value[0]

    def docker_volume_labels_exact() -> bool:
        if inventory is None:
            return False
        for user in inventory["users"]:
            for slot in user["slots"]:
                volume = inspect_volume(slot["volume_name"])
                labels = volume.get("Labels") or {}
                options = volume.get("Options") or {}
                expected = {
                    "platform.managed": "true",
                    "platform.provisioned": "true",
                    "platform.owner.user_id": user["user_id"],
                    "platform.owner.username": user["username"],
                    "platform.volume.slot": str(slot["slot_number"]),
                    "platform.volume.slot_id": slot["slot_id"],
                    "platform.quota.project_id": str(slot["project_id"]),
                    "platform.quota.hard_bytes": str(slot["hard_limit_bytes"]),
                    "platform.quota.enforced": "true",
                }
                if any(labels.get(key) != value for key, value in expected.items()):
                    return False
                if (
                    options.get("type") != "none"
                    or options.get("o") != "bind"
                    or options.get("device") != slot["path"]
                ):
                    return False
        return True

    check("docker_volume_labels_exact", docker_volume_labels_exact)

    def shared_quota_exact() -> bool:
        if inventory is None or not isinstance(inventory.get("shared"), dict):
            return False
        shared = inventory["shared"]
        report = quota_report(mount)
        volume = inspect_volume(shared["volume_name"])
        labels = volume.get("Labels") or {}
        options = volume.get("Options") or {}
        return (
            shared["volume_name"] == storage["shared_volume_name"]
            and int(shared["project_id"]) == storage["shared_project_id"]
            and int(shared["hard_limit_bytes"]) == storage["shared_hard_limit_bytes"]
            and report.get(int(shared["project_id"])) == int(shared["hard_limit_bytes"])
            and project_id(Path(shared["path"])) == int(shared["project_id"])
            and has_projinherit(Path(shared["path"]))
            and labels.get("platform.shared") == "true"
            and labels.get("platform.provisioned") == "true"
            and labels.get("platform.quota.project_id") == str(shared["project_id"])
            and labels.get("platform.quota.hard_bytes") == str(shared["hard_limit_bytes"])
            and labels.get("platform.quota.enforced") == "true"
            and options.get("type") == "none"
            and options.get("o") == "bind"
            and options.get("device") == shared["path"]
        )

    check("shared_quota_exact", shared_quota_exact)
    destination = write_health_manifest(config, "storage", checks)
    for error in errors:
        print(error, file=sys.stderr)
    print(destination)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HostConfigError as exc:
        raise SystemExit(f"ERROR: {exc}")
