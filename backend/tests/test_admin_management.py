from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from unittest.mock import AsyncMock

from app.accelerators import gpu_inventory_digest
from app.db import Base
from app.domain import HubServerState
from app.errors import AppError
from app.hub import FakeJupyterHubProvider, HubServer
from app.main import create_app
from app.models import (
    AuditEvent,
    EnvironmentVariable,
    MutationReceipt,
    Operation,
    SpawnAuthorization,
    User,
    Workspace,
    WorkspaceDeletionJob,
    WorkspaceVolumeSlot,
)
from app.policy_values import kernel_idle_timeout_is_valid
from app.services.deletions import (
    claim_deletion_job,
    complete_deletion_job,
    fail_deletion_job,
)
from app.services.mutations import mutation_request
from app.services.resource_policy import get_resource_policy
from app.services.workspaces import active_reservations
from app.worker import OperationWorker, read_admin_lifecycle_token

from conftest import login, mutation_headers, provision


@pytest.fixture
def management_env(settings, tmp_path):
    admin_token_path = tmp_path / "admin-lifecycle-token"
    admin_token = "admin-lifecycle-token-0123456789ab"
    admin_token_path.write_text(admin_token, encoding="ascii")
    admin_token_path.chmod(0o600)
    managed = replace(
        settings,
        admin_usernames=("admin", "admin-b"),
        workspace_deletion_enabled=True,
        deletion_max_attempts=1,
        worker_retry_seconds=0,
        admin_lifecycle_token_file=str(admin_token_path),
    )
    hub = FakeJupyterHubProvider()
    hub.register_login(
        "platform-admin-service",
        code="unused-admin-service-code",
        token=admin_token,
        scopes=("admin:servers",),
    )
    app = create_app(managed, hub)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=managed.portal_origin) as client:
        yield app, hub, client


def _create(client: TestClient, me: dict, *, key: str, **extra):
    environment = extra.pop("environment", [])
    payload = {"profile_id": "python-standard", "profile_version": 1, **extra}
    created = client.post(
        "/api/v1/workspaces",
        json=payload,
        headers=mutation_headers(me, key),
    )
    assert created.status_code == 202, created.text
    workspace_id = created.json()["workspace"]["id"]
    for number, item in enumerate(environment):
        response = client.put(
            f"/api/v1/workspaces/{workspace_id}/environment-variables/{item['name']}",
            json={"value": item["value"], "is_secret": item["is_secret"]},
            headers=mutation_headers(me, f"{key}-environment-{number}"),
        )
        assert response.status_code == 200, response.text
    started = client.post(
        f"/api/v1/workspaces/{workspace_id}/actions/start",
        headers=mutation_headers(me, f"{key}-start"),
    )
    assert started.status_code == 202, started.text
    return started.json()


def _worker(app, hub, worker_id: str = "management-worker") -> OperationWorker:
    return OperationWorker(
        app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id=worker_id,
    )


def test_lifecycle_attempt_limit_allows_stop_and_named_server_remove(settings):
    with pytest.raises(RuntimeError, match="must be at least 2"):
        replace(settings, worker_max_attempts=1).validate()


def test_admin_lifecycle_token_file_is_read_fail_closed(tmp_path):
    token_path = tmp_path / "admin-lifecycle-token"
    token_path.write_text("x" * 32, encoding="ascii")
    token_path.chmod(0o440)
    assert read_admin_lifecycle_token(token_path) == "x" * 32

    for unsafe_mode in (0o444, 0o450, 0o460, 0o660):
        token_path.chmod(unsafe_mode)
        with pytest.raises(ValueError, match="permissions are unsafe"):
            read_admin_lifecycle_token(token_path)
    token_path.chmod(0o440)

    symlink = tmp_path / "admin-lifecycle-token-link"
    symlink.symlink_to(token_path)
    with pytest.raises(ValueError, match="permissions are unsafe"):
        read_admin_lifecycle_token(symlink)

    token_path.chmod(0o600)
    token_path.write_text("too-short", encoding="ascii")
    token_path.chmod(0o440)
    with pytest.raises(ValueError, match="permissions are unsafe"):
        read_admin_lifecycle_token(token_path)

    token_path.chmod(0o600)
    token_path.write_text("x" * 31 + " ", encoding="ascii")
    token_path.chmod(0o440)
    with pytest.raises(ValueError, match="token is invalid"):
        read_admin_lifecycle_token(token_path)

    token_path.chmod(0o600)
    token_path.write_text("é" * 32, encoding="utf-8")
    token_path.chmod(0o440)
    with pytest.raises(ValueError, match="file is unavailable"):
        read_admin_lifecycle_token(token_path)


def test_persisted_resource_policy_cannot_exceed_new_deployment_ceiling(
    management_env,
):
    app, hub, client = management_env
    login(client, hub, "alice")
    provision(app, "alice")
    with app.state.session_factory() as db:
        policy = get_resource_policy(db, app.state.settings)
        old_budget = policy.cpu_budget_millicores
        lowered = replace(
            app.state.settings, workspace_cpu_budget_millicores=old_budget - 1
        )
        with pytest.raises(AppError) as error:
            get_resource_policy(db, lowered)
        assert error.value.status_code == 503
        assert error.value.code == "RESOURCE_POLICY_EXCEEDS_HARD_CEILING"

    object.__setattr__(
        app.state.settings, "workspace_cpu_budget_millicores", old_budget - 1
    )
    blocked_admission = client.get("/api/v1/capacity")
    assert blocked_admission.status_code == 503, blocked_admission.text
    assert (
        blocked_admission.json()["error"]["code"]
        == "RESOURCE_POLICY_EXCEEDS_HARD_CEILING"
    )
    with TestClient(app, base_url=app.state.settings.portal_origin) as admin_client:
        admin, *_ = login(admin_client, hub, "admin")
        visible = admin_client.get("/api/v1/admin/settings")
        assert visible.status_code == 200, visible.text
        assert admin_client.get("/api/v1/admin/capacity").status_code == 200
        current = visible.json()["resource_policy"]
        assert current["cpu_budget_millicores"] == old_budget
        assert current["hard_ceiling"]["cpu_millicores"] == old_budget - 1
        corrected = admin_client.patch(
            "/api/v1/admin/settings",
            json={
                "version": current["version"],
                "cpu_budget_millicores": old_budget - 1,
                "memory_budget_mb": current["memory_budget_mb"],
                "selectable_cpu_millicores": current["selectable_cpu_millicores"],
                "selectable_memory_mb": current["selectable_memory_mb"],
                "kernel_idle_timeout_seconds": current["kernel_idle_timeout_seconds"],
            },
            headers=mutation_headers(admin, "correct-lowered-hard-ceiling"),
        )
        assert corrected.status_code == 200, corrected.text
        assert (
            corrected.json()["resource_policy"]["cpu_budget_millicores"]
            == old_budget - 1
        )
    with app.state.session_factory() as db:
        assert (
            get_resource_policy(db, app.state.settings).cpu_budget_millicores
            == old_budget - 1
        )
    assert client.get("/api/v1/capacity").status_code == 200


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (0, True),
        (300, True),
        (3_600, True),
        (604_800, True),
        (-1, False),
        (1, False),
        (299, False),
        (301, False),
        (604_801, False),
        (True, False),
        ("3600", False),
    ],
)
def test_kernel_idle_timeout_value_contract(value, valid):
    assert kernel_idle_timeout_is_valid(value) is valid


