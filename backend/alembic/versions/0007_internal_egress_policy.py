"""Add durable desired/applied internal egress policy state.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import hashlib
from datetime import datetime

import sqlalchemy as sa
from alembic import op


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


EMPTY_POLICY_DIGEST = "sha256:" + hashlib.sha256(b"").hexdigest()


def upgrade() -> None:
    op.create_table(
        "internal_egress_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("desired_revision", sa.Integer(), nullable=False),
        sa.Column("desired_digest", sa.String(length=71), nullable=False),
        sa.Column("applied_revision", sa.Integer(), nullable=True),
        sa.Column("applied_digest", sa.String(length=71), nullable=True),
        sa.Column("apply_status", sa.String(length=16), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_internal_egress_policy_singleton"),
        sa.CheckConstraint(
            "desired_revision > 0",
            name="ck_internal_egress_policy_desired_revision",
        ),
        sa.CheckConstraint(
            "length(desired_digest) = 71 AND "
            "substr(desired_digest, 1, 7) = 'sha256:' AND "
            "substr(desired_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="ck_internal_egress_policy_desired_digest",
        ),
        sa.CheckConstraint(
            "applied_digest IS NULL OR "
            "(length(applied_digest) = 71 AND "
            "substr(applied_digest, 1, 7) = 'sha256:' AND "
            "substr(applied_digest, 8) NOT GLOB '*[^0-9a-f]*')",
            name="ck_internal_egress_policy_applied_digest",
        ),
        sa.CheckConstraint(
            "(applied_revision IS NULL AND applied_digest IS NULL) OR "
            "(applied_revision IS NOT NULL AND applied_revision > 0 AND "
            "applied_revision <= desired_revision AND applied_digest IS NOT NULL)",
            name="ck_internal_egress_policy_applied_binding",
        ),
        sa.CheckConstraint(
            "(applied_revision IS NULL AND applied_at IS NULL) OR "
            "(applied_revision IS NOT NULL AND applied_at IS NOT NULL)",
            name="ck_internal_egress_policy_applied_time_binding",
        ),
        sa.CheckConstraint(
            "apply_status IN ('PENDING', 'APPLYING', 'APPLIED', 'FAILED')",
            name="ck_internal_egress_policy_status",
        ),
        sa.CheckConstraint(
            "apply_status != 'APPLIED' OR "
            "(applied_revision IS NOT NULL AND applied_digest IS NOT NULL AND "
            "applied_revision = desired_revision AND "
            "applied_digest = desired_digest)",
            name="ck_internal_egress_policy_applied_current",
        ),
        sa.CheckConstraint(
            "(apply_status = 'FAILED' AND last_error_code IS NOT NULL) OR "
            "(apply_status != 'FAILED' AND last_error_code IS NULL)",
            name="ck_internal_egress_policy_error_binding",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR "
            "(length(last_error_code) BETWEEN 1 AND 64 AND "
            "substr(last_error_code, 1, 1) GLOB '[A-Z]' AND "
            "last_error_code NOT GLOB '*[^A-Z0-9_]*')",
            name="ck_internal_egress_policy_error_code",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "internal_egress_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("destination_cidr", sa.String(length=18), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "port BETWEEN 1024 AND 65535", name="ck_internal_egress_port"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_internal_egress_rule_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "destination_cidr",
            "port",
            name="uq_internal_egress_destination_port",
        ),
    )
    policy = sa.table(
        "internal_egress_policies",
        sa.column("id", sa.Integer()),
        sa.column("desired_revision", sa.Integer()),
        sa.column("desired_digest", sa.String()),
        sa.column("applied_revision", sa.Integer()),
        sa.column("applied_digest", sa.String()),
        sa.column("apply_status", sa.String()),
        sa.column("last_error_code", sa.String()),
        sa.column("updated_by_user_id", sa.String()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
        sa.column("applied_at", sa.DateTime()),
    )
    now = datetime.utcnow()
    op.bulk_insert(
        policy,
        [
            {
                "id": 1,
                "desired_revision": 1,
                "desired_digest": EMPTY_POLICY_DIGEST,
                "applied_revision": None,
                "applied_digest": None,
                "apply_status": "PENDING",
                "last_error_code": None,
                "updated_by_user_id": None,
                "created_at": now,
                "updated_at": now,
                "applied_at": None,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("internal_egress_rules")
    op.drop_table("internal_egress_policies")
