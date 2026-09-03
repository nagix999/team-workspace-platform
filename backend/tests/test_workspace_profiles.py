from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import Base
from app.errors import AppError
from app.hub import FakeJupyterHubProvider
from app.main import create_app
from app.models import (
    Operation,
    User,
    UserSession,
    Workspace,
    WorkspaceProfile,
    WorkspaceProfileOffer,
)
from app.services.profile_offers import ensure_default_offers
from app.services.resource_policy import get_resource_policy
from app.serialization import profile_dict

from conftest import login, mutation_headers, provision


@pytest.fixture
def resource_budget_env(settings):
    constrained = replace(
        settings,
        workspace_cpu_budget_millicores=2_000,
        workspace_memory_budget_mb=2_048,
        admin_usernames=("platform-admin",),
    )
    hub = FakeJupyterHubProvider()
    app = create_app(constrained, hub)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=constrained.portal_origin) as client:
        yield app, hub, client


def _add_profile(
    app,
    *,
    profile_id: str,
    version: int,
    cpu_limit: str = "2.0",
    memory_limit_mb: int = 2048,
    selectable: bool = True,
    disk_quota_enforced: bool = False,
) -> None:
    digest = (
        "sha256:"
        + hashlib.sha256(
            f"{profile_id}:{version}:{cpu_limit}:{memory_limit_mb}".encode()
        ).hexdigest()
    )
    with app.state.session_factory() as db:
        db.add(
            WorkspaceProfile(
                id=profile_id,
                version=version,
                name=f"{profile_id} v{version}",
                kernel_name="python3",
                kernel_display_name="Python 3 (ipykernel)",
                python_version="3.12.11",
                image_ref="example.invalid/singleuser@sha256:" + "c" * 64,
                cpu_limit=cpu_limit,
                memory_limit_mb=memory_limit_mb,
                pids_limit=256,
                private_disk_limit_mb=1024,
                private_disk_quota_enforced=disk_quota_enforced,
                provider_options_json=(
                    '{"gid":100,"private_disk_hard_limit_bytes":1073741824,'
                    '"uid":1000}'
                ),
                config_digest=digest,
                enabled=True,
                selectable=selectable,
            )
        )
        db.flush()
        ensure_default_offers(db)
        policy = get_resource_policy(db, app.state.settings, create=True)
        cpus = set(json.loads(policy.selectable_cpu_millicores_json))
        memories = set(json.loads(policy.selectable_memory_mb_json))
        cpus.add(int(float(cpu_limit) * 1000))
        memories.add(memory_limit_mb)
        policy.selectable_cpu_millicores_json = json.dumps(sorted(cpus))
        policy.selectable_memory_mb_json = json.dumps(sorted(memories))
        db.commit()


def test_profile_serialization_exposes_only_an_effective_disk_limit():
    profile = WorkspaceProfile(
        id="python-serialization",
        version=1,
        name="Python serialization",
        kernel_name="python3",
        kernel_display_name="Python 3",
        python_version="3.12.11",
        accelerator_kind="none",
        gpu_count=0,
        cuda_version=None,
        gpu_framework=None,
        gpu_framework_version=None,
        image_ref="example.invalid/singleuser@sha256:" + "d" * 64,
        cpu_limit="1.0",
        memory_limit_mb=1024,
        pids_limit=256,
        private_disk_limit_mb=1024,
        private_disk_quota_enforced=False,
        provider_options_json="{}",
        config_digest="sha256:" + "e" * 64,
        enabled=True,
        selectable=True,
    )

    serialized = profile_dict(profile)
    assert serialized["private_disk_limit_mb"] is None
    assert serialized["accelerator_kind"] == "none"
    assert serialized["gpu_count"] == 0
    assert serialized["cuda_version"] is None
    assert serialized["gpu_framework"] is None
    assert serialized["gpu_framework_version"] is None
    profile.private_disk_quota_enforced = True
    assert profile_dict(profile)["private_disk_limit_mb"] == 1024