def test_admin_kernel_idle_policy_validation_audit_and_replay(management_env):
    app, hub, client = management_env
    login(client, hub, "alice")
    provision(app, "alice")

    assert (
        client.get("/api/v1/capacity").json()["global"]["kernel_idle_timeout_seconds"]
        == 3_600
    )
    with TestClient(app, base_url=app.state.settings.portal_origin) as admin_client:
        admin, *_ = login(admin_client, hub, "admin")
        policy = admin_client.get("/api/v1/admin/settings").json()["resource_policy"]
        assert policy["kernel_idle_timeout_seconds"] == 3_600
        assert policy["kernel_idle_timeout_bounds"] == {
            "min_seconds": 300,
            "max_seconds": 604_800,
            "step_seconds": 60,
        }
        base_payload = {
            "version": policy["version"],
            "cpu_budget_millicores": policy["cpu_budget_millicores"],
            "memory_budget_mb": policy["memory_budget_mb"],
            "selectable_cpu_millicores": policy["selectable_cpu_millicores"],
            "selectable_memory_mb": policy["selectable_memory_mb"],
        }
        invalid_values = (-1, 299, 301, 604_801, True, 3_600.0, "3600")
        for index, invalid in enumerate(invalid_values):
            rejected = admin_client.patch(
                "/api/v1/admin/settings",
                json={**base_payload, "kernel_idle_timeout_seconds": invalid},
                headers=mutation_headers(admin, f"invalid-kernel-idle-{index}"),
            )
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
        missing = admin_client.patch(
            "/api/v1/admin/settings",
            json=base_payload,
            headers=mutation_headers(admin, "missing-kernel-idle"),
        )
        assert missing.status_code == 200, missing.text
        preserved = missing.json()["resource_policy"]
        assert preserved["version"] == policy["version"] + 1
        assert preserved["kernel_idle_timeout_seconds"] == 3_600
        assert preserved["gpu_budget_count"] == policy["gpu_budget_count"] == 0
        assert preserved["selectable_gpu_counts"] == [0]

        payload = {
            **base_payload,
            "version": preserved["version"],
            "kernel_idle_timeout_seconds": 0,
        }
        headers = mutation_headers(admin, "disable-kernel-idle-culling")
        updated = admin_client.patch(
            "/api/v1/admin/settings", json=payload, headers=headers
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["resource_policy"]["version"] == policy["version"] + 2
        assert updated.json()["resource_policy"]["kernel_idle_timeout_seconds"] == 0
        replay = admin_client.patch(
            "/api/v1/admin/settings", json=payload, headers=headers
        )
        assert replay.status_code == 200
        assert replay.json() == updated.json()

    assert (
        client.get("/api/v1/capacity").json()["global"]["kernel_idle_timeout_seconds"]
        == 0
    )
    with app.state.session_factory() as db:
        persisted = get_resource_policy(db, app.state.settings)
        assert persisted.kernel_idle_timeout_seconds == 0
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "RESOURCE_POLICY_UPDATED")
        ).all()
        assert len(events) == 2
        metadata_by_version = {
            metadata["version"]: metadata
            for metadata in (json.loads(event.safe_metadata_json) for event in events)
        }
        assert metadata_by_version[policy["version"] + 1] == {
            "version": policy["version"] + 1,
            "cpu_budget_millicores": policy["cpu_budget_millicores"],
            "memory_budget_mb": policy["memory_budget_mb"],
            "selectable_cpu_millicores": policy["selectable_cpu_millicores"],
            "selectable_memory_mb": policy["selectable_memory_mb"],
            "previous_gpu_budget_count": 0,
            "previous_selectable_gpu_counts": [0],
            "gpu_budget_count": 0,
            "selectable_gpu_counts": [0],
            "previous_kernel_idle_timeout_seconds": 3_600,
            "kernel_idle_timeout_seconds": 3_600,
        }
        assert metadata_by_version[policy["version"] + 2] == {
            "version": policy["version"] + 2,
            "cpu_budget_millicores": policy["cpu_budget_millicores"],
            "memory_budget_mb": policy["memory_budget_mb"],
            "selectable_cpu_millicores": policy["selectable_cpu_millicores"],
            "selectable_memory_mb": policy["selectable_memory_mb"],
            "previous_gpu_budget_count": 0,
            "previous_selectable_gpu_counts": [0],
            "gpu_budget_count": 0,
            "selectable_gpu_counts": [0],
            "previous_kernel_idle_timeout_seconds": 3_600,
            "kernel_idle_timeout_seconds": 0,
        }


