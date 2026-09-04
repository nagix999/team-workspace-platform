"""Private JupyterHub API for reconciler-only Docker resource snapshots."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from jupyterhub import orm
from jupyterhub.apihandlers.base import APIHandler
from jupyterhub.scopes import needs_scope
from tornado import web

from resource_usage import (
    collect_resource_usage_batch,
    public_resource_usage_item,
    public_resource_usage_snapshot,
    resource_usage_candidates,
)
from rbac_policy import PLATFORM_RECONCILER_SERVICE


MAX_RUNNING_WORKSPACES = 1000
_snapshot_lock = asyncio.Lock()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PlatformResourceUsageAPIHandler(APIHandler):
    """Return a bounded snapshot to the exact read-only reconciler service.

    ``read:servers`` alone is intentionally insufficient: filtered user tokens
    can hold that scope for their own server.  The explicit ORM service identity
    check prevents this aggregate endpoint from becoming a cross-user side
    channel.
    """

    _accept_cookie_auth = False
    _accept_token_auth = True

    @needs_scope("read:servers")
    async def get(self) -> None:
        principal = self.current_user
        if (
            getattr(self, "_token_authenticated", False) is not True
            or not isinstance(principal, orm.Service)
            or principal.name != PLATFORM_RECONCILER_SERVICE
        ):
            raise web.HTTPError(403)

        candidates = resource_usage_candidates(self.users)
        if len(candidates) > MAX_RUNNING_WORKSPACES:
            raise web.HTTPError(503, "resource snapshot exceeds platform bound")

        async with _snapshot_lock:
            results = await collect_resource_usage_batch(candidates)

        items: list[dict[str, Any]] = []
        unavailable = 0
        for (username, server_name, _spawner), result in zip(candidates, results):
            if result is None:
                unavailable += 1
                continue
            items.append(public_resource_usage_item(username, server_name, result))

        if unavailable:
            # Do not log Docker exception text or container identifiers. The
            # backend derives exactly which running records lack a fresh sample.
            self.log.warning(
                "resource usage unavailable for %d of %d managed workspaces",
                unavailable,
                len(candidates),
            )
        self.set_header("Cache-Control", "no-store")
        self.set_header("Content-Type", "application/json")
        self.write(
            json.dumps(
                public_resource_usage_snapshot(
                    captured_at=_timestamp(), items=items
                ),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )


RESOURCE_USAGE_HANDLERS = [
    (r"/api/platform/resource-usage", PlatformResourceUsageAPIHandler),
]
