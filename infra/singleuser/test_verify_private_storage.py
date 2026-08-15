from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_private_storage import (
    EXPECTED_PRIVATE_GID,
    EXPECTED_PRIVATE_MOUNT_PATH,
    EXPECTED_PRIVATE_UID,
    PrivateStorageContractError,
    private_runtime_contract,
    verify_private_storage,
)


def mountinfo(path: Path, *, writable: bool = True) -> str:
    metadata = path.stat()
    device = f"{os.major(metadata.st_dev)}:{os.minor(metadata.st_dev)}"
    options = "rw,nosuid,nodev" if writable else "ro,nosuid,nodev"
    return f"36 25 {device} / {path} {options} - ext4 /dev/test rw\n"


class PrivateStorageContractTests(unittest.TestCase):
    def test_normalizes_only_mount_root_and_preserves_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "work"
            private.mkdir()
            child = private / "notebook.py"
            child.write_bytes(b"print('preserve me')\n")
            os.chmod(child, 0o640)
            os.chmod(private, 0o6775)
            before = child.stat()

            verify_private_storage(
                str(private),
                os.geteuid(),
                os.getegid(),
                mountinfo=mountinfo(private),
            )

            after = child.stat()
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
            self.assertEqual(child.read_bytes(), b"print('preserve me')\n")
            self.assertEqual(after.st_ino, before.st_ino)
            self.assertEqual(after.st_uid, before.st_uid)
            self.assertEqual(after.st_gid, before.st_gid)
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
            self.assertEqual(after.st_size, before.st_size)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_non_mount_and_read_only_mount_fail_before_mode_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "work"
            private.mkdir()
            os.chmod(private, 0o775)

            with self.assertRaisesRegex(PrivateStorageContractError, "distinct mount"):
                verify_private_storage(
                    str(private),
                    os.geteuid(),
                    os.getegid(),
                    mountinfo="29 1 0:1 / / rw - ext4 /dev/test rw\n",
                )
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o775)

            with self.assertRaisesRegex(PrivateStorageContractError, "read-write"):
                verify_private_storage(
                    str(private),
                    os.geteuid(),
                    os.getegid(),
                    mountinfo=mountinfo(private, writable=False),
                )
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o775)

    def test_symlink_and_ownership_drift_fail_before_mode_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "target"
            private.mkdir()
            os.chmod(private, 0o775)
            link = root / "work"
            link.symlink_to(private, target_is_directory=True)

            with self.assertRaisesRegex(PrivateStorageContractError, "symlink"):
                verify_private_storage(
                    str(link),
                    os.geteuid(),
                    os.getegid(),
                    mountinfo=mountinfo(link),
                )

            drifted_uid = os.geteuid() + 1
            with (
                patch("verify_private_storage.os.geteuid", return_value=drifted_uid),
                self.assertRaisesRegex(PrivateStorageContractError, "ownership"),
            ):
                verify_private_storage(
                    str(private),
                    drifted_uid,
                    os.getegid(),
                    mountinfo=mountinfo(private),
                )
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o775)

    def test_process_identity_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "work"
            private.mkdir()
            expected_uid = os.geteuid()

            with (
                patch(
                    "verify_private_storage.os.geteuid",
                    return_value=expected_uid + 1,
                ),
                self.assertRaisesRegex(PrivateStorageContractError, "identity"),
            ):
                verify_private_storage(
                    str(private),
                    expected_uid,
                    os.getegid(),
                    mountinfo=mountinfo(private),
                )

    def test_runtime_contract_is_exact(self) -> None:
        environment = {
            "HOME": EXPECTED_PRIVATE_MOUNT_PATH,
            "PLATFORM_PRIVATE_MOUNT_PATH": EXPECTED_PRIVATE_MOUNT_PATH,
            "PLATFORM_PRIVATE_UID": str(EXPECTED_PRIVATE_UID),
            "PLATFORM_PRIVATE_GID": str(EXPECTED_PRIVATE_GID),
        }
        self.assertEqual(
            private_runtime_contract(environment),
            (
                EXPECTED_PRIVATE_MOUNT_PATH,
                EXPECTED_PRIVATE_UID,
                EXPECTED_PRIVATE_GID,
            ),
        )

        drifted_values = {
            "HOME": "/home/jovyan",
            "PLATFORM_PRIVATE_MOUNT_PATH": "/home/jovyan/other",
            "PLATFORM_PRIVATE_UID": "01000",
            "PLATFORM_PRIVATE_GID": "101",
        }
        for key, value in drifted_values.items():
            with self.subTest(key=key):
                drifted = environment.copy()
                drifted[key] = value
                with self.assertRaises(PrivateStorageContractError):
                    private_runtime_contract(drifted)


if __name__ == "__main__":
    unittest.main()
