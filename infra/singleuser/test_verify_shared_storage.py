from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_shared_storage import (
    SharedStorageContractError,
    verify_shared_storage,
)


def mountinfo(path: Path) -> str:
    return f"36 25 0:32 / {path} rw,nosuid,nodev - ext4 /dev/test rw\n"


class SharedStorageContractTests(unittest.TestCase):
    def test_setgid_shared_root_supports_group_writes_and_cleans_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "shared"
            shared.mkdir()
            os.chmod(shared, 0o2770)
            before = set(shared.iterdir())
            old_umask = os.umask(0o002)
            try:
                verify_shared_storage(
                    str(shared),
                    os.getegid(),
                    expected_owner_uid=os.geteuid(),
                    mountinfo=mountinfo(shared),
                )
            finally:
                os.umask(old_umask)

            self.assertEqual(set(shared.iterdir()), before)
            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o2770)

    def test_mount_root_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "shared"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(SharedStorageContractError, "symlink"):
                verify_shared_storage(
                    str(link),
                    os.getegid(),
                    expected_owner_uid=os.geteuid(),
                    mountinfo=mountinfo(link),
                )

    def test_non_mount_and_permission_drift_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "shared"
            shared.mkdir(mode=0o770)
            os.chmod(shared, 0o770)

            with self.assertRaisesRegex(SharedStorageContractError, "distinct mount"):
                verify_shared_storage(
                    str(shared),
                    os.getegid(),
                    expected_owner_uid=os.geteuid(),
                    mountinfo="29 1 0:1 / / rw - ext4 /dev/test rw\n",
                )
            with self.assertRaisesRegex(SharedStorageContractError, "mode"):
                verify_shared_storage(
                    str(shared),
                    os.getegid(),
                    expected_owner_uid=os.geteuid(),
                    mountinfo=mountinfo(shared),
                )

    def test_missing_runtime_group_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "shared"
            shared.mkdir()
            os.chmod(shared, 0o2770)
            shared_gid = os.getegid()

            with (
                patch("verify_shared_storage.os.getegid", return_value=1234),
                patch("verify_shared_storage.os.getgroups", return_value=[]),
                self.assertRaisesRegex(SharedStorageContractError, "shared group"),
            ):
                verify_shared_storage(
                    str(shared),
                    shared_gid,
                    expected_owner_uid=os.geteuid(),
                    mountinfo=mountinfo(shared),
                )


if __name__ == "__main__":
    unittest.main()
