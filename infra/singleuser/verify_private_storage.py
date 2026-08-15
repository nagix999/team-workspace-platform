#!/usr/bin/env python3
"""Verify and normalize the private workspace mount root before Jupyter starts.

The verifier deliberately operates on only one already-open directory file
descriptor.  It never traverses the workspace or changes user file contents.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import PurePosixPath
from typing import Mapping


EXPECTED_PRIVATE_MOUNT_PATH = "/home/jovyan/work"
EXPECTED_PRIVATE_UID = 1000
EXPECTED_PRIVATE_GID = 100
EXPECTED_ROOT_MODE = 0o700


class PrivateStorageContractError(RuntimeError):
    """The mounted private workspace does not match the platform contract."""


def _unescape_mountinfo_path(value: str) -> str:
    return (
        value.replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\012", "\n")
        .replace(r"\134", "\\")
    )


def _mount_record(document: str, path: str) -> tuple[set[str], tuple[int, int]]:
    match: tuple[set[str], tuple[int, int]] | None = None
    for line in document.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            raise PrivateStorageContractError("mountinfo contains an invalid record")
        separator = fields.index("-")
        if separator < 6 or len(fields) < separator + 4:
            raise PrivateStorageContractError("mountinfo contains an invalid record")
        point = _unescape_mountinfo_path(fields[4])
        if point != path:
            continue
        try:
            major_text, minor_text = fields[2].split(":", 1)
            device = (int(major_text, 10), int(minor_text, 10))
        except (TypeError, ValueError) as exc:
            raise PrivateStorageContractError(
                "private mount device is invalid"
            ) from exc
        if match is not None:
            raise PrivateStorageContractError("private mount record is ambiguous")
        match = (set(fields[5].split(",")), device)
    if match is None:
        raise PrivateStorageContractError("private path is not a distinct mount")
    return match


def _validated_path(value: str) -> str:
    if not value.startswith("/"):
        raise PrivateStorageContractError("private mount path is not absolute")
    normalized = str(PurePosixPath(value))
    if normalized != value or ".." in PurePosixPath(value).parts:
        raise PrivateStorageContractError("private mount path is not canonical")
    if normalized == "/":
        raise PrivateStorageContractError("private mount path is too broad")
    return normalized


def private_runtime_contract(environment: Mapping[str, str]) -> tuple[str, int, int]:
    """Return the fixed private-volume contract or reject environment drift."""

    if environment.get("HOME") != EXPECTED_PRIVATE_MOUNT_PATH:
        raise PrivateStorageContractError("HOME does not match the private mount")
    if environment.get("PLATFORM_PRIVATE_MOUNT_PATH") != EXPECTED_PRIVATE_MOUNT_PATH:
        raise PrivateStorageContractError("private mount path contract has drifted")
    if environment.get("PLATFORM_PRIVATE_UID") != str(EXPECTED_PRIVATE_UID):
        raise PrivateStorageContractError("private uid contract has drifted")
    if environment.get("PLATFORM_PRIVATE_GID") != str(EXPECTED_PRIVATE_GID):
        raise PrivateStorageContractError("private gid contract has drifted")
    return EXPECTED_PRIVATE_MOUNT_PATH, EXPECTED_PRIVATE_UID, EXPECTED_PRIVATE_GID


def verify_private_storage(
    mount_path: str,
    expected_uid: int,
    expected_gid: int,
    *,
    mountinfo: str | None = None,
) -> None:
    """Verify the private mount and normalize only its root directory mode."""

    path = _validated_path(mount_path)
    for value, name in ((expected_uid, "uid"), (expected_gid, "gid")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PrivateStorageContractError(f"private {name} is invalid")
    if os.geteuid() != expected_uid or os.getegid() != expected_gid:
        raise PrivateStorageContractError("runtime process identity has drifted")
    if os.path.realpath(path) != path:
        raise PrivateStorageContractError("private mount path traverses a symlink")

    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo", encoding="utf-8") as handle:
                mountinfo = handle.read()
        except OSError as exc:
            raise PrivateStorageContractError("cannot read process mountinfo") from exc
    mount_options, expected_device = _mount_record(mountinfo, path)
    if "rw" not in mount_options or "ro" in mount_options:
        raise PrivateStorageContractError("private mount is not read-write")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path, flags)
    except OSError as exc:
        raise PrivateStorageContractError(
            "cannot safely open private mount root"
        ) from exc

    try:
        root = os.fstat(directory_fd)
        if not stat.S_ISDIR(root.st_mode):
            raise PrivateStorageContractError("private mount root is not a directory")
        actual_device = (os.major(root.st_dev), os.minor(root.st_dev))
        if actual_device != expected_device:
            raise PrivateStorageContractError("private mount device has drifted")
        if root.st_uid != expected_uid or root.st_gid != expected_gid:
            raise PrivateStorageContractError(
                "private mount root ownership has drifted"
            )

        if stat.S_IMODE(root.st_mode) != EXPECTED_ROOT_MODE:
            try:
                os.fchmod(directory_fd, EXPECTED_ROOT_MODE)
            except OSError as exc:
                raise PrivateStorageContractError(
                    "cannot normalize private mount root mode"
                ) from exc

        root = os.fstat(directory_fd)
        if root.st_uid != expected_uid or root.st_gid != expected_gid:
            raise PrivateStorageContractError(
                "private mount root ownership has drifted"
            )
        if stat.S_IMODE(root.st_mode) != EXPECTED_ROOT_MODE:
            raise PrivateStorageContractError("private mount root mode has drifted")
    finally:
        os.close(directory_fd)


def main() -> int:
    try:
        mount_path, expected_uid, expected_gid = private_runtime_contract(os.environ)
        verify_private_storage(mount_path, expected_uid, expected_gid)
    except PrivateStorageContractError as exc:
        print(f"private storage verification failed: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
