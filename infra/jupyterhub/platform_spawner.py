"""Spawner lifecycle wrapper that minimizes Hub-side environment plaintext."""

from __future__ import annotations

from typing import Any

from spawn_guard import clear_user_environment


class PlatformDockerSpawnerMixin:
    """Clear user environment snapshots at every Docker start boundary.

    JupyterHub 5.5 has no ``post_spawn_hook``.  DockerSpawner's
    ``create_object`` is the last point at which the effective environment must
    remain available to the Hub: after that call Docker's immutable container
    configuration owns the runtime copy.  ``start`` supplies a second, broader
    ``finally`` guard for image-pull, stale-object removal, start, and address
    discovery failures that occur outside ``create_object``.
    """

    async def create_object(self) -> Any:
        try:
            return await super().create_object()
        finally:
            clear_user_environment(self)

    async def start(self) -> Any:
        try:
            return await super().start()
        finally:
            clear_user_environment(self)