def test_legacy_resource_policy_receipt_replays_when_idle_timeout_is_omitted(
    management_env,
):
    app, hub, client = management_env
    login(client, hub, "alice")
    provision(app, "alice")
    with TestClient(app, base_url=app.state.settings.portal_origin) as admin_client:
        admin, *_ = login(admin_client, hub, "admin")
        policy = admin_client.get("/api/v1/admin/settings").json()["resource_policy"]
        legacy_payload = {
            "version": policy["version"],
            "cpu_budget_millicores": policy["cpu_budget_millicores"],
            "memory_budget_mb": policy["memory_budget_mb"],
            "selectable_cpu_millicores": policy["selectable_cpu_millicores"],
            "selectable_memory_mb": policy["selectable_memory_mb"],
        }
        legacy_policy = {
            key: value
            for key, value in policy.items()
            if key
            not in {
                "gpu_budget_count",
                "selectable_gpu_counts",
                "available_gpu_counts",
                "kernel_idle_timeout_seconds",
                "kernel_idle_timeout_bounds",
            }
        }
        legacy_policy["hard_ceiling"] = {
            key: value
            for key, value in policy["hard_ceiling"].items()
            if key != "gpu_count"
        }
        legacy_response = {"resource_policy": legacy_policy}
        request = mutation_request(
            action="RESOURCE_POLICY_UPDATE",
            target_key="resource-policy:1",
            payload=legacy_payload,
            fingerprint_key=app.state.settings.internal_hmac_key,
        )
        idempotency_key = "legacy-resource-policy-receipt"
        with app.state.session_factory() as db:
            db.add(
                MutationReceipt(
                    id=str(uuid.uuid4()),
                    actor_user_id=admin["user"]["id"],
                    idempotency_key=idempotency_key,
                    action=request.action,
                    target_key=request.target_key,
                    request_fingerprint=request.fingerprint,
                    response_json=json.dumps(
                        legacy_response,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
            db.commit()

        replay = admin_client.patch(
            "/api/v1/admin/settings",
            json=legacy_payload,
            headers=mutation_headers(admin, idempotency_key),
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == legacy_response

    with app.state.session_factory() as db:
        persisted = get_resource_policy(db, app.state.settings)
        assert persisted.version == policy["version"]
        assert persisted.kernel_idle_timeout_seconds == 3_600
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "RESOURCE_POLICY_UPDATED"
                )
            )
            == 0
        )


def test_gpu_capacity_and_legacy_policy_update_preserve_gpu_fields(settings):
    gpu_id = "GPU-01234567-89ab-cdef-0123-456789abcdef"
    gpu_settings = replace(
        settings,
        admin_usernames=("admin",),
        nvidia_gpu_device_ids=(gpu_id,),
    )
    hub = FakeJupyterHubProvider()
    app = create_app(gpu_settings, hub)
    Base.metadata.create_all(app.state.engine)

    with TestClient(app, base_url=gpu_settings.portal_origin) as client:
        login(client, hub, "alice")
        provision(app, "alice")
        admin, *_ = login(client, hub, "admin")

        policy = client.get("/api/v1/admin/settings").json()["resource_policy"]
        assert policy["gpu_budget_count"] == 1
        assert policy["selectable_gpu_counts"] == [0]
        assert policy["available_gpu_counts"] == [0]
        assert policy["hard_ceiling"]["gpu_count"] == 1

        public_capacity = client.get("/api/v1/capacity")
        assert public_capacity.status_code == 200, public_capacity.text
        assert public_capacity.json()["global"]["resources"]["gpu_count"] == {
            "reserved": 0,
            "limit": 1,
        }
        admin_capacity = client.get("/api/v1/admin/capacity")
        assert admin_capacity.status_code == 200, admin_capacity.text
        assert admin_capacity.json()["resources"]["gpu_count"] == {
            "reserved": 0,
            "limit": 1,
        }

        # A pre-GPU v1 client sends neither GPU field.  Resolving the missing
        # additions under the policy write lock must preserve, rather than
        # silently reset, the administrator's current GPU budget and choices.
        legacy_payload = {
            "version": policy["version"],
            "cpu_budget_millicores": policy["cpu_budget_millicores"],
            "memory_budget_mb": policy["memory_budget_mb"],
            "selectable_cpu_millicores": policy["selectable_cpu_millicores"],
            "selectable_memory_mb": policy["selectable_memory_mb"],
        }
        updated = client.patch(
            "/api/v1/admin/settings",
            json=legacy_payload,
            headers=mutation_headers(admin, "legacy-client-preserves-gpu-policy"),
        )
        assert updated.status_code == 200, updated.text
        updated_policy = updated.json()["resource_policy"]
        assert updated_policy["gpu_budget_count"] == 1
        assert updated_policy["selectable_gpu_counts"] == [0]

    with app.state.session_factory() as db:
        persisted = get_resource_policy(db, gpu_settings)
        assert persisted.gpu_budget_count == 1
        assert json.loads(persisted.selectable_gpu_counts_json) == [0]


def test_gpu_inventory_digest_matches_hub_contract_vector():
    assert gpu_inventory_digest(("GPU-01234567-89ab-cdef-0123-456789abcdef",)) == (
        "sha256:df1a007a9153b95d91a0881a0eb37c0" "9590d10572c2c51afeae956271349a0b8"
    )


def test_unclaimed_start_releases_resources_when_stop_observes_not_found(
    management_env,
):
    app, hub, client = management_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create(client, me, key="reservation-race-create")
    workspace_id = created["workspace"]["id"]
    stopped = client.post(
        f"/api/v1/workspaces/{workspace_id}/actions/stop",
        headers=mutation_headers(me, "reservation-race-stop"),
    )
    assert stopped.status_code == 202, stopped.text
    with app.state.session_factory() as db:
        reservation = active_reservations(db)
        assert reservation.count == 0
        assert reservation.cpu_millicores == 0
        assert reservation.memory_mb == 0
    assert not asyncio.run(_worker(app, hub, "reservation-race-worker").process_next())


def test_expired_running_observation_is_stale_and_start_rechecks_hub(
    management_env,
):
    app, hub, client = management_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create(client, me, key="freshness-create")
    workspace_id = created["workspace"]["id"]
    worker = _worker(app, hub, "freshness-worker")
    assert asyncio.run(worker.process_next()) is True
    assert hub.start_count == 1

    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        assert workspace and workspace.observed_state == "RUNNING"
        hub.servers.pop(("alice", workspace.hub_server_name))
        workspace.stale = False
        workspace.last_reconciled_at = datetime.utcnow() - timedelta(
            seconds=app.state.settings.reconciliation_freshness_seconds + 1
        )
        db.commit()

    stale = client.get(f"/api/v1/workspaces/{workspace_id}")
    assert stale.status_code == 200, stale.text
    assert stale.json()["workspace"]["stale"] is True
    assert stale.json()["workspace"]["launch_url"] is None

    started = client.post(
        f"/api/v1/workspaces/{workspace_id}/actions/start",
        headers=mutation_headers(me, "freshness-start"),
    )
    assert started.status_code == 202, started.text
    assert started.json()["operation"]["status"] == "PENDING"
    assert asyncio.run(worker.process_next()) is True
    assert hub.start_count == 2

    current = client.get(f"/api/v1/workspaces/{workspace_id}")
    assert current.status_code == 200, current.text
    assert current.json()["workspace"]["stale"] is False
    assert current.json()["workspace"]["launch_url"] is not None
    completed = client.get(f"/api/v1/operations/{started.json()['operation']['id']}")
    assert completed.status_code == 200, completed.text
    assert completed.json()["operation"]["status"] == "SUCCEEDED"


def test_configured_admin_allowlist_revokes_sessions_and_queued_service_work(
    management_env,
):
    app, hub, alice_client = management_env
    alice, *_ = login(alice_client, hub, "alice")
    provision(app, "alice")
    created = _create(alice_client, alice, key="admin-revocation-create")
    workspace_id = created["workspace"]["id"]
    assert asyncio.run(
        _worker(app, hub, "admin-revocation-create-worker").process_next()
    )

    with TestClient(app, base_url=app.state.settings.portal_origin) as admin_client:
        admin, *_ = login(admin_client, hub, "admin")
        stopped = admin_client.post(
            f"/api/v1/admin/workspaces/{workspace_id}/actions/stop",
            headers=mutation_headers(admin, "admin-revocation-stop"),
        )
        assert stopped.status_code == 202, stopped.text
        operation_id = stopped.json()["operation"]["id"]

        object.__setattr__(app.state.settings, "admin_usernames", ())
        current = admin_client.get("/api/v1/me")
        assert current.status_code == 200
        assert current.json()["user"]["role"] == "USER"
        assert admin_client.get("/api/v1/admin/settings").status_code == 403

        assert asyncio.run(
            _worker(app, hub, "admin-revocation-service-worker").process_next()
        )
        with app.state.session_factory() as db:
            operation = db.get(Operation, operation_id)
            assert operation and operation.status == "FAILED"
            assert operation.error_code == "ADMIN_AUTHORITY_REVOKED"

        object.__setattr__(app.state.settings, "admin_usernames", ("admin", "admin-b"))
        promoted = admin_client.get("/api/v1/me")
        assert promoted.status_code == 200
        assert promoted.json()["user"]["role"] == "ADMIN"
        assert admin_client.get("/api/v1/admin/settings").status_code == 200

    with app.state.session_factory() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "ADMIN_ROLE_RECONCILED")
        ).all()
        assert len(events) == 2


