from __future__ import annotations

from alembic import command
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from test_multi_gpu_migration import _config, _seed_bound_gpu_history


def test_0009_preserves_workspace_and_enforces_usage_bounds(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'resource-usage.db'}"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url)
    monkeypatch.setenv("PLATFORM_ALLOW_INSECURE_DEV_SECRETS", "1")
    monkeypatch.setenv("PLATFORM_INSECURE_LOCAL_DEV", "true")
    config = _config()
    command.upgrade(config, "0007")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _seed_bound_gpu_history(connection)
    engine.dispose()

    command.upgrade(config, "0009")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT display_name, cpu_usage_millicores, memory_usage_bytes, "
                "memory_limit_bytes, resource_usage_observed_at FROM workspaces"
            )
        ).one()
        assert tuple(row) == ("GPU workspace", None, None, None, None)
        connection.execute(
            text(
                "UPDATE workspaces SET cpu_usage_millicores=125, "
                "memory_usage_bytes=1024, memory_limit_bytes=2048, "
                "resource_usage_observed_at=CURRENT_TIMESTAMP"
            )
        )
    for assignment in (
        "cpu_usage_millicores=-1",
        "memory_usage_bytes=-1",
        "memory_limit_bytes=0",
        "memory_usage_bytes=4096",
        "memory_usage_bytes=NULL",
    ):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(f"UPDATE workspaces SET {assignment}"))
    engine.dispose()

    command.downgrade(config, "0008")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(workspaces)"))
        }
        assert "cpu_usage_millicores" not in columns
        assert connection.execute(text("SELECT count(*) FROM workspaces")).scalar_one() == 1
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()
