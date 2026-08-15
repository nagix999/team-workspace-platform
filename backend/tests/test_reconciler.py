from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import Base
from app.domain import HubServerState
from app.hub import FakeJupyterHubProvider, HubServer
from app.main import create_app
from app.models import AuditEvent, Operation, Workspace
from app.reconciler import (
    HubSnapshot,
    HubSnapshotClient,
    ReconcilerConfig,
    ReconciliationError,
    WorkspaceReconciler,
    clear_heartbeat,
    heartbeat_is_fresh,
    read_reconciler_token,
    write_heartbeat,
)
from app.worker import OperationWorker

from conftest import login, mutation_headers, provision


@pytest.fixture
def reconciliation_env(settings):
    configured = replace(
        settings,
        admin_usernames=("admin",),
        reconciliation_freshness_seconds=30,
        worker_retry_seconds=0,
    )
    hub = FakeJupyterHubProvider()
    app = create_app(configured, hub)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=configured.portal_origin) as client:
        yield app, hub, client


class StaticSnapshotSource:
    def __init__(self, snapshot: HubSnapshot | None = None, *, unavailable=False):
        self.snapshot = snapshot or HubSnapshot(servers={})
        self.unavailable = unavailable

    async def fetch(self) -> HubSnapshot:
        if self.unavailable:
            raise ReconciliationError("synthetic unavailable")
        return self.snapshot


class BrokenAsyncBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"kind":"service"'
        raise httpx.ReadError("synthetic mid-body disconnect")


def test_reconciler_config_and_token_file_are_minimal_and_strict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    token_path = tmp_path / "reconciler-token"
    token_path.write_text("x" * 32, encoding="ascii")
    token_path.chmod(0o440)
    assert read_reconciler_token(token_path) == "x" * 32

    for unsafe_mode in (0o441, 0o450, 0o460, 0o470, 0o640 | 0o001):
        token_path.chmod(unsafe_mode)
        with pytest.raises(ReconciliationError, match="permissions are unsafe"):
            read_reconciler_token(token_path)
    token_path.chmod(0o440)

    monkeypatch.setenv("PLATFORM_ENFORCE_SAFE_SQLITE", "sometimes")
    with pytest.raises(RuntimeError, match="must be true or false"):
        ReconcilerConfig.from_env()
    monkeypatch.setenv("PLATFORM_ENFORCE_SAFE_SQLITE", "false")
    config = ReconcilerConfig.from_env()
    assert config.enforce_safe_sqlite is False
    assert set(config.__dict__) == {
        "database_url",
        "hub_internal_url",
        "token_file",
        "interval_seconds",
        "freshness_seconds",
        "enforce_safe_sqlite",
    }

    monkeypatch.setenv("PLATFORM_TOKEN_ENCRYPTION_KEY_FILE", "/forbidden/key")
    with pytest.raises(RuntimeError, match="forbidden credential"):
        ReconcilerConfig.from_env()
    monkeypatch.delenv("PLATFORM_TOKEN_ENCRYPTION_KEY_FILE")

    # A symlink cannot turn a differently managed file into this process's
    # credential, even when the target mode itself is acceptable.
    symlink = tmp_path / "reconciler-token-link"
    os.symlink(token_path, symlink)
    with pytest.raises(ReconciliationError, match="permissions are unsafe"):
        read_reconciler_token(symlink)


def _running_workspace(app, hub, client):
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    response = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "reconciler-create"),
    )
    assert response.status_code == 202, response.text
    workspace_id = response.json()["workspace"]["id"]
    started = client.post(
        f"/api/v1/workspaces/{workspace_id}/actions/start",
        headers=mutation_headers(me, "reconciler-start"),
    )
    assert started.status_code == 202, started.text
    worker = OperationWorker(
        app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id="reconciler-test-worker",
    )
    assert asyncio.run(worker.process_next()) is True
    return workspace_id