def test_workspace_name_and_plain_secret_environment_contract(management_env):
    app, hub, client = management_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")

    capacity = client.get("/api/v1/capacity")
    assert capacity.status_code == 200
    assert capacity.json()["user"]["next_default_workspace_name"] == "환경-1"
    created = _create(
        client,
        me,
        key="named-environment-create",
        name="  팀 개발  ",
        environment=[
            {"name": "MODE", "value": "development", "is_secret": False},
            {"name": "API_KEY", "value": "secret-create-value", "is_secret": True},
        ],
    )
    workspace_id = created["workspace"]["id"]
    assert created["workspace"]["name"] == "팀 개발"

    listed = client.get(f"/api/v1/workspaces/{workspace_id}/environment-variables")
    assert listed.status_code == 200
    by_name = {item["name"]: item for item in listed.json()["items"]}
    assert by_name["MODE"]["value"] == "development"
    assert by_name["MODE"]["is_secret"] is False
    assert by_name["API_KEY"]["value"] is None
    assert by_name["API_KEY"]["is_secret"] is True

    secret_put = client.put(
        "/api/v1/me/environment-variables/TEAM_TOKEN",
        json={"value": "secret-global-value", "is_secret": True},
        headers=mutation_headers(me, "secret-global-put"),
    )
    assert secret_put.status_code == 200, secret_put.text
    assert secret_put.json()["item"]["value"] is None
    secret_version = secret_put.json()["item"]["version"]
    ordinary_put = client.put(
        "/api/v1/me/environment-variables/TEAM_LABEL",
        json={"value": "ai-labs", "is_secret": False},
        headers=mutation_headers(me, "ordinary-global-put"),
    )
    assert ordinary_put.status_code == 200, ordinary_put.text
    assert ordinary_put.json()["item"]["value"] is None

    global_list = client.get("/api/v1/me/environment-variables").json()["items"]
    global_by_name = {item["name"]: item for item in global_list}
    assert global_by_name["TEAM_TOKEN"]["value"] is None
    assert global_by_name["TEAM_LABEL"]["value"] == "ai-labs"

    transitioned = client.put(
        "/api/v1/me/environment-variables/TEAM_TOKEN",
        json={
            "value": "now-ordinary",
            "is_secret": False,
            "expected_version": secret_version,
        },
        headers=mutation_headers(me, "secret-to-ordinary"),
    )
    assert transitioned.status_code == 200, transitioned.text
    assert (
        next(
            item
            for item in client.get("/api/v1/me/environment-variables").json()["items"]
            if item["name"] == "TEAM_TOKEN"
        )["value"]
        == "now-ordinary"
    )

    with app.state.session_factory() as db:
        secret = db.scalar(
            select(EnvironmentVariable).where(EnvironmentVariable.name == "API_KEY")
        )
        ordinary = db.scalar(
            select(EnvironmentVariable).where(EnvironmentVariable.name == "MODE")
        )
        receipts = db.scalars(select(MutationReceipt)).all()
        audits = db.scalars(select(AuditEvent)).all()
        assert secret and secret.plain_value is None and secret.value_cipher
        assert "secret-create-value" not in secret.value_cipher
        assert ordinary and ordinary.plain_value == "development"
        serialized_receipts = "\n".join(item.response_json for item in receipts)
        serialized_audits = "\n".join(item.safe_metadata_json for item in audits)
        assert "secret-global-value" not in serialized_receipts
        assert "secret-global-value" not in serialized_audits


def test_environment_delete_response_matches_mutation_contract(management_env):
    app, hub, client = management_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create(
        client,
        me,
        key="delete-environment-create",
        environment=[{"name": "LOCAL_TEMP", "value": "one", "is_secret": False}],
    )
    workspace_id = created["workspace"]["id"]
    global_put = client.put(
        "/api/v1/me/environment-variables/GLOBAL_TEMP",
        json={"value": "two", "is_secret": False},
        headers=mutation_headers(me, "delete-environment-global-put"),
    )
    assert global_put.status_code == 200, global_put.text

    global_delete = client.delete(
        "/api/v1/me/environment-variables/GLOBAL_TEMP",
        params={"expected_version": global_put.json()["item"]["version"]},
        headers=mutation_headers(me, "delete-environment-global"),
    )
    assert global_delete.status_code == 200, global_delete.text
    assert global_delete.json()["item"] is None
    assert global_delete.json()["changed"] is True
    assert type(global_delete.json()["restart_required"]) is bool
    replay = client.delete(
        "/api/v1/me/environment-variables/GLOBAL_TEMP",
        params={"expected_version": global_put.json()["item"]["version"]},
        headers=mutation_headers(me, "delete-environment-global"),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == global_delete.json()

    local = client.get(
        f"/api/v1/workspaces/{workspace_id}/environment-variables"
    ).json()["items"][0]
    workspace_delete = client.delete(
        f"/api/v1/workspaces/{workspace_id}/environment-variables/LOCAL_TEMP",
        params={"expected_version": local["version"]},
        headers=mutation_headers(me, "delete-environment-workspace"),
    )
    assert workspace_delete.status_code == 200, workspace_delete.text
    assert workspace_delete.json()["item"] is None
    assert workspace_delete.json()["changed"] is True
    assert type(workspace_delete.json()["restart_required"]) is bool


def test_environment_payload_limit_rolls_back_and_create_rejects_environment(
    management_env,
):
    app, hub, client = management_env
    me, *_ = login(client, hub, "payload-user")
    user_id = provision(app, "payload-user")
    large = "x" * 16_384
    for number in range(3):
        response = client.put(
            f"/api/v1/me/environment-variables/BIG{number}",
            json={"value": large, "is_secret": False},
            headers=mutation_headers(me, f"large-global-{number}"),
        )
        assert response.status_code == 200, response.text
    with app.state.session_factory() as db:
        before_generation = db.get(User, user_id).environment_generation

    rejected = client.put(
        "/api/v1/me/environment-variables/BIG3",
        json={"value": large, "is_secret": False},
        headers=mutation_headers(me, "large-global-rejected"),
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "ENVIRONMENT_PAYLOAD_LIMIT"
    with app.state.session_factory() as db:
        user = db.get(User, user_id)
        assert user and user.environment_generation == before_generation
        assert (
            db.scalar(
                select(func.count(EnvironmentVariable.id)).where(
                    EnvironmentVariable.owner_user_id == user_id,
                    EnvironmentVariable.name == "BIG3",
                    EnvironmentVariable.deleted_at.is_(None),
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(MutationReceipt.id)).where(
                    MutationReceipt.idempotency_key == "large-global-rejected"
                )
            )
            == 0
        )

    rejected_create = client.post(
        "/api/v1/workspaces",
        json={
            "profile_id": "python-standard",
            "profile_version": 1,
            "environment": [
                {"name": f"BIG{number}", "value": large, "is_secret": False}
                for number in range(4)
            ],
        },
        headers=mutation_headers(me, "large-initial-create"),
    )
    assert rejected_create.status_code == 422
    assert rejected_create.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    with app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count(Workspace.id)).where(
                    Workspace.owner_user_id == user_id
                )
            )
            == 0
        )


