"""Fail-closed resource usage collection for managed single-user containers.

The portal API must not receive the Docker socket.  JupyterHub already owns the
socket for DockerSpawner, so its private, service-only endpoint uses these pure
helpers to turn Docker's cumulative counters into a small metrics snapshot.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any

import docker
from docker.utils import kwargs_from_env


MAX_CPU_COUNT = 4096
MAX_SIGNED_64 = (1 << 63) - 1
DOCKER_CALL_TIMEOUT_SECONDS = 4.0
DOCKER_COLLECTION_TIMEOUT_SECONDS = 12.0
MAX_CONCURRENT_DOCKER_CALLS = 32
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
USERNAME_RE = re.compile(r"^(?!.*--)[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")
SERVER_NAME_RE = re.compile(r"^ws-[a-z0-9](?:[a-z0-9-]{6,61}[a-z0-9])$")
PROFILE_BINDING_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}@[1-9][0-9]{0,9}$")
SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_stats_executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_DOCKER_CALLS,
    thread_name_prefix="platform-resource-stats",
)


class ResourceUsageError(RuntimeError):
    """A non-sensitive resource metrics collection failure."""


@dataclass(frozen=True)
class ResourceUsage:
    cpu_usage_millicores: int
    memory_usage_bytes: int
    memory_limit_bytes: int


def public_resource_usage_item(
    username: str, server_name: str, usage: ResourceUsage
) -> dict[str, Any]:
    """Serialize only the public, backend-reviewed item contract."""

    return {
        "username": username,
        "server_name": server_name,
        "cpu_usage_millicores": usage.cpu_usage_millicores,
        "memory_usage_bytes": usage.memory_usage_bytes,
        "memory_limit_bytes": usage.memory_limit_bytes,
    }


def public_resource_usage_snapshot(
    *, captured_at: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Serialize the exact aggregate contract without Docker metadata."""

    return {"schema_version": 1, "captured_at": captured_at, "items": items}


def _counter(value: Any, where: str, *, positive: bool = False) -> int:
    if (
        type(value) is not int
        or value < (1 if positive else 0)
        or value > MAX_SIGNED_64
    ):
        raise ResourceUsageError(f"Docker {where} counter is invalid")
    return value


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResourceUsageError(f"Docker {where} metrics are invalid")
    return value


def resource_usage_from_docker_stats(value: Any) -> ResourceUsage:
    """Normalize one non-streaming Docker stats response.

    CPU is reported as millicores, following the Docker CLI convention of
    scaling the container/system counter delta by the daemon's online CPU
    count. Memory excludes inactive file cache when Docker exposes that cgroup
    counter, also matching the value operators normally see in ``docker stats``.
    """

    root = _mapping(value, "root")
    cpu = _mapping(root.get("cpu_stats"), "CPU")
    previous_cpu = _mapping(root.get("precpu_stats"), "previous CPU")
    cpu_usage = _mapping(cpu.get("cpu_usage"), "CPU usage")
    previous_cpu_usage = _mapping(
        previous_cpu.get("cpu_usage"), "previous CPU usage"
    )
    total = _counter(cpu_usage.get("total_usage"), "CPU total")
    previous_total = _counter(
        previous_cpu_usage.get("total_usage"), "previous CPU total"
    )
    system = _counter(cpu.get("system_cpu_usage"), "system CPU")
    previous_system = _counter(
        previous_cpu.get("system_cpu_usage"), "previous system CPU"
    )
    if total < previous_total or system <= previous_system:
        raise ResourceUsageError("Docker CPU sample interval is invalid")

    online_cpus = cpu.get("online_cpus")
    if online_cpus is not None and type(online_cpus) is not int:
        raise ResourceUsageError("Docker online CPU count is invalid")
    if online_cpus is None or online_cpus == 0:
        per_cpu = cpu_usage.get("percpu_usage")
        if not isinstance(per_cpu, list):
            raise ResourceUsageError("Docker online CPU count is unavailable")
        online_cpus = len(per_cpu)
    if not 1 <= online_cpus <= MAX_CPU_COUNT:
        raise ResourceUsageError("Docker online CPU count is invalid")

    cpu_delta = total - previous_total
    system_delta = system - previous_system
    # Integer rounding keeps the public contract deterministic and avoids
    # serializing host-dependent floating point artifacts.
    cpu_usage_millicores = (
        cpu_delta * online_cpus * 1000 + system_delta // 2
    ) // system_delta
    if not 0 <= cpu_usage_millicores <= online_cpus * 1000:
        raise ResourceUsageError("Docker CPU utilization is outside host bounds")

    memory = _mapping(root.get("memory_stats"), "memory")
    raw_memory_usage = _counter(memory.get("usage"), "memory usage")
    memory_limit = _counter(memory.get("limit"), "memory limit", positive=True)
    inactive_file = 0
    if "stats" in memory:
        memory_detail = memory["stats"]
        if not isinstance(memory_detail, dict):
            raise ResourceUsageError("Docker memory detail metrics are invalid")
        # cgroup v2 exposes inactive_file; v1 commonly exposes
        # total_inactive_file. Prefer the v2 key if both happen to exist.
        cache_value = memory_detail.get(
            "inactive_file", memory_detail.get("total_inactive_file", 0)
        )
        inactive_file = _counter(cache_value, "inactive file")
        if inactive_file > raw_memory_usage:
            raise ResourceUsageError("Docker inactive memory exceeds usage")
    memory_usage = raw_memory_usage - inactive_file
    if memory_usage > memory_limit:
        raise ResourceUsageError("Docker memory usage exceeds its limit")
    return ResourceUsage(
        cpu_usage_millicores=cpu_usage_millicores,
        memory_usage_bytes=memory_usage,
        memory_limit_bytes=memory_limit,
    )


