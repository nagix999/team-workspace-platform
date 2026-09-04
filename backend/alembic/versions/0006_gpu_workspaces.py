"""Add fail-closed single-GPU workspace contracts.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _disable_sqlite_foreign_keys() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    # SQLite rejects a batch recreation of workspace_profiles while populated
    # child tables still reference it.  This must be the first statement in the
    # downgrade, before SQLite has opened a write transaction.  Alembic uses a
    # disposable NullPool connection; application connections independently
    # enable foreign_keys again.
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("could not suspend SQLite foreign-key checks")


def _assert_foreign_keys() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("0006 downgrade produced foreign-key violations")


def upgrade() -> None:
    # Additive ALTERs deliberately avoid batch-recreating workspace_profiles and
    # workspaces: both are referenced by durable history tables on SQLite.
    # SQLite cannot add a table CHECK after the fact.  Add the nullable/value
    # columns first, then attach the cross-column contract to the final
    # accelerator_kind column so upgraded databases enforce the same invariant
    # as a fresh metadata-created database.
    op.add_column(
        "workspace_profiles",
        sa.Column("gpu_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "workspace_profiles", sa.Column("cuda_version", sa.String(16), nullable=True)
    )
    op.add_column(
        "workspace_profiles", sa.Column("gpu_framework", sa.String(32), nullable=True)
    )
    op.add_column(
        "workspace_profiles",
        sa.Column("gpu_framework_version", sa.String(32), nullable=True),
    )
    op.add_column(
        "workspace_profiles",
        sa.Column(
            "accelerator_kind",
            sa.String(16),
            sa.CheckConstraint(
                "(accelerator_kind = 'none' AND gpu_count = 0 AND "
                "cuda_version IS NULL AND gpu_framework IS NULL AND "
                "gpu_framework_version IS NULL) OR "
                "(accelerator_kind = 'nvidia' AND gpu_count = 1 AND "
                "cuda_version IS NOT NULL AND gpu_framework = 'pytorch' AND "
                "gpu_framework_version IS NOT NULL)",
                name="ck_profiles_accelerator_contract",
            ),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
    )

    op.add_column(
        "workspaces",
        sa.Column("assigned_gpu_device_id", sa.String(96), nullable=True),
    )
    op.create_index(
        "uq_workspace_assigned_gpu_device",
        "workspaces",
        ["assigned_gpu_device_id"],
        unique=True,
        sqlite_where=sa.text("assigned_gpu_device_id IS NOT NULL"),
    )

    op.add_column(
        "spawn_authorizations",
        sa.Column("gpu_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "spawn_authorizations",
        sa.Column("gpu_device_id", sa.String(96), nullable=True),
    )
    op.add_column(
        "spawn_authorizations",
        sa.Column(
            "gpu_inventory_digest",
            sa.String(71),
            sa.CheckConstraint(
                "(gpu_count = 0 AND gpu_device_id IS NULL AND "
                "gpu_inventory_digest IS NULL) OR "
                "(gpu_count = 1 AND gpu_device_id IS NOT NULL AND "
                "gpu_inventory_digest IS NOT NULL)",
                name="ck_spawn_auth_gpu_contract",
            ),
            nullable=True,
        ),
    )

    op.add_column(
        "resource_policies",
        sa.Column(
            "gpu_budget_count",
            sa.Integer(),
            sa.CheckConstraint(
                "gpu_budget_count BETWEEN 0 AND 1",
                name="ck_resource_policy_gpu_budget",
            ),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "resource_policies",
        sa.Column(
            "selectable_gpu_counts_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[0]'"),
        ),
    )


def downgrade() -> None:
    _disable_sqlite_foreign_keys()
    # Later migrations may have batch-recreated these tables, which turns the
    # originally column-attached SQLite CHECKs into table-level constraints.
    # Recreate the tables while removing the CHECK and its columns together so
    # SQLite never observes a constraint that references an already-dropped
    # column.
    with op.batch_alter_table("resource_policies", recreate="always") as batch:
        batch.drop_constraint("ck_resource_policy_gpu_budget", type_="check")
        batch.drop_column("selectable_gpu_counts_json")
        batch.drop_column("gpu_budget_count")

    with op.batch_alter_table("spawn_authorizations", recreate="always") as batch:
        batch.drop_constraint("ck_spawn_auth_gpu_contract", type_="check")
        batch.drop_column("gpu_inventory_digest")
        batch.drop_column("gpu_device_id")
        batch.drop_column("gpu_count")

    op.drop_index("uq_workspace_assigned_gpu_device", table_name="workspaces")
    op.drop_column("workspaces", "assigned_gpu_device_id")

    with op.batch_alter_table("workspace_profiles", recreate="always") as batch:
        batch.drop_constraint("ck_profiles_accelerator_contract", type_="check")
        batch.drop_column("accelerator_kind")
        batch.drop_column("gpu_framework_version")
        batch.drop_column("gpu_framework")
        batch.drop_column("cuda_version")
        batch.drop_column("gpu_count")
    _assert_foreign_keys()