def test_admin_restart_removes_stopped_named_server_before_fresh_spawn(
    management_env,
):
    app, hub, client = management_env
    hub.auto_ready = False
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create(client, me, key="restart-create")
    workspace_id = created["workspace"]["id"]
    worker = _worker(app, hub, "restart-worker")

    assert asyncio.run(worker.process_next()) is True
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        operation = db.get(Operation, created["operation"]["id"])
        authorization = db.scalar(
            select(SpawnAuthorization).where(
                SpawnAuthorization.operation_id == operation.id
            )
        )
        assert workspace and operation and authorization
        authorization.consumed_at = datetime.utcnow()
        operation.next_attempt_at = datetime.utcnow()
        server_name = workspace.hub_server_name
        db.commit()
    hub.complete_start("alice", server_name)
    assert asyncio.run(worker.process_next()) is True

    changed = client.put(
        f"/api/v1/workspaces/{workspace_id}/environment-variables/MODE",
        json={"value": "new", "is_secret": False},
        headers=mutation_headers(me, "restart-environment-change"),
    )
    assert changed.status_code == 200
    assert changed.json()["restart_required"] is True
    with TestClient(app, base_url=app.state.settings.portal_origin) as admin_client:
        admin, *_ = login(admin_client, hub, "admin")
        restarted = admin_client.post(
            f"/api/v1/admin/workspaces/{workspace_id}/actions/restart",
            headers=mutation_headers(admin, "explicit-admin-restart"),
        )
        assert restarted.status_code == 202, restarted.text
        # The inventory is authoritative even after a browser refresh.
        target = next(
            item
            for item in admin_client.get("/api/v1/admin/workspaces").json()["items"]
            if item["id"] == workspace_id
        )
        assert target["active_operation"]["id"] == restarted.json()["operation"]["id"]
        assert target["active_operation"]["operation_type"] == "RESTART"
        before_remove = hub.remove_count
        assert asyncio.run(worker.process_next()) is True
        assert hub.remove_count == before_remove + 1
        with app.state.session_factory() as db:
            operation = db.get(Operation, restarted.json()["operation"]["id"])
            authorizations = db.scalars(
                select(SpawnAuthorization)
                .where(SpawnAuthorization.operation_id == operation.id)
                .order_by(SpawnAuthorization.attempt_no.desc())
            ).all()
            assert operation and operation.lifecycle_checkpoint == "RESTART_STARTING"
            assert operation.status == "PENDING"
            assert operation.credential_mode == "ADMIN_SERVICE"
            assert operation.actor_user_id == admin["user"]["id"]
            assert len(authorizations) == 1
            authorizations[0].consumed_at = datetime.utcnow()
            operation.next_attempt_at = datetime.utcnow()
            db.commit()
    hub.complete_start("alice", server_name)
    assert asyncio.run(worker.process_next()) is True
    response = client.get(f"/api/v1/workspaces/{workspace_id}").json()["workspace"]
    assert response["restart_required"] is False


def test_delete_does_not_queue_volume_wipe_until_hub_confirms_not_found(
    management_env,
):
    app, hub, client = management_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create(client, me, key="delete-not-found-gate-create")
    workspace_id = created["workspace"]["id"]
    worker = _worker(app, hub, "delete-not-found-gate-worker")
    assert asyncio.run(worker.process_next()) is True
    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-not-found-gate"),
    )
    assert deleted.status_code == 202
    hub.request_remove = AsyncMock(
        return_value=HubServer(
            state=HubServerState.STOPPED, ready=False, progress_percent=100
        )
    )
    assert asyncio.run(worker.process_next()) is True
    hub.request_remove.assert_awaited_once()
    with app.state.session_factory() as db:
        operation = db.get(Operation, deleted.json()["operation"]["id"])
        workspace = db.get(Workspace, workspace_id)
        slot = db.get(WorkspaceVolumeSlot, workspace.private_volume_slot_id)
        assert db.get(WorkspaceDeletionJob, workspace_id) is None
        assert operation and operation.status == "PENDING"
        assert workspace and workspace.observed_state == "STOPPED"
        assert slot and slot.provision_status == "PROVISIONED"
        operation.attempts = app.state.settings.worker_max_attempts
        operation.next_attempt_at = datetime.utcnow()
        db.commit()
    assert asyncio.run(worker.process_next()) is True
    with app.state.session_factory() as db:
        operation = db.get(Operation, deleted.json()["operation"]["id"])
        assert operation and operation.status == "FAILED"
        assert operation.error_code == "LIFECYCLE_ATTEMPTS_EXHAUSTED"
    retry = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-not-found-gate-retry"),
    )
    assert retry.status_code == 202, retry.text


def test_stopping_delete_times_out_and_can_be_retried(management_env):
    app, hub, client = management_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create(client, me, key="delete-timeout-create")
    workspace_id = created["workspace"]["id"]
    worker = _worker(app, hub, "delete-timeout-worker")
    assert asyncio.run(worker.process_next()) is True
    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-timeout"),
    )
    assert deleted.status_code == 202
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        operation = db.get(Operation, deleted.json()["operation"]["id"])
        assert workspace and operation
        hub.servers[("alice", workspace.hub_server_name)] = HubServer(
            state=HubServerState.STOPPING
        )
        operation.requested_at = datetime.utcnow() - timedelta(
            seconds=app.state.settings.lifecycle_timeout_seconds + 1
        )
        db.commit()
    assert asyncio.run(worker.process_next()) is True
    workspace_response = client.get(f"/api/v1/workspaces/{workspace_id}")
    assert workspace_response.status_code == 200
    assert workspace_response.json()["workspace"]["can_retry_delete"] is True
    with app.state.session_factory() as db:
        operation = db.get(Operation, deleted.json()["operation"]["id"])
        assert operation and operation.status == "FAILED"
        assert operation.error_code == "LIFECYCLE_TIMEOUT"
    retry = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-timeout-retry"),
    )
    assert retry.status_code == 202, retry.text


