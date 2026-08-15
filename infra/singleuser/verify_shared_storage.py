#!/usr/bin/env python3
"""Fail closed when the team shared-volume mount contract has drifted.

The Docker control plane verifies the volume name and labels before create.  This
probe runs inside the single-user container and verifies the other half of the
contract: the configured path is an actual mount, its root metadata is safe, the
runtime process belongs to the collaboration group, and a group-writable file can
be created without following a user-controlled symlink.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from pathlib import PurePosixPath


EXPECTED_ROOT_MODE = 0o2770
PROBE_FILE_MODE = 0o664


class SharedStorageContractError(RuntimeError):
    """The mounted shared directory does not match the platform contract."""


def _mount_points(document: str) -> set[str]:
    points: set[str] = set()
    for line in document.splitlines():
        fields = line.split()
        if len(fields) < 6 or "-" not in fields:
            raise SharedStorageContractError("mountinfo contains an invalid record")
        # Linux mountinfo escapes space, tab, newline and backslash in paths.
        point = (
            fields[4]
            .replace(r"\040", " ")
            .replace(r"\011", "\t")
            .replace(r"\012", "\n")
            .replace(r"\134", "\\")
        )
        points.add(point)
    return points


def _validated_path(value: str) -> str:
    if not value.startswith("/"):
        raise SharedStorageContractError("shared mount path is not absolute")
    normalized = str(PurePosixPath(value))
    if normalized != value or ".." in PurePosixPath(value).parts:
        raise SharedStorageContractError("shared mount path is not canonical")
    if normalized in {"/", "/home", "/home/jovyan"}:
        raise SharedStorageContractError("shared mount path is too broad")
    return normalized


def verify_shared_storage(
    mount_path: str,
    shared_gid: int,
    *,
    expected_owner_uid: int = 0,
    mountinfo: str | None = None,
) -> None:
    """Verify metadata and an actual collaborative write through one directory FD."""

    path = _validated_path(mount_path)
    if (
        isinstance(shared_gid, bool)
        or not isinstance(shared_gid, int)
        or shared_gid <= 0
    ):
        raise SharedStorageContractError("shared gid is invalid")
    if os.path.realpath(path) != path:
        raise SharedStorageContractError("shared mount path traverses a symlink")

    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo", encoding="utf-8") as handle:
                mountinfo = handle.read()
        except OSError as exc:
            raise SharedStorageContractError("cannot read process mountinfo") from exc
    if path not in _mount_points(mountinfo):
        raise SharedStorageContractError("shared path is not a distinct mount")

    groups = {os.getegid(), *os.getgroups()}
    if shared_gid not in groups:
        raise SharedStorageContractError("runtime process is not in the shared group")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path, flags)
    except OSError as exc:
        raise SharedStorageContractError(
            "cannot safely open shared mount root"
        ) from exc

    probe_name = f".platform-write-probe-{secrets.token_hex(16)}"
    probe_fd: int | None = None
    created = False
    try:
        root = os.fstat(directory_fd)
        if not stat.S_ISDIR(root.st_mode):
            raise SharedStorageContractError("shared mount root is not a directory")
        if root.st_uid != expected_owner_uid or root.st_gid != shared_gid:
            raise SharedStorageContractError("shared mount root ownership has drifted")
        if stat.S_IMODE(root.st_mode) != EXPECTED_ROOT_MODE:
            raise SharedStorageContractError("shared mount root mode has drifted")

        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        create_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            probe_fd = os.open(
                probe_name,
                create_flags,
                0o666,
                dir_fd=directory_fd,
            )
            created = True
            os.write(probe_fd, b"platform shared storage probe\n")
            probe = os.fstat(probe_fd)
        except OSError as exc:
            raise SharedStorageContractError("shared mount is not writable") from exc

        if not stat.S_ISREG(probe.st_mode):
            raise SharedStorageContractError("shared write probe is not a regular file")
        if probe.st_uid != os.geteuid() or probe.st_gid != shared_gid:
            raise SharedStorageContractError("shared file ownership inheritance failed")
        if stat.S_IMODE(probe.st_mode) != PROBE_FILE_MODE:
            raise SharedStorageContractError("collaborative umask is not 0002")
    finally:
        if probe_fd is not None:
            os.close(probe_fd)
        if created:
            try:
                os.unlink(probe_name, dir_fd=directory_fd)
            except OSError as exc:
                os.close(directory_fd)
                raise SharedStorageContractError(
                    "cannot remove shared write probe"
                ) from exc
        os.close(directory_fd)


def main() -> int:
    mount_path = os.environ.get("PLATFORM_SHARED_MOUNT_PATH", "")
    raw_gid = os.environ.get("PLATFORM_SHARED_GID", "")
    try:
        shared_gid = int(raw_gid, 10)
    except ValueError:
        print(
            "shared storage verification failed: shared gid is invalid", file=sys.stderr
        )
        return 78
    try:
        verify_shared_storage(mount_path, shared_gid)
    except SharedStorageContractError as exc:
        print(f"shared storage verification failed: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
