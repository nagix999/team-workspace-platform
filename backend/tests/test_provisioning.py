from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.admin import provision_user
from app.db import Base
from app.hub import FakeJupyterHubProvider
from app.main import create_app
from app.models import (
    AuditEvent,
    User,
    UserProvisioningJob,
    WorkspaceProfile,
    WorkspaceVolumeSlot,
)
from app.security import internal_signature
from app.services.profile_offers import ensure_default_offers
from app.services.resource_policy import get_resource_policy

from conftest import login


CLAIM_PATH = "/internal/v1/user-provisioning/claim"
COMPLETE_PATH = "/internal/v1/user-provisioning/complete"
FAIL_PATH = "/internal/v1/user-provisioning/fail"


@pytest.fixture
def provisioning_env(settings):
    local = replace(
        settings,
        portal_origin="http://platform.localhost:8080",
        hub_public_url="http://hub.localhost:8080",
        hub_user_domain="hub.localhost",
        oauth_redirect_uri=("http://platform.localhost:8080/api/v1/auth/callback"),
        cookie_secure=False,
        insecure_local_dev=True,
        web_provisioning_enabled=True,
    )
    hub = FakeJupyterHubProvider()
    app = create_app(local, hub)
    Base.metadata.create_all(app.state.engine)
    with app.state.session_factory() as db:
        db.add(
            WorkspaceProfile(
                id="python-standard",
                version=1,
                name="Python standard",
                kernel_name="python3",
                kernel_display_name="Python 3",
                python_version="3.12.0",
                image_ref="quay.io/jupyterhub/singleuser:5.5",
                cpu_limit="1.0",
                memory_limit_mb=1024,
                pids_limit=256,
                private_disk_limit_mb=1024,
                private_disk_quota_enforced=False,
                provider_options_json=(
                    '{"gid":100,"private_disk_hard_limit_bytes":1073741824,'
                    '"uid":1000}'
                ),
                config_digest="sha256:" + "b" * 64,
                enabled=True,
                selectable=True,
            )
        )
        db.flush()
        ensure_default_offers(db)
        get_resource_policy(db, local, create=True)
        db.commit()
    with TestClient(app, base_url=local.portal_origin) as client:
        yield app, hub, client