def test_failed_external_deletion_user_retry_keeps_stable_identity_and_wipes(
    management_env,
):
    app, hub, client = management_env
    me, *_ = login(client, hub, "alice")
    user_id = provision(app, "alice")
    created = _create(
        client,
        me,
        key="delete-create",
        environment=[{"name": "DELETE_SECRET", "value": "wipe-me", "is_secret": True}],
    )
    workspace_id = created["workspace"]["id"]
    worker = _worker(app, hub, "delete-worker")
    assert asyncio.run(worker.process_next()) is True

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-initial"),
    )
    assert deleted.status_code == 202, deleted.text
    before_remove = hub.remove_count
    assert asyncio.run(worker.process_next()) is True
    assert hub.remove_count == before_remove + 1
    with app.state.session_factory() as db:
        job = db.get(WorkspaceDeletionJob, workspace_id)
        workspace = db.get(Workspace, workspace_id)
        slot = db.get(WorkspaceVolumeSlot, workspace.private_volume_slot_id)
        assert job and workspace and slot
        assert workspace.observed_state == "NOT_FOUND"
        assert slot.provision_status == "WIPING"
        deletion_id = job.deletion_id
        bound_spec = workspace.spec_version

    global_change = client.put(
        "/api/v1/me/environment-variables/AFTER_DELETE",
        json={"value": "kept-global", "is_secret": False},
        headers=mutation_headers(me, "global-after-delete"),
    )
    assert global_change.status_code == 200, global_change.text
    with app.state.session_factory() as db:
        assert db.get(Workspace, workspace_id).spec_version == bound_spec
        claim = claim_deletion_job(
            db, settings=app.state.settings, worker_id="volume-agent"
        )
    assert claim and claim.deletion_id == deletion_id
    with app.state.session_factory() as db:
        fail_deletion_job(
            db,
            settings=app.state.settings,
            worker_id="volume-agent",
            workspace_id=workspace_id,
            attempt_no=claim.attempt_no,
            request_id="deletion-fail",
        )

    retry = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-external-retry"),
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["operation"]["status"] == "WAITING_EXTERNAL"
    with app.state.session_factory() as db:
        retried_job = db.get(WorkspaceDeletionJob, workspace_id)
        assert retried_job and retried_job.deletion_id == deletion_id
        claim = claim_deletion_job(
            db, settings=app.state.settings, worker_id="volume-agent"
        )
    assert claim and claim.deletion_id == deletion_id
    manifest = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "owner_user_id": user_id,
        "username": "alice",
        "workspace_spec_version": claim.workspace_spec_version,
        "private_volume_slot_id": claim.private_volume_slot_id,
        "private_volume_slot_number": claim.private_volume_slot_number,
        "private_volume_name": claim.private_volume_name,
        "hard_limit_bytes": 1024 * 1024 * 1024,
        # Unlimited local volumes report the allocator/label ID. The DB keeps
        # a synthetic uniqueness sentinel, so deletion must not compare these
        # two unrelated values as if quota enforcement were enabled.
        "project_id": 10005,
        "uid": 1000,
        "gid": 100,
        "mode": "0700",
        "volume_recreated": True,
    }
    with app.state.session_factory() as db:
        complete_deletion_job(
            db,
            settings=app.state.settings,
            worker_id="volume-agent",
            workspace_id=workspace_id,
            attempt_no=claim.attempt_no,
            manifest=manifest,
            request_id="deletion-complete",
        )
        workspace = db.get(Workspace, workspace_id)
        slot = db.get(WorkspaceVolumeSlot, claim.private_volume_slot_id)
        assert slot and slot.quota_project_id != manifest["project_id"]
        workspace_items = db.scalars(
            select(EnvironmentVariable).where(
                EnvironmentVariable.workspace_id == workspace_id
            )
        ).all()
        snapshots = db.scalars(
            select(SpawnAuthorization).where(
                SpawnAuthorization.workspace_id == workspace_id
            )
        ).all()
        global_item = db.scalar(
            select(EnvironmentVariable).where(
                EnvironmentVariable.owner_user_id == user_id,
                EnvironmentVariable.name == "AFTER_DELETE",
            )
        )
        assert workspace and workspace.archived_at is not None
        assert slot and slot.provision_status == "PROVISIONED"
        assert all(
            item.deleted_at is not None
            and item.value_cipher is None
            and item.plain_value is None
            for item in workspace_items
        )
        assert all(item.environment_snapshot_cipher is None for item in snapshots)
    assert global_item and global_item.plain_value == "kept-global"


def test_failed_external_delete_retry_removes_rediscovered_stopped_record(
    management_env,
):
    app, hub, client = management_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create(client, me, key="delete-rediscovered-create")
    workspace_id = created["workspace"]["id"]
    worker = _worker(app, hub, "delete-rediscovered-worker")
    assert asyncio.run(worker.process_next()) is True

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-rediscovered-initial"),
    )
    assert deleted.status_code == 202, deleted.text
    assert asyncio.run(worker.process_next()) is True
    deletion_id = None
    for attempt in range(1, app.state.settings.deletion_max_attempts + 1):
        with app.state.session_factory() as db:
            claim = claim_deletion_job(
                db, settings=app.state.settings, worker_id="failed-volume-agent"
            )
        assert claim and claim.attempt_no == attempt
        deletion_id = claim.deletion_id
        with app.state.session_factory() as db:
            fail_deletion_job(
                db,
                settings=app.state.settings,
                worker_id="failed-volume-agent",
                workspace_id=workspace_id,
                attempt_no=claim.attempt_no,
                request_id=f"delete-rediscovered-fail-{attempt}",
            )

    with app.state.session_factory() as db:
        workspace = db.get(Workspace, workspace_id)
        assert workspace
        workspace.observed_state = "STOPPED"
        workspace.stale = False
        server_name = workspace.hub_server_name
        db.commit()
    hub.servers[("alice", server_name)] = HubServer(
        state=HubServerState.STOPPED,
        ready=False,
        progress_percent=100,
    )

    retry = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-rediscovered-retry"),
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["operation"]["status"] == "PENDING"
    before_remove = hub.remove_count
    assert asyncio.run(worker.process_next()) is True
    assert hub.remove_count == before_remove + 1
    with app.state.session_factory() as db:
        job = db.get(WorkspaceDeletionJob, workspace_id)
        operation = db.get(Operation, retry.json()["operation"]["id"])
        workspace = db.get(Workspace, workspace_id)
        assert job and job.deletion_id == deletion_id
        assert job.status == "PENDING" and job.attempts == 0
        assert job.operation_id == operation.id
        assert operation and operation.status == "WAITING_EXTERNAL"
        assert workspace and workspace.observed_state == "NOT_FOUND"


def test_expired_final_deletion_lease_converges_to_retryable_failure(
    management_env,
):
    app, hub, client = management_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = _create(client, me, key="delete-expired-create")
    workspace_id = created["workspace"]["id"]
    worker = _worker(app, hub, "delete-expired-control-worker")
    assert asyncio.run(worker.process_next()) is True
    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-expired-request"),
    )
    assert deleted.status_code == 202, deleted.text
    assert asyncio.run(worker.process_next()) is True

    with app.state.session_factory() as db:
        claim = claim_deletion_job(
            db, settings=app.state.settings, worker_id="crashed-volume-agent"
        )
        assert claim and claim.attempt_no == app.state.settings.deletion_max_attempts
    with app.state.session_factory() as db:
        job = db.get(WorkspaceDeletionJob, workspace_id)
        assert job
        job.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    with app.state.session_factory() as db:
        assert (
            claim_deletion_job(
                db, settings=app.state.settings, worker_id="replacement-volume-agent"
            )
            is None
        )
    with app.state.session_factory() as db:
        job = db.get(WorkspaceDeletionJob, workspace_id)
        operation = db.get(Operation, claim.operation_id)
        workspace = db.get(Workspace, workspace_id)
        slot = db.get(WorkspaceVolumeSlot, workspace.private_volume_slot_id)
        assert job and job.status == "FAILED" and job.lease_owner is None
        assert operation and operation.status == "FAILED"
        assert workspace and workspace.deletion_checkpoint == "DELETION_FAILED"
        assert slot and slot.provision_status == "WIPING"

    visible = client.get(f"/api/v1/workspaces/{workspace_id}")
    assert visible.status_code == 200
    assert visible.json()["workspace"]["can_retry_delete"] is True
    retried = client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(me, "delete-expired-retry"),
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["operation"]["status"] == "WAITING_EXTERNAL"
    with app.state.session_factory() as db:
        job = db.get(WorkspaceDeletionJob, workspace_id)
        assert job and job.status == "PENDING" and job.attempts == 0