def test_profile_serialization_exposes_pinned_gpu_runtime_metadata():
    profile = WorkspaceProfile(
        id="python-gpu-serialization",
        version=1,
        name="Python GPU serialization",
        kernel_name="python3",
        kernel_display_name="Python 3 (CUDA)",
        python_version="3.12.11",
        accelerator_kind="nvidia",
        gpu_count=1,
        cuda_version="12.6",
        gpu_framework="pytorch",
        gpu_framework_version="2.7.1",
        image_ref="example.invalid/singleuser-gpu@sha256:" + "d" * 64,
        cpu_limit="4.0",
        memory_limit_mb=8192,
        pids_limit=512,
        private_disk_limit_mb=None,
        private_disk_quota_enforced=False,
        provider_options_json="{}",
        config_digest="sha256:" + "e" * 64,
        enabled=True,
        selectable=True,
    )

    serialized = profile_dict(profile)
    assert serialized["accelerator_kind"] == "nvidia"
    assert serialized["gpu_count"] == 1
    assert serialized["cuda_version"] == "12.6"
    assert serialized["gpu_framework"] == "pytorch"
    assert serialized["gpu_framework_version"] == "2.7.1"


def test_existing_offer_remains_pinned_when_new_runtime_version_is_imported(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    _add_profile(app, profile_id="python-standard", version=2)

    catalog = client.get("/api/v1/workspace-profiles")
    assert catalog.status_code == 200
    item = catalog.json()["items"][0]
    assert item["id"] == "python-standard"
    assert item["version"] == 1
    assert item["name"] == "Python standard"
    assert item["cpu_limit"] == "1.0"
    assert item["memory_limit_mb"] == 1024
    assert item["accelerator_kind"] == "none"
    assert item["gpu_count"] == 0
    with app.state.session_factory() as db:
        stored = db.get(WorkspaceProfile, ("python-standard", 2))
        assert stored is not None
        # The legacy slot/profile value remains intact; only the public effective
        # limit is null while enforcement is disabled.
        assert stored.private_disk_limit_mb == 1024

    created = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "exact-profile-v2"),
    )
    assert created.status_code == 202, created.text
    workspace = created.json()["workspace"]
    assert workspace["profile_id"] == "python-standard"
    assert workspace["profile_version"] == 1
    assert workspace["profile_name"] == "Python standard"
    assert workspace["kernel_name"] == "python3"
    assert workspace["kernel_display_name"] == "Python 3"
    assert workspace["python_version"] == "3.12.0"
    assert workspace["cpu_limit"] == "1.0"
    assert workspace["memory_limit_mb"] == 1024
    assert workspace["accelerator_kind"] == "none"
    assert workspace["gpu_count"] == 0
    assert workspace["private_disk_limit_mb"] is None
    assert workspace["private_disk_quota_enforced"] is False
    assert "image_ref" not in workspace
    assert "provider_options_json" not in workspace


def test_bootstrap_offer_moves_when_its_old_runtime_is_retired(app_env):
    app, hub, client = app_env
    login(client, hub, "alice")
    provision(app, "alice")
    _add_profile(app, profile_id="python-standard", version=2)
    with app.state.session_factory() as db:
        old = db.get(WorkspaceProfile, ("python-standard", 1))
        offer = db.get(WorkspaceProfileOffer, "python-standard")
        assert old is not None and offer is not None
        old.selectable = False
        original_version = offer.row_version
        ensure_default_offers(db)
        db.commit()

        assert offer.runtime_profile_id == "python-standard"
        assert offer.runtime_profile_version == 2
        assert offer.row_version == original_version + 1


def test_catalog_discloses_a_numeric_disk_limit_only_when_enforced(app_env):
    app, hub, client = app_env
    login(client, hub, "alice")
    _add_profile(
        app,
        profile_id="python-enforced",
        version=1,
        disk_quota_enforced=True,
    )

    catalog = client.get("/api/v1/workspace-profiles")
    assert catalog.status_code == 200
    enforced = next(
        item for item in catalog.json()["items"] if item["id"] == "python-enforced"
    )
    assert enforced["private_disk_limit_mb"] == 1024
    assert enforced["private_disk_quota_enforced"] is True


