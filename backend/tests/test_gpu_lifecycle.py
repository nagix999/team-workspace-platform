from __future__ import annotations

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
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
    WorkspaceGpuLease,
    WorkspaceProfile,
)
from app.services.gpu_allocations import workspace_gpu_device_ids
from app.services.workspaces import (
    WorkspaceService,
    active_reservations,
)
from app.worker import OperationWorker

from conftest import login, mutation_headers, provision


GPU_A = "GPU-01234567-89ab-cdef-0123-456789abcdef"
GPU_B = "GPU-fedcba98-7654-3210-fedc-ba9876543210"
GPU_C = "GPU-11111111-2222-3333-4444-555555555555"
GPU_D = "GPU-99999999-aaaa-bbbb-cccc-dddddddddddd"
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


@pytest.fixture
def multi_gpu_env(tmp_path):
    gpu_ids = (GPU_A, GPU_C, GPU_B)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'platform-multi-gpu.db'}",
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
        workspace_cpu_budget_millicores=32_000,
        workspace_memory_budget_mb=32_768,
        nvidia_gpu_device_ids=gpu_ids,
        portal_security_domain="example.com",
        hub_user_security_domain="example.net",
    )
    hub = FakeJupyterHubProvider()
    app = create_app(settings, hub)
    Base.metadata.create_all(app.state.engine)
    with app.state.session_factory() as db:
        for count in (1, 2):
            db.add(
                WorkspaceProfile(
                    id=f"{GPU_PROFILE_ID}-gpu{count}",
                    version=1,
                    name=f"Python 3.12 CUDA · GPU {count}",
                    kernel_name="python312-cuda",
                    kernel_display_name="Python 3.12 (CUDA 12.6)",
                    python_version="3.12.13",
                    accelerator_kind="nvidia",
                    gpu_count=count,
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
                    config_digest="sha256:" + str(count) * 64,
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


def _create_gpu_count(client: TestClient, me: dict, key: str, count: int) -> dict:
    response = client.post(
        "/api/v1/workspaces",
        json={
            "profile_id": f"{GPU_PROFILE_ID}-gpu{count}",
            "profile_version": 1,
        },
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

    # Two authenticated clients are required for the HTTP-level race. Do not
    # open two TestClient lifespans on the same ASGI app: Starlette's blocking
    # portals can deadlock one another. A second app/engine against the same
    # SQLite database preserves the real cross-process transaction boundary.
    bob_app = create_app(app.state.settings, hub)
    with TestClient(bob_app, base_url=app.state.settings.portal_origin) as bob_client:
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


def test_stop_during_claimed_gpu_start_keeps_lease_until_hub_stop(gpu_env):
    app, hub, alice_client = gpu_env
    alice, *_ = login(alice_client, hub, "alice")
    provision(app, "alice")
    alice_workspace = _create_gpu(alice_client, alice, "inflight-alice")

    # Use a separate ASGI app against the same SQLite database so Bob can make
    # an independent admission request while Alice's lifecycle worker is
    # deliberately paused inside the Hub request.
    bob_app = create_app(app.state.settings, hub)
    with TestClient(bob_app, base_url=app.state.settings.portal_origin) as bob_client:
        bob, *_ = login(bob_client, hub, "bob")
        provision(app, "bob")
        bob_workspace = _create_gpu(bob_client, bob, "inflight-bob")

        started = _start_gpu(
            alice_client,
            alice,
            alice_workspace["id"],
            "inflight-alice-start",
        )
        assert started.status_code == 202, started.text
        start_operation_id = started.json()["operation"]["id"]

        request_started = Event()
        allow_request_return = Event()
        original_request_start = hub.request_start

        async def paused_request_start(*args, **kwargs):
            # Register the server on the Hub side before pausing.  The portal's
            # last observation is still the older NOT_FOUND value, which is the
            # exact window in which the former STOP no-op released the GPU.
            result = await original_request_start(*args, **kwargs)
            request_started.set()
            if not allow_request_return.wait(timeout=10):
                raise TimeoutError("test did not release the paused Hub start")
            return result

        hub.request_start = paused_request_start
        start_worker = _worker(app, hub, "inflight-start-worker")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, start_worker.process_next())
            assert request_started.wait(timeout=5)

            stopped = alice_client.post(
                f"/api/v1/workspaces/{alice_workspace['id']}/actions/stop",
                headers=mutation_headers(alice, "inflight-alice-stop"),
            )
            assert stopped.status_code == 202, stopped.text
            assert stopped.json()["operation"]["status"] == "PENDING"

            with app.state.session_factory() as db:
                start_operation = db.get(Operation, start_operation_id)
                row = db.get(Workspace, alice_workspace["id"])
                assert start_operation and start_operation.status == "CANCELLED"
                assert row and workspace_gpu_device_ids(db, row) == (GPU_A,)
                assert db.scalar(
                    select(func.count()).select_from(WorkspaceGpuLease)
                ) == 1
                assert db.scalar(
                    select(func.count())
                    .select_from(SpawnAuthorization)
                    .where(SpawnAuthorization.operation_id == start_operation_id)
                ) == 1

            # The superseded start has already created a Hub server, but its GPU
            # remains exclusively leased until the queued STOP observes and
            # tears down that server.
            rejected_overlap = _start_gpu(
                bob_client,
                bob,
                bob_workspace["id"],
                "inflight-bob-overlap",
            )
            assert rejected_overlap.status_code == 429, rejected_overlap.text
            assert rejected_overlap.json()["error"]["code"] in {
                "GPU_CAPACITY_LIMIT",
                "RESOURCE_CAPACITY_LIMIT",
            }

            allow_request_return.set()
            assert future.result(timeout=5) is True

        assert asyncio.run(
            _worker(app, hub, "inflight-stop-worker").process_next()
        )
        assert hub.stop_count == 1
        with app.state.session_factory() as db:
            row = db.get(Workspace, alice_workspace["id"])
            assert row and row.observed_state == "STOPPED"
            assert workspace_gpu_device_ids(db, row) == ()
            assert db.scalar(
                select(func.count()).select_from(WorkspaceGpuLease)
            ) == 0

        admitted_after_cleanup = _start_gpu(
            bob_client,
            bob,
            bob_workspace["id"],
            "inflight-bob-after-cleanup",
        )
        assert admitted_after_cleanup.status_code == 202, admitted_after_cleanup.text
        with app.state.session_factory() as db:
            row = db.get(Workspace, bob_workspace["id"])
            assert row and workspace_gpu_device_ids(db, row) == (GPU_A,)
            assert db.scalar(
                select(func.count()).select_from(WorkspaceGpuLease)
            ) == 1


def test_start_during_claimed_gpu_stop_queues_real_hub_start(gpu_env):
    app, hub, client = gpu_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    workspace = _create_gpu(client, me, "inflight-stop-start")
    with app.state.session_factory() as db:
        server_name = db.get(Workspace, workspace["id"]).hub_server_name

    started = _start_gpu(client, me, workspace["id"], "inflight-stop-initial-start")
    assert started.status_code == 202, started.text
    assert asyncio.run(_worker(app, hub, "initial-start-worker").process_next())

    stopped = client.post(
        f"/api/v1/workspaces/{workspace['id']}/actions/stop",
        headers=mutation_headers(me, "inflight-stop-request"),
    )
    assert stopped.status_code == 202, stopped.text
    stop_operation_id = stopped.json()["operation"]["id"]

    request_stopped = Event()
    allow_request_return = Event()
    original_request_stop = hub.request_stop

    async def paused_request_stop(*args, **kwargs):
        # Stop the Hub server before pausing. The portal row still contains the
        # earlier fresh RUNNING observation until this worker returns.
        result = await original_request_stop(*args, **kwargs)
        request_stopped.set()
        if not allow_request_return.wait(timeout=10):
            raise TimeoutError("test did not release the paused Hub stop")
        return result

    hub.request_stop = paused_request_stop
    stop_worker = _worker(app, hub, "inflight-stop-worker")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, stop_worker.process_next())
        assert request_stopped.wait(timeout=5)

        restarted = _start_gpu(
            client,
            me,
            workspace["id"],
            "inflight-stop-recovery-start",
        )
        assert restarted.status_code == 202, restarted.text
        assert restarted.json()["operation"]["status"] == "PENDING"
        restart_operation_id = restarted.json()["operation"]["id"]

        with app.state.session_factory() as db:
            stop_operation = db.get(Operation, stop_operation_id)
            restart_operation = db.get(Operation, restart_operation_id)
            row = db.get(Workspace, workspace["id"])
            assert stop_operation and stop_operation.status == "CANCELLED"
            assert restart_operation and restart_operation.status == "PENDING"
            assert row and row.desired_state == "RUNNING"
            assert workspace_gpu_device_ids(db, row) == (GPU_A,)

        allow_request_return.set()
        assert future.result(timeout=5) is True

    # The cancelled worker cannot apply its stale STOP result. The newly queued
    # START must perform a real Hub call and converge both planes to RUNNING.
    assert hub.servers[("alice", server_name)].state == HubServerState.STOPPED
    assert asyncio.run(_worker(app, hub, "recovery-start-worker").process_next())
    assert hub.start_count == 2
    assert hub.stop_count == 1
    with app.state.session_factory() as db:
        row = db.get(Workspace, workspace["id"])
        assert row and row.desired_state == "RUNNING"
        assert row.observed_state == "RUNNING"
        assert workspace_gpu_device_ids(db, row) == (GPU_A,)


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
        # RUNNING intent remains fail-closed for CPU/RAM as well as the GPU
        # lease. A lost Hub response can otherwise leave a real container
        # outside aggregate admission accounting until reconciliation.
        assert reservation.count == 1
        assert reservation.cpu_millicores == 2_000
        assert reservation.memory_mb == 1_024
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


def test_multi_gpu_pool_allocates_deterministically_and_releases_after_stop(
    multi_gpu_env,
):
    app, hub, alice_client = multi_gpu_env
    alice, *_ = login(alice_client, hub, "alice")
    provision(app, "alice")
    alice_workspace = _create_gpu_count(alice_client, alice, "alice-two", 2)
    started = _start_gpu(
        alice_client, alice, alice_workspace["id"], "alice-two-start"
    )
    assert started.status_code == 202, started.text

    with TestClient(app, base_url=app.state.settings.portal_origin) as bob_client:
        bob, *_ = login(bob_client, hub, "bob")
        provision(app, "bob")
        bob_workspace = _create_gpu_count(bob_client, bob, "bob-one", 1)
        bob_started = _start_gpu(
            bob_client, bob, bob_workspace["id"], "bob-one-start"
        )
        assert bob_started.status_code == 202, bob_started.text

    with app.state.session_factory() as db:
        alice_row = db.get(Workspace, alice_workspace["id"])
        bob_row = db.get(Workspace, bob_workspace["id"])
        assert alice_row and bob_row
        assert workspace_gpu_device_ids(db, alice_row) == (GPU_A, GPU_C)
        assert workspace_gpu_device_ids(db, bob_row) == (GPU_B,)
        assert db.scalar(select(func.count()).select_from(WorkspaceGpuLease)) == 3
        assert active_reservations(db).gpu_count == 3

    assert asyncio.run(_worker(app, hub, "multi-start-worker").process_next())
    with app.state.session_factory() as db:
        authorization = db.scalar(
            select(SpawnAuthorization)
            .where(SpawnAuthorization.workspace_id == alice_workspace["id"])
            .order_by(SpawnAuthorization.attempt_no.desc())
        )
        assert authorization is not None
        assert authorization.gpu_count == 2
        assert json.loads(authorization.gpu_device_ids_json) == [GPU_A, GPU_C]

    # Drain Bob's earlier START operation before enqueueing Alice's STOP so the
    # next worker invocation deterministically observes the stop request.
    assert asyncio.run(_worker(app, hub, "multi-bob-start-worker").process_next())

    stopped = alice_client.post(
        f"/api/v1/workspaces/{alice_workspace['id']}/actions/stop",
        headers=mutation_headers(alice, "alice-two-stop"),
    )
    assert stopped.status_code == 202, stopped.text
    with app.state.session_factory() as db:
        row = db.get(Workspace, alice_workspace["id"])
        assert row and workspace_gpu_device_ids(db, row) == (GPU_A, GPU_C)

    assert asyncio.run(_worker(app, hub, "multi-stop-worker").process_next())
    with app.state.session_factory() as db:
        row = db.get(Workspace, alice_workspace["id"])
        assert row and workspace_gpu_device_ids(db, row) == ()
        assert active_reservations(db).gpu_count == 1


def test_active_reservations_reads_profiles_and_all_gpu_leases_in_one_query(
    multi_gpu_env,
):
    app, hub, client = multi_gpu_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    one_gpu = _create_gpu_count(client, me, "one-query-one", 1)
    two_gpu = _create_gpu_count(client, me, "one-query-two", 2)
    assert _start_gpu(
        client, me, one_gpu["id"], "one-query-one-start"
    ).status_code == 202
    assert _start_gpu(
        client, me, two_gpu["id"], "one-query-two-start"
    ).status_code == 202

    select_statements: list[str] = []

    def capture_select(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", capture_select)
    try:
        with app.state.session_factory() as db:
            reservations = active_reservations(db)
    finally:
        event.remove(app.state.engine, "before_cursor_execute", capture_select)

    assert reservations.count == 2
    assert reservations.gpu_count == 3
    assert len(select_statements) == 1
    assert "workspace_gpu_leases" in select_statements[0]


def test_concurrent_two_gpu_starts_cannot_overlap_device_leases(multi_gpu_env):
    app, hub, alice_client = multi_gpu_env
    alice, *_ = login(alice_client, hub, "alice")
    provision(app, "alice")
    alice_workspace = _create_gpu_count(alice_client, alice, "alice-two-race", 2)

    bob_app = create_app(app.state.settings, hub)
    with TestClient(bob_app, base_url=app.state.settings.portal_origin) as bob_client:
        bob, *_ = login(bob_client, hub, "bob")
        provision(app, "bob")
        bob_workspace = _create_gpu_count(bob_client, bob, "bob-two-race", 2)
        barrier = Barrier(2)

        def start(client, me, workspace_id, key):
            barrier.wait()
            return _start_gpu(client, me, workspace_id, key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [
                future.result()
                for future in (
                    pool.submit(
                        start,
                        alice_client,
                        alice,
                        alice_workspace["id"],
                        "alice-two-race-start",
                    ),
                    pool.submit(
                        start,
                        bob_client,
                        bob,
                        bob_workspace["id"],
                        "bob-two-race-start",
                    ),
                )
            ]

    assert sorted(response.status_code for response in responses) == [202, 429]
    with app.state.session_factory() as db:
        leases = db.scalars(
            select(WorkspaceGpuLease).order_by(WorkspaceGpuLease.gpu_device_id)
        ).all()
        assert len(leases) == 2
        assert len({lease.workspace_id for lease in leases}) == 1
        assert [lease.gpu_device_id for lease in leases] == [GPU_A, GPU_C]


def test_multi_gpu_spawn_rejects_assignment_not_contained_in_runtime_pool(
    multi_gpu_env,
):
    app, hub, client = multi_gpu_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    workspace = _create_gpu_count(client, me, "pool-subset", 2)
    started = _start_gpu(client, me, workspace["id"], "pool-subset-start")
    assert started.status_code == 202, started.text

    # The durable assignment is A+C, while this simulated runtime inventory
    # advertises A+B. Authorization must fail closed instead of silently
    # substituting a different device or issuing a partial binding.
    drifted_settings = replace(
        app.state.settings, nvidia_gpu_device_ids=(GPU_A, GPU_D, GPU_B)
    )
    assert asyncio.run(
        _worker(app, hub, "pool-subset-worker", settings=drifted_settings).process_next()
    )
    with app.state.session_factory() as db:
        operation = db.get(Operation, started.json()["operation"]["id"])
        row = db.get(Workspace, workspace["id"])
        assert operation and operation.status == "FAILED"
        assert operation.error_code == "GPU_ALLOCATION_INVARIANT_FAILED"
        assert row and workspace_gpu_device_ids(db, row) == (GPU_A, GPU_C)
        assert db.scalar(select(func.count()).select_from(SpawnAuthorization)) == 0
        assert active_reservations(db).gpu_count == 2
        assert active_reservations(db).gpu_count == 2
