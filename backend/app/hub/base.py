from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..domain import HubServerState


class HubError(RuntimeError):
    pass


class HubAuthError(HubError):
    pass


class HubCapacityError(HubError):
    pass


class HubUnavailableError(HubError):
    pass


class HubRequestError(HubError):
    """A non-authentication Hub request rejection that must not be retried."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    expires_at: datetime
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class HubPrincipal:
    username: str
    scopes: tuple[str, ...]

    def can_manage_own_servers(self) -> bool:
        return any(
            scope == "servers!user" or scope.startswith("servers!user=")
            for scope in self.scopes
        )

    def can_admin_servers(self) -> bool:
        return "admin:servers" in self.scopes


@dataclass(frozen=True)
class ApprovedProfile:
    id: str
    version: int
    config_digest: str


@dataclass(frozen=True)
class HubResourceUsage:
    cpu_usage_millicores: int
    memory_usage_bytes: int
    memory_limit_bytes: int
    observed_at: datetime


@dataclass(frozen=True)
class HubServer:
    state: HubServerState
    ready: bool = False
    progress_percent: int | None = None
    full_url: str | None = None
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    failure_summary: str | None = None
    resource_usage: HubResourceUsage | None = None


class JupyterHubProvider(Protocol):
    def authorization_url(self, *, state: str, code_challenge: str) -> str: ...

    async def exchange_code(self, *, code: str, pkce_verifier: str) -> OAuthToken: ...

    async def resolve_principal(self, user_oauth_token: str) -> HubPrincipal: ...

    async def request_start(
        self,
        principal: HubPrincipal,
        server_name: str,
        approved_profile: ApprovedProfile,
        spawn_ticket: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer: ...

    async def request_stop(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer: ...

    async def request_remove(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer: ...

    async def get_server(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer: ...

    async def get_spawn_progress(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer: ...

    async def aclose(self) -> None: ...
