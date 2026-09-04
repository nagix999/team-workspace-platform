from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


GPU_A = "GPU-01234567-89ab-cdef-0123-456789abcdef"


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


def _seed_bound_gpu_history(connection) -> None:
    values = {
        "user": "10000000-0000-0000-0000-000000000001",
        "slot": "10000000-0000-0000-0000-000000000002",
        "workspace": "10000000-0000-0000-0000-000000000003",
        "operation": "10000000-0000-0000-0000-000000000004",
        "authorization": "10000000-0000-0000-0000-000000000005",
        "gpu": GPU_A,
        "digest": "sha256:" + "a" * 64,
        "inventory": "sha256:" + "b" * 64,
    }
    connection.execute(
        text(
            "INSERT INTO users (id, auth_provider, auth_subject, hub_username, "
            "role, status, created_at, updated_at) VALUES "
            "(:user, 'jupyterhub', 'alice', 'alice', 'USER', 'ACTIVE', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        values,
    )
    connection.execute(
        text(
            "INSERT INTO workspace_profiles (id, version, name, image_ref, "
            "cpu_limit, memory_limit_mb, pids_limit, private_disk_limit_mb, "
            "provider_options_json, config_digest, enabled, kernel_name, "
            "private_disk_quota_enforced, selectable, python_version, "
            "kernel_display_name, gpu_count, cuda_version, gpu_framework, "
            "gpu_framework_version, accelerator_kind) VALUES "
            "('gpu-profile', 1, 'GPU', 'image', '2', 2048, 512, 1024, '{}', "
            ":digest, 1, 'python312-cuda', 0, 1, '3.12.13', 'Python CUDA', "
            "1, '12.6', 'pytorch', '2.7.1', 'nvidia')"
        ),
        values,
    )
    connection.execute(
        text(
            "INSERT INTO workspace_profile_offers (id, row_version, name, "
            "runtime_profile_id, runtime_profile_version, enabled, created_at, "
            "updated_at) VALUES ('gpu-offer', 1, 'GPU', 'gpu-profile', 1, 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO workspace_volume_slots (id, owner_user_id, slot_no, "
            "volume_name, quota_project_id, hard_limit_mb, provision_status, "
            "verified_at) VALUES (:slot, :user, 1, 'volume-a', 1001, 1024, "
            "'PROVISIONED', CURRENT_TIMESTAMP)"
        ),
        values,
    )
    connection.execute(
        text(
            "INSERT INTO workspaces (id, owner_user_id, profile_id, "
            "profile_version, hub_target_key, hub_server_name, "
            "private_volume_slot_id, desired_state, observed_state, stale, "
            "spec_version, row_version, created_at, updated_at, display_name, "
            "profile_offer_id, profile_offer_version, assigned_gpu_device_id) "
            "VALUES (:workspace, :user, 'gpu-profile', 1, 'target-a', 'server-a', "
            ":slot, 'RUNNING', 'RUNNING', 0, 1, 1, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, 'GPU workspace', 'gpu-offer', 1, :gpu)"
        ),
        values,
    )
    connection.execute(
        text(
            "INSERT INTO operations (id, workspace_id, requested_by_user_id, "
            "operation_type, status, idempotency_key, attempts, requested_at, "
            "actor_user_id) VALUES (:operation, :workspace, :user, 'START', "
            "'RUNNING', 'start-a', 1, CURRENT_TIMESTAMP, :user)"
        ),
        values,
    )
    connection.execute(
        text(
            "INSERT INTO spawn_authorizations (id, ticket_hash, operation_id, "
            "attempt_no, workspace_id, owner_user_id, workspace_spec_version, "
            "private_volume_slot_id, hub_username, hub_server_name, profile_id, "
            "profile_version, profile_config_digest, expires_at, gpu_count, "
            "gpu_device_id, gpu_inventory_digest) VALUES (:authorization, "
            ":ticket, :operation, 1, :workspace, :user, 1, :slot, 'alice', "
            "'server-a', 'gpu-profile', 1, :digest, CURRENT_TIMESTAMP, 1, :gpu, "
            ":inventory)"
        ),
        {**values, "ticket": "c" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO resource_policies (id, version, cpu_budget_millicores, "
            "memory_budget_mb, selectable_cpu_millicores_json, "
            "selectable_memory_mb_json, created_at, updated_at, "
            "gpu_budget_count, selectable_gpu_counts_json) VALUES "
            "(1, 1, 8000, 8192, '[2000]', '[2048]', CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, 1, '[0,1]')"
        )
    )


def test_0008_preserves_bound_history_and_backfills_exact_gpu_lease(
    tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'multi-gpu.db'}"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url)
    monkeypatch.setenv("PLATFORM_ALLOW_INSECURE_DEV_SECRETS", "1")
    monkeypatch.setenv("PLATFORM_INSECURE_LOCAL_DEV", "true")
    config = _config()
    command.upgrade(config, "0007")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _seed_bound_gpu_history(connection)
    engine.dispose()

    command.upgrade(config, "0008")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        for table in (
            "users",
            "workspace_profiles",
            "workspace_profile_offers",
            "workspace_volume_slots",
            "workspaces",
            "operations",
            "spawn_authorizations",
            "resource_policies",
        ):
            assert connection.execute(
                text(f"SELECT count(*) FROM {table}")
            ).scalar_one() == 1
        assert connection.execute(
            text(
                "SELECT assigned_gpu_device_id, assigned_gpu_device_ids_json "
                "FROM workspaces"
            )
        ).one() == (GPU_A, f'["{GPU_A}"]')
        assert connection.execute(
            text("SELECT gpu_device_id, workspace_id FROM workspace_gpu_leases")
        ).one() == (GPU_A, "10000000-0000-0000-0000-000000000003")
        assert connection.execute(
            text("SELECT gpu_device_ids_json FROM spawn_authorizations")
        ).scalar_one() == f'["{GPU_A}"]'

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workspace_profiles SET gpu_count=2 WHERE id='gpu-profile'")
        )
        connection.execute(
            text("UPDATE resource_policies SET gpu_budget_count=2 WHERE id=1")
        )
    with pytest.raises(IntegrityError, match="ck_profiles_accelerator_contract"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE workspace_profiles SET gpu_count=65 WHERE id='gpu-profile'")
            )
    with pytest.raises(IntegrityError, match="ck_resource_policy_gpu_budget"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE resource_policies SET gpu_budget_count=65 WHERE id=1")
            )
    with engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(
                text(
                    "INSERT INTO workspace_gpu_leases "
                    "(gpu_device_id, workspace_id, created_at) VALUES "
                    "('GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', "
                    "'ffffffff-ffff-ffff-ffff-ffffffffffff', CURRENT_TIMESTAMP)"
                )
            )
        connection.rollback()
    engine.dispose()


def test_0008_rejects_noncanonical_legacy_assignment(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'invalid-gpu.db'}"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url)
    monkeypatch.setenv("PLATFORM_ALLOW_INSECURE_DEV_SECRETS", "1")
    monkeypatch.setenv("PLATFORM_INSECURE_LOCAL_DEV", "true")
    config = _config()
    command.upgrade(config, "0007")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _seed_bound_gpu_history(connection)
        connection.execute(
            text("UPDATE workspaces SET assigned_gpu_device_id='GPU-not-canonical'")
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="non-canonical GPU UUID"):
        command.upgrade(config, "0008")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0007"
        workspace_columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(workspaces)"))
        }
        assert "assigned_gpu_device_ids_json" not in workspace_columns
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()
