from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import select

from app.models import EnvironmentVariable, SpawnAuthorization, Workspace
from app.security import internal_signature
from app.services.resource_policy import get_resource_policy
from app.worker import OperationWorker

from conftest import login, mutation_headers, provision


def _headers(key: str, path: str, body: bytes, *, nonce: str) -> dict[str, str]:
    timestamp = int(time.time())
    digest = hashlib.sha256(body).hexdigest()
    signature = internal_signature(
        key, timestamp=timestamp, nonce=nonce, method="POST", path=path, body=body
    )
    return {
        "Content-Type": "application/json",
        "X-Platform-HMAC-Version": "v1",
        "X-Platform-Timestamp": str(timestamp),
        "X-Platform-Nonce": nonce,
        "X-Platform-Content-SHA256": digest,
        "X-Platform-Signature": f"v1={signature}",
    }


def test_guard_hmac_consume_and_check_exact_contract(app_env):
    app, hub, client = app_env
    hub.auto_ready = False
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    created = client.post(
        "/api/v1/workspaces",
        json={
            "profile_id": "python-standard",
            "profile_version": 1,
        },
        headers=mutation_headers(me, "spawn-contract"),
    ).json()
    for number, (name, value, is_secret) in enumerate(
        (
            ("MODE", "development", False),
            ("API_KEY", "never-log-this", True),
        )
    ):
        response = client.put(
            f"/api/v1/workspaces/{created['workspace']['id']}/environment-variables/{name}",
            json={"value": value, "is_secret": is_secret},
            headers=mutation_headers(me, f"spawn-contract-env-{number}"),
        )
        assert response.status_code == 200, response.text
    started = client.post(
        f"/api/v1/workspaces/{created['workspace']['id']}/actions/start",
        headers=mutation_headers(me, "spawn-contract-start"),
    ).json()
    worker = OperationWorker(
        app.state.settings,
        app.state.session_factory,
        hub,
        app.state.token_cipher,
        worker_id="contract-worker",
    )
    operation_id = worker._claim()
    assert operation_id == started["operation"]["id"]
    with app.state.session_factory() as db:
        workspace = db.get(Workspace, created["workspace"]["id"])
        profile = db.get(
            __import__("app.models", fromlist=["WorkspaceProfile"]).WorkspaceProfile,
            ("python-standard", 1),
        )
        assert workspace and profile
        policy = get_resource_policy(db, app.state.settings)
        policy.kernel_idle_timeout_seconds = 7_200
        db.commit()
        ticket, _ = worker._create_spawn_authorization(
            operation_id, workspace.id, profile
        )

    consume_path = "/internal/v1/spawn-authorizations/consume"
    consume_payload = {
        "schema_version": 2,
        "username": "alice",
        "server_name": workspace.hub_server_name,
        "profile_id": "python-standard",
        "profile_version": 1,
        "spawn_ticket": ticket,
    }
    body = json.dumps(consume_payload, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    consumed = client.post(
        consume_path,
        content=body,
        headers=_headers(
            app.state.settings.internal_hmac_key,
            consume_path,
            body,
            nonce="consume-nonce-12345678",
        ),
    )
    assert consumed.status_code == 200, consumed.text
    response = consumed.json()
    assert set(response) == {"schema_version", "authorized", "authorization"}
    authorization = response["authorization"]
    assert set(authorization) == {
        "spawn_authorization_id",
        "workspace_id",
        "operation_id",
        "attempt_no",
        "workspace_spec_version",
        "username",
        "server_name",
        "profile_id",
        "profile_version",
        "profile_config_digest",
        "runtime_base_profile_id",
        "runtime_base_profile_version",
        "runtime_base_profile_config_digest",
        "cpu_limit_millicores",
        "memory_limit_bytes",
        "private_volume_slot_id",
        "private_volume_slot_number",
        "private_volume_name",
        "private_disk_hard_limit_bytes",
        "uid",
        "gid",
        "environment",
        "environment_digest",
        "user_environment_generation",
        "workspace_environment_generation",
        "kernel_idle_timeout_seconds",
        "gpu_count",
        "gpu_device_ids",
        "gpu_inventory_digest",
        "valid_until_unix",
    }
    assert authorization["kernel_idle_timeout_seconds"] == 7_200
    assert authorization["gpu_count"] == 0
    assert authorization["gpu_device_ids"] == []
    assert authorization["gpu_inventory_digest"] is None
    assert authorization["environment"] == {
        "API_KEY": "never-log-this",
        "MODE": "development",
    }
    assert authorization["environment_digest"].startswith("hmac-sha256:")
    with app.state.session_factory() as db:
        stored = db.get(SpawnAuthorization, authorization["spawn_authorization_id"])
        secret = db.scalar(
            select(EnvironmentVariable).where(EnvironmentVariable.name == "API_KEY")
        )
        assert stored and stored.environment_snapshot_cipher is None
        assert stored.kernel_idle_timeout_seconds == 7_200
        assert secret and secret.plain_value is None
        assert secret.value_cipher and "never-log-this" not in secret.value_cipher

    check_path = "/internal/v1/spawn-authorizations/check"
    check_authorization = {
        key: value for key, value in authorization.items() if key != "environment"
    }
    # Changing the global policy later must not mutate the consumed launch
    # contract during the post-spawn validation race window.
    with app.state.session_factory() as db:
        policy = get_resource_policy(db, app.state.settings)
        policy.kernel_idle_timeout_seconds = 300
        db.commit()
    tampered_body = json.dumps(
        {
            "schema_version": 2,
            **check_authorization,
            "kernel_idle_timeout_seconds": 3_600,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    tampered = client.post(
        check_path,
        content=tampered_body,
        headers=_headers(
            app.state.settings.internal_hmac_key,
            check_path,
            tampered_body,
            nonce="check-tampered-12345678",
        ),
    )
    assert tampered.status_code == 403
    assert tampered.json()["error"]["code"] == "SPAWN_BINDING_MISMATCH"
    check_body = json.dumps(
        {"schema_version": 2, **check_authorization},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    checked = client.post(
        check_path,
        content=check_body,
        headers=_headers(
            app.state.settings.internal_hmac_key,
            check_path,
            check_body,
            nonce="check-nonce-123456789",
        ),
    )
    assert checked.status_code == 200, checked.text
    assert checked.json() == {
        "schema_version": 2,
        "authorized": True,
        "spawn_authorization_id": authorization["spawn_authorization_id"],
    }

    replay = client.post(
        check_path,
        content=check_body,
        headers=_headers(
            app.state.settings.internal_hmac_key,
            check_path,
            check_body,
            nonce="check-nonce-123456789",
        ),
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "INTERNAL_REPLAY_REJECTED"
