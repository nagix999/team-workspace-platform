from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "auth_provider", "auth_subject", name="uq_users_auth_identity"
        ),
        UniqueConstraint("id", "hub_username", name="uq_users_id_hub_username"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    auth_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="jupyterhub"
    )
    auth_subject: Mapped[str] = mapped_column(String(64), nullable=False)
    hub_username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="PROVISIONING"
    )
    environment_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class UserProvisioningJob(Base):
    __tablename__ = "user_provisioning_jobs"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(String(512))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    hub_oauth_token_cipher: Mapped[str | None] = mapped_column(Text)
    hub_scopes_json: Mapped[str] = mapped_column(Text, nullable=False)
    hub_oauth_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    last_seen_bucket: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuthTransaction(Base):
    __tablename__ = "auth_transactions"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    pkce_verifier_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_path: Mapped[str] = mapped_column(String(512), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)


class WorkspaceProfile(Base):
    __tablename__ = "workspace_profiles"
    __table_args__ = (
        UniqueConstraint(
            "id", "version", "config_digest", name="uq_profiles_digest_binding"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kernel_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="python3"
    )
    kernel_display_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="Python 3"
    )
    python_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy"
    )
    image_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    cpu_limit: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    pids_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    private_disk_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    private_disk_quota_enforced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    idle_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    provider_options_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    config_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    selectable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WorkspaceProfileOffer(Base):
    __tablename__ = "workspace_profile_offers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["runtime_profile_id", "runtime_profile_version"],
            ["workspace_profiles.id", "workspace_profiles.version"],
            name="fk_profile_offer_runtime_profile",
        ),
        CheckConstraint("row_version > 0", name="ck_profile_offer_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    runtime_profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)


class WorkspaceVolumeSlot(Base):
    __tablename__ = "workspace_volume_slots"
    __table_args__ = (
        CheckConstraint("slot_no BETWEEN 1 AND 5", name="ck_volume_slots_slot_no"),
        UniqueConstraint("owner_user_id", "slot_no", name="uq_volume_slots_owner_slot"),
        UniqueConstraint("id", "owner_user_id", name="uq_volume_slots_id_owner"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    slot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    volume_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    quota_project_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    hard_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    provision_status: Mapped[str] = mapped_column(String(24), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "hub_server_name", name="uq_workspaces_owner_server"
        ),
        UniqueConstraint("id", "owner_user_id", name="uq_workspaces_id_owner"),
        ForeignKeyConstraint(
            ["profile_id", "profile_version"],
            ["workspace_profiles.id", "workspace_profiles.version"],
            name="fk_workspaces_profile",
        ),
        ForeignKeyConstraint(
            ["private_volume_slot_id", "owner_user_id"],
            ["workspace_volume_slots.id", "workspace_volume_slots.owner_user_id"],
            name="fk_workspaces_volume_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_offer_id: Mapped[str | None] = mapped_column(String(64))
    profile_offer_version: Mapped[int | None] = mapped_column(Integer)
    profile_offer_name_snapshot: Mapped[str | None] = mapped_column(String(80))
    hub_target_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    hub_server_name: Mapped[str] = mapped_column(String(64), nullable=False)
    private_volume_slot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    desired_state: Mapped[str] = mapped_column(String(24), nullable=False)
    observed_state: Mapped[str] = mapped_column(String(24), nullable=False)
    hub_server_url: Mapped[str | None] = mapped_column(Text)
    progress_percent: Mapped[int | None] = mapped_column(Integer)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_summary: Mapped[str | None] = mapped_column(String(512))
    hub_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    hub_last_activity_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime)
    deletion_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    deletion_checkpoint: Mapped[str | None] = mapped_column(String(64))
    environment_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    applied_user_environment_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    applied_workspace_environment_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)


Index(
    "uq_active_workspace_volume_slot",
    Workspace.private_volume_slot_id,
    unique=True,
    sqlite_where=Workspace.archived_at.is_(None),
)


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_operations_actor_idempotency",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "requested_by_user_id"],
            ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_operations_workspace_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    auth_session_id_hash: Mapped[str | None] = mapped_column(
        ForeignKey("user_sessions.id_hash", ondelete="SET NULL")
    )
    credential_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="USER_DELEGATED"
    )
    operation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    lifecycle_checkpoint: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transient_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(String(512))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )


class SpawnAuthorization(Base):
    __tablename__ = "spawn_authorizations"
    __table_args__ = (
        UniqueConstraint(
            "operation_id", "attempt_no", name="uq_spawn_auth_operation_attempt"
        ),
        ForeignKeyConstraint(
            ["profile_id", "profile_version", "profile_config_digest"],
            [
                "workspace_profiles.id",
                "workspace_profiles.version",
                "workspace_profiles.config_digest",
            ],
            name="fk_spawn_auth_profile_digest",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_spawn_auth_workspace_owner",
        ),
        ForeignKeyConstraint(
            ["private_volume_slot_id", "owner_user_id"],
            ["workspace_volume_slots.id", "workspace_volume_slots.owner_user_id"],
            name="fk_spawn_auth_volume_owner",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "hub_username"],
            ["users.id", "users.hub_username"],
            name="fk_spawn_auth_user_name",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticket_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workspace_spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    private_volume_slot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    hub_username: Mapped[str] = mapped_column(String(64), nullable=False)
    hub_server_name: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_config_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    user_environment_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    workspace_environment_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    environment_digest: Mapped[str | None] = mapped_column(String(76))
    environment_snapshot_cipher: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class ResourcePolicy(Base):
    __tablename__ = "resource_policies"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_resource_policy_singleton"),
        CheckConstraint("version > 0", name="ck_resource_policy_version"),
        CheckConstraint(
            "cpu_budget_millicores > 0", name="ck_resource_policy_cpu_budget"
        ),
        CheckConstraint(
            "memory_budget_mb > 0", name="ck_resource_policy_memory_budget"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cpu_budget_millicores: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_budget_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    selectable_cpu_millicores_json: Mapped[str] = mapped_column(Text, nullable=False)
    selectable_memory_mb_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class EnvironmentVariable(Base):
    __tablename__ = "environment_variables"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'USER' AND workspace_id IS NULL) OR "
            "(scope = 'WORKSPACE' AND workspace_id IS NOT NULL)",
            name="ck_environment_variable_scope_target",
        ),
        CheckConstraint("row_version > 0", name="ck_environment_variable_version"),
        CheckConstraint(
            "(deleted_at IS NULL AND value_fingerprint IS NOT NULL AND "
            "((is_secret = 1 AND value_cipher IS NOT NULL AND plain_value IS NULL) "
            "OR (is_secret = 0 AND value_cipher IS NULL AND plain_value IS NOT NULL))) "
            "OR (deleted_at IS NOT NULL AND value_cipher IS NULL AND "
            "plain_value IS NULL AND value_fingerprint IS NULL)",
            name="ck_environment_variable_value_storage",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_environment_variable_workspace_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    workspace_id: Mapped[str | None] = mapped_column(String(36))
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False)
    value_cipher: Mapped[str | None] = mapped_column(Text)
    plain_value: Mapped[str | None] = mapped_column(Text)
    value_fingerprint: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


Index(
    "uq_active_user_environment_name",
    EnvironmentVariable.owner_user_id,
    EnvironmentVariable.name,
    unique=True,
    sqlite_where=(
        (EnvironmentVariable.scope == "USER") & EnvironmentVariable.deleted_at.is_(None)
    ),
)
Index(
    "uq_active_workspace_environment_name",
    EnvironmentVariable.workspace_id,
    EnvironmentVariable.name,
    unique=True,
    sqlite_where=(
        (EnvironmentVariable.scope == "WORKSPACE")
        & EnvironmentVariable.deleted_at.is_(None)
    ),
)


class MutationReceipt(Base):
    __tablename__ = "mutation_receipts"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_mutation_receipt_actor_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )


class WorkspaceDeletionJob(Base):
    __tablename__ = "workspace_deletion_jobs"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), primary_key=True
    )
    deletion_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    expected_spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(String(512))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class InternalRequestNonce(Base):
    __tablename__ = "internal_request_nonces"

    nonce: Mapped[str] = mapped_column(String(128), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
