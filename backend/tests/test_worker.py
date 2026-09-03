from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.domain import HubServerState
from app.hub import HubRequestError, HubServer
from app.models import Operation, SpawnAuthorization, Workspace
from app.services.resource_policy import get_resource_policy
from app.worker import OperationWorker

from conftest import login, mutation_headers, provision


def _create_and_start(client, me, key: str) -> dict[str, object]:
    created = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, f"{key}-create"),
    ).json()
    workspace_id = created["workspace"]["id"]
    started = client.post(
        f"/api/v1/workspaces/{workspace_id}/actions/start",
        headers=mutation_headers(me, f"{key}-start"),
    )
    assert started.status_code == 202, started.text
    return {
        "workspace": created["workspace"],
        "operation": started.json()["operation"],
    }


def test_worker_converges_create_and_launches_with_tokenless_url(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create_and_start(client, me, "worker")
    worker = OperationWorker(
        app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id="test-worker",
    )
    assert asyncio.run(worker.process_next()) is True
    operation = client.get(f"/api/v1/operations/{created['operation']['id']}").json()[
        "operation"
    ]
    assert operation["status"] == "SUCCEEDED"
    workspace = client.get(f"/api/v1/workspaces/{created['workspace']['id']}").json()[
        "workspace"
    ]
    assert workspace["observed_state"] == "RUNNING"
    launch = client.get(workspace["launch_url"], follow_redirects=False)
    assert launch.status_code == 303
    assert "token=" not in launch.headers["location"]


def test_worker_snapshots_nondefault_kernel_idle_policy(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    with app.state.session_factory() as db:
        policy = get_resource_policy(db, app.state.settings)
        policy.kernel_idle_timeout_seconds = 7_200
        db.commit()

    created = _create_and_start(client, me, "kernel-idle-snapshot")
    worker = OperationWorker(
        app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id="kernel-idle-worker",
    )
    assert asyncio.run(worker.process_next()) is True
    with app.state.session_factory() as db:
        authorization = db.scalar(
            select(SpawnAuthorization).where(
                SpawnAuthorization.operation_id == created["operation"]["id"]
            )
        )
        policy = get_resource_policy(db, app.state.settings)
        assert authorization and authorization.kernel_idle_timeout_seconds == 7_200
        policy.kernel_idle_timeout_seconds = 300
        db.commit()

    with app.state.session_factory() as db:
        authorization = db.scalar(
            select(SpawnAuthorization).where(
                SpawnAuthorization.operation_id == created["operation"]["id"]
            )
        )
        assert authorization and authorization.kernel_idle_timeout_seconds == 7_200


def test_new_worker_reclaims_pending_async_spawn(app_env):
    app, hub, client = app_env
    hub.auto_ready = False
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create_and_start(client, me, "async")
    first = OperationWorker(
        app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id="worker-a",
    )
    asyncio.run(first.process_next())
    with app.state.session_factory() as db:
        operation = db.get(Operation, created["operation"]["id"])
        workspace = db.get(Workspace, created["workspace"]["id"])
        assert operation and operation.status == "PENDING"
        assert workspace and workspace.observed_state == "STARTING"
        server_name = workspace.hub_server_name
        operation.next_attempt_at = datetime.utcnow()
        db.commit()
    hub.complete_start("alice", server_name)
    second = OperationWorker(
        app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id="worker-b",
    )
    asyncio.run(second.process_next())
    with app.state.session_factory() as db:
        operation = db.get(Operation, created["operation"]["id"])
        assert operation and operation.status == "SUCCEEDED"


def test_worker_persists_sampled_spawn_progress_for_portal(app_env):
    app, hub, client = app_env
    hub.auto_ready = False
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create_and_start(client, me, "progress")
    worker = OperationWorker(
        app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id="progress-worker",
    )
    asyncio.run(worker.process_next())
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, created["workspace"]["id"])
        assert workspace
        hub.servers[("alice", workspace.hub_server_name)] = HubServer(
            state=HubServerState.STARTING, progress_percent=67
        )
    asyncio.run(worker.process_next())
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, created["workspace"]["id"])
        operation = db.get(Operation, created["operation"]["id"])
        assert workspace and workspace.progress_percent == 67
        assert workspace.observed_state == "STARTING"
        assert operation and operation.status == "PENDING"


def test_worker_caps_spawn_ticket_reissuance_and_fails_terminally(app_env):
    app, hub, client = app_env
    hub.auto_ready = False
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create_and_start(client, me, "bounded")
    settings = replace(app.state.settings, worker_max_attempts=3)
    worker = OperationWorker(
        settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id="bounded-worker",
    )

    for attempt in range(1, 4):
        assert asyncio.run(worker.process_next()) is True
        with app.state.session_factory() as db:
            operation = db.get(Operation, created["operation"]["id"])
            workspace = db.get(Workspace, created["workspace"]["id"])
            assert operation and operation.attempts == attempt
            assert operation.status == "PENDING"
            assert workspace
            hub.servers[("alice", workspace.hub_server_name)] = HubServer(
                state=HubServerState.STOPPED, progress_percent=100
            )

    assert asyncio.run(worker.process_next()) is True
    with app.state.session_factory() as db:
        operation = db.get(Operation, created["operation"]["id"])
        workspace = db.get(Workspace, created["workspace"]["id"])
        authorization_count = db.scalar(
            select(func.count()).select_from(SpawnAuthorization)
        )
        assert operation and operation.status == "FAILED"
        assert operation.attempts == 3
        assert operation.error_code == "SPAWN_ATTEMPTS_EXHAUSTED"
        assert workspace and workspace.last_error_code == "SPAWN_ATTEMPTS_EXHAUSTED"
        assert authorization_count == 3
    assert hub.start_count == 3


def test_worker_turns_permanent_hub_rejection_into_terminal_failure(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create_and_start(client, me, "rejected")
    hub.request_start = AsyncMock(
        side_effect=HubRequestError("invalid spawn options", status_code=422)
    )
    worker = OperationWorker(
        app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id="rejected-worker",
    )

    assert asyncio.run(worker.process_next()) is True
    with app.state.session_factory() as db:
        operation = db.get(Operation, created["operation"]["id"])
        assert operation and operation.status == "FAILED"
        assert operation.error_code == "HUB_REQUEST_REJECTED"
