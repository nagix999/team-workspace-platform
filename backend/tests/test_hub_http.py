from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.domain import HubServerState
from app.hub import ApprovedProfile, HubPrincipal, HubRequestError
from app.hub.http import HTTPJupyterHubProvider


def test_authorization_url_requests_delegated_server_scope(settings):
    client = httpx.AsyncClient()
    provider = HTTPJupyterHubProvider(settings, client)
    try:
        query = parse_qs(
            urlsplit(
                provider.authorization_url(
                    state="state-value", code_challenge="challenge-value"
                )
            ).query
        )
    finally:
        asyncio.run(client.aclose())

    assert query["scope"] == ["servers!user"]
    assert query["code_challenge_method"] == ["S256"]


class HangingEventStream(httpx.AsyncByteStream):
    def __init__(self, first_event: dict[str, object]) -> None:
        self.first_event = first_event
        self.closed = False

    async def __aiter__(self):
        payload = json.dumps(self.first_event).encode("utf-8")
        yield b"data: " + payload + b"\n\n"
        await asyncio.Future()

    async def aclose(self) -> None:
        self.closed = True


def test_progress_sampling_reads_replayed_event_and_closes_long_stream(settings):
    stream = HangingEventStream({"progress": 42, "message": "Spawning"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/hub/api/users/alice/servers/ws-one/progress"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream,
        )

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HTTPJupyterHubProvider(
            replace(settings, hub_progress_sample_seconds=0.02), client
        )
        started = time.monotonic()
        try:
            result = await provider.get_spawn_progress(
                HubPrincipal("alice", ("servers!user",)), "ws-one", "oauth-token"
            )
        finally:
            await client.aclose()
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    assert result.state == HubServerState.STARTING
    assert result.progress_percent == 42
    assert elapsed < 0.5
    assert stream.closed is True


def test_progress_sampling_recognizes_hub_failure_without_reflecting_html(settings):
    body = (
        'data: {"progress": 100, "failed": true, "message": "Spawn failed", '
        '"html_message": "<script>secret()</script>"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, text=body
        )

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HTTPJupyterHubProvider(settings, client)
        try:
            return await provider.get_spawn_progress(
                HubPrincipal("alice", ("servers!user",)), "ws-one", "oauth-token"
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())
    assert result.state == HubServerState.FAILED
    assert result.progress_percent == 100
    assert result.failure_summary == (
        "JupyterHub reported that the workspace failed to start"
    )
    assert "script" not in result.failure_summary


def test_progress_ready_event_is_resolved_to_authoritative_full_url(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/progress"):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text='data: {"progress": 100, "ready": true, "url": "/user/alice/ws-one/"}\n\n',
            )
        assert request.url.path == "/hub/api/users/alice"
        return httpx.Response(
            200,
            json={
                "servers": {
                    "ws-one": {
                        "pending": None,
                        "ready": True,
                        "progress": 100,
                        "full_url": "https://alice.hub.example.net/user/alice/ws-one/",
                        "started": "2026-08-10T00:00:00Z",
                        "last_activity": None,
                    }
                }
            },
        )

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HTTPJupyterHubProvider(settings, client)
        try:
            return await provider.get_spawn_progress(
                HubPrincipal("alice", ("servers!user",)), "ws-one", "oauth-token"
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())
    assert result.state == HubServerState.RUNNING
    assert result.ready is True
    assert result.full_url == "https://alice.hub.example.net/user/alice/ws-one/"


def test_unexpected_start_4xx_is_converted_to_domain_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "invalid options"})

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HTTPJupyterHubProvider(settings, client)
        try:
            with pytest.raises(HubRequestError) as caught:
                await provider.request_start(
                    HubPrincipal("alice", ("servers!user",)),
                    "ws-one",
                    ApprovedProfile("python-standard", 1, "sha256:" + "a" * 64),
                    "ticket-value",
                    "oauth-token",
                )
            return caught.value
        finally:
            await client.aclose()

    error = asyncio.run(scenario())
    assert error.status_code == 422


def test_httpx_raise_for_status_hook_cannot_escape_provider_boundary(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, json={"message": "rejected"})

    async def reject_status(response: httpx.Response) -> None:
        response.raise_for_status()

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            event_hooks={"response": [reject_status]},
        )
        provider = HTTPJupyterHubProvider(settings, client)
        try:
            with pytest.raises(HubRequestError) as caught:
                await provider.resolve_principal("oauth-token")
            return caught.value
        finally:
            await client.aclose()

    error = asyncio.run(scenario())
    assert error.status_code == 418


def test_progress_4xx_with_still_starting_model_is_permanent_domain_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/progress"):
            return httpx.Response(400, text="not starting")
        return httpx.Response(
            200,
            json={
                "servers": {
                    "ws-one": {
                        "pending": "spawn",
                        "ready": False,
                        "progress": 10,
                    }
                }
            },
        )

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HTTPJupyterHubProvider(settings, client)
        try:
            with pytest.raises(HubRequestError) as caught:
                await provider.get_spawn_progress(
                    HubPrincipal("alice", ("servers!user",)),
                    "ws-one",
                    "oauth-token",
                )
            return caught.value
        finally:
            await client.aclose()

    error = asyncio.run(scenario())
    assert error.status_code == 400


def test_remove_named_server_uses_json_body_and_confirms_not_found(settings):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            assert request.url.path == "/hub/api/users/alice/servers/ws-one"
            assert request.url.query == b""
            assert json.loads(request.content) == {"remove": True}
            return httpx.Response(204)
        assert request.method == "GET"
        assert request.url.path == "/hub/api/users/alice"
        return httpx.Response(200, json={"servers": {}})

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HTTPJupyterHubProvider(settings, client)
        try:
            return await provider.request_remove(
                HubPrincipal("operator", ("admin:servers",)),
                "ws-one",
                "admin-token",
                target_username="alice",
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())
    assert result.state == HubServerState.NOT_FOUND
    assert [request.method for request in requests] == ["DELETE", "GET"]