def validate_managed_container(
    inspected: Any,
    *,
    expected_container_id: str,
    username: str,
    server_name: str,
) -> str:
    """Return the immutable container ID only for the expected managed server."""

    root = _mapping(inspected, "inspect")
    container_id = root.get("Id")
    state = root.get("State")
    config = root.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        not isinstance(expected_container_id, str)
        or not CONTAINER_ID_RE.fullmatch(expected_container_id)
        or not isinstance(username, str)
        or not USERNAME_RE.fullmatch(username)
        or not isinstance(server_name, str)
        or not SERVER_NAME_RE.fullmatch(server_name)
        or not isinstance(container_id, str)
        or not CONTAINER_ID_RE.fullmatch(container_id)
        or container_id != expected_container_id
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or not isinstance(labels, dict)
        or labels.get("platform.managed") != "true"
        or labels.get("platform.kind") != "jupyter-singleuser"
        or labels.get("platform.username") != username
        or labels.get("platform.server_name") != server_name
        or not isinstance(labels.get("platform.workspace_id"), str)
        or not OPAQUE_ID_RE.fullmatch(labels["platform.workspace_id"])
        or not isinstance(labels.get("platform.spawn_authorization_id"), str)
        or not OPAQUE_ID_RE.fullmatch(labels["platform.spawn_authorization_id"])
        or not isinstance(labels.get("platform.profile"), str)
        or not PROFILE_BINDING_RE.fullmatch(labels["platform.profile"])
        or not isinstance(labels.get("platform.profile_digest"), str)
        or not SHA256_DIGEST_RE.fullmatch(labels["platform.profile_digest"])
    ):
        raise ResourceUsageError("Docker managed-container identity is invalid")
    return container_id


DockerClientFactory = Callable[[Any], Any]


def _new_stats_client(spawner: Any) -> Any:
    """Create one short-lived Docker client for a concurrent stats worker."""

    tls_config = getattr(spawner, "tls_config", {})
    client_kwargs = getattr(spawner, "client_kwargs", {})
    if not isinstance(tls_config, dict) or not isinstance(client_kwargs, dict):
        raise ResourceUsageError("Docker client configuration is invalid")
    kwargs: dict[str, Any] = {"version": "auto"}
    if tls_config:
        kwargs["tls"] = docker.tls.TLSConfig(**tls_config)
    kwargs.update(kwargs_from_env())
    kwargs.update(client_kwargs)
    # Do not let an unrelated DockerSpawner setting weaken this endpoint's
    # per-call bound. The aggregate await below adds a separate total bound.
    kwargs["timeout"] = DOCKER_CALL_TIMEOUT_SECONDS
    return docker.APIClient(**kwargs)


