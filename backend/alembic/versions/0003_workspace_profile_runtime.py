"""Add kernel and Python metadata to immutable workspace profiles.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing version rows remain usable by already-created workspaces. New policy
    # imports use a new profile version with exact runtime metadata and a new digest.
    op.add_column(
        "workspace_profiles",
        sa.Column(
            "kernel_name",
            sa.String(64),
            nullable=False,
            server_default="python3",
        ),
    )
    op.add_column(
        "workspace_profiles",
        sa.Column(
            "private_disk_quota_enforced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "workspace_profiles",
        sa.Column(
            "selectable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "workspace_profiles",
        sa.Column(
            "python_version",
            sa.String(32),
            nullable=False,
            server_default="legacy",
        ),
    )
    op.add_column(
        "workspace_profiles",
        sa.Column(
            "kernel_display_name",
            sa.String(128),
            nullable=False,
            server_default="Python 3",
        ),
    )


def downgrade() -> None:
    op.drop_column("workspace_profiles", "kernel_display_name")
    op.drop_column("workspace_profiles", "python_version")
    op.drop_column("workspace_profiles", "selectable")
    op.drop_column("workspace_profiles", "private_disk_quota_enforced")
    op.drop_column("workspace_profiles", "kernel_name")
