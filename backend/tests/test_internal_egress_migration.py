from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    return Config(str(backend_root / "alembic.ini"))


def test_0007_upgrade_seeds_policy_preserves_0006_data_and_downgrades(
    tmp_path, monkeypatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'egress-0006.db'}"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url)
    monkeypatch.setenv("PLATFORM_ALLOW_INSECURE_DEV_SECRETS", "1")
    monkeypatch.setenv("PLATFORM_INSECURE_LOCAL_DEV", "true")
    config = _alembic_config()
    command.upgrade(config, "0006")

    user_id = "10000000-0000-0000-0000-000000000001"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, auth_provider, auth_subject, hub_username, display_name, "
                "role, status, environment_generation, created_at, updated_at) "
                "VALUES (:id, 'jupyterhub', 'existing-admin', 'existing-admin', "
                "'Existing admin', 'ADMIN', 'ACTIVE', 1, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            ),
            {"id": user_id},
        )
    engine.dispose()

    command.upgrade(config, "0007")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        policy = connection.execute(
            text(
                "SELECT desired_revision, desired_digest, applied_revision, "
                "applied_digest, apply_status, last_error_code, applied_at "
                "FROM internal_egress_policies WHERE id = 1"
            )
        ).one()
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        preserved_user = connection.execute(
            text("SELECT hub_username FROM users WHERE id = :id"), {"id": user_id}
        ).scalar_one()
    assert tuple(policy) == (
        1,
        "sha256:" + hashlib.sha256(b"").hexdigest(),
        None,
        None,
        "PENDING",
        None,
        None,
    )
    assert revision == "0007"
    assert preserved_user == "existing-admin"

    empty_digest = policy.desired_digest
    with pytest.raises(IntegrityError, match="ck_internal_egress_policy_singleton"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO internal_egress_policies "
                    "(id, desired_revision, desired_digest, apply_status, "
                    "created_at, updated_at) VALUES "
                    "(2, 1, :digest, 'PENDING', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"digest": empty_digest},
            )
    with pytest.raises(
        IntegrityError, match="ck_internal_egress_policy_desired_digest"
    ):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE internal_egress_policies SET desired_digest='sha256:BAD' "
                    "WHERE id=1"
                )
            )
    with pytest.raises(
        IntegrityError, match="ck_internal_egress_policy_applied_digest"
    ):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE internal_egress_policies SET applied_revision=1, "
                    "applied_digest='sha256:BAD', applied_at=CURRENT_TIMESTAMP "
                    "WHERE id=1"
                )
            )
    with pytest.raises(
        IntegrityError, match="ck_internal_egress_policy_applied_binding"
    ):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE internal_egress_policies SET applied_revision=1, "
                    "applied_digest=NULL, applied_at=CURRENT_TIMESTAMP WHERE id=1"
                )
            )
    with pytest.raises(
        IntegrityError, match="ck_internal_egress_policy_applied_time_binding"
    ):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE internal_egress_policies SET applied_revision=1, "
                    "applied_digest=:digest, applied_at=NULL WHERE id=1"
                ),
                {"digest": empty_digest},
            )
    with pytest.raises(IntegrityError, match="ck_internal_egress_policy_applied"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE internal_egress_policies SET apply_status='APPLIED' "
                    "WHERE id=1"
                )
            )
    with pytest.raises(IntegrityError, match="ck_internal_egress_policy_status"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE internal_egress_policies SET apply_status='UNKNOWN' "
                    "WHERE id=1"
                )
            )
    with pytest.raises(IntegrityError, match="ck_internal_egress_policy_error_binding"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE internal_egress_policies SET apply_status='FAILED' "
                    "WHERE id=1"
                )
            )
    with pytest.raises(IntegrityError, match="ck_internal_egress_policy_error_binding"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE internal_egress_policies SET last_error_code='ERROR' "
                    "WHERE id=1"
                )
            )

    body = b"10.255.255.254/32 8000\n"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO internal_egress_rules "
                "(id, destination_cidr, port, row_version, created_by_user_id, "
                "updated_by_user_id, created_at, updated_at) VALUES "
                "(:id, '10.255.255.254/32', 8000, 1, :user_id, :user_id, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "id": "20000000-0000-0000-0000-000000000001",
                "user_id": user_id,
            },
        )
        connection.execute(
            text(
                "UPDATE internal_egress_policies SET desired_revision=2, "
                "desired_digest=:digest, updated_by_user_id=:user_id WHERE id=1"
            ),
            {"digest": digest, "user_id": user_id},
        )
    for port in (80, 65536):
        with pytest.raises(IntegrityError, match="ck_internal_egress_port"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO internal_egress_rules "
                        "(id, destination_cidr, port, row_version, created_at, "
                        "updated_at) VALUES (:id, '10.255.255.253/32', :port, 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"id": f"30000000-0000-0000-0000-{port:012d}", "port": port},
                )
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO internal_egress_rules "
                    "(id, destination_cidr, port, row_version, created_at, updated_at) "
                    "VALUES ('40000000-0000-0000-0000-000000000001', "
                    "'10.255.255.254/32', 8000, 1, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                )
            )
    engine.dispose()

    command.downgrade(config, "0006")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0006"
        )
        assert (
            connection.execute(
                text("SELECT hub_username FROM users WHERE id = :id"), {"id": user_id}
            ).scalar_one()
            == "existing-admin"
        )
        tables = set(inspect(connection).get_table_names())
        assert "internal_egress_policies" not in tables
        assert "internal_egress_rules" not in tables
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()
