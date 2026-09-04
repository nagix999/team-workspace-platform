from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import resource_usage  # noqa: E402
from resource_usage import (  # noqa: E402
    MAX_SIGNED_64,
    ResourceUsage,
    ResourceUsageError,
    collect_docker_resource_usage,
    collect_resource_usage_batch,
    collect_spawner_resource_usage,
    public_resource_usage_item,
    public_resource_usage_snapshot,
    resource_usage_candidates,
    resource_usage_from_docker_stats,
    validate_managed_container,
)
from rbac_policy import (  # noqa: E402
    PLATFORM_RECONCILER_SERVICE,
    platform_load_roles,
)


CONTAINER_ID = "a" * 64
OTHER_CONTAINER_ID = "b" * 64
USERNAME = "alice"
SERVER_NAME = "ws-0123456789abcdef0123456789abcdef"


def docker_stats() -> dict[str, Any]:
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 300},
            "system_cpu_usage": 1_000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100},
            "system_cpu_usage": 500,
        },
        "memory_stats": {
            "usage": 1_000_000,
            "limit": 2_000_000,
            "stats": {"inactive_file": 100_000},
        },
    }


def managed_inspect() -> dict[str, Any]:
    return {
        "Id": CONTAINER_ID,
        "State": {"Running": True},
        "Config": {
            "Labels": {
                "platform.managed": "true",
                "platform.kind": "jupyter-singleuser",
                "platform.username": USERNAME,
                "platform.server_name": SERVER_NAME,
                "platform.workspace_id": "workspace-12345678",
                "platform.spawn_authorization_id": "authorization-12345678",
                "platform.profile": "python312-cpu1-mem1024@1",
                "platform.profile_digest": "sha256:" + "c" * 64,
                "unrelated.image.label": "allowed",
            }
        },
    }


class ResourceUsageCalculationTests(unittest.TestCase):
    def test_cpu_millicores_and_cgroup_v2_inactive_file(self) -> None:
        usage = resource_usage_from_docker_stats(docker_stats())

        self.assertEqual(
            usage,
            ResourceUsage(
                cpu_usage_millicores=800,
                memory_usage_bytes=900_000,
                memory_limit_bytes=2_000_000,
            ),
        )

    def test_cpu_integer_rounding_and_percpu_fallback(self) -> None:
        value = docker_stats()
        value["cpu_stats"]["cpu_usage"]["total_usage"] = 101
        value["cpu_stats"]["system_cpu_usage"] = 503
        del value["cpu_stats"]["online_cpus"]
        value["cpu_stats"]["cpu_usage"]["percpu_usage"] = [1, 1]

        usage = resource_usage_from_docker_stats(value)

        self.assertEqual(usage.cpu_usage_millicores, 667)

        zero_online = docker_stats()
        zero_online["cpu_stats"]["online_cpus"] = 0
        zero_online["cpu_stats"]["cpu_usage"]["percpu_usage"] = [1, 1, 1, 1]
        self.assertEqual(
            resource_usage_from_docker_stats(zero_online).cpu_usage_millicores,
            1_600,
        )

    def test_cgroup_v1_and_missing_cache_counters(self) -> None:
        v1 = docker_stats()
        v1["memory_stats"]["stats"] = {"total_inactive_file": 250_000}
        self.assertEqual(
            resource_usage_from_docker_stats(v1).memory_usage_bytes,
            750_000,
        )

        no_detail = docker_stats()
        del no_detail["memory_stats"]["stats"]
        self.assertEqual(
            resource_usage_from_docker_stats(no_detail).memory_usage_bytes,
            1_000_000,
        )

    def test_cgroup_v2_counter_takes_precedence_if_both_exist(self) -> None:
        value = docker_stats()
        value["memory_stats"]["stats"] = {
            "inactive_file": 100_000,
            "total_inactive_file": 400_000,
        }

        self.assertEqual(
            resource_usage_from_docker_stats(value).memory_usage_bytes,
            900_000,
        )

    def test_malformed_and_overflowing_stats_fail_closed(self) -> None:
        cases: list[tuple[str, Any]] = []

        malformed_root = []
        cases.append(("root", malformed_root))

        boolean_counter = docker_stats()
        boolean_counter["cpu_stats"]["cpu_usage"]["total_usage"] = True
        cases.append(("boolean counter", boolean_counter))

        overflowing_counter = docker_stats()
        overflowing_counter["cpu_stats"]["cpu_usage"][
            "total_usage"
        ] = MAX_SIGNED_64 + 1
        cases.append(("counter overflow", overflowing_counter))

        regressed_cpu = docker_stats()
        regressed_cpu["cpu_stats"]["cpu_usage"]["total_usage"] = 99
        cases.append(("CPU regression", regressed_cpu))

        zero_interval = docker_stats()
        zero_interval["cpu_stats"]["system_cpu_usage"] = 500
        cases.append(("zero system interval", zero_interval))

        excessive_cpu = docker_stats()
        excessive_cpu["cpu_stats"]["cpu_usage"]["total_usage"] = 1_000
        excessive_cpu["cpu_stats"]["system_cpu_usage"] = 501
        cases.append(("CPU host overflow", excessive_cpu))

        invalid_cpu_count = docker_stats()
        invalid_cpu_count["cpu_stats"]["online_cpus"] = True
        cases.append(("CPU count", invalid_cpu_count))

        malformed_memory_detail = docker_stats()
        malformed_memory_detail["memory_stats"]["stats"] = None
        cases.append(("memory detail", malformed_memory_detail))

        excessive_inactive = docker_stats()
        excessive_inactive["memory_stats"]["stats"][
            "inactive_file"
        ] = 1_000_001
        cases.append(("inactive memory", excessive_inactive))

        over_limit = docker_stats()
        over_limit["memory_stats"].update(
            {"usage": 2_000_001, "limit": 2_000_000, "stats": {}}
        )
        cases.append(("memory limit", over_limit))

        invalid_limit = docker_stats()
        invalid_limit["memory_stats"]["limit"] = 0
        cases.append(("zero limit", invalid_limit))

        for label, value in cases:
            with self.subTest(label=label), self.assertRaises(ResourceUsageError):
                resource_usage_from_docker_stats(value)

    def test_public_snapshot_has_only_the_reviewed_json_contract(self) -> None:
        item = public_resource_usage_item(
            USERNAME,
            SERVER_NAME,
            ResourceUsage(125, 1_024, 2_048),
        )
        snapshot = public_resource_usage_snapshot(
            captured_at="2026-09-04T12:34:56Z", items=[item]
        )

        self.assertEqual(
            snapshot,
            {
                "schema_version": 1,
                "captured_at": "2026-09-04T12:34:56Z",
                "items": [
                    {
                        "username": USERNAME,
                        "server_name": SERVER_NAME,
                        "cpu_usage_millicores": 125,
                        "memory_usage_bytes": 1_024,
                        "memory_limit_bytes": 2_048,
                    }
                ],
            },
        )
        serialized = repr(snapshot)
        self.assertNotIn(CONTAINER_ID, serialized)
        self.assertNotIn("Labels", serialized)