def _mutation_headers(me: dict, *, key: str | None = None) -> dict[str, str]:
    headers = {
        "Origin": "http://platform.localhost:8080",
        "X-CSRF-Token": me["csrf_token"],
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _signed_headers(key: str, path: str, body: bytes, *, nonce: str) -> dict[str, str]:
    timestamp = int(time.time())
    return {
        "Content-Type": "application/json",
        "X-Platform-HMAC-Version": "v1",
        "X-Platform-Timestamp": str(timestamp),
        "X-Platform-Nonce": nonce,
        "X-Platform-Content-SHA256": hashlib.sha256(body).hexdigest(),
        "X-Platform-Signature": "v1="
        + internal_signature(
            key,
            timestamp=timestamp,
            nonce=nonce,
            method="POST",
            path=path,
            body=body,
        ),
    }


def _internal_post(app, client, path: str, payload: dict, *, nonce: str):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return client.post(
        path,
        content=body,
        headers=_signed_headers(
            app.state.settings.internal_hmac_key,
            path,
            body,
            nonce=nonce,
        ),
    )


def _request_job(client, me: dict):
    return client.post(
        "/api/v1/me/provisioning",
        headers=_mutation_headers(me),
    )


def _claim(app, client, worker_id: str, *, nonce: str):
    return _internal_post(
        app,
        client,
        CLAIM_PATH,
        {"schema_version": 1, "worker_id": worker_id},
        nonce=nonce,
    )


def _manifest(user_id: str, username: str) -> dict:
    user_uuid = uuid.UUID(user_id)
    return {
        "schema_version": 1,
        "unsafe_local_dev": True,
        "user_id": user_id,
        "username": username,
        "uid": 1000,
        "gid": 100,
        "slots": [
            {
                "slot_id": str(
                    uuid.uuid5(
                        user_uuid,
                        f"workspace-volume-slot-{number}",
                    )
                ),
                "slot_number": number,
                "volume_name": f"jupyter-user-{username}-slot-{number}",
                "hard_limit_bytes": 1024 * 1024 * 1024,
                "project_id": 10_000 + number - 1,
            }
            for number in range(1, 6)
        ],
    }


def test_public_request_is_feature_gated_and_csrf_protected(app_env):
    _app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    assert me["provisioning"]["status"] == "MANUAL_REQUIRED"

    missing_csrf = client.post("/api/v1/me/provisioning")
    assert missing_csrf.status_code == 403
    disabled = client.post(
        "/api/v1/me/provisioning",
        headers={
            "Origin": "https://platform.example.com",
            "X-CSRF-Token": me["csrf_token"],
        },
    )
    assert disabled.status_code == 409
    assert disabled.json()["error"]["code"] == "WEB_PROVISIONING_DISABLED"


def test_web_provisioning_cannot_be_enabled_in_production(settings):
    production = replace(settings, web_provisioning_enabled=True)
    with pytest.raises(RuntimeError, match="restricted to an explicit local test mode"):
        production.validate()


def test_internal_hmac_key_requires_32_bytes(settings):
    weak = replace(settings, internal_hmac_key="too-short")
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        weak.validate()


def test_public_request_is_idempotent_and_me_reports_job(provisioning_env):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    assert me["provisioning"] == {
        "status": "NOT_REQUESTED",
        "attempts": 0,
        "max_attempts": 3,
        "error_code": None,
        "error_summary": None,
        "requested_at": None,
        "started_at": None,
        "completed_at": None,
        "updated_at": None,
    }

    first = _request_job(client, me)
    second = _request_job(client, me)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert first.json()["provisioning"] == {
        "status": "PENDING",
        "attempts": 0,
        "max_attempts": 3,
        "error_code": None,
        "error_summary": None,
        "requested_at": first.json()["provisioning"]["requested_at"],
        "started_at": None,
        "completed_at": None,
        "updated_at": first.json()["provisioning"]["updated_at"],
    }
    refreshed = client.get("/api/v1/me").json()
    assert refreshed["provisioning"]["status"] == "PENDING"
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(UserProvisioningJob.user_id))) == 1
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "USER_PROVISIONING_REQUESTED"
                )
            )
            == 1
        )


def test_claim_complete_activates_user_and_enables_workspace(provisioning_env):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    requested = _request_job(client, me)
    assert requested.status_code == 202

    claim = _claim(app, client, "hub-agent-1", nonce="provision-claim-00000001")
    assert claim.status_code == 200, claim.text
    claimed = claim.json()
    assert set(claimed) == {
        "schema_version",
        "user_id",
        "username",
        "attempt_no",
        "lease_expires_at",
    }
    assert claimed["username"] == "alice"
    assert claimed["attempt_no"] == 1

    complete = _internal_post(
        app,
        client,
        COMPLETE_PATH,
        {
            "schema_version": 1,
            "worker_id": "hub-agent-1",
            "user_id": claimed["user_id"],
            "attempt_no": claimed["attempt_no"],
            "manifest": _manifest(claimed["user_id"], "alice"),
        },
        nonce="provision-complete-00001",
    )
    assert complete.status_code == 204, complete.text
    assert complete.content == b""

    with app.state.session_factory() as db:
        user = db.get(User, claimed["user_id"])
        job = db.get(UserProvisioningJob, claimed["user_id"])
        assert user and user.status == "ACTIVE"
        assert job and job.status == "SUCCEEDED"
        assert job.lease_owner is None and job.lease_expires_at is None
        assert (
            db.scalar(
                select(func.count(WorkspaceVolumeSlot.id)).where(
                    WorkspaceVolumeSlot.owner_user_id == claimed["user_id"]
                )
            )
            == 5
        )

    created = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=_mutation_headers(me, key="create-after-provisioning"),
    )
    assert created.status_code == 202, created.text
    assert created.json()["operation"]["status"] == "SUCCEEDED"
    assert created.json()["workspace"]["desired_state"] == "STOPPED"


