"""Least-privilege JupyterHub roles used by the platform services."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any


PLATFORM_API_SERVICE = "platform-api"
PLATFORM_RECONCILER_SERVICE = "platform-reconciler"
PLATFORM_ADMIN_LIFECYCLE_SERVICE = "platform-admin-lifecycle"


def validate_builtin_admin_browser_access(default_roles: list[dict[str, Any]]) -> None:
    """Fail startup if a Hub upgrade removes admin notebook-content access.

    Cross-user launch deliberately uses the administrator's Hub browser login,
    never the lifecycle service token.  JupyterHub's single-user OAuth client
    narrows this broad built-in scope to its own target server.
    """

    admin = next((role for role in default_roles if role.get("name") == "admin"), None)
    scopes = admin.get("scopes", []) if isinstance(admin, dict) else []
    if not isinstance(scopes, list) or "access:servers" not in scopes:
        raise RuntimeError(
            "JupyterHub built-in admin role lacks required browser server access"
        )


def validate_singleuser_browser_oauth_contract(spawner_class: type[Any]) -> None:
    """Fail if a single-user OAuth client can request broad browser scopes.

    An administrator may hold broad ``access:servers`` in their Hub browser
    session, but the OAuth token issued to one notebook origin must be narrowed
    to that exact owner/server.  This probes the installed DockerSpawner/JupyterHub
    implementation during config load so an upgrade cannot silently widen it.
    """

    username = "platform-admin-probe"
    server_name = "ws-0123456789abcdef0123456789abcdef"
    spawner = spawner_class(
        user=SimpleNamespace(name=username),
        orm_spawner=SimpleNamespace(name=server_name, server=None),
    )
    if spawner.oauth_client_allowed_scopes:
        raise RuntimeError(
            "single-user OAuth client has unexpected additional allowed scopes"
        )
    try:
        # JupyterHub 5.5 loads its config from initialize() while its main
        # asyncio loop is already running. asyncio.run() in that thread would
        # fail even though the scope contract is valid, so probe the installed
        # async method in one isolated short-lived thread/event loop.
        with ThreadPoolExecutor(max_workers=1) as executor:
            scopes = executor.submit(
                asyncio.run, spawner._get_oauth_client_allowed_scopes()
            ).result(timeout=5)
    except Exception as exc:
        raise RuntimeError("cannot validate single-user OAuth scope contract") from exc
    expected = [f"access:servers!server={username}/{server_name}"]
    if scopes != expected:
        raise RuntimeError("single-user OAuth client scope is not target-server exact")


def platform_load_roles() -> list[dict[str, Any]]:
    return [
        {
            # Override the built-in user role without broad service access.
            # `self` preserves JupyterHub's standard own-resource permissions.
            "name": "user",
            "description": "Standard user privileges with portal OAuth access",
            "scopes": [
                "self",
                f"access:services!service={PLATFORM_API_SERVICE}",
            ],
        },
        {
            "name": "platform-reconciler-read-only",
            "description": "Read users/server state; never start or stop servers",
            "scopes": ["list:users", "read:servers"],
            "services": [PLATFORM_RECONCILER_SERVICE],
        },
        {
            # This credential is mounted into the operation worker only.  It is
            # deliberately unable to read notebook contents, manage users or
            # mint/revoke tokens.  JupyterHub's admin:servers scope is the
            # narrow built-in scope that permits cross-user server lifecycle.
            "name": "platform-admin-lifecycle",
            "description": "Cross-user server lifecycle for audited admin operations",
            "scopes": ["admin:servers"],
            "services": [PLATFORM_ADMIN_LIFECYCLE_SERVICE],
        },
    ]
