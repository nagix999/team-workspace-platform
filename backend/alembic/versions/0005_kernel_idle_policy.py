"""Add managed idle-kernel policy and spawn authorization snapshot.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


_KERNEL_IDLE_CHECK = (
    "kernel_idle_timeout_seconds = 0 OR "
    "(kernel_idle_timeout_seconds BETWEEN 300 AND 604800 AND "
    "kernel_idle_timeout_seconds % 60 = 0)"
)


def upgrade() -> None:
    with op.batch_alter_table("resource_policies", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "kernel_idle_timeout_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("3600"),
            )
        )
        batch.create_check_constraint(
            "ck_resource_policy_kernel_idle_timeout", _KERNEL_IDLE_CHECK
        )

    with op.batch_alter_table("spawn_authorizations", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "kernel_idle_timeout_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("3600"),
            )
        )
        batch.create_check_constraint(
            "ck_spawn_auth_kernel_idle_timeout", _KERNEL_IDLE_CHECK
        )


def downgrade() -> None:
    with op.batch_alter_table("spawn_authorizations", recreate="always") as batch:
        batch.drop_constraint("ck_spawn_auth_kernel_idle_timeout", type_="check")
        batch.drop_column("kernel_idle_timeout_seconds")

    with op.batch_alter_table("resource_policies", recreate="always") as batch:
        batch.drop_constraint("ck_resource_policy_kernel_idle_timeout", type_="check")
        batch.drop_column("kernel_idle_timeout_seconds")