def test_manifest_mismatch_rolls_back_slots_and_active_transition(
    provisioning_env,
):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    _request_job(client, me)
    claimed = _claim(app, client, "hub-agent-1", nonce="bad-manifest-claim-0001").json()
    manifest = _manifest(claimed["user_id"], "alice")
    manifest["slots"][4]["hard_limit_bytes"] = 512 * 1024 * 1024

    rejected = _internal_post(
        app,
        client,
        COMPLETE_PATH,
        {
            "schema_version": 1,
            "worker_id": "hub-agent-1",
            "user_id": claimed["user_id"],
            "attempt_no": 1,
            "manifest": manifest,
        },
        nonce="bad-manifest-complete-01",
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "PROVISIONING_MANIFEST_REJECTED"
    with app.state.session_factory() as db:
        user = db.get(User, claimed["user_id"])
        job = db.get(UserProvisioningJob, claimed["user_id"])
        assert user and user.status == "PROVISIONING"
        assert job and job.status == "RUNNING"
        assert (
            db.scalar(
                select(func.count(WorkspaceVolumeSlot.id)).where(
                    WorkspaceVolumeSlot.owner_user_id == claimed["user_id"]
                )
            )
            == 0
        )


def test_non_contiguous_project_id_manifest_fails_closed(provisioning_env):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    _request_job(client, me)
    claimed = _claim(app, client, "hub-agent-1", nonce="project-gap-claim-00001").json()
    manifest = _manifest(claimed["user_id"], "alice")
    manifest["slots"][2]["project_id"] += 100

    rejected = _internal_post(
        app,
        client,
        COMPLETE_PATH,
        {
            "schema_version": 1,
            "worker_id": "hub-agent-1",
            "user_id": claimed["user_id"],
            "attempt_no": 1,
            "manifest": manifest,
        },
        nonce="project-gap-complete-001",
    )
    assert rejected.status_code == 409
    with app.state.session_factory() as db:
        user = db.get(User, claimed["user_id"])
        assert user and user.status == "PROVISIONING"
        assert db.scalar(select(func.count(WorkspaceVolumeSlot.id))) == 0


def test_unsafe_manifest_marker_must_be_exact_json_boolean(provisioning_env):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    _request_job(client, me)
    claimed = _claim(app, client, "hub-agent-1", nonce="marker-type-claim-00001").json()
    manifest = _manifest(claimed["user_id"], "alice")
    manifest["unsafe_local_dev"] = 1
    rejected = _internal_post(
        app,
        client,
        COMPLETE_PATH,
        {
            "schema_version": 1,
            "worker_id": "hub-agent-1",
            "user_id": claimed["user_id"],
            "attempt_no": 1,
            "manifest": manifest,
        },
        nonce="marker-type-complete-001",
    )
    assert rejected.status_code == 422
    with app.state.session_factory() as db:
        user = db.get(User, claimed["user_id"])
        assert user and user.status == "PROVISIONING"
        assert db.scalar(select(func.count(WorkspaceVolumeSlot.id))) == 0


def test_fail_is_generic_bounded_and_public_retry_resets_attempts(
    provisioning_env,
):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    _request_job(client, me)
    user_id = me["user"]["id"]

    for attempt in range(1, 4):
        claim = _claim(
            app,
            client,
            "hub-agent-1",
            nonce=f"failure-claim-{attempt:08d}",
        )
        assert claim.status_code == 200
        assert claim.json()["attempt_no"] == attempt
        failed = _internal_post(
            app,
            client,
            FAIL_PATH,
            {
                "schema_version": 1,
                "worker_id": "hub-agent-1",
                "user_id": user_id,
                "attempt_no": attempt,
            },
            nonce=f"failure-report-{attempt:08d}",
        )
        assert failed.status_code == 204

    status = client.get("/api/v1/me").json()["provisioning"]
    assert status["status"] == "FAILED"
    assert status["attempts"] == 3
    assert status["error_code"] == "PROVISIONING_ATTEMPTS_EXHAUSTED"
    assert "Docker" not in status["error_summary"]

    retried = _request_job(client, me)
    assert retried.status_code == 202
    assert retried.json()["provisioning"]["status"] == "PENDING"
    assert retried.json()["provisioning"]["attempts"] == 0
    assert retried.json()["provisioning"]["error_summary"] is None


def test_stale_claim_and_extra_failure_detail_are_rejected(provisioning_env):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    _request_job(client, me)
    user_id = me["user"]["id"]
    first = _claim(app, client, "hub-agent-old", nonce="stale-claim-first-0001").json()

    leaked_detail = _internal_post(
        app,
        client,
        FAIL_PATH,
        {
            "schema_version": 1,
            "worker_id": "hub-agent-old",
            "user_id": user_id,
            "attempt_no": first["attempt_no"],
            "error_summary": "sensitive Docker daemon output",
        },
        nonce="failure-extra-detail-0001",
    )
    assert leaked_detail.status_code == 422

    with app.state.session_factory() as db:
        job = db.get(UserProvisioningJob, user_id)
        assert job
        job.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    second = _claim(app, client, "hub-agent-new", nonce="stale-claim-second-001")
    assert second.status_code == 200
    assert second.json()["attempt_no"] == 2

    stale = _internal_post(
        app,
        client,
        FAIL_PATH,
        {
            "schema_version": 1,
            "worker_id": "hub-agent-old",
            "user_id": user_id,
            "attempt_no": 1,
        },
        nonce="stale-failure-report-001",
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PROVISIONING_LEASE_LOST"


def test_running_job_with_missing_lease_is_reclaimed(provisioning_env):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    _request_job(client, me)
    first = _claim(
        app, client, "hub-agent-old", nonce="null-lease-first-claim-01"
    ).json()
    with app.state.session_factory() as db:
        job = db.get(UserProvisioningJob, first["user_id"])
        assert job
        job.lease_expires_at = None
        db.commit()
    reclaimed = _claim(app, client, "hub-agent-new", nonce="null-lease-reclaim-0001")
    assert reclaimed.status_code == 200
    assert reclaimed.json()["attempt_no"] == 2


def test_claim_requires_hmac_and_returns_204_when_queue_is_empty(
    provisioning_env,
):
    app, _hub, client = provisioning_env
    unsigned = client.post(
        CLAIM_PATH,
        json={"schema_version": 1, "worker_id": "hub-agent-1"},
    )
    assert unsigned.status_code == 401
    nonce = "empty-claim-000000001"
    empty = _claim(app, client, "hub-agent-1", nonce=nonce)
    assert empty.status_code == 204
    assert empty.content == b""
    replay = _claim(app, client, "hub-agent-1", nonce=nonce)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "INTERNAL_REPLAY_REJECTED"

    wrong_schema_type = _internal_post(
        app,
        client,
        CLAIM_PATH,
        {"schema_version": True, "worker_id": "hub-agent-1"},
        nonce="claim-schema-boolean-001",
    )
    assert wrong_schema_type.status_code == 422


def test_active_user_request_returns_succeeded_idempotently(provisioning_env):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    provision_user(
        app.state.settings,
        username="alice",
        manifest_path=None,
        local_project_id_base=15_000,
    )

    first = _request_job(client, me)
    second = _request_job(client, me)
    assert first.status_code == second.status_code == 202
    assert first.json()["provisioning"]["status"] == "SUCCEEDED"
    assert second.json() == first.json()
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(UserProvisioningJob.user_id))) == 1


