from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import offline_maintenance
from app.db import Base
from app.models import (
    AuditEvent,
    Operation,
    SpawnAuthorization,
    User,
    UserProvisioningJob,
    Workspace,
    WorkspaceDeletionJob,
    WorkspaceProfile,
    WorkspaceVolumeSlot,
)
from app.offline_maintenance import OfflineMaintenanceError, quiesce_stopped_intent


USER_ID = "11111111-1111-1111-1111-111111111111"
SLOT_ID = "22222222-2222-2222-2222-222222222222"
WORKSPACE_ID = "33333333-3333-3333-3333-333333333333"
OPERATION_ID = "44444444-4444-4444-4444-444444444444"
CONSUMED_AUTH_ID = "55555555-5555-5555-5555-555555555555"
UNCONSUMED_AUTH_ID = "66666666-6666-6666-6666-666666666666"
PROFILE_DIGEST = "sha256:" + "a" * 64


def _database(tmp_path: Path, *, revision: str = "0007") -> Path:
    path = tmp_path / "platform.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )

    now = datetime.utcnow()
    with Session(engine) as db:
        db.add(
            User(
                id=USER_ID,
                auth_provider="jupyterhub",
                auth_subject="offline-user",
                hub_username="offline-user",
                role="USER",
                status="ACTIVE",
                environment_generation=1,
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(
            WorkspaceProfile(
                id="offline-profile",
                version=1,
                name="Offline profile",
                kernel_name="python3",
                kernel_display_name="Python 3",
                python_version="3.12.0",
                image_ref="example.invalid/singleuser@sha256:" + "b" * 64,
                cpu_limit="1.0",
                memory_limit_mb=1024,
                pids_limit=256,
                private_disk_limit_mb=1024,
                private_disk_quota_enforced=False,
                provider_options_json="{}",
                config_digest=PROFILE_DIGEST,
                enabled=True,
                selectable=True,
            )
        )
        db.add(
            WorkspaceVolumeSlot(
                id=SLOT_ID,
                owner_user_id=USER_ID,
                slot_no=1,
                volume_name="offline-user-slot-1",
                quota_project_id=10001,
                hard_limit_mb=1024,
                provision_status="PROVISIONED",
                verified_at=now,
            )
        )
        db.flush()
        db.add(
            Workspace(
                id=WORKSPACE_ID,
                owner_user_id=USER_ID,
                profile_id="offline-profile",
                profile_version=1,
                hub_target_key="jupyterhub:offline-user:ws-offline",
                hub_server_name="ws-offline",
                private_volume_slot_id=SLOT_ID,
                display_name="Offline environment",
                desired_state="RUNNING",
                observed_state="STOPPED",
                hub_server_url="https://preserved.invalid/user/offline-user/ws-offline/",
                progress_percent=87,
                stale=False,
                last_error_code="PRESERVE_ME",
                last_error_summary="Preserve prior diagnostic state",
                last_reconciled_at=now - timedelta(minutes=1),
                environment_generation=3,
                applied_user_environment_generation=2,
                applied_workspace_environment_generation=2,
                spec_version=7,
                row_version=11,
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
        db.flush()
        db.add(
            Operation(
                id=OPERATION_ID,
                workspace_id=WORKSPACE_ID,
                requested_by_user_id=USER_ID,
                actor_user_id=USER_ID,
                credential_mode="USER_DELEGATED",
                operation_type="START",
                status="SUCCEEDED",
                idempotency_key="offline-original-start",
                attempts=1,
                transient_failures=0,
                requested_at=now - timedelta(minutes=2),
                started_at=now - timedelta(minutes=2),
                completed_at=now - timedelta(minutes=1),
            )
        )
        db.flush()
        for attempt, authorization_id, consumed_at in (
            (1, CONSUMED_AUTH_ID, now - timedelta(minutes=1)),
            (2, UNCONSUMED_AUTH_ID, None),
        ):
            db.add(
                SpawnAuthorization(
                    id=authorization_id,
                    ticket_hash=("c" if consumed_at else "d") * 64,
                    operation_id=OPERATION_ID,
                    attempt_no=attempt,
                    workspace_id=WORKSPACE_ID,
                    owner_user_id=USER_ID,
                    workspace_spec_version=7,
                    private_volume_slot_id=SLOT_ID,
                    hub_username="offline-user",
                    hub_server_name="ws-offline",
                    profile_id="offline-profile",
                    profile_version=1,
                    profile_config_digest=PROFILE_DIGEST,
                    user_environment_generation=1,
                    workspace_environment_generation=3,
                    kernel_idle_timeout_seconds=7_200,
                    expires_at=now + timedelta(minutes=5),
                    consumed_at=consumed_at,
                )
            )
        db.add(
            AuditEvent(
                id="77777777-7777-7777-7777-777777777777",
                actor_user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                action="WORKSPACE_START_COMPLETED",
                result="SUCCEEDED",
                request_id="original-history",
                safe_metadata_json="{}",
                created_at=now - timedelta(minutes=1),
            )
        )
        db.commit()
    engine.dispose()
    return path


def _workspace_snapshot(path: Path) -> tuple[object, ...]:
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT desired_state, observed_state, spec_version, row_version, "
                "hub_server_url, progress_percent, stale, last_error_code, "
                "last_error_summary, last_reconciled_at FROM workspaces WHERE id=:id"
            ),
            {"id": WORKSPACE_ID},
        ).one()
    engine.dispose()
    return tuple(row)


def test_dry_run_lists_targets_without_mutating_database(tmp_path):
    path = _database(tmp_path)
    before = _workspace_snapshot(path)

    result = quiesce_stopped_intent(path)

    assert result == {
        "action": "quiesce-stopped-intent",
        "applied": False,
        "schema_revision": "0007",
        "target_count": 1,
        "workspace_ids": [WORKSPACE_ID],
    }
    assert _workspace_snapshot(path) == before
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM audit_events")).scalar() == 1
        )
        assert (
            connection.execute(
                text("SELECT revoked_at FROM spawn_authorizations WHERE id=:id"),
                {"id": UNCONSUMED_AUTH_ID},
            ).scalar()
            is None
        )
    engine.dispose()


def test_apply_updates_only_intent_versions_unconsumed_auth_and_audit(tmp_path):
    path = _database(tmp_path)
    before = _workspace_snapshot(path)

    result = quiesce_stopped_intent(
        path,
        apply=True,
        expected_count=1,
        backup_bundle_id="production-20260815T120000Z-42",
    )

    after = _workspace_snapshot(path)
    assert after[:4] == ("STOPPED", "STOPPED", 8, 12)
    assert after[4:] == before[4:]
    assert result["target_count"] == 1
    assert result["revoked_unconsumed_authorizations"] == 1
    assert result["audit_event_count"] == 1

    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        operation = connection.execute(
            text(
                "SELECT operation_type, status, attempts, completed_at "
                "FROM operations WHERE id=:id"
            ),
            {"id": OPERATION_ID},
        ).one()
        assert tuple(operation[:3]) == ("START", "SUCCEEDED", 1)
        authorizations = connection.execute(
            text(
                "SELECT id, consumed_at, revoked_at, "
                "kernel_idle_timeout_seconds FROM spawn_authorizations ORDER BY id"
            )
        ).all()
        consumed = next(row for row in authorizations if row.id == CONSUMED_AUTH_ID)
        unconsumed = next(row for row in authorizations if row.id == UNCONSUMED_AUTH_ID)
        assert consumed.consumed_at is not None and consumed.revoked_at is None
        assert unconsumed.consumed_at is None and unconsumed.revoked_at is not None
        assert all(row.kernel_idle_timeout_seconds == 7_200 for row in authorizations)
        audit = connection.execute(
            text(
                "SELECT actor_user_id, action, result, request_id, safe_metadata_json "
                "FROM audit_events WHERE action='OFFLINE_WORKSPACE_QUIESCED'"
            )
        ).one()
        assert audit.actor_user_id is None
        assert audit.action == "OFFLINE_WORKSPACE_QUIESCED"
        assert audit.result == "APPLIED"
        assert audit.request_id.startswith("offline-maintenance:")
        metadata = json.loads(audit.safe_metadata_json)
        assert metadata == {
            "backup_bundle_id": "production-20260815T120000Z-42",
            "preserved_observed_state": "STOPPED",
            "previous_desired_state": "RUNNING",
            "reason": "CONTAINERS_REMOVED_WHILE_STOPPED",
            "revoked_unconsumed_authorizations": 1,
            "schema_version": 1,
        }
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_expected_count_mismatch_rolls_back_every_change(tmp_path):
    path = _database(tmp_path)
    before = _workspace_snapshot(path)

    with pytest.raises(OfflineMaintenanceError) as caught:
        quiesce_stopped_intent(
            path,
            apply=True,
            expected_count=2,
            backup_bundle_id="production-backup",
        )

    assert caught.value.code == "EXPECTED_COUNT_MISMATCH"
    assert _workspace_snapshot(path) == before
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM audit_events")).scalar() == 1
        )
        assert (
            connection.execute(
                text("SELECT revoked_at FROM spawn_authorizations WHERE id=:id"),
                {"id": UNCONSUMED_AUTH_ID},
            ).scalar()
            is None
        )
    engine.dispose()