class ManagedContainerValidationTests(unittest.TestCase):
    def test_accepts_exact_persisted_id_and_managed_labels(self) -> None:
        self.assertEqual(
            validate_managed_container(
                managed_inspect(),
                expected_container_id=CONTAINER_ID,
                username=USERNAME,
                server_name=SERVER_NAME,
            ),
            CONTAINER_ID,
        )

    def test_rejects_replacement_id_and_label_spoofing(self) -> None:
        cases: list[tuple[str, dict[str, Any], str]] = []

        replacement = managed_inspect()
        replacement["Id"] = OTHER_CONTAINER_ID
        cases.append(("replacement id", replacement, CONTAINER_ID))

        not_running = managed_inspect()
        not_running["State"]["Running"] = False
        cases.append(("not running", not_running, CONTAINER_ID))

        for label_name in (
            "platform.managed",
            "platform.kind",
            "platform.username",
            "platform.server_name",
            "platform.spawn_authorization_id",
            "platform.profile",
            "platform.profile_digest",
        ):
            spoofed = managed_inspect()
            spoofed["Config"]["Labels"][label_name] = "spoofed"
            cases.append((label_name, spoofed, CONTAINER_ID))

        bad_workspace = managed_inspect()
        bad_workspace["Config"]["Labels"]["platform.workspace_id"] = "short"
        cases.append(("workspace id", bad_workspace, CONTAINER_ID))

        for label, inspected, expected_id in cases:
            with self.subTest(label=label), self.assertRaises(ResourceUsageError):
                validate_managed_container(
                    inspected,
                    expected_container_id=expected_id,
                    username=USERNAME,
                    server_name=SERVER_NAME,
                )

        with self.assertRaises(ResourceUsageError):
            validate_managed_container(
                managed_inspect(),
                expected_container_id=CONTAINER_ID,
                username=USERNAME,
                server_name="default-server",
            )


class ResourceUsageCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_restored_ready_spawner_needs_no_transient_profile_marker(
        self,
    ) -> None:
        class RestoredSpawner:
            object_id = CONTAINER_ID
            ready = True

        spawner = RestoredSpawner()
        self.assertFalse(hasattr(spawner, "_platform_profile"))
        calls: list[tuple[str, str]] = []

        async def collect(**kwargs: Any) -> ResourceUsage:
            self.assertIs(kwargs["spawner"], spawner)
            self.assertEqual(kwargs["expected_container_id"], CONTAINER_ID)
            calls.append((kwargs["username"], kwargs["server_name"]))
            return resource_usage_from_docker_stats(docker_stats())

        usage = await collect_spawner_resource_usage(
            username=USERNAME,
            server_name=SERVER_NAME,
            spawner=spawner,
            docker_collector=collect,
        )

        self.assertEqual(usage.memory_limit_bytes, 2_000_000)
        self.assertEqual(calls, [(USERNAME, SERVER_NAME)])

    async def test_dedicated_client_inspects_exact_id_collects_and_closes(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[Any, ...]] = []

            def inspect_container(self, container_id: str) -> dict[str, Any]:
                self.calls.append(("inspect", container_id))
                return managed_inspect()

            def stats(self, container_id: str, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(("stats", container_id, kwargs))
                return docker_stats()

            def close(self) -> None:
                self.calls.append(("close",))

        client = FakeClient()
        spawner = object()

        def client_factory(candidate: Any) -> FakeClient:
            self.assertIs(candidate, spawner)
            return client

        usage = await collect_docker_resource_usage(
            username=USERNAME,
            server_name=SERVER_NAME,
            spawner=spawner,
            expected_container_id=CONTAINER_ID,
            client_factory=client_factory,
        )

        self.assertEqual(usage.cpu_usage_millicores, 800)
        self.assertEqual(
            client.calls,
            [
                ("inspect", CONTAINER_ID),
                (
                    "stats",
                    CONTAINER_ID,
                    {"stream": False, "one_shot": False},
                ),
                ("close",),
            ],
        )

    async def test_missing_container_is_an_item_level_miss(self) -> None:
        class MissingSpawner:
            object_id = CONTAINER_ID
            ready = True

        async def missing(**kwargs: Any) -> None:
            return None

        self.assertIsNone(
            await collect_spawner_resource_usage(
                username=USERNAME,
                server_name=SERVER_NAME,
                spawner=MissingSpawner(),
                docker_collector=missing,
            )
        )

    async def test_non_ready_spawner_is_never_inspected(self) -> None:
        class NonReadySpawner:
            object_id = CONTAINER_ID
            ready = False

            async def get_object(self) -> dict[str, Any]:
                raise AssertionError("non-ready spawner was inspected")

        with self.assertRaisesRegex(ResourceUsageError, "not ready"):
            await collect_spawner_resource_usage(
                username=USERNAME,
                server_name=SERVER_NAME,
                spawner=NonReadySpawner(),
            )

    async def test_collection_timeout_is_sanitized(self) -> None:
        class SlowSpawner:
            object_id = CONTAINER_ID
            ready = True

        async def slow(**kwargs: Any) -> ResourceUsage:
            await asyncio.sleep(60)
            return ResourceUsage(0, 0, 1)

        with (
            patch.object(resource_usage, "DOCKER_COLLECTION_TIMEOUT_SECONDS", 0.001),
            self.assertRaisesRegex(ResourceUsageError, "metrics are unavailable"),
        ):
            await collect_spawner_resource_usage(
                username=USERNAME,
                server_name=SERVER_NAME,
                spawner=SlowSpawner(),
                docker_collector=slow,
            )

    async def test_batch_is_concurrent_bounded_ordered_and_partial(self) -> None:
        candidates = [
            (USERNAME, f"{SERVER_NAME}-{index}", object()) for index in range(5)
        ]
        active = 0
        maximum_active = 0

        async def collector(
            *, username: str, server_name: str, spawner: Any
        ) -> ResourceUsage | None:
            nonlocal active, maximum_active
            self.assertEqual(username, USERNAME)
            self.assertIsNotNone(spawner)
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0.01)
                if server_name.endswith("-2"):
                    raise RuntimeError("sensitive Docker error")
                index = int(server_name.rsplit("-", 1)[1])
                return ResourceUsage(index, 100 + index, 1_000)
            finally:
                active -= 1

        results = await collect_resource_usage_batch(
            candidates, collector=collector, max_concurrency=2
        )

        self.assertEqual(maximum_active, 2)
        self.assertEqual(
            [None if result is None else result.cpu_usage_millicores for result in results],
            [0, 1, None, 3, 4],
        )

    async def test_dedicated_docker_workers_are_actually_concurrent(self) -> None:
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        class FakeClient:
            def __init__(self, server_name: str) -> None:
                self.server_name = server_name

            def inspect_container(self, container_id: str) -> dict[str, Any]:
                inspected = managed_inspect()
                inspected["Config"]["Labels"][
                    "platform.server_name"
                ] = self.server_name
                return inspected

            def stats(self, container_id: str, **kwargs: Any) -> dict[str, Any]:
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.02)
                    return docker_stats()
                finally:
                    with lock:
                        active -= 1

            def close(self) -> None:
                return None

        async def collector(
            *, username: str, server_name: str, spawner: Any
        ) -> ResourceUsage | None:
            async def docker_collector(**kwargs: Any) -> ResourceUsage | None:
                return await collect_docker_resource_usage(
                    **kwargs,
                    client_factory=lambda candidate: FakeClient(server_name),
                )

            return await collect_spawner_resource_usage(
                username=username,
                server_name=server_name,
                spawner=spawner,
                docker_collector=docker_collector,
            )

        candidates = [
            (
                USERNAME,
                f"{SERVER_NAME}-{index}",
                type(
                    "ReadySpawner",
                    (),
                    {"ready": True, "object_id": CONTAINER_ID},
                )(),
            )
            for index in range(4)
        ]

        # The command sandbox may suppress the selector's cross-thread wakeup;
        # a short test-only heartbeat lets the loop observe completed executor
        # futures without waiting for the production timeout timer.
        batch = asyncio.create_task(
            collect_resource_usage_batch(
                candidates, collector=collector, max_concurrency=2
            )
        )
        while not batch.done():
            await asyncio.sleep(0.005)
        results = await batch

        self.assertEqual(maximum_active, 2)
        self.assertTrue(all(isinstance(result, ResourceUsage) for result in results))

    async def test_batch_rejects_invalid_concurrency(self) -> None:
        for value in (True, 0, 257):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await collect_resource_usage_batch([], max_concurrency=value)