def test_active_user_with_incomplete_inventory_is_not_reported_succeeded(
    provisioning_env,
):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    user_id = me["user"]["id"]
    with app.state.session_factory() as db:
        user = db.get(User, user_id)
        assert user
        user.status = "ACTIVE"
        db.commit()

    current = client.get("/api/v1/me")
    assert current.status_code == 200
    assert current.json()["provisioning"]["status"] == "FAILED"
    assert (
        current.json()["provisioning"]["error_code"] == "PROVISIONING_INVARIANT_FAILED"
    )
    requested = _request_job(client, me)
    assert requested.status_code == 409
    assert requested.json()["error"]["code"] == "PROVISIONING_INVARIANT_FAILED"


def test_inventory_drift_overrides_succeeded_job_and_blocks_workspace_create(
    provisioning_env,
):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    _request_job(client, me)
    claimed = _claim(app, client, "hub-agent-1", nonce="drift-claim-000000001").json()
    completed = _internal_post(
        app,
        client,
        COMPLETE_PATH,
        {
            "schema_version": 1,
            "worker_id": "hub-agent-1",
            "user_id": claimed["user_id"],
            "attempt_no": 1,
            "manifest": _manifest(claimed["user_id"], "alice"),
        },
        nonce="drift-complete-000001",
    )
    assert completed.status_code == 204
    with app.state.session_factory() as db:
        slot = db.scalar(
            select(WorkspaceVolumeSlot).where(
                WorkspaceVolumeSlot.owner_user_id == claimed["user_id"],
                WorkspaceVolumeSlot.slot_no == 1,
            )
        )
        assert slot
        slot.provision_status = "ERROR"
        db.commit()

    current = client.get("/api/v1/me").json()["provisioning"]
    assert current["status"] == "FAILED"
    assert current["error_code"] == "PROVISIONING_INVARIANT_FAILED"
    create = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=_mutation_headers(me, key="create-after-drift"),
    )
    assert create.status_code == 409
    assert create.json()["error"]["code"] == "PROVISIONING_INVARIANT_FAILED"