def test_control_plane_delete_retry_and_admin_actor_operation_visibility(
    management_env,
):
    app, hub, alice_client = management_env
    alice, *_ = login(alice_client, hub, "alice")
    user_id = provision(app, "alice")
    created = _create(alice_client, alice, key="lifecycle-delete-create")
    workspace_id = created["workspace"]["id"]
    first = alice_client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(alice, "lifecycle-delete-first"),
    )
    assert first.status_code == 202
    active = alice_client.get(f"/api/v1/workspaces/{workspace_id}").json()["workspace"][
        "active_operation"
    ]
    assert active["id"] == first.json()["operation"]["id"]
    assert active["operation_type"] == "DELETE"
    duplicate = alice_client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(alice, "lifecycle-delete-pending"),
    )
    assert duplicate.status_code == 409
    with app.state.session_factory() as db:
        first_operation = db.get(Operation, first.json()["operation"]["id"])
        assert first_operation
        first_operation.status = "FAILED"
        first_operation.error_code = "HUB_REQUEST_REJECTED"
        first_operation.error_summary = "safe failure"
        first_operation.completed_at = datetime.utcnow()
        first_operation.next_attempt_at = None
        db.commit()
    retried = alice_client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=mutation_headers(alice, "lifecycle-delete-user-retry"),
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["operation"]["status"] == "PENDING"

    with app.state.session_factory() as db:
        retry_operation = db.get(Operation, retried.json()["operation"]["id"])
        owner = db.get(User, user_id)
        assert retry_operation and owner
        retry_operation.status = "FAILED"
        retry_operation.error_code = "HUB_UNAVAILABLE"
        retry_operation.completed_at = datetime.utcnow()
        retry_operation.next_attempt_at = None
        owner.status = "DISABLED"
        db.commit()

    with TestClient(app, base_url=app.state.settings.portal_origin) as admin_client:
        admin, *_ = login(admin_client, hub, "admin")
        admin_retry = admin_client.delete(
            f"/api/v1/admin/workspaces/{workspace_id}",
            headers=mutation_headers(admin, "lifecycle-delete-admin-retry"),
        )
        assert admin_retry.status_code == 202, admin_retry.text
        operation_id = admin_retry.json()["operation"]["id"]
        polled = admin_client.get(f"/api/v1/operations/{operation_id}")
        assert polled.status_code == 200, polled.text
        with app.state.session_factory() as db:
            operation = db.get(Operation, operation_id)
            assert operation
            assert operation.actor_user_id == admin["user"]["id"]
            assert operation.requested_by_user_id == user_id
            assert operation.auth_session_id_hash is None
            assert operation.credential_mode == "ADMIN_SERVICE"


