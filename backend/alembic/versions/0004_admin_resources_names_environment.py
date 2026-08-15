"""Add admin resource policy, workspace names, deletion jobs and environment data.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


_LEGACY_SPAWN_COLUMNS = (
    "id",
    "ticket_hash",
    "operation_id",
    "attempt_no",
    "workspace_id",
    "owner_user_id",
    "workspace_spec_version",
    "private_volume_slot_id",
    "hub_username",
    "hub_server_name",
    "profile_id",
    "profile_version",
    "profile_config_digest",
    "expires_at",
    "consumed_at",
    "revoked_at",
)


def _backup_spawn_authorizations() -> None:
    columns = ", ".join(_LEGACY_SPAWN_COLUMNS)
    op.execute(
        sa.text(
            "CREATE TEMPORARY TABLE _migration_0004_spawn_backup AS "
            f"SELECT {columns} FROM spawn_authorizations"
        )
    )


def _restore_spawn_authorizations() -> None:
    columns = ", ".join(_LEGACY_SPAWN_COLUMNS)
    # SQLite batch-recreates the operations parent table. With foreign keys on,
    # dropping the old parent cascades into spawn_authorizations. Restore the exact
    # child history only after both parent rebuilds have completed.
    op.execute(sa.text("DELETE FROM spawn_authorizations"))
    op.execute(
        sa.text(
            f"INSERT INTO spawn_authorizations ({columns}) "
            f"SELECT {columns} FROM _migration_0004_spawn_backup"
        )
    )
    op.execute(sa.text("DROP TABLE _migration_0004_spawn_backup"))


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "environment_generation", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("display_name", sa.String(80), nullable=False, server_default="환경"),
    )
    op.add_column(
        "workspaces", sa.Column("profile_offer_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "workspaces", sa.Column("profile_offer_version", sa.Integer(), nullable=True)
    )
    op.add_column(
        "workspaces",
        sa.Column("profile_offer_name_snapshot", sa.String(80), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "environment_generation", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "applied_user_environment_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "applied_workspace_environment_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE workspaces SET applied_user_environment_generation = 1, "
            "applied_workspace_environment_generation = 1"
        )
    )
    # Existing rows already have an exact retained slot. It gives them a stable,
    # collision-free default without relying on timestamp ordering.
    op.execute(
        sa.text(
            "UPDATE workspaces SET display_name = '환경-' || "
            "COALESCE((SELECT slot_no FROM workspace_volume_slots "
            "WHERE workspace_volume_slots.id = workspaces.private_volume_slot_id), 1)"
        )
    )

    _backup_spawn_authorizations()
    with op.batch_alter_table("operations", recreate="always") as batch:
        batch.drop_constraint("uq_operations_user_idempotency", type_="unique")
        batch.add_column(sa.Column("actor_user_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("request_fingerprint", sa.String(64)))
        batch.add_column(sa.Column("lifecycle_checkpoint", sa.String(32)))
        batch.add_column(
            sa.Column(
                "credential_mode",
                sa.String(24),
                nullable=False,
                server_default="USER_DELEGATED",
            )
        )
        batch.create_foreign_key(
            "fk_operations_actor_user",
            "users",
            ["actor_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_operations_actor_idempotency",
            ["actor_user_id", "idempotency_key"],
        )
    op.execute(sa.text("UPDATE operations SET actor_user_id = requested_by_user_id"))
    with op.batch_alter_table("operations", recreate="always") as batch:
        batch.alter_column(
            "actor_user_id",
            existing_type=sa.String(36),
            nullable=False,
        )
    _restore_spawn_authorizations()

    op.add_column(
        "spawn_authorizations",
        sa.Column(
            "user_environment_generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "spawn_authorizations",
        sa.Column(
            "workspace_environment_generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "spawn_authorizations", sa.Column("environment_digest", sa.String(76))
    )
    op.add_column(
        "spawn_authorizations", sa.Column("environment_snapshot_cipher", sa.Text())
    )
    op.execute(
        sa.text(
            "UPDATE spawn_authorizations SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE consumed_at IS NULL AND revoked_at IS NULL"
        )
    )

    op.create_table(
        "workspace_profile_offers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.String(512)),
        sa.Column("runtime_profile_id", sa.String(64), nullable=False),
        sa.Column("runtime_profile_version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("disabled_at", sa.DateTime()),
        sa.CheckConstraint("row_version > 0", name="ck_profile_offer_version"),
        sa.ForeignKeyConstraint(
            ["runtime_profile_id", "runtime_profile_version"],
            ["workspace_profiles.id", "workspace_profiles.version"],
            name="fk_profile_offer_runtime_profile",
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO workspace_profile_offers "
            "(id, row_version, name, description, runtime_profile_id, "
            "runtime_profile_version, enabled, created_by_user_id, created_at, "
            "updated_at, disabled_at) "
            "SELECT p.id, 1, substr(p.name, 1, 80), NULL, p.id, p.version, 1, "
            "NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL "
            "FROM workspace_profiles p WHERE p.enabled = 1 AND p.selectable = 1 "
            "AND p.version = (SELECT max(p2.version) FROM workspace_profiles p2 "
            "WHERE p2.id = p.id AND p2.enabled = 1 AND p2.selectable = 1)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE workspaces SET "
            "profile_offer_id = (SELECT o.id FROM workspace_profile_offers o "
            "WHERE o.runtime_profile_id = workspaces.profile_id AND "
            "o.runtime_profile_version = workspaces.profile_version LIMIT 1), "
            "profile_offer_version = (SELECT o.row_version FROM workspace_profile_offers o "
            "WHERE o.runtime_profile_id = workspaces.profile_id AND "
            "o.runtime_profile_version = workspaces.profile_version LIMIT 1), "
            "profile_offer_name_snapshot = (SELECT o.name FROM workspace_profile_offers o "
            "WHERE o.runtime_profile_id = workspaces.profile_id AND "
            "o.runtime_profile_version = workspaces.profile_version LIMIT 1)"
        )
    )

    op.create_table(
        "resource_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("cpu_budget_millicores", sa.Integer(), nullable=False),
        sa.Column("memory_budget_mb", sa.Integer(), nullable=False),
        sa.Column("selectable_cpu_millicores_json", sa.Text(), nullable=False),
        sa.Column("selectable_memory_mb_json", sa.Text(), nullable=False),
        sa.Column(
            "updated_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_resource_policy_singleton"),
        sa.CheckConstraint("version > 0", name="ck_resource_policy_version"),
        sa.CheckConstraint(
            "cpu_budget_millicores > 0", name="ck_resource_policy_cpu_budget"
        ),
        sa.CheckConstraint(
            "memory_budget_mb > 0", name="ck_resource_policy_memory_budget"
        ),
    )
    op.create_table(
        "environment_variables",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.String(36)),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("is_secret", sa.Boolean(), nullable=False),
        sa.Column("value_cipher", sa.Text()),
        sa.Column("plain_value", sa.Text()),
        sa.Column("value_fingerprint", sa.String(64)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime()),
        sa.CheckConstraint(
            "(scope = 'USER' AND workspace_id IS NULL) OR "
            "(scope = 'WORKSPACE' AND workspace_id IS NOT NULL)",
            name="ck_environment_variable_scope_target",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_environment_variable_version"),
        sa.CheckConstraint(
            "(deleted_at IS NULL AND value_fingerprint IS NOT NULL AND "
            "((is_secret = 1 AND value_cipher IS NOT NULL AND plain_value IS NULL) "
            "OR (is_secret = 0 AND value_cipher IS NULL AND plain_value IS NOT NULL))) "
            "OR (deleted_at IS NOT NULL AND value_cipher IS NULL AND "
            "plain_value IS NULL AND value_fingerprint IS NULL)",
            name="ck_environment_variable_value_storage",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_environment_variable_workspace_owner",
        ),
    )
    op.create_index(
        "uq_active_user_environment_name",
        "environment_variables",
        ["owner_user_id", "name"],
        unique=True,
        sqlite_where=sa.text("scope = 'USER' AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_active_workspace_environment_name",
        "environment_variables",
        ["workspace_id", "name"],
        unique=True,
        sqlite_where=sa.text("scope = 'WORKSPACE' AND deleted_at IS NULL"),
    )
    op.create_table(
        "mutation_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_key", sa.String(255), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_mutation_receipt_actor_idempotency",
        ),
    )
    op.create_table(
        "workspace_deletion_jobs",
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("deletion_id", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey("operations.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("expected_spec_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_summary", sa.String(512)),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("workspace_deletion_jobs")
    op.drop_table("mutation_receipts")
    op.drop_index(
        "uq_active_workspace_environment_name", table_name="environment_variables"
    )
    op.drop_index("uq_active_user_environment_name", table_name="environment_variables")
    op.drop_table("environment_variables")
    op.drop_table("resource_policies")
    op.drop_table("workspace_profile_offers")
    op.drop_column("spawn_authorizations", "environment_snapshot_cipher")
    op.drop_column("spawn_authorizations", "environment_digest")
    op.drop_column("spawn_authorizations", "workspace_environment_generation")
    op.drop_column("spawn_authorizations", "user_environment_generation")
    _backup_spawn_authorizations()
    with op.batch_alter_table("operations", recreate="always") as batch:
        batch.drop_constraint("uq_operations_actor_idempotency", type_="unique")
        batch.drop_constraint("fk_operations_actor_user", type_="foreignkey")
        batch.drop_column("lifecycle_checkpoint")
        batch.drop_column("request_fingerprint")
        batch.drop_column("credential_mode")
        batch.drop_column("actor_user_id")
        batch.create_unique_constraint(
            "uq_operations_user_idempotency",
            ["requested_by_user_id", "idempotency_key"],
        )
    _restore_spawn_authorizations()
    op.drop_column("workspaces", "applied_workspace_environment_generation")
    op.drop_column("workspaces", "applied_user_environment_generation")
    op.drop_column("workspaces", "environment_generation")
    op.drop_column("workspaces", "profile_offer_name_snapshot")
    op.drop_column("workspaces", "profile_offer_version")
    op.drop_column("workspaces", "profile_offer_id")
    op.drop_column("workspaces", "display_name")
    op.drop_column("users", "environment_generation")