def test_stale_exact_version_and_missing_version_are_rejected(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    _add_profile(app, profile_id="python-standard", version=2)
    with app.state.session_factory() as db:
        offer = db.get(WorkspaceProfileOffer, "python-standard")
        assert offer is not None
        offer.row_version = 2
        offer.name = "Renamed standard"
        db.commit()

    stale = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "stale-profile-v1"),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PROFILE_VERSION_STALE"

    missing_version = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard"},
        headers=mutation_headers(me, "missing-profile-version"),
    )
    assert missing_version.status_code == 422
    assert missing_version.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image", "attacker.invalid/root:latest"),
        ("command", ["sh", "-c", "id"]),
        ("volume_name", "somebody-elses-volume"),
        ("cpu_limit", 99),
        ("memory_limit_mb", 999999),
        ("private_disk_limit_mb", 999999),
    ],
)
def test_create_rejects_client_supplied_execution_fields(
    app_env, field: str, value: object
):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    response = client.post(
        "/api/v1/workspaces",
        json={
            "profile_id": "python-standard",
            "profile_version": 1,
            field: value,
        },
        headers=mutation_headers(me, f"forbidden-{field}"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_profile_version_is_a_strict_positive_integer(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    for number, invalid in enumerate((True, "1", 0, -1)):
        response = client.post(
            "/api/v1/workspaces",
            json={"profile_id": "python-standard", "profile_version": invalid},
            headers=mutation_headers(me, f"invalid-version-{number}"),
        )
        assert response.status_code == 422


def test_idempotency_key_cannot_be_rebound_to_another_profile_or_action(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    _add_profile(app, profile_id="python-large", version=1)

    first = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "bound-request"),
    )
    assert first.status_code == 202
    replay = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "bound-request"),
    )
    assert replay.status_code == 202
    assert replay.json()["workspace"]["id"] == first.json()["workspace"]["id"]

    rebound_profile = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-large", "profile_version": 1},
        headers=mutation_headers(me, "bound-request"),
    )
    assert rebound_profile.status_code == 409
    assert rebound_profile.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    rebound_action = client.post(
        f"/api/v1/workspaces/{first.json()['workspace']['id']}/actions/start",
        headers=mutation_headers(me, "bound-request"),
    )
    assert rebound_action.status_code == 409
    assert rebound_action.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Workspace.id))) == 1
        assert db.scalar(select(func.count(Operation.id))) == 1


def test_unselectable_profile_is_hidden_but_pinned_workspace_can_restart(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "before-unselectable"),
    )
    assert created.status_code == 202
    workspace_id = created.json()["workspace"]["id"]

    with app.state.session_factory() as db:
        profile = db.get(WorkspaceProfile, ("python-standard", 1))
        workspace = db.get(Workspace, workspace_id)
        create_operation = db.get(Operation, created.json()["operation"]["id"])
        assert profile and workspace and create_operation
        profile.selectable = False
        workspace.observed_state = "RUNNING"
        workspace.stale = False
        create_operation.status = "SUCCEEDED"
        db.commit()

    assert client.get("/api/v1/workspace-profiles").json() == {"items": []}
    rejected_new = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "after-unselectable-new"),
    )
    assert rejected_new.status_code == 409
    assert rejected_new.json()["error"]["code"] == "PROFILE_NOT_SELECTABLE"

    stopped = client.post(
        f"/api/v1/workspaces/{workspace_id}/actions/stop",
        headers=mutation_headers(me, "after-unselectable-stop"),
    )
    assert stopped.status_code == 202
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        stop_operation = db.get(Operation, stopped.json()["operation"]["id"])
        assert workspace and stop_operation
        workspace.observed_state = "STOPPED"
        workspace.stale = False
        stop_operation.status = "SUCCEEDED"
        db.commit()
    restarted = client.post(
        f"/api/v1/workspaces/{workspace_id}/actions/start",
        headers=mutation_headers(me, "after-unselectable-start"),
    )
    assert restarted.status_code == 202, restarted.text