def test_admin_resource_policy_profile_offer_and_workspace_inventory(management_env):
    app, hub, alice_client = management_env
    alice, _, _, alice_token = login(alice_client, hub, "alice")
    alice_id = provision(app, "alice")
    created = _create(alice_client, alice, key="admin-target-create")
    workspace_id = created["workspace"]["id"]
    assert alice_client.get("/api/v1/admin/settings").status_code == 403
    assert (
        alice_client.get(
            f"/api/v1/admin/workspaces/{workspace_id}/launch",
            follow_redirects=False,
        ).status_code
        == 403
    )

    with TestClient(app, base_url=app.state.settings.portal_origin) as admin_client:
        admin, *_ = login(admin_client, hub, "admin")
        settings_response = admin_client.get("/api/v1/admin/settings")
        assert settings_response.status_code == 200
        policy = settings_response.json()["resource_policy"]
        assert policy["hard_ceiling"] == {
            "cpu_millicores": app.state.settings.workspace_cpu_budget_millicores,
            "memory_mb": app.state.settings.workspace_memory_budget_mb,
            "gpu_count": 0,
        }
        assert policy["gpu_budget_count"] == 0
        assert policy["selectable_gpu_counts"] == [0]
        assert policy["available_gpu_counts"] == [0]
        too_high = admin_client.patch(
            "/api/v1/admin/settings",
            json={
                "version": policy["version"],
                "cpu_budget_millicores": policy["hard_ceiling"]["cpu_millicores"] + 1,
                "memory_budget_mb": policy["memory_budget_mb"],
                "selectable_cpu_millicores": policy["selectable_cpu_millicores"],
                "selectable_memory_mb": policy["selectable_memory_mb"],
                "kernel_idle_timeout_seconds": policy["kernel_idle_timeout_seconds"],
            },
            headers=mutation_headers(admin, "policy-above-ceiling"),
        )
        assert too_high.status_code == 422
        assert (
            too_high.json()["error"]["code"] == "RESOURCE_BUDGET_EXCEEDS_HARD_CEILING"
        )
        gpu_too_high = admin_client.patch(
            "/api/v1/admin/settings",
            json={
                "version": policy["version"],
                "cpu_budget_millicores": policy["cpu_budget_millicores"],
                "memory_budget_mb": policy["memory_budget_mb"],
                "selectable_cpu_millicores": policy["selectable_cpu_millicores"],
                "selectable_memory_mb": policy["selectable_memory_mb"],
                "gpu_budget_count": 1,
                "selectable_gpu_counts": [0],
                "kernel_idle_timeout_seconds": policy["kernel_idle_timeout_seconds"],
            },
            headers=mutation_headers(admin, "gpu-policy-above-ceiling"),
        )
        assert gpu_too_high.status_code == 422
        assert (
            gpu_too_high.json()["error"]["code"]
            == "RESOURCE_BUDGET_EXCEEDS_HARD_CEILING"
        )
        below_reserved = admin_client.patch(
            "/api/v1/admin/settings",
            json={
                "version": policy["version"],
                "cpu_budget_millicores": 500,
                "memory_budget_mb": policy["memory_budget_mb"],
                "selectable_cpu_millicores": policy["selectable_cpu_millicores"],
                "selectable_memory_mb": policy["selectable_memory_mb"],
                "kernel_idle_timeout_seconds": policy["kernel_idle_timeout_seconds"],
            },
            headers=mutation_headers(admin, "policy-below-reserved"),
        )
        assert below_reserved.status_code == 409
        assert (
            below_reserved.json()["error"]["code"] == "RESOURCE_BUDGET_BELOW_RESERVED"
        )

        offers = admin_client.get("/api/v1/admin/profiles")
        assert offers.status_code == 200
        templates = offers.json()["runtime_templates"]
        assert templates
        assert "image_ref" not in templates[0]
        assert "provider_options_json" not in templates[0]
        runtime = templates[0]
        assert runtime["accelerator_kind"] == "none"
        assert runtime["gpu_count"] == 0
        assert runtime["cuda_version"] is None
        created_offer = admin_client.post(
            "/api/v1/admin/profiles",
            json={
                "name": "AI Lab Python",
                "description": "Managed lab preset",
                "runtime_profile_id": runtime["id"],
                "runtime_profile_version": runtime["version"],
                "enabled": True,
            },
            headers=mutation_headers(admin, "profile-offer-create"),
        )
        assert created_offer.status_code == 201, created_offer.text
        offer = created_offer.json()["profile"]
        assert offer["effective_selectable"] is True
        assert "image_ref" not in offer["runtime_profile"]
        disabled = admin_client.delete(
            f"/api/v1/admin/profiles/{offer['id']}",
            params={"expected_version": offer["version"]},
            headers=mutation_headers(admin, "profile-offer-disable"),
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["profile"]["effective_selectable"] is False

        inventory = admin_client.get("/api/v1/admin/workspaces")
        assert inventory.status_code == 200
        target = next(
            item for item in inventory.json()["items"] if item["id"] == workspace_id
        )
        assert target["owner"]["username"] == "alice"
        capacity = admin_client.get("/api/v1/admin/capacity").json()
        assert capacity["workspaces"]["created"] == 1
        assert capacity["workspaces"]["reserved"] == 1

        failed_launch = admin_client.get(
            f"/api/v1/admin/workspaces/{workspace_id}/launch",
            follow_redirects=False,
        )
        assert failed_launch.status_code == 409
        with app.state.session_factory() as db:
            assert (
                db.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.action == "ADMIN_WORKSPACE_LAUNCH_REQUESTED"
                    )
                )
                == 0
            )
            workspace = db.get(Workspace, workspace_id)
            assert workspace
            workspace.observed_state = "RUNNING"
            workspace.stale = False
            workspace.last_reconciled_at = datetime.utcnow()
            db.commit()
        launched = admin_client.get(
            f"/api/v1/admin/workspaces/{workspace_id}/launch",
            follow_redirects=False,
        )
        assert launched.status_code == 303
        assert "token=" not in launched.headers["location"]
        with app.state.session_factory() as db:
            event = db.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "ADMIN_WORKSPACE_LAUNCH_REQUESTED"
                )
            )
            assert event and event.actor_user_id == admin["user"]["id"]
            assert event.workspace_id == workspace_id
            assert json.loads(event.safe_metadata_json) == {
                "target_owner_user_id": alice_id
            }

        stopped = admin_client.post(
            f"/api/v1/admin/workspaces/{workspace_id}/actions/stop",
            headers=mutation_headers(admin, "admin-stop-target"),
        )
        assert stopped.status_code == 202, stopped.text
        operation_id = stopped.json()["operation"]["id"]
        refreshed = admin_client.get("/api/v1/admin/workspaces").json()["items"]
        refreshed_target = next(
            item for item in refreshed if item["id"] == workspace_id
        )
        assert refreshed_target["active_operation"]["id"] == operation_id
        assert refreshed_target["active_operation"]["operation_type"] == "STOP"
        assert admin_client.get(f"/api/v1/operations/{operation_id}").status_code == 200
        # The active-operation summary is visible to every administrator. A
        # different administrator must therefore be able to poll the same detail
        # after a refresh, while ordinary users remain owner-scoped.
        with TestClient(
            app, base_url=app.state.settings.portal_origin
        ) as second_admin_client:
            login(second_admin_client, hub, "admin-b")
            assert (
                second_admin_client.get(
                    f"/api/v1/operations/{operation_id}"
                ).status_code
                == 200
            )
        with TestClient(app, base_url=app.state.settings.portal_origin) as bob_client:
            login(bob_client, hub, "bob")
            assert (
                bob_client.get(f"/api/v1/operations/{operation_id}").status_code == 404
            )
        with app.state.session_factory() as db:
            operation = db.get(Operation, operation_id)
            assert operation
            assert operation.actor_user_id == admin["user"]["id"]
            assert operation.requested_by_user_id == alice_id
            assert operation.credential_mode == "ADMIN_SERVICE"
            assert operation.auth_session_id_hash is None
        unavailable_worker = OperationWorker(
            replace(
                app.state.settings,
                admin_lifecycle_token_file="/missing/admin-lifecycle-token",
            ),
            app.state.session_factory,
            hub,
            app.state.token_cipher,
            worker_id="missing-admin-token-worker",
        )
        # The older CREATE may be claimed first and fail as superseded; the next
        # claim is the admin service operation whose credential boundary we test.
        for _ in range(2):
            with app.state.session_factory() as db:
                current = db.get(Operation, operation_id)
                if current and current.status == "FAILED":
                    break
            assert asyncio.run(unavailable_worker.process_next()) is True
        with app.state.session_factory() as db:
            operation = db.get(Operation, operation_id)
            assert operation and operation.status == "FAILED"
            assert operation.error_code == "ADMIN_LIFECYCLE_UNAVAILABLE"
            workspace = db.get(Workspace, workspace_id)
            assert workspace
            workspace.observed_state = "STOPPED"
            workspace.stale = False
            db.commit()

        token_path = Path(app.state.settings.admin_lifecycle_token_file)
        token_path.write_text(alice_token, encoding="utf-8")
        wrong_scope = admin_client.post(
            f"/api/v1/admin/workspaces/{workspace_id}/actions/start",
            headers=mutation_headers(admin, "admin-wrong-scope-start"),
        )
        assert wrong_scope.status_code == 202, wrong_scope.text
        worker = _worker(app, hub, "wrong-scope-admin-worker")
        assert asyncio.run(worker.process_next()) is True
        with app.state.session_factory() as db:
            operation = db.get(Operation, wrong_scope.json()["operation"]["id"])
            assert operation and operation.status == "FAILED"
            assert operation.error_code == "ADMIN_LIFECYCLE_UNAVAILABLE"
            assert "login" not in (operation.error_summary or "").lower()
            workspace = db.get(Workspace, workspace_id)
            assert workspace
            workspace.observed_state = "RUNNING"
            workspace.stale = False
            db.commit()

        token_path.write_text("unknown-admin-token", encoding="utf-8")
        invalid_token = admin_client.post(
            f"/api/v1/admin/workspaces/{workspace_id}/actions/stop",
            headers=mutation_headers(admin, "admin-invalid-token-stop"),
        )
        assert invalid_token.status_code == 202, invalid_token.text
        assert asyncio.run(worker.process_next()) is True
        with app.state.session_factory() as db:
            operation = db.get(Operation, invalid_token.json()["operation"]["id"])
            assert operation and operation.status == "FAILED"
            assert operation.error_code == "ADMIN_LIFECYCLE_UNAVAILABLE"


def test_tampered_secret_cipher_is_a_controlled_error(management_env):
    app, hub, client = management_env
    me, *_ = login(client, hub, "cipher-user")
    provision(app, "cipher-user")
    created = _create(
        client,
        me,
        key="cipher-create",
        environment=[{"name": "TOKEN", "value": "cipher-secret", "is_secret": True}],
    )
    with app.state.session_factory() as db:
        item = db.scalar(
            select(EnvironmentVariable).where(EnvironmentVariable.name == "TOKEN")
        )
        assert item and item.value_cipher
        item.value_cipher = item.value_cipher[:-2] + "AA"
        db.commit()
    worker = _worker(app, hub, "cipher-worker")
    assert asyncio.run(worker.process_next()) is True
    with app.state.session_factory() as db:
        operation = db.get(Operation, created["operation"]["id"])
        assert operation and operation.status == "FAILED"
        assert operation.error_code == "ENVIRONMENT_DECRYPT_FAILED"
        assert "cipher-secret" not in (operation.error_summary or "")
