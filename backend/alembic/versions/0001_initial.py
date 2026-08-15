"""Initial platform control-plane schema.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("auth_provider", sa.String(32), nullable=False),
        sa.Column("auth_subject", sa.String(64), nullable=False),
        sa.Column("hub_username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128)),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "auth_provider", "auth_subject", name="uq_users_auth_identity"
        ),
        sa.UniqueConstraint("hub_username"),
        sa.UniqueConstraint("id", "hub_username", name="uq_users_id_hub_username"),
    )
    op.create_table(
        "auth_transactions",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("pkce_verifier_cipher", sa.Text(), nullable=False),
        sa.Column("redirect_path", sa.String(512), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
    )
    op.create_table(
        "workspace_profiles",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("image_ref", sa.String(512), nullable=False),
        sa.Column("cpu_limit", sa.String(32), nullable=False),
        sa.Column("memory_limit_mb", sa.Integer(), nullable=False),
        sa.Column("pids_limit", sa.Integer(), nullable=False),
        sa.Column("private_disk_limit_mb", sa.Integer(), nullable=False),
        sa.Column("idle_timeout_seconds", sa.Integer()),
        sa.Column("provider_options_json", sa.Text(), nullable=False),
        sa.Column("config_digest", sa.String(71), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "id", "version", "config_digest", name="uq_profiles_digest_binding"
        ),
    )
    op.create_table(
        "user_sessions",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("hub_oauth_token_cipher", sa.Text()),
        sa.Column("hub_scopes_json", sa.Text(), nullable=False),
        sa.Column("hub_oauth_expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_bucket", sa.DateTime(), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime()),
    )
    op.create_table(
        "workspace_volume_slots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("volume_name", sa.String(255), nullable=False, unique=True),
        sa.Column("quota_project_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("hard_limit_mb", sa.Integer(), nullable=False),
        sa.Column("provision_status", sa.String(24), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("slot_no BETWEEN 1 AND 5", name="ck_volume_slots_slot_no"),
        sa.UniqueConstraint(
            "owner_user_id", "slot_no", name="uq_volume_slots_owner_slot"
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_volume_slots_id_owner"),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("hub_target_key", sa.String(255), nullable=False, unique=True),
        sa.Column("hub_server_name", sa.String(64), nullable=False),
        sa.Column("private_volume_slot_id", sa.String(36), nullable=False),
        sa.Column("desired_state", sa.String(24), nullable=False),
        sa.Column("observed_state", sa.String(24), nullable=False),
        sa.Column("hub_server_url", sa.Text()),
        sa.Column("progress_percent", sa.Integer()),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_error_summary", sa.String(512)),
        sa.Column("hub_started_at", sa.DateTime()),
        sa.Column("hub_last_activity_at", sa.DateTime()),
        sa.Column("last_reconciled_at", sa.DateTime()),
        sa.Column("deletion_started_at", sa.DateTime()),
        sa.Column("deletion_checkpoint", sa.String(64)),
        sa.Column("spec_version", sa.Integer(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime()),
        sa.ForeignKeyConstraint(
            ["profile_id", "profile_version"],
            ["workspace_profiles.id", "workspace_profiles.version"],
            name="fk_workspaces_profile",
        ),
        sa.ForeignKeyConstraint(
            ["private_volume_slot_id", "owner_user_id"],
            ["workspace_volume_slots.id", "workspace_volume_slots.owner_user_id"],
            name="fk_workspaces_volume_owner",
        ),
        sa.UniqueConstraint(
            "owner_user_id", "hub_server_name", name="uq_workspaces_owner_server"
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_workspaces_id_owner"),
    )
    op.create_index(
        "uq_active_workspace_volume_slot",
        "workspaces",
        ["private_volume_slot_id"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requested_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "auth_session_id_hash",
            sa.String(64),
            sa.ForeignKey("user_sessions.id_hash", ondelete="SET NULL"),
        ),
        sa.Column("operation_type", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "transient_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_summary", sa.String(512)),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("next_attempt_at", sa.DateTime()),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.ForeignKeyConstraint(
            ["workspace_id", "requested_by_user_id"],
            ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_operations_workspace_owner",
        ),
        sa.UniqueConstraint(
            "requested_by_user_id",
            "idempotency_key",
            name="uq_operations_user_idempotency",
        ),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="SET NULL"),
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("safe_metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spawn_authorizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticket_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey("operations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("workspace_spec_version", sa.Integer(), nullable=False),
        sa.Column("private_volume_slot_id", sa.String(36), nullable=False),
        sa.Column("hub_username", sa.String(64), nullable=False),
        sa.Column("hub_server_name", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("profile_config_digest", sa.String(71), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
        sa.ForeignKeyConstraint(
            ["profile_id", "profile_version", "profile_config_digest"],
            [
                "workspace_profiles.id",
                "workspace_profiles.version",
                "workspace_profiles.config_digest",
            ],
            name="fk_spawn_auth_profile_digest",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_spawn_auth_workspace_owner",
        ),
        sa.ForeignKeyConstraint(
            ["private_volume_slot_id", "owner_user_id"],
            ["workspace_volume_slots.id", "workspace_volume_slots.owner_user_id"],
            name="fk_spawn_auth_volume_owner",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "hub_username"],
            ["users.id", "users.hub_username"],
            name="fk_spawn_auth_user_name",
        ),
        sa.UniqueConstraint(
            "operation_id", "attempt_no", name="uq_spawn_auth_operation_attempt"
        ),
    )
    op.create_table(
        "internal_request_nonces",
        sa.Column("nonce", sa.String(128), primary_key=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_internal_request_nonces_expires_at",
        "internal_request_nonces",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_internal_request_nonces_expires_at", table_name="internal_request_nonces"
    )
    op.drop_table("internal_request_nonces")
    op.drop_table("spawn_authorizations")
    op.drop_table("audit_events")
    op.drop_table("operations")
    op.drop_index("uq_active_workspace_volume_slot", table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_table("workspace_volume_slots")
    op.drop_table("user_sessions")
    op.drop_table("workspace_profiles")
    op.drop_table("auth_transactions")
    op.drop_table("users")