def _collect_docker_resource_usage_sync(
    *,
    username: str,
    server_name: str,
    spawner: Any,
    expected_container_id: str,
    client_factory: DockerClientFactory,
) -> ResourceUsage | None:
    client = client_factory(spawner)
    try:
        inspected = client.inspect_container(expected_container_id)
        if inspected is None:
            return None
        container_id = validate_managed_container(
            inspected,
            expected_container_id=expected_container_id,
            username=username,
            server_name=server_name,
        )
        stats = client.stats(
            container_id,
            stream=False,
            # The daemon waits for two samples so precpu_stats is a real
            # interval instead of an empty first sample.
            one_shot=False,
        )
        return resource_usage_from_docker_stats(stats)
    finally:
        client.close()


async def collect_docker_resource_usage(
    *,
    username: str,
    server_name: str,
    spawner: Any,
    expected_container_id: str,
    client_factory: DockerClientFactory = _new_stats_client,
) -> ResourceUsage | None:
    """Run inspect+stats on a dedicated client outside Hub's event loop.

    DockerSpawner deliberately serializes lifecycle calls through one global
    single-thread executor. A short-lived client per stats worker provides real
    bounded concurrency without widening or interfering with that executor.
    """

    call = partial(
        _collect_docker_resource_usage_sync,
        username=username,
        server_name=server_name,
        spawner=spawner,
        expected_container_id=expected_container_id,
        client_factory=client_factory,
    )
    return await asyncio.get_running_loop().run_in_executor(_stats_executor, call)


DockerUsageCollector = Callable[..., Awaitable[ResourceUsage | None]]


async def collect_spawner_resource_usage(
    *,
    username: str,
    server_name: str,
    spawner: Any,
    docker_collector: DockerUsageCollector = collect_docker_resource_usage,
) -> ResourceUsage | None:
    """Collect one ready spawner without leaking a Docker/container identity."""

    try:
        if getattr(spawner, "ready", False) is not True:
            raise ResourceUsageError("Docker managed spawner is not ready")
        # DockerSpawner persists object_id in the Hub database, including
        # across Hub restarts. Inspect that exact ID rather than resolving the
        # mutable container name, so a replacement cannot authenticate itself
        # with copied labels.
        expected_container_id = getattr(spawner, "object_id", None)
        if (
            not isinstance(expected_container_id, str)
            or not CONTAINER_ID_RE.fullmatch(expected_container_id)
        ):
            raise ResourceUsageError("Docker managed-container identity is invalid")
        return await asyncio.wait_for(
            docker_collector(
                username=username,
                server_name=server_name,
                spawner=spawner,
                expected_container_id=expected_container_id,
            ),
            timeout=DOCKER_COLLECTION_TIMEOUT_SECONDS,
        )
    except ResourceUsageError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ResourceUsageError("Docker resource metrics are unavailable") from exc


ResourceUsageCollector = Callable[..., Awaitable[ResourceUsage | None]]


async def collect_resource_usage_batch(
    candidates: Sequence[tuple[str, str, Any]],
    *,
    collector: ResourceUsageCollector = collect_spawner_resource_usage,
    max_concurrency: int = MAX_CONCURRENT_DOCKER_CALLS,
) -> list[ResourceUsage | None]:
    """Collect an ordered snapshot with bounded Docker concurrency.

    An inspect/stats failure is local to one item. Cancellation of the request
    itself still propagates so JupyterHub can promptly abandon disconnected or
    shutting-down requests.
    """

    if type(max_concurrency) is not int or not 1 <= max_concurrency <= 256:
        raise ValueError("resource usage concurrency is invalid")
    semaphore = asyncio.Semaphore(max_concurrency)

    async def collect_one(
        username: str, server_name: str, spawner: Any
    ) -> ResourceUsage | None:
        async with semaphore:
            try:
                return await collector(
                    username=username,
                    server_name=server_name,
                    spawner=spawner,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The endpoint exposes neither Docker error text nor a partial
                # object. The caller records only a non-sensitive miss count.
                return None

    return list(
        await asyncio.gather(
            *(
                collect_one(username, server_name, spawner)
                for username, server_name, spawner in candidates
            )
        )
    )
# JupyterHub's UserDict is keyed by integer ORM user ID in 5.5.  Keep the
# traversal helper dependency-free so its public-name contract is unit tested
# without importing the full Hub application.
def resource_usage_candidates(users: Any) -> list[tuple[str, str, Any]]:
    candidates: list[tuple[str, str, Any]] = []
    for user in sorted(users.values(), key=lambda candidate: candidate.name):
        username = user.name
        for server_name, spawner in sorted(user.spawners.items()):
            if getattr(spawner, "ready", False) is True:
                candidates.append((username, server_name, spawner))
    return candidates