def test_aggregate_resource_admission_and_capacity_envelope(resource_budget_env):
    app, hub, client = resource_budget_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    for number in range(2):
        created = client.post(
            "/api/v1/workspaces",
            json={"profile_id": "python-standard", "profile_version": 1},
            headers=mutation_headers(me, f"resource-create-{number}"),
        )
        assert created.status_code == 202, created.text
        response = client.post(
            f"/api/v1/workspaces/{created.json()['workspace']['id']}/actions/start",
            headers=mutation_headers(me, f"resource-start-{number}"),
        )
        assert response.status_code == 202, response.text

    candidate = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "resource-create-over-limit"),
    )
    assert candidate.status_code == 202
    rejected = client.post(
        f"/api/v1/workspaces/{candidate.json()['workspace']['id']}/actions/start",
        headers=mutation_headers(me, "resource-start-over-limit"),
    )
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "RESOURCE_CAPACITY_LIMIT"

    capacity = client.get("/api/v1/capacity").json()
    assert capacity["global"] == {
        "active": 2,
        "limit": 15,
        "kernel_idle_timeout_seconds": 3_600,
        "resources": {
            "cpu_millicores": {"reserved": 2_000, "limit": 2_000},
            "memory_mb": {"reserved": 2_048, "limit": 2_048},
            "gpu_count": {"reserved": 0, "limit": 0},
        },
    }

    login(client, hub, "platform-admin")
    admin_capacity = client.get("/api/v1/admin/capacity")
    assert admin_capacity.status_code == 200
    admin_body = admin_capacity.json()
    assert admin_body["workspaces"] == {
        "created": 3,
        "running": 0,
        "reserved": 2,
        "limit": 15,
    }
    assert admin_body["resources"] == capacity["global"]["resources"]


def test_start_is_rejected_when_other_reservations_fill_resource_budget(
    resource_budget_env,
):
    app, hub, client = resource_budget_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    candidate = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "candidate-create"),
    ).json()
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, candidate["workspace"]["id"])
        operation = db.get(Operation, candidate["operation"]["id"])
        assert workspace and operation
        workspace.desired_state = "STOPPED"
        workspace.observed_state = "STOPPED"
        workspace.stale = False
        operation.status = "SUCCEEDED"
        db.commit()

    for number in range(2):
        created = client.post(
            "/api/v1/workspaces",
            json={"profile_id": "python-standard", "profile_version": 1},
            headers=mutation_headers(me, f"budget-filler-{number}"),
        )
        assert created.status_code == 202
        response = client.post(
            f"/api/v1/workspaces/{created.json()['workspace']['id']}/actions/start",
            headers=mutation_headers(me, f"budget-filler-start-{number}"),
        )
        assert response.status_code == 202

    rejected = client.post(
        f"/api/v1/workspaces/{candidate['workspace']['id']}/actions/start",
        headers=mutation_headers(me, "candidate-restart"),
    )
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "RESOURCE_CAPACITY_LIMIT"


def test_begin_immediate_serializes_concurrent_stopped_workspace_creation(
    resource_budget_env,
):
    app, hub, client = resource_budget_env
    login(client, hub, "race-user")
    user_id = provision(app, "race-user")
    barrier = Barrier(3)

    def create(number: int) -> str:
        with app.state.session_factory() as db:
            user = db.get(User, user_id)
            portal_session = db.scalar(
                select(UserSession).where(UserSession.user_id == user_id)
            )
            assert user and portal_session
            barrier.wait()
            try:
                app.state.workspace_service.create(
                    db,
                    user=user,
                    portal_session=portal_session,
                    profile_id="python-standard",
                    profile_version=1,
                    display_name=None,
                    idempotency_key=f"concurrent-resource-{number}",
                    request_id=f"concurrent-resource-{number}",
                )
            except AppError as exc:
                return exc.code
            return "ACCEPTED"

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(create, range(3)))
    assert sorted(results) == ["ACCEPTED", "ACCEPTED", "ACCEPTED"]
    capacity = client.get("/api/v1/capacity").json()["global"]
    assert capacity["active"] == 0
    assert capacity["resources"]["cpu_millicores"]["reserved"] == 0
    assert capacity["resources"]["memory_mb"]["reserved"] == 0
