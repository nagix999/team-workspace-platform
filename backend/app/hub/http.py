from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

import httpx

from ..config import Settings
from ..domain import HubServerState
from .base import (
    ApprovedProfile,
    HubAuthError,
    HubCapacityError,
    HubPrincipal,
    HubRequestError,
    HubServer,
    HubUnavailableError,
    OAuthToken,
)


_MAX_PROGRESS_STREAM_CHARS = 64 * 1024
_MAX_PROGRESS_EVENTS = 128


class _ProgressStreamGone(RuntimeError):
    """The spawn resolved between the model read and progress request."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"progress stream returned HTTP {status_code}")
        self.status_code = status_code


def _parse_hub_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _progress_percent(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return max(0, min(100, int(value)))


class HTTPJupyterHubProvider:
    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0)
        )

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self.settings.oauth_client_id,
                "response_type": "code",
                "redirect_uri": self.settings.oauth_redirect_uri,
                # oauth_client_allowed_scopes is only an upper bound. Without
                # an explicit request, Hub grants identity/service access but
                # not delegated named-server lifecycle permission.
                "scope": "servers!user",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.settings.hub_public_url}/hub/api/oauth2/authorize?{query}"

    async def exchange_code(self, *, code: str, pkce_verifier: str) -> OAuthToken:
        try:
            response = await self.client.post(
                f"{self.settings.hub_internal_url}/hub/api/oauth2/token",
                data={
                    "client_id": self.settings.oauth_client_id,
                    "client_secret": self.settings.oauth_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.settings.oauth_redirect_uri,
                    "code_verifier": pkce_verifier,
                },
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400, 401, 403}:
                raise HubAuthError("JupyterHub rejected the OAuth code") from exc
            self._raise_status_error(exc, "JupyterHub OAuth endpoint")
            raise AssertionError("unreachable")
        except httpx.HTTPError as exc:
            raise HubUnavailableError("JupyterHub OAuth endpoint unavailable") from exc
        if response.status_code in {400, 401, 403}:
            raise HubAuthError("JupyterHub rejected the OAuth code")
        self._require_success(response, "JupyterHub OAuth endpoint")
        data = self._json_object(response, "JupyterHub OAuth endpoint")
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise HubAuthError("JupyterHub returned no access token")
        expires_in = int(data.get("expires_in", self.settings.session_absolute_seconds))
        expires_in = min(expires_in, self.settings.session_absolute_seconds)
        scope_value = data.get("scope", "")
        scopes = (
            tuple(scope_value.split())
            if isinstance(scope_value, str)
            else tuple(scope_value or ())
        )
        return OAuthToken(
            token, datetime.utcnow() + timedelta(seconds=expires_in), scopes
        )

    async def resolve_principal(self, user_oauth_token: str) -> HubPrincipal:
        response = await self._request("GET", "/hub/api/user", token=user_oauth_token)
        if response.status_code in {401, 403}:
            raise HubAuthError("delegated token is no longer valid")
        self._require_success(response, "JupyterHub principal endpoint")
        data = self._json_object(response, "JupyterHub principal endpoint")
        name = data.get("name")
        if not isinstance(name, str):
            raise HubAuthError("JupyterHub returned an invalid principal")
        scopes = tuple(
            scope for scope in data.get("scopes", ()) if isinstance(scope, str)
        )
        return HubPrincipal(name, scopes)

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
        target = target_username or principal.username
        username_path = quote(target, safe="")
        server_path = quote(server_name, safe="")
        response = await self._request(
            "POST",
            f"/hub/api/users/{username_path}/servers/{server_path}",
            token=user_oauth_token,
            json={
                "profile_id": approved_profile.id,
                "profile_version": approved_profile.version,
                "spawn_ticket": spawn_ticket,
            },
        )
        if response.status_code == 429:
            raise HubCapacityError("JupyterHub active-server capacity is full")
        if response.status_code in {401, 403}:
            raise HubAuthError("delegated token cannot start this server")
        self._require_success(response, "JupyterHub start request")
        if response.status_code == 202:
            return HubServer(state=HubServerState.STARTING, progress_percent=0)
        return await self.get_server(
            principal,
            server_name,
            user_oauth_token,
            target_username=target,
        )

    async def request_stop(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer:
        username_path = quote(target_username or principal.username, safe="")
        server_path = quote(server_name, safe="")
        response = await self._request(
            "DELETE",
            f"/hub/api/users/{username_path}/servers/{server_path}",
            token=user_oauth_token,
        )
        if response.status_code == 404:
            return HubServer(state=HubServerState.STOPPED, progress_percent=100)
        if response.status_code in {401, 403}:
            raise HubAuthError("delegated token cannot stop this server")
        self._require_success(response, "JupyterHub stop request")
        if response.status_code == 202:
            return HubServer(state=HubServerState.STOPPING)
        return HubServer(state=HubServerState.STOPPED, progress_percent=100)

    async def request_remove(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer:
        username_path = quote(target_username or principal.username, safe="")
        server_path = quote(server_name, safe="")
        response = await self._request(
            "DELETE",
            f"/hub/api/users/{username_path}/servers/{server_path}",
            token=user_oauth_token,
            json={"remove": True},
        )
        if response.status_code == 404:
            return HubServer(state=HubServerState.NOT_FOUND, progress_percent=100)
        if response.status_code in {401, 403}:
            raise HubAuthError("delegated token cannot remove this server")
        self._require_success(response, "JupyterHub remove request")
        return await self.get_server(
            principal,
            server_name,
            user_oauth_token,
            target_username=target_username,
        )

    async def get_server(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer:
        username_path = quote(target_username or principal.username, safe="")
        response = await self._request(
            "GET", f"/hub/api/users/{username_path}", token=user_oauth_token
        )
        if response.status_code in {401, 403}:
            raise HubAuthError("delegated token cannot read this server")
        if response.status_code == 404:
            return HubServer(state=HubServerState.NOT_FOUND)
        self._require_success(response, "JupyterHub server model")
        servers = self._json_object(response, "JupyterHub server model").get(
            "servers", {}
        )
        if not isinstance(servers, dict):
            raise HubUnavailableError("JupyterHub returned an invalid server model")
        model = servers.get(server_name)
        if not isinstance(model, dict):
            return HubServer(state=HubServerState.NOT_FOUND)
        pending = model.get("pending")
        ready = bool(model.get("ready"))
        if ready:
            state = HubServerState.RUNNING
        elif pending == "spawn":
            state = HubServerState.STARTING
        elif pending == "stop":
            state = HubServerState.STOPPING
        else:
            state = HubServerState.STOPPED
        return HubServer(
            state=state,
            ready=ready,
            progress_percent=_progress_percent(model.get("progress")),
            full_url=(
                model.get("full_url")
                if isinstance(model.get("full_url"), str)
                else None
            ),
            started_at=_parse_hub_datetime(model.get("started")),
            last_activity_at=_parse_hub_datetime(model.get("last_activity")),
        )

    async def get_spawn_progress(
        self,
        principal: HubPrincipal,
        server_name: str,
        user_oauth_token: str,
        *,
        target_username: str | None = None,
    ) -> HubServer:
        """Sample the replayable Hub SSE stream without waiting for spawn completion.

        JupyterHub keeps this request open until the spawn resolves. Each worker
        reconciliation therefore reads the replayed events for only a short,
        configured wall-clock window and closes the stream. The next pass can
        reconnect and obtain newer replayed events.
        """

        username_path = quote(target_username or principal.username, safe="")
        server_path = quote(server_name, safe="")
        path = f"/hub/api/users/{username_path}/servers/{server_path}/progress"
        latest = HubServer(state=HubServerState.STARTING)

        async def _sample() -> HubServer:
            nonlocal latest
            try:
                async with self.client.stream(
                    "GET",
                    f"{self.settings.hub_internal_url}{path}",
                    headers={
                        "Authorization": f"Bearer {user_oauth_token}",
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache",
                    },
                ) as response:
                    if response.status_code in {401, 403}:
                        raise HubAuthError("delegated token cannot read spawn progress")
                    if response.status_code in {400, 404}:
                        raise _ProgressStreamGone(response.status_code)
                    self._require_success(response, "JupyterHub spawn progress")

                    chars_read = 0
                    event_count = 0
                    async for line in response.aiter_lines():
                        chars_read += len(line)
                        if chars_read > _MAX_PROGRESS_STREAM_CHARS:
                            raise HubUnavailableError(
                                "JupyterHub spawn progress response is too large"
                            )
                        if not line.startswith("data:"):
                            continue
                        event_count += 1
                        if event_count > _MAX_PROGRESS_EVENTS:
                            raise HubUnavailableError(
                                "JupyterHub spawn progress returned too many events"
                            )
                        try:
                            event = json.loads(line.split(":", 1)[1].strip())
                        except (json.JSONDecodeError, UnicodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        progress = _progress_percent(event.get("progress"))
                        if event.get("failed") is True:
                            return HubServer(
                                state=HubServerState.FAILED,
                                progress_percent=(
                                    progress if progress is not None else 100
                                ),
                                failure_summary="JupyterHub reported that the workspace failed to start",
                            )
                        if event.get("ready") is True:
                            return HubServer(
                                state=HubServerState.RUNNING,
                                ready=True,
                                progress_percent=(
                                    progress if progress is not None else 100
                                ),
                            )
                        if progress is not None:
                            latest = HubServer(
                                state=HubServerState.STARTING,
                                progress_percent=progress,
                            )
                    return latest
            except httpx.HTTPStatusError as exc:
                self._raise_status_error(exc, "JupyterHub spawn progress")
                raise AssertionError("unreachable")
            except httpx.HTTPError as exc:
                raise HubUnavailableError(
                    "JupyterHub spawn progress unavailable"
                ) from exc

        try:
            sampled = await asyncio.wait_for(
                _sample(), timeout=self.settings.hub_progress_sample_seconds
            )
        except asyncio.TimeoutError:
            sampled = latest
        except _ProgressStreamGone as exc:
            refreshed = await self.get_server(
                principal,
                server_name,
                user_oauth_token,
                target_username=target_username,
            )
            if refreshed.state == HubServerState.STARTING:
                raise HubRequestError(
                    "JupyterHub rejected progress for a server that is still starting",
                    status_code=exc.status_code,
                ) from exc
            return refreshed

        if sampled.state == HubServerState.RUNNING and sampled.ready:
            # The event URL is only a path. Re-read the model to obtain JupyterHub
            # 5.5's authoritative full_url (including per-user domains).
            return await self.get_server(
                principal,
                server_name,
                user_oauth_token,
                target_username=target_username,
            )
        return sampled

    def _require_success(self, response: httpx.Response, context: str) -> None:
        if 200 <= response.status_code < 300:
            return
        if response.status_code >= 500:
            raise HubUnavailableError(f"{context} failed")
        raise HubRequestError(
            f"{context} was rejected", status_code=response.status_code
        )

    def _json_object(self, response: httpx.Response, context: str) -> dict[str, object]:
        try:
            value = response.json()
        except (ValueError, UnicodeError) as exc:
            raise HubUnavailableError(f"{context} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise HubUnavailableError(f"{context} returned invalid JSON")
        return value

    def _raise_status_error(self, exc: httpx.HTTPStatusError, context: str) -> None:
        response = exc.response
        if response.status_code in {401, 403}:
            raise HubAuthError(f"{context} rejected delegated authentication") from exc
        if response.status_code == 429:
            raise HubCapacityError(
                f"{context} rejected the active-server capacity"
            ) from exc
        if response.status_code >= 500:
            raise HubUnavailableError(f"{context} failed") from exc
        raise HubRequestError(
            f"{context} was rejected", status_code=response.status_code
        ) from exc

    async def _request(
        self, method: str, path: str, *, token: str, **kwargs: object
    ) -> httpx.Response:
        try:
            return await self.client.request(
                method,
                f"{self.settings.hub_internal_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        except httpx.HTTPStatusError as exc:
            self._raise_status_error(exc, "JupyterHub API request")
            raise AssertionError("unreachable")
        except httpx.HTTPError as exc:
            raise HubUnavailableError("JupyterHub API unavailable") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
