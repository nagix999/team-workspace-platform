from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

from ..domain import HubServerState
from .base import ApprovedProfile, HubAuthError, HubPrincipal, HubServer, OAuthToken


class FakeJupyterHubProvider:
    """Deterministic in-memory Hub used by service and API tests."""

    def __init__(
        self, *, public_url: str = "https://hub.example.net", auto_ready: bool = True
    ) -> None:
        self.public_url = public_url.rstrip("/")
        self.auto_ready = auto_ready
        self.tokens: dict[str, HubPrincipal] = {}
        self.codes: dict[str, str] = {}
        self.servers: dict[tuple[str, str], HubServer] = {}
        self.start_count = 0
        self.stop_count = 0
        self.remove_count = 0

    def register_login(
        self,
        username: str,
        *,
        code: str,
        token: str,
        scopes: tuple[str, ...] = ("servers!user",),
    ) -> None:
        self.codes[code] = token
        self.tokens[token] = HubPrincipal(username=username, scopes=scopes)

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return f"{self.public_url}/hub/api/oauth2/authorize?{urlencode({'state': state, 'code_challenge': code_challenge})}"

    async def exchange_code(self, *, code: str, pkce_verifier: str) -> OAuthToken:
        del pkce_verifier
        token = self.codes.pop(code, None)
        if token is None:
            raise HubAuthError("invalid or reused OAuth code")
        principal = self.tokens[token]
        return OAuthToken(
            access_token=token,
            expires_at=datetime.utcnow() + timedelta(hours=8),
            scopes=principal.scopes,
        )

    async def resolve_principal(self, user_oauth_token: str) -> HubPrincipal:
        try:
            return self.tokens[user_oauth_token]
        except KeyError as exc:
            raise HubAuthError("invalid delegated token") from exc

    async def request_start(
        self,
        principal: HubPrincipal,
        server_name: str,
        approved_profile: ApprovedProfile,
        spawn_ticket: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer:
        del approved_profile, spawn_ticket
        if await self.resolve_principal(user_oauth_token) != principal:
            raise HubAuthError("token owner mismatch")
        self.start_count += 1
        target = target_username or principal.username
        state = HubServerState.RUNNING if self.auto_ready else HubServerState.STARTING
        server = HubServer(
            state=state,
            ready=self.auto_ready,
            progress_percent=100 if self.auto_ready else 25,
            full_url=(
                f"https://{target}.hub.example.net/user/" f"{target}/{server_name}/"
                if self.auto_ready
                else None
            ),
            started_at=datetime.utcnow() if self.auto_ready else None,
        )
        self.servers[(target, server_name)] = server
        return server

    async def request_stop(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer:
        if await self.resolve_principal(user_oauth_token) != principal:
            raise HubAuthError("token owner mismatch")
        self.stop_count += 1
        server = HubServer(
            state=HubServerState.STOPPED, ready=False, progress_percent=100
        )
        self.servers[(target_username or principal.username, server_name)] = server
        return server

    async def request_remove(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer:
        if await self.resolve_principal(user_oauth_token) != principal:
            raise HubAuthError("token owner mismatch")
        self.remove_count += 1
        self.servers.pop((target_username or principal.username, server_name), None)
        return HubServer(state=HubServerState.NOT_FOUND, progress_percent=100)

    async def get_server(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer:
        if await self.resolve_principal(user_oauth_token) != principal:
            raise HubAuthError("token owner mismatch")
        return self.servers.get(
            (target_username or principal.username, server_name),
            HubServer(state=HubServerState.NOT_FOUND),
        )

    async def get_spawn_progress(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer:
        return await self.get_server(
            principal,
            server_name,
            user_oauth_token,
            target_username=target_username,
        )

    def complete_start(self, username: str, server_name: str) -> None:
        self.servers[(username, server_name)] = HubServer(
            state=HubServerState.RUNNING,
            ready=True,
            progress_percent=100,
            full_url=f"https://{username}.hub.example.net/user/{username}/{server_name}/",
            started_at=datetime.utcnow(),
        )

    async def aclose(self) -> None:
        return None
