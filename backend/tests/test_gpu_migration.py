from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    return Config(str(backend_root / "alembic.ini"))


def test_0006_upgrade_preserves_cpu_rows_and_enforces_gpu_contracts(
    tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'gpu-0005.db'}"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url)
    monkeypatch.setenv("PLATFORM_ALLOW_INSECURE_DEV_SECRETS", "1")
    monkeypatch.setenv("PLATFORM_INSECURE_LOCAL_DEV", "true")
    config = _alembic_config()
    command.upgrade(config, "0005")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspace_profiles "
                "(id, version, name, image_ref, cpu_limit, memory_limit_mb, "
                "pids_limit, private_disk_limit_mb, provider_options_json, "
                "config_digest, enabled, kernel_name, "
                "private_disk_quota_enforced, selectable, python_version, "
                "kernel_display_name) VALUES "
                "('cpu-profile', 1, 'CPU profile', :image, '1', 1024, 256, "
                "1024, '{}', :digest, 1, 'python3', 0, 1, '3.12.13', "
                "'Python 3.12')"
            ),
            {
                "image": "registry.example/singleuser@sha256:" + "a" * 64,
                "digest": "sha256:" + "b" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO resource_policies "
                "(id, version, cpu_budget_millicores, memory_budget_mb, "
                "selectable_cpu_millicores_json, selectable_memory_mb_json, "
                "kernel_idle_timeout_seconds, created_at, updated_at) VALUES "
                "(1, 3, 4000, 4096, '[1000]', '[1024]', 3600, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()

    command.upgrade(config, "0006")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        profile = connection.execute(
            text(
                "SELECT accelerator_kind, gpu_count, cuda_version, "
                "gpu_framework, gpu_framework_version FROM workspace_profiles"
            )
        ).one()
        policy = connection.execute(
            text(
                "SELECT gpu_budget_count, selectable_gpu_counts_json "
                "FROM resource_policies WHERE id = 1"
            )
        ).one()
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert tuple(profile) == ("none", 0, None, None, None)
    assert tuple(policy) == (0, "[0]")
    assert revision == "0006"

    with pytest.raises(IntegrityError, match="ck_profiles_accelerator_contract"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workspace_profiles SET accelerator_kind='nvidia' "
                    "WHERE id='cpu-profile'"
                )
            )
    with pytest.raises(IntegrityError, match="ck_resource_policy_gpu_budget"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE resource_policies SET gpu_budget_count=2 WHERE id=1")
            )
    with pytest.raises(IntegrityError, match="ck_spawn_auth_gpu_contract"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO spawn_authorizations "
                    "(id, ticket_hash, operation_id, attempt_no, workspace_id, "
                    "owner_user_id, workspace_spec_version, private_volume_slot_id, "
                    "hub_username, hub_server_name, profile_id, profile_version, "
                    "profile_config_digest, expires_at, gpu_count, gpu_device_id, "
                    "gpu_inventory_digest) VALUES "
                    "(:id, :ticket, :operation, 1, :workspace, :owner, 1, :slot, "
                    "'gpu-user', 'gpu-server', 'cpu-profile', 1, :digest, "
                    "CURRENT_TIMESTAMP, 1, NULL, :inventory)"
                ),
                {
                    "id": "10000000-0000-0000-0000-000000000001",
                    "ticket": "c" * 64,
                    "operation": "10000000-0000-0000-0000-000000000002",
                    "workspace": "10000000-0000-0000-0000-000000000003",
                    "owner": "10000000-0000-0000-0000-000000000004",
                    "slot": "10000000-0000-0000-0000-000000000005",
                    "digest": "sha256:" + "b" * 64,
                    "inventory": "sha256:" + "d" * 64,
                },
            )
    engine.dispose()

    command.downgrade(config, "0005")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0005"
        )
        for table in (
            "workspace_profiles",
            "workspaces",
            "spawn_authorizations",
            "resource_policies",
        ):
            columns = {
                row[1]
                for row in connection.execute(text(f"PRAGMA table_info({table})"))
            }
            assert not any(
                "gpu" in column or "accelerator" in column for column in columns
            )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()