def test_fresh_running_snapshot_restores_admin_count_and_launch(reconciliation_env):
    app, hub, client = reconciliation_env
    workspace_id = _running_workspace(app, hub, client)
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        assert workspace
        workspace.last_reconciled_at = datetime.utcnow() - timedelta(minutes=5)
        db.commit()
        server_name = workspace.hub_server_name

    admin, *_ = login(client, hub, "admin")
    capacity = client.get("/api/v1/admin/capacity").json()
    assert capacity["workspaces"]["running"] == 0
    assert (
        client.get(
            f"/api/v1/admin/workspaces/{workspace_id}/launch",
            follow_redirects=False,
        ).status_code
        == 409
    )

    snapshot = HubSnapshot(
        servers={
            ("alice", server_name): HubServer(
                state=HubServerState.RUNNING,
                ready=True,
                progress_percent=100,
                started_at=datetime.utcnow(),
            )
        }
    )
    reconciler = WorkspaceReconciler(
        app.state.session_factory, StaticSnapshotSource(snapshot)
    )
    assert asyncio.run(reconciler.run_once()) is True

    capacity = client.get("/api/v1/admin/capacity").json()
    assert capacity["workspaces"]["running"] == 1
    launched = client.get(
        f"/api/v1/admin/workspaces/{workspace_id}/launch", follow_redirects=False
    )
    assert launched.status_code == 303
    assert "token=" not in launched.headers["location"]
    assert admin["user"]["role"] == "ADMIN"


def test_external_stop_remove_is_idempotently_audited(reconciliation_env):
    app, hub, client = reconciliation_env
    workspace_id = _running_workspace(app, hub, client)
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        assert workspace
        server_name = workspace.hub_server_name

    source = StaticSnapshotSource(
        HubSnapshot(
            servers={
                ("alice", server_name): HubServer(
                    state=HubServerState.STOPPED, progress_percent=100
                )
            }
        )
    )
    reconciler = WorkspaceReconciler(app.state.session_factory, source)
    assert asyncio.run(reconciler.run_once()) is True
    source.snapshot = HubSnapshot(servers={})
    assert asyncio.run(reconciler.run_once()) is True
    assert asyncio.run(reconciler.run_once()) is True

    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        events = db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == "EXTERNAL_CHANGE",
            )
            .order_by(AuditEvent.created_at)
        ).all()
        assert workspace and workspace.observed_state == "NOT_FOUND"
        assert workspace.stale is False
        assert len(events) == 2
        assert '"observed_state":"STOPPED"' in events[0].safe_metadata_json
        assert '"observed_state":"NOT_FOUND"' in events[1].safe_metadata_json


def test_unavailable_marks_stale_and_active_operation_is_skipped(reconciliation_env):
    app, hub, client = reconciliation_env
    running_id = _running_workspace(app, hub, client)
    me = client.get("/api/v1/me").json()
    pending = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "reconciler-pending-create"),
    )
    assert pending.status_code == 202, pending.text
    pending_id = pending.json()["workspace"]["id"]
    started = client.post(
        f"/api/v1/workspaces/{pending_id}/actions/start",
        headers=mutation_headers(me, "reconciler-pending-start"),
    )
    assert started.status_code == 202, started.text
    old_timestamp = datetime.utcnow() - timedelta(minutes=5)
    with app.state.session_factory() as db:
        pending_workspace = db.get(Workspace, pending_id)
        assert pending_workspace
        pending_workspace.observed_state = "RUNNING"
        pending_workspace.stale = False
        pending_workspace.last_reconciled_at = old_timestamp
        db.commit()

    successful = WorkspaceReconciler(
        app.state.session_factory, StaticSnapshotSource(HubSnapshot(servers={}))
    )
    assert asyncio.run(successful.run_once()) is True
    with app.state.session_factory() as db:
        pending_workspace = db.get(Workspace, pending_id)
        active = db.scalar(
            select(func.count(Operation.id)).where(
                Operation.workspace_id == pending_id,
                Operation.status == "PENDING",
            )
        )
        assert active == 1
        assert pending_workspace and pending_workspace.observed_state == "RUNNING"
        assert pending_workspace.last_reconciled_at == old_timestamp

    unavailable = WorkspaceReconciler(
        app.state.session_factory, StaticSnapshotSource(unavailable=True)
    )
    assert asyncio.run(unavailable.run_once()) is False
    with app.state.session_factory() as db:
        assert db.get(Workspace, running_id).stale is True
        assert db.get(Workspace, pending_id).stale is True