def test_disabled_user_is_never_claimed_or_reactivated(provisioning_env):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    _request_job(client, me)
    user_id = me["user"]["id"]
    with app.state.session_factory() as db:
        user = db.get(User, user_id)
        assert user
        user.status = "DISABLED"
        db.commit()

    public_retry = _request_job(client, me)
    assert public_retry.status_code == 401
    claim = _claim(app, client, "hub-agent-1", nonce="disabled-user-claim-0001")
    assert claim.status_code == 204
    with app.state.session_factory() as db:
        user = db.get(User, user_id)
        job = db.get(UserProvisioningJob, user_id)
        assert user and user.status == "DISABLED"
        assert job and job.status == "FAILED"
        assert job.error_code == "USER_NOT_ACTIVE"
        assert db.scalar(select(func.count(WorkspaceVolumeSlot.id))) == 0


def test_legacy_local_cli_activation_keeps_explicit_project_id_base(
    provisioning_env,
):
    app, hub, client = provisioning_env
    login(client, hub, "alice")
    provision_user(
        app.state.settings,
        username="alice",
        manifest_path=None,
        local_project_id_base=12_345,
    )
    with app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.hub_username == "alice"))
        assert user and user.status == "ACTIVE"
        slots = db.scalars(
            select(WorkspaceVolumeSlot)
            .where(WorkspaceVolumeSlot.owner_user_id == user.id)
            .order_by(WorkspaceVolumeSlot.slot_no)
        ).all()
        assert [slot.quota_project_id for slot in slots] == list(range(12_345, 12_350))


def test_cli_activation_rejects_unknown_user_state_without_slots(
    provisioning_env,
):
    app, hub, client = provisioning_env
    me, *_ = login(client, hub, "alice")
    with app.state.session_factory() as db:
        user = db.get(User, me["user"]["id"])
        assert user
        user.status = "CORRUPT"
        db.commit()

    with pytest.raises(ValueError, match="does not permit"):
        provision_user(
            app.state.settings,
            username="alice",
            manifest_path=None,
            local_project_id_base=20_000,
        )
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(WorkspaceVolumeSlot.id))) == 0
