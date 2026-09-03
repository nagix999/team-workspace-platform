from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import Base, create_database_engine, create_session_factory
from app.errors import AppError
from app.hub import FakeJupyterHubProvider
from app.main import create_app
from app.models import AuditEvent, InternalEgressPolicy, InternalEgressRule, User
from app.services.internal_egress import (
    ACK_POLICY_MODE,
    DESIRED_POLICY_MODE,
    EMPTY_POLICY_DIGEST,
    IMMUTABLE_PLATFORM_NETWORKS,
    RUNTIME_DIRECTORY_MODE,
    canonical_policy_body,
    create_internal_egress_rule,
    desired_policy_payload,
    get_internal_egress_policy,
    normalize_destination_cidr,
    parse_ack_payload,
    policy_digest,
    retry_internal_egress_policy,
    sync_internal_egress_runtime,
    update_internal_egress_rule,
    validate_internal_service_port,
)
from app.worker import OperationWorker

from conftest import login, mutation_headers


def _admin() -> User:
    return User(
        id=str(uuid.uuid4()),
        auth_provider="jupyterhub",
        auth_subject="admin",
        hub_username="admin",
        role="ADMIN",
        status="ACTIVE",
        environment_generation=1,
    )


def _database(tmp_path: Path, settings):
    isolated = replace(settings, database_url=f"sqlite:///{tmp_path / 'egress.db'}")
    engine = create_database_engine(isolated)
    Base.metadata.create_all(engine)
    return engine, create_session_factory(engine)


def _runtime_layout(tmp_path: Path) -> Path:
    runtime = tmp_path / "policy-runtime"
    runtime.mkdir(mode=0o755)
    (runtime / "desired").mkdir(mode=RUNTIME_DIRECTORY_MODE)
    (runtime / "ack").mkdir(mode=RUNTIME_DIRECTORY_MODE)
    runtime.chmod(0o755)
    for path in (runtime / "desired", runtime / "ack"):
        path.chmod(RUNTIME_DIRECTORY_MODE)
    return runtime


