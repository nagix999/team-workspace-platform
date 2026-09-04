from __future__ import annotations

from datetime import datetime, timedelta

from .models import (
    AuditEvent,
    Operation,
    User,
    Workspace,
    WorkspaceDeletionJob,
    WorkspaceProfile,
)


def iso(value: datetime | None) -> str | None:
    return f"{value.isoformat()}Z" if value else None


def user_dict(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "username": user.hub_username,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
    }


def profile_dict(profile: WorkspaceProfile) -> dict[str, object]:
    # image/provider options are intentionally not public API fields.
    # A configured slot size is only a real user-facing limit when the quota
    # enforcement fact is true.  Keep the non-null database value for legacy
    # profile/slot compatibility, but never advertise an unenforced number as a
    # limit.
    effective_disk_limit_mb = (
        profile.private_disk_limit_mb if profile.private_disk_quota_enforced else None
    )
    return {
        "id": profile.id,
        "version": profile.version,
        "name": profile.name,
        "kernel_name": profile.kernel_name,
        "kernel_display_name": profile.kernel_display_name,
        "python_version": profile.python_version,
        "accelerator_kind": profile.accelerator_kind,
        "gpu_count": profile.gpu_count,
        "cuda_version": profile.cuda_version,
        "gpu_framework": profile.gpu_framework,
        "gpu_framework_version": profile.gpu_framework_version,
        "cpu_limit": profile.cpu_limit,
        "memory_limit_mb": profile.memory_limit_mb,
        "pids_limit": profile.pids_limit,
        "private_disk_limit_mb": effective_disk_limit_mb,
        "private_disk_quota_enforced": profile.private_disk_quota_enforced,
    }