def test_mid_body_hub_disconnect_marks_workspace_stale(reconciliation_env):
    app, hub, client = reconciliation_env
    workspace_id = _running_workspace(app, hub, client)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=BrokenAsyncBody(),
        )

    async def scenario() -> bool:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = HubSnapshotClient(
            hub_internal_url="http://jupyterhub:8081",
            token="x" * 32,
            client=http_client,
        )
        try:
            return await WorkspaceReconciler(
                app.state.session_factory, source
            ).run_once()
        finally:
            await http_client.aclose()

    assert asyncio.run(scenario()) is False
    with app.state.session_factory() as db:
        assert db.get(Workspace, workspace_id).stale is True


def test_old_snapshot_does_not_overwrite_newer_worker_result(reconciliation_env):
    app, hub, client = reconciliation_env
    workspace_id = _running_workspace(app, hub, client)
    captured_at = datetime.utcnow() - timedelta(seconds=5)
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        assert workspace
        workspace.last_reconciled_at = datetime.utcnow()
        db.commit()

    snapshot = HubSnapshot(servers={}, captured_at=captured_at)
    reconciler = WorkspaceReconciler(
        app.state.session_factory, StaticSnapshotSource(snapshot)
    )
    assert asyncio.run(reconciler.run_once()) is True
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        external_events = db.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == "EXTERNAL_CHANGE",
            )
        )
        assert workspace and workspace.observed_state == "RUNNING"
        assert external_events == 0


def test_old_snapshot_skips_terminal_operation_completed_during_fetch(
    reconciliation_env,
):
    app, hub, client = reconciliation_env
    workspace_id = _running_workspace(app, hub, client)
    captured_at = datetime.utcnow()
    me = client.get("/api/v1/me").json()
    stop = client.post(
        f"/api/v1/workspaces/{workspace_id}/actions/stop",
        headers=mutation_headers(me, "reconciler-terminal-race"),
    )
    assert stop.status_code == 202, stop.text
    operation_id = stop.json()["operation"]["id"]
    with app.state.session_factory() as db:
        operation = db.get(Operation, operation_id)
        workspace = db.get(Workspace, workspace_id)
        assert operation and workspace
        operation.status = "FAILED"
        operation.completed_at = captured_at + timedelta(seconds=1)
        workspace.last_reconciled_at = captured_at - timedelta(seconds=1)
        db.commit()

    reconciler = WorkspaceReconciler(
        app.state.session_factory,
        StaticSnapshotSource(HubSnapshot(servers={}, captured_at=captured_at)),
    )
    assert asyncio.run(reconciler.run_once()) is True
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        external_events = db.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == "EXTERNAL_CHANGE",
            )
        )
        assert workspace and workspace.observed_state == "RUNNING"
        assert external_events == 0


def test_snapshot_older_than_freshness_is_rejected_and_cannot_heartbeat(
    reconciliation_env, tmp_path: Path
):
    app, hub, client = reconciliation_env
    workspace_id = _running_workspace(app, hub, client)
    old_snapshot = HubSnapshot(
        servers={}, captured_at=datetime.utcnow() - timedelta(seconds=31)
    )
    reconciler = WorkspaceReconciler(
        app.state.session_factory,
        StaticSnapshotSource(old_snapshot),
        maximum_snapshot_age_seconds=30,
    )
    assert asyncio.run(reconciler.run_once()) is False
    assert reconciler.last_successful_snapshot_at is None
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        assert workspace and workspace.stale is True
        assert workspace.observed_state == "RUNNING"

    heartbeat = tmp_path / "reconciler-health.json"
    write_heartbeat(old_snapshot.captured_at, heartbeat)
    assert heartbeat.stat().st_mode & 0o777 == 0o600
    assert heartbeat_is_fresh(freshness_seconds=30, path=heartbeat) is False
    write_heartbeat(datetime.utcnow(), heartbeat)
    assert heartbeat_is_fresh(freshness_seconds=30, path=heartbeat) is True
    heartbeat.chmod(0o644)
    assert heartbeat_is_fresh(freshness_seconds=30, path=heartbeat) is False
    clear_heartbeat(heartbeat)
    assert not heartbeat.exists()