@pytest.mark.parametrize(
    "busy_kind, expected_detail",
    [
        ("operation", "active_operations"),
        ("provisioning", "active_provisioning_jobs"),
        ("deletion", "active_deletion_jobs"),
        ("observed", "actively_observed_workspaces"),
        ("unsupported", "unsupported_running_intent"),
    ],
)
def test_busy_or_unsupported_state_fails_closed(
    tmp_path, busy_kind: str, expected_detail: str
):
    path = _database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    now = datetime.utcnow()
    with Session(engine) as db:
        workspace = db.get(Workspace, WORKSPACE_ID)
        operation = db.get(Operation, OPERATION_ID)
        assert workspace is not None and operation is not None
        if busy_kind == "operation":
            operation.status = "PENDING"
            operation.completed_at = None
        elif busy_kind == "provisioning":
            db.add(
                UserProvisioningJob(
                    user_id=USER_ID,
                    status="RUNNING",
                    attempts=1,
                    requested_at=now,
                    updated_at=now,
                )
            )
        elif busy_kind == "deletion":
            db.add(
                WorkspaceDeletionJob(
                    workspace_id=WORKSPACE_ID,
                    deletion_id="88888888-8888-8888-8888-888888888888",
                    operation_id=OPERATION_ID,
                    expected_spec_version=workspace.spec_version,
                    status="RUNNING",
                    attempts=1,
                    requested_at=now,
                    updated_at=now,
                )
            )
        elif busy_kind == "observed":
            workspace.desired_state = "STOPPED"
            workspace.observed_state = "RUNNING"
        else:
            workspace.observed_state = "FAILED"
        db.commit()
    engine.dispose()
    before = _workspace_snapshot(path)

    with pytest.raises(OfflineMaintenanceError) as caught:
        quiesce_stopped_intent(path)

    assert caught.value.code == "DATABASE_NOT_QUIESCENT"
    assert expected_detail in caught.value.details
    assert _workspace_snapshot(path) == before