def workspace_dict(
    workspace: Workspace,
    profile: WorkspaceProfile | None = None,
    owner: User | None = None,
    deletion_job: WorkspaceDeletionJob | None = None,
    latest_delete_operation: Operation | None = None,
    active_operation: Operation | None = None,
    freshness_cutoff: datetime | None = None,
    resource_usage_ttl_seconds: int | None = None,
) -> dict[str, object]:
    effective_stale = workspace.stale or bool(
        freshness_cutoff is not None
        and (
            workspace.last_reconciled_at is None
            or workspace.last_reconciled_at < freshness_cutoff
        )
    )
    launch_url = None
    if (
        workspace.observed_state == "RUNNING"
        and not effective_stale
        and workspace.archived_at is None
        and workspace.deletion_started_at is None
    ):
        launch_url = f"/api/v1/workspaces/{workspace.id}/launch"
    usage_values = (
        workspace.cpu_usage_millicores,
        workspace.memory_usage_bytes,
        workspace.memory_limit_bytes,
        workspace.resource_usage_observed_at,
    )
    resource_usage: dict[str, object] | None = None
    if (
        workspace.observed_state == "RUNNING"
        and all(value is not None for value in usage_values)
    ):
        usage_expires_at: datetime | None = None
        if resource_usage_ttl_seconds is not None:
            # The measurement is only current while both the metrics sample and
            # its containing lifecycle observation are current. Hub lifecycle
            # collection precedes Docker stats collection, so extending this
            # deadline from the newer metrics timestamp can briefly present a
            # sample as live after the workspace itself has become stale.
            usage_freshness_sources = [workspace.resource_usage_observed_at]
            if workspace.last_reconciled_at is not None:
                usage_freshness_sources.append(workspace.last_reconciled_at)
            usage_expires_at = min(usage_freshness_sources) + timedelta(
                seconds=resource_usage_ttl_seconds
            )
        usage_stale = effective_stale or bool(
            freshness_cutoff is not None
            and workspace.resource_usage_observed_at < freshness_cutoff
        )
        resource_usage = {
            "cpu_millicores": workspace.cpu_usage_millicores,
            "memory_bytes": workspace.memory_usage_bytes,
            "memory_limit_bytes": workspace.memory_limit_bytes,
            "observed_at": iso(workspace.resource_usage_observed_at),
            "expires_at": iso(usage_expires_at),
            "stale": usage_stale,
        }
    value: dict[str, object] = {
        "id": workspace.id,
        "name": workspace.display_name,
        "profile_id": workspace.profile_offer_id or workspace.profile_id,
        "profile_version": workspace.profile_offer_version or workspace.profile_version,
        "desired_state": workspace.desired_state,
        "observed_state": workspace.observed_state,
        "progress_percent": workspace.progress_percent,
        "launch_url": launch_url,
        "stale": effective_stale,
        "resource_usage": resource_usage,
        "last_error_code": workspace.last_error_code,
        "last_error_summary": workspace.last_error_summary,
        "restart_required": (
            bool(
                workspace.observed_state == "RUNNING"
                and (
                    workspace.applied_user_environment_generation
                    != owner.environment_generation
                    or workspace.applied_workspace_environment_generation
                    != workspace.environment_generation
                )
            )
            if owner is not None
            else False
        ),
        "deletion_checkpoint": workspace.deletion_checkpoint,
        "deletion_status": (
            deletion_job.status
            if deletion_job is not None
            else (
                "FAILED"
                if latest_delete_operation is not None
                and latest_delete_operation.status in {"FAILED", "AUTH_REQUIRED"}
                else None
            )
        ),
        "can_retry_delete": bool(
            workspace.deletion_started_at is not None
            and (
                (deletion_job is not None and deletion_job.status == "FAILED")
                or (
                    latest_delete_operation is not None
                    and latest_delete_operation.status in {"FAILED", "AUTH_REQUIRED"}
                )
            )
        ),
        "active_operation": (
            {
                "id": active_operation.id,
                "operation_type": active_operation.operation_type,
                "status": active_operation.status,
                "progress_percent": workspace.progress_percent,
                "requested_at": iso(active_operation.requested_at),
            }
            if active_operation is not None
            else None
        ),
        "created_at": iso(workspace.created_at),
        "updated_at": iso(workspace.updated_at),
    }
    if profile is not None:
        # This is read from the workspace's pinned (id, version) FK row, not from the
        # current catalog version. It therefore remains a reproducible display
        # snapshot when a newer profile version is published.
        effective_disk_limit_mb = (
            profile.private_disk_limit_mb
            if profile.private_disk_quota_enforced
            else None
        )
        value.update(
            {
                "profile_name": workspace.profile_offer_name_snapshot or profile.name,
                "kernel_name": profile.kernel_name,
                "kernel_display_name": profile.kernel_display_name,
                "python_version": profile.python_version,
                "accelerator_kind": profile.accelerator_kind,
                "gpu_count": profile.gpu_count,
                "cuda_version": profile.cuda_version,
                "gpu_framework": profile.gpu_framework,
                "gpu_framework_version": profile.gpu_framework_version,
                "cpu_limit": profile.cpu_limit,
                "memory_limit_mb": profile.memory_limit_mb,
                "private_disk_limit_mb": effective_disk_limit_mb,
                "private_disk_quota_enforced": profile.private_disk_quota_enforced,
            }
        )
    return value


def operation_dict(operation: Operation) -> dict[str, object]:
    return {
        "id": operation.id,
        "workspace_id": operation.workspace_id,
        "operation_type": operation.operation_type,
        "actor_user_id": operation.actor_user_id,
        "status": operation.status,
        "attempts": operation.attempts,
        "error_code": operation.error_code,
        "error_summary": operation.error_summary,
        "requested_at": iso(operation.requested_at),
        "started_at": iso(operation.started_at),
        "completed_at": iso(operation.completed_at),
    }


def audit_dict(event: AuditEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "actor_user_id": event.actor_user_id,
        "workspace_id": event.workspace_id,
        "action": event.action,
        "result": event.result,
        "request_id": event.request_id,
        "safe_metadata_json": event.safe_metadata_json,
        "created_at": iso(event.created_at),
    }