@pytest.mark.parametrize(
    "user_options", [None, {"spawn_ticket": "already-consumed-sensitive-value"}]
)
def test_hub_snapshot_client_strictly_parses_least_privilege_page(user_options):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/hub/api/user":
            return httpx.Response(
                200,
                json={
                    "kind": "service",
                    "admin": False,
                    "name": "platform-reconciler",
                    "token_id": "token-id",
                    "session_id": None,
                    "scopes": ["list:users", "read:servers", "read:users:name"],
                },
            )
        assert request.url.path == "/hub/api/users"
        assert request.url.params["include_stopped_servers"] == "1"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "kind": "user",
                        "name": "alice",
                        "admin": False,
                        "servers": {
                            "ws-0123456789abcdef0123456789abcdef": {
                                "name": "ws-0123456789abcdef0123456789abcdef",
                                "full_name": "alice/ws-0123456789abcdef0123456789abcdef",
                                "last_activity": None,
                                "started": "2026-08-11T00:00:00Z",
                                "pending": None,
                                "ready": True,
                                "stopped": False,
                                "url": "/user/alice/ws-0123456789abcdef0123456789abcdef/",
                                # JupyterHub 5.5 uses null for some stopped or
                                # never-spawned records and a dict otherwise.
                                "user_options": user_options,
                                "progress_url": "/hub/api/users/alice/servers/ws/progress",
                                "full_url": "https://alice.hub.example.net/user/alice/ws/",
                                "full_progress_url": "https://hub.example.net/progress",
                            }
                        },
                    }
                ],
                "_pagination": {
                    "offset": 0,
                    "limit": 100,
                    "total": 1,
                    "next": None,
                },
            },
        )

    async def scenario():
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = HubSnapshotClient(
            hub_internal_url="http://jupyterhub:8081",
            token="x" * 32,
            client=http_client,
        )
        try:
            return await source.fetch()
        finally:
            await http_client.aclose()

    snapshot = asyncio.run(scenario())
    server = snapshot.servers[("alice", "ws-0123456789abcdef0123456789abcdef")]
    assert server.state == HubServerState.RUNNING
    assert server.full_url is None
    assert "already-consumed-sensitive-value" not in repr(server)


@pytest.mark.parametrize("malformed_pending", [[], {}])
def test_hub_snapshot_client_rejects_unhashable_pending_as_snapshot_error(
    malformed_pending,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/hub/api/user":
            return httpx.Response(
                200,
                json={
                    "kind": "service",
                    "admin": False,
                    "name": "platform-reconciler",
                    "token_id": "token-id",
                    "session_id": None,
                    "scopes": ["list:users", "read:servers", "read:users:name"],
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "kind": "user",
                        "name": "alice",
                        "admin": False,
                        "servers": {
                            "ws-0123456789abcdef0123456789abcdef": {
                                "name": "ws-0123456789abcdef0123456789abcdef",
                                "full_name": "alice/ws-0123456789abcdef0123456789abcdef",
                                "last_activity": None,
                                "started": None,
                                "pending": malformed_pending,
                                "ready": False,
                                "stopped": False,
                                "url": "/user/alice/ws/",
                                "user_options": None,
                                "progress_url": "/hub/api/users/alice/servers/ws/progress",
                                "full_url": None,
                                "full_progress_url": None,
                            }
                        },
                    }
                ],
                "_pagination": {
                    "offset": 0,
                    "limit": 100,
                    "total": 1,
                    "next": None,
                },
            },
        )

    async def scenario() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = HubSnapshotClient(
            hub_internal_url="http://jupyterhub:8081",
            token="x" * 32,
            client=http_client,
        )
        try:
            with pytest.raises(ReconciliationError, match="pending state"):
                await source.fetch()
        finally:
            await http_client.aclose()

    asyncio.run(scenario())