def test_exact_database_revision_is_required(tmp_path):
    path = _database(tmp_path, revision="0004")

    with pytest.raises(OfflineMaintenanceError) as caught:
        quiesce_stopped_intent(path)

    assert caught.value.code == "DATABASE_REVISION_MISMATCH"
    assert _workspace_snapshot(path)[0] == "RUNNING"


def test_enforced_unsafe_sqlite_runtime_fails_before_database_mutation(
    tmp_path, monkeypatch
):
    path = _database(tmp_path)
    before = _workspace_snapshot(path)
    monkeypatch.setattr(offline_maintenance, "_sqlite_version_is_safe", lambda _: False)

    with pytest.raises(OfflineMaintenanceError) as caught:
        quiesce_stopped_intent(path, enforce_safe_sqlite=True)

    assert caught.value.code == "UNSAFE_SQLITE_RUNTIME"
    assert _workspace_snapshot(path) == before


def test_cli_dry_run_emits_machine_readable_result(tmp_path, monkeypatch, capsys):
    path = _database(tmp_path)
    monkeypatch.setattr(offline_maintenance, "PRODUCTION_DATABASE_PATH", path)
    monkeypatch.setenv(
        "PLATFORM_DATABASE_URL", offline_maintenance.PRODUCTION_DATABASE_URL
    )
    monkeypatch.setenv("PLATFORM_ENFORCE_SAFE_SQLITE", "true")
    monkeypatch.setattr(offline_maintenance, "_sqlite_version_is_safe", lambda _: True)

    assert offline_maintenance.main(["quiesce-stopped-intent"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["applied"] is False
    assert output["target_count"] == 1
    assert output["workspace_ids"] == [WORKSPACE_ID]