def _write_ack(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(ACK_POLICY_MODE)


def test_exact_private_destination_and_control_port_validation() -> None:
    assert normalize_destination_cidr("10.255.255.254/32") == "10.255.255.254/32"
    assert normalize_destination_cidr("192.168.9.1/32") == "192.168.9.1/32"
    assert validate_internal_service_port(8000) == 8000

    for value in (
        "10.255.255.254/24",
        "10.255.255.0254/32",
        "10.255.255.254",
        "203.0.113.10/32",
        "fd00::1/128",
    ):
        with pytest.raises(ValueError):
            normalize_destination_cidr(value)
    for network in IMMUTABLE_PLATFORM_NETWORKS:
        with pytest.raises(ValueError, match="immutable platform network"):
            normalize_destination_cidr(f"{network.network_address + 1}/32")
    for value in (1, 80, 1023, 2375, 2376, 2377, 3128, 4243, 6443, 10250, 65536):
        with pytest.raises(ValueError):
            validate_internal_service_port(value)
    with pytest.raises(ValueError):
        validate_internal_service_port(True)


def test_canonical_body_and_ack_protocol_are_exact() -> None:
    rules = [
        InternalEgressRule(id="b", destination_cidr="192.168.1.2/32", port=8443),
        InternalEgressRule(id="a", destination_cidr="10.255.255.254/32", port=8000),
    ]
    body = b"10.255.255.254/32 8000\n192.168.1.2/32 8443\n"
    assert canonical_policy_body(rules) == body
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    assert policy_digest(body) == digest
    assert desired_policy_payload(7, rules) == (
        f"PLATFORM_INTERNAL_EGRESS_V1 7 {digest}\n".encode() + body
    )
    assert (
        parse_ack_payload(
            f"PLATFORM_INTERNAL_EGRESS_ACK_V1 7 {digest} APPLIED NONE\n".encode()
        ).revision
        == 7
    )

    malformed = (
        b"",
        f"PLATFORM_INTERNAL_EGRESS_ACK_V1 7 {digest} APPLIED NONE".encode(),
        f"PLATFORM_INTERNAL_EGRESS_ACK_V1 07 {digest} APPLIED NONE\n".encode(),
        f"PLATFORM_INTERNAL_EGRESS_ACK_V1 7 {digest} APPLIED ERROR\n".encode(),
        f"PLATFORM_INTERNAL_EGRESS_ACK_V1 7 {digest} FAILED bad-code\n".encode(),
        f"PLATFORM_INTERNAL_EGRESS_ACK_V1 7 sha256:{'A' * 64} APPLIED NONE\n".encode(),
    )
    for value in malformed:
        with pytest.raises(ValueError):
            parse_ack_payload(value)


def test_metadata_created_database_gets_fail_closed_singleton_and_retry(
    tmp_path, settings
) -> None:
    engine, factory = _database(tmp_path, settings)
    with factory() as db:
        admin = _admin()
        db.add(admin)
        policy = get_internal_egress_policy(db, create=True)
        assert policy.id == 1
        assert policy.desired_revision == 1
        assert policy.desired_digest == EMPTY_POLICY_DIGEST
        assert policy.apply_status == "PENDING"
        db.commit()

    with factory() as db:
        admin = db.scalar(select(User).where(User.hub_username == "admin"))
        assert admin is not None
        policy, _rule = create_internal_egress_rule(
            db,
            actor_user_id=admin.id,
            expected_revision=1,
            destination_cidr="10.255.255.254/32",
            port=8000,
        )
        digest = policy.desired_digest
        assert policy.desired_revision == 2
        policy.apply_status = "FAILED"
        policy.last_error_code = "SQUID_PARSE_FAILED"
        db.commit()

    with factory() as db:
        admin = db.scalar(select(User).where(User.hub_username == "admin"))
        assert admin is not None
        policy = retry_internal_egress_policy(
            db, actor_user_id=admin.id, expected_revision=2
        )
        assert policy.desired_revision == 3
        assert policy.desired_digest == digest
        assert policy.apply_status == "PENDING"
        assert policy.last_error_code is None
        with pytest.raises(AppError) as error:
            retry_internal_egress_policy(
                db, actor_user_id=admin.id, expected_revision=3
            )
        assert error.value.code == "INTERNAL_EGRESS_POLICY_NOT_FAILED"
        db.rollback()
    engine.dispose()


def test_worker_publishes_and_imports_ack_without_losing_last_good(
    tmp_path, settings
) -> None:
    engine, factory = _database(tmp_path, settings)
    runtime = _runtime_layout(tmp_path)
    with factory() as db:
        admin = _admin()
        db.add(admin)
        get_internal_egress_policy(db, create=True)
        db.flush()
        policy, _rule = create_internal_egress_rule(
            db,
            actor_user_id=admin.id,
            expected_revision=1,
            destination_cidr="10.255.255.254/32",
            port=8000,
        )
        revision = policy.desired_revision
        digest = policy.desired_digest
        db.commit()

    assert sync_internal_egress_runtime(factory, runtime) is True
    desired = runtime / "desired" / "policy.txt"
    assert stat.S_IMODE(desired.stat().st_mode) == DESIRED_POLICY_MODE
    assert desired.read_bytes().startswith(
        f"PLATFORM_INTERNAL_EGRESS_V1 {revision} {digest}\n".encode()
    )
    with factory() as db:
        assert db.get(InternalEgressPolicy, 1).apply_status == "APPLYING"

    _write_ack(
        runtime / "ack" / "status.txt",
        f"PLATFORM_INTERNAL_EGRESS_ACK_V1 {revision} {digest} APPLIED NONE\n".encode(),
    )
    assert sync_internal_egress_runtime(factory, runtime) is True
    with factory() as db:
        policy = db.get(InternalEgressPolicy, 1)
        assert policy is not None
        assert policy.apply_status == "APPLIED"
        assert policy.applied_revision == revision
        assert policy.applied_digest == digest
        rule = db.scalar(select(InternalEgressRule))
        assert rule is not None
        update_internal_egress_rule(
            db,
            rule_id=rule.id,
            actor_user_id=rule.updated_by_user_id,
            expected_revision=revision,
            expected_version=1,
            destination_cidr=rule.destination_cidr,
            port=8443,
        )
        db.commit()

    assert sync_internal_egress_runtime(factory, runtime) is True
    with factory() as db:
        policy = db.get(InternalEgressPolicy, 1)
        assert policy is not None
        failed_revision = policy.desired_revision
        failed_digest = policy.desired_digest
        assert policy.apply_status == "APPLYING"
    # The read-only ACK mount still contains the prior revision. It is stale,
    # not a failure, while Squid consumes the new desired file.
    assert sync_internal_egress_runtime(factory, runtime) is False
    _write_ack(
        runtime / "ack" / "status.txt",
        (
            f"PLATFORM_INTERNAL_EGRESS_ACK_V1 {failed_revision} {failed_digest} "
            "FAILED SQUID_PARSE_FAILED\n"
        ).encode(),
    )
    assert sync_internal_egress_runtime(factory, runtime) is True
    with factory() as db:
        policy = db.get(InternalEgressPolicy, 1)
        assert policy is not None
        assert policy.apply_status == "FAILED"
        assert policy.applied_revision == revision
        assert policy.applied_digest == digest
        assert db.scalar(select(func.count(AuditEvent.id))) == 2
    engine.dispose()


def test_worker_ignores_preexisting_malformed_ack_until_after_publish(
    tmp_path, settings
) -> None:
    engine, factory = _database(tmp_path, settings)
    runtime = _runtime_layout(tmp_path)
    ack = runtime / "ack" / "status.txt"
    _write_ack(ack, b"malformed\n")
    os.utime(ack, ns=(1, 1))

    assert sync_internal_egress_runtime(factory, runtime) is True
    assert sync_internal_egress_runtime(factory, runtime) is False
    with factory() as db:
        policy = db.get(InternalEgressPolicy, 1)
        assert policy is not None
        assert policy.apply_status == "APPLYING"
        assert policy.last_error_code is None

    desired_mtime = (runtime / "desired" / "policy.txt").stat().st_mtime_ns
    os.utime(ack, ns=(desired_mtime + 1, desired_mtime + 1))
    assert sync_internal_egress_runtime(factory, runtime) is True
    with factory() as db:
        policy = db.get(InternalEgressPolicy, 1)
        assert policy is not None
        assert policy.apply_status == "FAILED"
        assert policy.last_error_code == "ACK_INVALID"
    engine.dispose()


def test_worker_retries_ack_atomic_rename_race_without_false_failure(
    tmp_path, settings, monkeypatch
) -> None:
    engine, factory = _database(tmp_path, settings)
    runtime = _runtime_layout(tmp_path)
    assert sync_internal_egress_runtime(factory, runtime) is True
    with factory() as db:
        policy = db.get(InternalEgressPolicy, 1)
        assert policy is not None
        revision = policy.desired_revision
        digest = policy.desired_digest
    ack = runtime / "ack" / "status.txt"
    payload = (
        f"PLATFORM_INTERNAL_EGRESS_ACK_V1 {revision} {digest} APPLIED NONE\n".encode()
    )
    _write_ack(ack, payload)
    desired_mtime = (runtime / "desired" / "policy.txt").stat().st_mtime_ns
    os.utime(ack, ns=(desired_mtime + 1, desired_mtime + 1))

    original_open = os.open
    raced = False

    def atomic_race(path, flags, *args):
        nonlocal raced
        if not raced and os.fspath(path) == os.fspath(ack):
            raced = True
            replacement = ack.with_name("status.replacement")
            _write_ack(replacement, payload)
            os.utime(replacement, ns=(desired_mtime + 2, desired_mtime + 2))
            os.replace(replacement, ack)
        return original_open(path, flags, *args)

    monkeypatch.setattr("app.services.internal_egress.os.open", atomic_race)
    assert sync_internal_egress_runtime(factory, runtime) is True
    assert raced is True
    with factory() as db:
        policy = db.get(InternalEgressPolicy, 1)
        assert policy is not None
        assert policy.apply_status == "APPLIED"
        assert policy.last_error_code is None
    engine.dispose()


@pytest.mark.parametrize(
    "unsafe_kind",
    ["missing", "subdir_mode", "subdir_file", "subdir_symlink", "file", "symlink"],
)
def test_worker_rejects_unsafe_runtime_directory_contract(
    tmp_path, settings, unsafe_kind
) -> None:
    engine, factory = _database(tmp_path, settings)
    runtime = tmp_path / "unsafe-runtime"
    if unsafe_kind == "subdir_mode":
        runtime.mkdir(mode=0o755)
        runtime.chmod(0o755)
        (runtime / "desired").mkdir(mode=0o755)
        (runtime / "desired").chmod(0o755)
        (runtime / "ack").mkdir(mode=RUNTIME_DIRECTORY_MODE)
        (runtime / "ack").chmod(RUNTIME_DIRECTORY_MODE)
    elif unsafe_kind == "subdir_file":
        runtime.mkdir(mode=0o755)
        runtime.chmod(0o755)
        (runtime / "desired").write_text("not a directory", encoding="ascii")
        (runtime / "ack").mkdir(mode=RUNTIME_DIRECTORY_MODE)
        (runtime / "ack").chmod(RUNTIME_DIRECTORY_MODE)
    elif unsafe_kind == "subdir_symlink":
        runtime.mkdir(mode=0o755)
        runtime.chmod(0o755)
        desired_target = tmp_path / "desired-target"
        desired_target.mkdir(mode=RUNTIME_DIRECTORY_MODE)
        desired_target.chmod(RUNTIME_DIRECTORY_MODE)
        (runtime / "desired").symlink_to(desired_target, target_is_directory=True)
        (runtime / "ack").mkdir(mode=RUNTIME_DIRECTORY_MODE)
        (runtime / "ack").chmod(RUNTIME_DIRECTORY_MODE)
    elif unsafe_kind == "file":
        runtime.write_text("not a directory", encoding="ascii")
    elif unsafe_kind == "symlink":
        target = _runtime_layout(tmp_path)
        runtime.symlink_to(target, target_is_directory=True)

    assert sync_internal_egress_runtime(factory, runtime) is True
    with factory() as db:
        policy = db.get(InternalEgressPolicy, 1)
        assert policy is not None
        assert policy.apply_status == "FAILED"
        assert policy.last_error_code == "POLICY_PUBLISH_FAILED"
    engine.dispose()


@pytest.mark.parametrize("unsafe_kind", ["mode", "directory", "symlink"])
def test_worker_rejects_unsafe_ack_file_contract(
    tmp_path, settings, unsafe_kind
) -> None:
    engine, factory = _database(tmp_path, settings)
    runtime = _runtime_layout(tmp_path)
    ack = runtime / "ack" / "status.txt"
    assert sync_internal_egress_runtime(factory, runtime) is True
    if unsafe_kind == "mode":
        ack.write_text("invalid\n", encoding="ascii")
        ack.chmod(0o666)
    elif unsafe_kind == "directory":
        ack.mkdir()
    else:
        target = tmp_path / "ack-target"
        target.write_text("invalid\n", encoding="ascii")
        ack.symlink_to(target)

    assert sync_internal_egress_runtime(factory, runtime) is True
    with factory() as db:
        policy = db.get(InternalEgressPolicy, 1)
        assert policy is not None
        assert policy.apply_status == "FAILED"
        assert policy.last_error_code == "ACK_INVALID"
    engine.dispose()


def test_worker_policy_sync_exception_does_not_block_lifecycle_claim(
    tmp_path, settings, monkeypatch
) -> None:
    engine, factory = _database(tmp_path, settings)
    managed = replace(settings, internal_egress_policy_dir="/unavailable/policy")
    worker = OperationWorker(
        managed,
        factory,
        FakeJupyterHubProvider(),
        object(),  # type: ignore[arg-type]
        worker_id="egress-isolation-test",
    )
    claimed: list[bool] = []

    def fail_sync(*_args, **_kwargs):
        raise RuntimeError("test policy synchronization failure")

    monkeypatch.setattr("app.worker.sync_internal_egress_runtime", fail_sync)
    monkeypatch.setattr(worker, "_claim", lambda: claimed.append(True) or None)

    assert asyncio.run(worker.process_next()) is False
    assert claimed == [True]
    engine.dispose()


def test_settings_use_exact_internal_egress_policy_directory_env(
    monkeypatch,
) -> None:
    from app.config import Settings

    monkeypatch.setenv("PLATFORM_ALLOW_INSECURE_DEV_SECRETS", "1")
    monkeypatch.setenv("PLATFORM_INSECURE_LOCAL_DEV", "true")
    monkeypatch.setenv(
        "PLATFORM_INTERNAL_EGRESS_POLICY_DIR", "/var/lib/platform-egress-policy"
    )
    monkeypatch.setenv(
        "PLATFORM_INTERNAL_EGRESS_POLICY_RUNTIME_DIR", "/ignored/legacy-name"
    )
    loaded = Settings.from_env()
    assert loaded.internal_egress_policy_dir == "/var/lib/platform-egress-policy"


def test_admin_api_crud_retry_idempotency_and_audit(settings) -> None:
    managed = replace(settings, admin_usernames=("admin",))
    hub = FakeJupyterHubProvider()
    app = create_app(managed, hub)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=managed.portal_origin) as anonymous:
        assert anonymous.get("/api/v1/admin/internal-egress-policy").status_code == 401

    with TestClient(app, base_url=managed.portal_origin) as user_client:
        login(user_client, hub, "alice")
        assert (
            user_client.get("/api/v1/admin/internal-egress-policy").status_code == 403
        )

    with TestClient(app, base_url=managed.portal_origin) as client:
        admin, *_ = login(client, hub, "admin")
        initial = client.get("/api/v1/admin/internal-egress-policy")
        assert initial.status_code == 200, initial.text
        assert initial.json()["policy"] == {
            "desired_revision": 1,
            "desired_digest": EMPTY_POLICY_DIGEST,
            "applied_revision": None,
            "applied_digest": None,
            "apply_status": "PENDING",
            "last_error_code": None,
            "last_error_summary": None,
            "updated_at": initial.json()["policy"]["updated_at"],
        }
        assert initial.json()["rules"] == []

        headers = mutation_headers(admin, "egress-create-1")
        created = client.post(
            "/api/v1/admin/internal-egress-policy/rules",
            json={
                "expected_revision": 1,
                "destination_cidr": "10.255.255.254/32",
                "port": 8000,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["policy"]["desired_revision"] == 2
        assert created.json()["policy"]["apply_status"] == "PENDING"
        rule = created.json()["rules"][0]
        assert rule["row_version"] == 1
        replay = client.post(
            "/api/v1/admin/internal-egress-policy/rules",
            json={
                "expected_revision": 1,
                "destination_cidr": "10.255.255.254/32",
                "port": 8000,
            },
            headers=headers,
        )
        assert replay.status_code == 201
        assert replay.json() == created.json()

        blocked = client.post(
            "/api/v1/admin/internal-egress-policy/rules",
            json={
                "expected_revision": 2,
                "destination_cidr": "172.29.0.10/32",
                "port": 8000,
            },
            headers=mutation_headers(admin, "egress-blocked-network"),
        )
        assert blocked.status_code == 422

        updated = client.patch(
            f"/api/v1/admin/internal-egress-policy/rules/{rule['id']}",
            json={
                "expected_revision": 2,
                "expected_version": 1,
                "destination_cidr": "10.255.255.254/32",
                "port": 8443,
            },
            headers=mutation_headers(admin, "egress-update-1"),
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["policy"]["desired_revision"] == 3
        assert updated.json()["rules"][0]["row_version"] == 2

        deleted = client.delete(
            f"/api/v1/admin/internal-egress-policy/rules/{rule['id']}",
            params={"expected_revision": 3, "expected_version": 2},
            headers=mutation_headers(admin, "egress-delete-1"),
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["policy"]["desired_revision"] == 4
        assert deleted.json()["rules"] == []

        with app.state.session_factory() as db:
            policy = db.get(InternalEgressPolicy, 1)
            assert policy is not None
            policy.apply_status = "FAILED"
            policy.last_error_code = "SQUID_PARSE_FAILED"
            db.commit()
        retried = client.post(
            "/api/v1/admin/internal-egress-policy/retry",
            json={"expected_revision": 4},
            headers=mutation_headers(admin, "egress-retry-1"),
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["policy"]["desired_revision"] == 5
        assert retried.json()["policy"]["apply_status"] == "PENDING"
        assert retried.json()["policy"]["desired_digest"] == EMPTY_POLICY_DIGEST
        replay = client.post(
            "/api/v1/admin/internal-egress-policy/retry",
            json={"expected_revision": 4},
            headers=mutation_headers(admin, "egress-retry-1"),
        )
        assert replay.status_code == 200
        assert replay.json() == retried.json()

    with app.state.session_factory() as db:
        actions = set(
            db.scalars(
                select(AuditEvent.action).where(
                    AuditEvent.action.like("INTERNAL_EGRESS%")
                )
            ).all()
        )
        assert actions == {
            "INTERNAL_EGRESS_RULE_CREATED",
            "INTERNAL_EGRESS_RULE_UPDATED",
            "INTERNAL_EGRESS_RULE_DELETED",
            "INTERNAL_EGRESS_POLICY_RETRIED",
        }
    app.state.engine.dispose()
