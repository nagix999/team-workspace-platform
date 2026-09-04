"""Add multi-GPU profiles, assignments and exclusive device leases.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


_PROFILE_GPU_CHECK = (
    "(accelerator_kind = 'none' AND gpu_count = 0 AND "
    "cuda_version IS NULL AND gpu_framework IS NULL AND "
    "gpu_framework_version IS NULL) OR "
    "(accelerator_kind = 'nvidia' AND gpu_count BETWEEN 1 AND 64 AND "
    "cuda_version IS NOT NULL AND gpu_framework = 'pytorch' AND "
    "gpu_framework_version IS NOT NULL)"
)
_LEGACY_PROFILE_GPU_CHECK = _PROFILE_GPU_CHECK.replace(
    "gpu_count BETWEEN 1 AND 64", "gpu_count = 1"
)
_SPAWN_GPU_CHECK = (
    "(gpu_count = 0 AND gpu_device_id IS NULL AND "
    "gpu_device_ids_json IS NULL AND gpu_inventory_digest IS NULL) OR "
    "(gpu_count BETWEEN 1 AND 64 AND gpu_device_id IS NOT NULL AND "
    "gpu_device_ids_json IS NOT NULL AND gpu_inventory_digest IS NOT NULL)"
)
_LEGACY_SPAWN_GPU_CHECK = (
    "(gpu_count = 0 AND gpu_device_id IS NULL AND "
    "gpu_inventory_digest IS NULL) OR "
    "(gpu_count = 1 AND gpu_device_id IS NOT NULL AND "
    "gpu_inventory_digest IS NOT NULL)"
)
_GPU_UUID_RE = re.compile(
    r"^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _disable_sqlite_foreign_keys() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    # SQLite refuses to drop/recreate a referenced parent while FK enforcement is
    # active, and defer_foreign_keys does not defer DROP TABLE. This is the first
    # statement in the migration, before SQLite has opened a write transaction.
    # The migration connection is discarded afterward; every application
    # connection independently enables foreign_keys and we prove the graph below.
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("could not suspend SQLite foreign-key checks")


def _assert_foreign_keys() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("0008 migration produced foreign-key violations")


def _backfill_assignment_json(
    *, table: str, identity_column: str, scalar_column: str, json_column: str
) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"SELECT {identity_column}, {scalar_column} FROM {table} "
            f"WHERE {scalar_column} IS NOT NULL"
        )
    ).all()
    for identity, device_id in rows:
        if not isinstance(device_id, str) or _GPU_UUID_RE.fullmatch(device_id) is None:
            raise RuntimeError(
                f"cannot migrate non-canonical GPU UUID in {table}.{scalar_column}"
            )
        bind.execute(
            sa.text(
                f"UPDATE {table} SET {json_column} = :assignment "
                f"WHERE {identity_column} = :identity"
            ),
            {
                "assignment": json.dumps([device_id], separators=(",", ":")),
                "identity": identity,
            },
        )


def _validate_legacy_assignments() -> None:
    bind = op.get_bind()
    for table, column in (
        ("workspaces", "assigned_gpu_device_id"),
        ("spawn_authorizations", "gpu_device_id"),
    ):
        values = bind.execute(
            sa.text(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")
        ).scalars()
        for device_id in values:
            if (
                not isinstance(device_id, str)
                or _GPU_UUID_RE.fullmatch(device_id) is None
            ):
                raise RuntimeError(
                    f"cannot migrate non-canonical GPU UUID in {table}.{column}"
                )


def upgrade() -> None:
    # Validate all legacy data before the first non-transactional SQLite DDL.
    _validate_legacy_assignments()
    _disable_sqlite_foreign_keys()
    with op.batch_alter_table("workspace_profiles", recreate="always") as batch:
        batch.drop_constraint("ck_profiles_accelerator_contract", type_="check")
        batch.create_check_constraint(
            "ck_profiles_accelerator_contract", _PROFILE_GPU_CHECK
        )

    op.add_column(
        "workspaces", sa.Column("assigned_gpu_device_ids_json", sa.Text(), nullable=True)
    )
    _backfill_assignment_json(
        table="workspaces",
        identity_column="id",
        scalar_column="assigned_gpu_device_id",
        json_column="assigned_gpu_device_ids_json",
    )
    op.create_table(
        "workspace_gpu_leases",
        sa.Column("gpu_device_id", sa.String(96), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("gpu_device_id"),
        sa.UniqueConstraint(
            "workspace_id", "gpu_device_id", name="uq_workspace_gpu_lease_binding"
        ),
    )
    op.create_index(
        "ix_workspace_gpu_leases_workspace_id",
        "workspace_gpu_leases",
        ["workspace_id"],
    )
    op.execute(
        sa.text(
            "INSERT INTO workspace_gpu_leases "
            "(gpu_device_id, workspace_id, created_at) "
            "SELECT assigned_gpu_device_id, id, CURRENT_TIMESTAMP FROM workspaces "
            "WHERE assigned_gpu_device_id IS NOT NULL"
        )
    )

    op.add_column(
        "spawn_authorizations",
        sa.Column("gpu_device_ids_json", sa.Text(), nullable=True),
    )
    _backfill_assignment_json(
        table="spawn_authorizations",
        identity_column="id",
        scalar_column="gpu_device_id",
        json_column="gpu_device_ids_json",
    )
    with op.batch_alter_table("spawn_authorizations", recreate="always") as batch:
        batch.drop_constraint("ck_spawn_auth_gpu_contract", type_="check")
        batch.create_check_constraint("ck_spawn_auth_gpu_contract", _SPAWN_GPU_CHECK)

    with op.batch_alter_table("resource_policies", recreate="always") as batch:
        batch.drop_constraint("ck_resource_policy_gpu_budget", type_="check")
        batch.create_check_constraint(
            "ck_resource_policy_gpu_budget", "gpu_budget_count BETWEEN 0 AND 64"
        )
    _assert_foreign_keys()


def downgrade() -> None:
    _disable_sqlite_foreign_keys()
    bind = op.get_bind()
    unsafe = bind.execute(
        sa.text(
            "SELECT "
            "EXISTS(SELECT 1 FROM workspace_profiles WHERE gpu_count > 1) OR "
            "EXISTS(SELECT 1 FROM spawn_authorizations WHERE gpu_count > 1) OR "
            "EXISTS(SELECT 1 FROM resource_policies WHERE gpu_budget_count > 1)"
        )
    ).scalar_one()
    if unsafe:
        raise RuntimeError(
            "cannot downgrade 0008 while multi-GPU profiles or policy are present"
        )

    op.drop_index(
        "ix_workspace_gpu_leases_workspace_id", table_name="workspace_gpu_leases"
    )
    op.drop_table("workspace_gpu_leases")

    with op.batch_alter_table("spawn_authorizations", recreate="always") as batch:
        batch.drop_constraint("ck_spawn_auth_gpu_contract", type_="check")
        batch.drop_column("gpu_device_ids_json")
        batch.create_check_constraint(
            "ck_spawn_auth_gpu_contract", _LEGACY_SPAWN_GPU_CHECK
        )

    with op.batch_alter_table("resource_policies", recreate="always") as batch:
        batch.drop_constraint("ck_resource_policy_gpu_budget", type_="check")
        batch.create_check_constraint(
            "ck_resource_policy_gpu_budget", "gpu_budget_count BETWEEN 0 AND 1"
        )

    op.drop_column("workspaces", "assigned_gpu_device_ids_json")
    with op.batch_alter_table("workspace_profiles", recreate="always") as batch:
        batch.drop_constraint("ck_profiles_accelerator_contract", type_="check")
        batch.create_check_constraint(
            "ck_profiles_accelerator_contract", _LEGACY_PROFILE_GPU_CHECK
        )
    _assert_foreign_keys()