class ResourceUsageWiringContractTests(unittest.TestCase):
    def test_hub_integer_user_map_keys_never_replace_public_username(self) -> None:
        ready_spawner = type("ReadySpawner", (), {"ready": True})()
        stopped_spawner = type("StoppedSpawner", (), {"ready": False})()
        users = {
            42: type(
                "HubUser",
                (),
                {
                    "name": USERNAME,
                    "spawners": {
                        SERVER_NAME: ready_spawner,
                        "stopped": stopped_spawner,
                    },
                },
            )()
        }

        candidates = resource_usage_candidates(users)

        self.assertEqual(candidates, [(USERNAME, SERVER_NAME, ready_spawner)])
        self.assertNotEqual(candidates[0][0], "42")

    def test_reconciler_role_and_handler_are_exactly_service_scoped(self) -> None:
        roles = {role["name"]: role for role in platform_load_roles()}
        role = roles["platform-reconciler-read-only"]
        self.assertEqual(
            role,
            {
                "name": "platform-reconciler-read-only",
                "description": "Read users/server state; never start or stop servers",
                "scopes": ["list:users", "read:servers"],
                "services": [PLATFORM_RECONCILER_SERVICE],
            },
        )

        handler = (ROOT / "resource_usage_handler.py").read_text(encoding="utf-8")
        self.assertIn('@needs_scope("read:servers")', handler)
        self.assertIn('principal.name != PLATFORM_RECONCILER_SERVICE', handler)
        self.assertIn('isinstance(principal, orm.Service)', handler)
        self.assertIn('"_token_authenticated", False) is not True', handler)
        self.assertIn("_accept_cookie_auth = False", handler)
        self.assertIn("_accept_token_auth = True", handler)
        self.assertIn(
            '(r"/api/platform/resource-usage", PlatformResourceUsageAPIHandler)',
            handler,
        )
        self.assertNotIn("_platform_profile", handler)

    def test_hub_image_and_config_install_the_private_handler(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        config = (ROOT / "jupyterhub_config.py").read_text(encoding="utf-8")

        self.assertIn("resource_usage.py resource_usage_handler.py", dockerfile)
        self.assertIn(
            "from resource_usage_handler import RESOURCE_USAGE_HANDLERS", config
        )
        self.assertIn(
            "c.JupyterHub.extra_handlers = RESOURCE_USAGE_HANDLERS", config
        )


if __name__ == "__main__":
    unittest.main()
