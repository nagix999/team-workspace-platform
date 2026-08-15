from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


HOST_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOST_ROOT))

from provision_local_dev_volumes import (  # noqa: E402
    reserve_project_id_block,
)
from local_volume_policy import (  # noqa: E402
    ALLOCATION_REGISTRY_NAME,
    ALLOCATION_LOCK_NAME,
    atomic_json,
)
import local_volume_policy  # noqa: E402


def user_id(number: int) -> str:
    return str(uuid.UUID(int=number + 1))


def manifest(user_number: int, username: str, project_id_base: int) -> dict:
    owner_id = user_id(user_number)
    return {
        "schema_version": 1,
        "unsafe_local_dev": True,
        "user_id": owner_id,
        "username": username,
        "uid": 1000,
        "gid": 100,
        "slots": [
            {
                "slot_id": str(
                    uuid.uuid5(uuid.UUID(owner_id), f"workspace-volume-slot-{number}")
                ),
                "slot_number": number,
                "volume_name": f"jupyter-user-{username}-slot-{number}",
                "hard_limit_bytes": 1024,
                "project_id": project_id_base + number - 1,
            }
            for number in range(1, 6)
        ],
    }


class LocalProjectIdAllocationTests(unittest.TestCase):
    def test_host_cli_reexports_the_shared_allocator(self) -> None:
        self.assertIs(
            reserve_project_id_block, local_volume_policy.reserve_project_id_block
        )

    def test_sequential_users_get_distinct_blocks_and_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_output = root / f"local-user-{user_id(0)}.json"
            second_output = root / f"local-user-{user_id(1)}.json"
            first = reserve_project_id_block(
                output=first_output, user_id=user_id(0), username="alice"
            )
            second = reserve_project_id_block(
                output=second_output, user_id=user_id(1), username="bob"
            )
            retried = reserve_project_id_block(
                output=first_output, user_id=user_id(0), username="alice"
            )

            self.assertEqual((first, second, retried), (10000, 10005, 10000))

    def test_ten_concurrent_users_get_ten_distinct_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def allocate(number: int) -> int:
                return reserve_project_id_block(
                    output=root / f"local-user-{user_id(number)}.json",
                    user_id=user_id(number),
                    username=f"user{number}",
                )

            with ThreadPoolExecutor(max_workers=10) as executor:
                bases = list(executor.map(allocate, range(10)))

            self.assertEqual(sorted(bases), list(range(10000, 10050, 5)))
            registry = json.loads((root / ALLOCATION_REGISTRY_NAME).read_text())
            self.assertEqual(len(registry["allocations"]), 10)

    def test_existing_legacy_manifest_is_validated_imported_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / f"local-user-{user_id(0)}.json"
            output.write_text(json.dumps(manifest(0, "alice", 12000)))

            allocated = reserve_project_id_block(
                output=output,
                user_id=user_id(0),
                username="alice",
            )

            self.assertEqual(allocated, 12000)
            registry = json.loads((root / ALLOCATION_REGISTRY_NAME).read_text())
            self.assertEqual(registry["allocations"][0]["project_id_base"], 12000)

    def test_conflicting_existing_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / f"local-user-{user_id(0)}.json"
            value = manifest(0, "alice", 10000)
            value["slots"][1]["project_id"] = 10099
            output.write_text(json.dumps(value))

            with self.assertRaisesRegex(RuntimeError, "not one contiguous block"):
                reserve_project_id_block(
                    output=output,
                    user_id=user_id(0),
                    username="alice",
                )

    def test_registry_with_overlapping_blocks_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = {
                "schema_version": 1,
                "allocations": [
                    {
                        "user_id": user_id(0),
                        "username": "alice",
                        "project_id_base": 10000,
                    },
                    {
                        "user_id": user_id(1),
                        "username": "bob",
                        "project_id_base": 10002,
                    },
                ],
            }
            (root / ALLOCATION_REGISTRY_NAME).write_text(json.dumps(registry))

            with self.assertRaisesRegex(RuntimeError, "blocks overlap"):
                reserve_project_id_block(
                    output=root / f"local-user-{user_id(2)}.json",
                    user_id=user_id(2),
                    username="carol",
                )

    def test_shared_state_modes_allow_host_and_hub_group_interoperation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / f"local-user-{user_id(0)}.json"
            project_id_base = reserve_project_id_block(
                output=output,
                user_id=user_id(0),
                username="alice",
            )
            atomic_json(output, manifest(0, "alice", project_id_base))

            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o2770)
            for path in (
                root / ALLOCATION_LOCK_NAME,
                root / ALLOCATION_REGISTRY_NAME,
                output,
            ):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)
                self.assertEqual(path.stat().st_gid, root.stat().st_gid)

    def test_host_then_web_allocator_reuses_one_registry_without_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host_output = root / f"local-user-{user_id(0)}.json"
            host_base = reserve_project_id_block(
                output=host_output,
                user_id=user_id(0),
                username="alice",
            )
            atomic_json(host_output, manifest(0, "alice", host_base))

            web_output = root / f"local-user-{user_id(1)}.json"
            web_base = local_volume_policy.reserve_project_id_block(
                output=web_output,
                user_id=user_id(1),
                username="bob",
            )
            atomic_json(web_output, manifest(1, "bob", web_base))

            retried = reserve_project_id_block(
                output=host_output,
                user_id=user_id(0),
                username="alice",
            )
            self.assertEqual((host_base, web_base, retried), (10000, 10005, 10000))


if __name__ == "__main__":
    unittest.main()
