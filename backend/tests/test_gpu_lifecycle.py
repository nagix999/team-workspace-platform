from __future__ import annotations

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db import Base
from app.domain import DesiredState, HubServerState
from app.errors import AppError
from app.hub import FakeJupyterHubProvider, HubServer
from app.main import create_app
from app.models import (
    Operation,
    SpawnAuthorization,
    User,
    UserSession,
    Workspace,
    WorkspaceProfile,
)
from app.services.workspaces import (
    WorkspaceService,
    active_reservations,
)
from app.worker import OperationWorker

from conftest import login, mutation_headers, provision


GPU_A = "GPU-01234567-89ab-cdef-0123-456789abcdef"
GPU_B = "GPU-fedcba98-7654-3210-fedc-ba9876543210"
GPU_PROFILE_ID = "python312-cuda126-pytorch271"


@pytest.fixture
def gpu_env(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'platform-gpu.db'}",
        portal_origin="https://platform.example.com",
        hub_internal_url="http://hub:8081",
        hub_public_url="https://hub.example.net",
        hub_user_domain="hub.example.net",
        oauth_client_id="service-platform-api",
        oauth_client_secret="test-oauth-client-secret",
        oauth_redirect_uri="https://platform.example.com/api/v1/auth/callback",
        token_encryption_key=base64.urlsafe_b64encode(bytes(range(32))).decode(),
        token_encryption_key_id="test-v1",
        session_hash_key="test-session-hash-key-that-is-long",
        internal_hmac_key="test-internal-hmac-key-that-is-long",
        worker_retry_seconds=0,
        workspace_cpu_budget_millicores=16_000,
        workspace_memory_budget_mb=16_384,
        nvidia_gpu_device_ids=(GPU_A,),
        portal_security_domain="example.com",
        hub_user_security_domain="example.net",
    )
    hub = FakeJupyterHubProvider()
    app = create_app(settings, hub)
    Base.metadata.create_all(app.state.engine)
    with app.state.session_factory() as db:
        db.add(
            WorkspaceProfile(
                id=GPU_PROFILE_ID,
                version=1,
                name="Python 3.12 CUDA 12.6 PyTorch 2.7.1",
                kernel_name="python312-cuda",
                kernel_display_name="Python 3.12 (CUDA 12.6)",
                python_version="3.12.13",
                accelerator_kind="nvidia",
                gpu_count=1,
                cuda_version="12.6",
                gpu_framework="pytorch",
                gpu_framework_version="2.7.1",
                image_ref="example.invalid/singleuser-cuda@sha256:" + "c" * 64,
                cpu_limit="2.0",
                memory_limit_mb=1024,
                pids_limit=512,
                private_disk_limit_mb=1024,
                private_disk_quota_enforced=False,
                provider_options_json=json.dumps(
                    {
                        "gid": 100,
                        "private_disk_hard_limit_bytes": 1073741824,
                        "uid": 1000,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                config_digest="sha256:" + "d" * 64,
                enabled=True,
                selectable=True,
            )
        )
        db.commit()
    with TestClient(app, base_url=settings.portal_origin) as client:
        yield app, hub, client


def _create_gpu(client: TestClient, me: dict, key: str) -> dict:
    response = client.post(
        "/api/v1/workspaces",
        json={"profile_id": GPU_PROFILE_ID, "profile_version": 1},
        headers=mutation_headers(me, f"{key}-create"),
    )
    assert response.status_code == 202, response.text
    return response.json()["workspace"]


def _start_gpu(client: TestClient, me: dict, workspace_id: str, key: str):
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/actions/start",
        headers=mutation_headers(me, key),
    )


def _worker(app, hub, worker_id: str, *, settings=None) -> OperationWorker:
    return OperationWorker(
        settings or app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id=worker_id,
    )


def test_concurrent_gpu_starts_create_exactly_one_durable_device_owner(gpu_env):
    app, hub, alice_client = gpu_env
    alice, *_ = login(alice_client, hub, "alice")
    provision(app, "alice")
    alice_workspace = _create_gpu(alice_client, alice, "alice-gpu")

    with TestClient(app, base_url=app.state.settings.portal_origin) as bob_client:
        bob, *_ = login(bob_client, hub, "bob")
        provision(app, "bob")
        bob_workspace = _create_gpu(bob_client, bob, "bob-gpu")
        barrier = Barrier(2)

        def start(client, me, workspace_id, key):
            barrier.wait()
            return _start_gpu(client, me, workspace_id, key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            alice_future = pool.submit(
                start,
                alice_client,
                alice,
                alice_workspace["id"],
                "alice-concurrent-start",
            )
            bob_future = pool.submit(
                start,
                bob_client,
                bob,
                bob_workspace["id"],
                "bob-concurrent-start",
            )
            responses = [alice_future.result(), bob_future.result()]

    assert sorted(response.status_code for response in responses) == [202, 429]
    rejected = next(response for response in responses if response.status_code == 429)
    assert rejected.json()["error"]["code"] in {
        "GPU_CAPACITY_LIMIT",
        "RESOURCE_CAPACITY_LIMIT",
    }

    with app.state.session_factory() as db:
        owners = db.scalars(
            select(Workspace).where(Workspace.assigned_gpu_device_id == GPU_A)
        ).all()
        assert len(owners) == 1
        reservation = active_reservations(db)
        assert reservation.count == 1
        assert reservation.gpu_count == len(owners) == 1

        loser = db.scalar(select(Workspace).where(Workspace.id != owners[0].id))
        assert loser is not None
        loser.assigned_gpu_device_id = GPU_A
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_confirmed_stop_releases_gpu_only_after_hub_observation(gpu_env):
    app, hub, client = gpu_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    workspace = _create_gpu(client, me, "confirmed-stop")
    started = _start_gpu(client, me, workspace["id"], "confirmed-stop-start")
    assert started.status_code == 202, started.text
    assert asyncio.run(_worker(app, hub, "gpu-start-worker").process_next())

    stopped = client.post(
        f"/api/v1/workspaces/{workspace['id']}/actions/stop",
        headers=mutation_headers(me, "confirmed-stop-request"),
    )
    assert stopped.status_code == 202, stopped.text
    with app.state.session_factory() as db:
        row = db.get(Workspace, workspace["id"])
        assert row and row.assigned_gpu_device_id == GPU_A
        assert active_reservations(db).gpu_count == 1

    assert asyncio.run(_worker(app, hub, "gpu-stop-worker").process_next())
    with app.state.session_factory() as db:
        row = db.get(Workspace, workspace["id"])
        assert row and row.observed_state == "STOPPED"
        assert row.assigned_gpu_device_id is None
        reservation = active_reservations(db)
        assert reservation.count == 0
        assert reservation.gpu_count == 0


@pytest.mark.parametrize(
    "terminal_state, expected_observed",
    [
        (HubServerState.FAILED, "FAILED"),
        (HubServerState.NOT_FOUND, "NOT_FOUND"),
    ],
)
def test_terminal_gpu_start_failure_retains_lease_and_allows_same_device_retry(
    gpu_env, terminal_state, expected_observed
):
    app, hub, client = gpu_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    workspace = _create_gpu(client, me, f"terminal-{terminal_state.value.lower()}")
    started = _start_gpu(
        client,
        me,
        workspace["id"],
        f"terminal-{terminal_state.value.lower()}-start",
    )
    assert started.status_code == 202, started.text
    operation_id = started.json()["operation"]["id"]
    hub.request_start = AsyncMock(
        return_value=HubServer(state=terminal_state, progress_percent=100)
    )
    one_attempt = replace(app.state.settings, worker_max_attempts=1)

    assert asyncio.run(
        _worker(app, hub, "terminal-gpu-worker", settings=one_attempt).process_next()
    )
    with app.state.session_factory() as db:
        operation = db.get(Operation, operation_id)
        row = db.get(Workspace, workspace["id"])
        assert operation and operation.status == "FAILED"
        assert operation.error_code == "SPAWN_ATTEMPTS_EXHAUSTED"
        assert row and row.observed_state == expected_observed
        assert row.desired_state == "RUNNING"
        assert row.assigned_gpu_device_id == GPU_A
        reservation = active_reservations(db)
        assert reservation.count == 0
        assert reservation.gpu_count == 1

    retried = _start_gpu(
        client,
        me,
        workspace["id"],
        f"terminal-{terminal_state.value.lower()}-retry",
    )
    assert retried.status_code == 202, retried.text
    with app.state.session_factory() as db:
        row = db.get(Workspace, workspace["id"])
        assert row and row.assigned_gpu_device_id == GPU_A
        reservation = active_reservations(db)
        assert reservation.count == 1
        assert reservation.gpu_count == 1


def test_gpu_inventory_uuid_drift_fails_closed_until_explicit_stop(gpu_env):
    app, hub, client = gpu_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    workspace = _create_gpu(client, me, "uuid-drift")
    started = _start_gpu(client, me, workspace["id"], "uuid-drift-start")
    assert started.status_code == 202, started.text
    operation_id = started.json()["operation"]["id"]
    drifted_settings = replace(app.state.settings, nvidia_gpu_device_ids=(GPU_B,))

    assert asyncio.run(
        _worker(app, hub, "uuid-drift-worker", settings=drifted_settings).process_next()
    )
    with app.state.session_factory() as db:
        operation = db.get(Operation, operation_id)
        row = db.get(Workspace, workspace["id"])
        assert operation and operation.status == "FAILED"
        assert operation.error_code == "GPU_ALLOCATION_INVARIANT_FAILED"
        assert row and row.assigned_gpu_device_id == GPU_A
        assert db.scalar(select(func.count()).select_from(SpawnAuthorization)) == 0
        assert active_reservations(db).gpu_count == 1

    service = WorkspaceService(drifted_settings, app.state.token_cipher)
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.hub_username == "alice"))
        portal_session = db.scalar(
            select(UserSession).where(UserSession.user_id == owner.id)
        )
        assert owner and portal_session
        with pytest.raises(AppError) as rejected:
            service.action(
                db,
                owner=owner,
                actor=owner,
                portal_session=portal_session,
                credential_mode="USER_DELEGATED",
                workspace_id=workspace["id"],
                target=DesiredState.RUNNING,
                idempotency_key="uuid-drift-retry-before-stop",
                request_id="test:uuid-drift-retry-before-stop",
            )
        assert rejected.value.code == "GPU_ALLOCATION_INVARIANT_FAILED"

    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.hub_username == "alice"))
        portal_session = db.scalar(
            select(UserSession).where(UserSession.user_id == owner.id)
        )
        assert owner and portal_session
        stopped = service.action(
            db,
            owner=owner,
            actor=owner,
            portal_session=portal_session,
            credential_mode="USER_DELEGATED",
            workspace_id=workspace["id"],
            target=DesiredState.STOPPED,
            idempotency_key="uuid-drift-explicit-stop",
            request_id="test:uuid-drift-explicit-stop",
        )
        assert stopped.operation.status == "SUCCEEDED"
        assert stopped.workspace.assigned_gpu_device_id is None

    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.hub_username == "alice"))
        portal_session = db.scalar(
            select(UserSession).where(UserSession.user_id == owner.id)
        )
        assert owner and portal_session
        retried = service.action(
            db,
            owner=owner,
            actor=owner,
            portal_session=portal_session,
            credential_mode="USER_DELEGATED",
            workspace_id=workspace["id"],
            target=DesiredState.RUNNING,
            idempotency_key="uuid-drift-retry-after-stop",
            request_id="test:uuid-drift-retry-after-stop",
        )
        assert retried.workspace.assigned_gpu_device_id == GPU_B
        assert active_reservations(db).gpu_count == 1


def test_active_gpu_runtime_without_device_assignment_fails_closed(gpu_env):
    app, hub, client = gpu_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    workspace = _create_gpu(client, me, "missing-device")
    started = _start_gpu(client, me, workspace["id"], "missing-device-start")
    assert started.status_code == 202, started.text

    with app.state.session_factory() as db:
        row = db.get(Workspace, workspace["id"])
        assert row and row.assigned_gpu_device_id == GPU_A
        row.assigned_gpu_device_id = None
        db.commit()

    with app.state.session_factory() as db:
        with pytest.raises(AppError) as rejected:
            active_reservations(db)
        assert rejected.value.code == "GPU_ALLOCATION_INVARIANT_FAILED"
