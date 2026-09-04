"""Cache observed workspace CPU and memory usage.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "cpu_usage_millicores",
            sa.Integer(),
            sa.CheckConstraint(
                "cpu_usage_millicores IS NULL OR cpu_usage_millicores >= 0",
                name="ck_workspaces_cpu_usage_nonnegative",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "memory_usage_bytes",
            sa.BigInteger(),
            sa.CheckConstraint(
                "memory_usage_bytes IS NULL OR memory_usage_bytes >= 0",
                name="ck_workspaces_memory_usage_nonnegative",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "memory_limit_bytes",
            sa.BigInteger(),
            sa.CheckConstraint(
                "memory_limit_bytes IS NULL OR memory_limit_bytes > 0",
                name="ck_workspaces_memory_limit_positive",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "resource_usage_observed_at",
            sa.DateTime(),
            sa.CheckConstraint(
                "(cpu_usage_millicores IS NULL AND memory_usage_bytes IS NULL AND "
                "memory_limit_bytes IS NULL AND resource_usage_observed_at IS NULL) OR "
                "(cpu_usage_millicores IS NOT NULL AND memory_usage_bytes IS NOT NULL "
                "AND memory_limit_bytes IS NOT NULL AND "
                "resource_usage_observed_at IS NOT NULL AND "
                "memory_usage_bytes <= memory_limit_bytes)",
                name="ck_workspaces_resource_usage_complete",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "resource_usage_observed_at")
    op.drop_column("workspaces", "memory_limit_bytes")
    op.drop_column("workspaces", "memory_usage_bytes")
    op.drop_column("workspaces", "cpu_usage_millicores")
