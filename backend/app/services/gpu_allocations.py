from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..accelerators import gpu_device_ids_json, parse_gpu_device_ids_json
from ..models import SpawnAuthorization, Workspace, WorkspaceGpuLease


def workspace_gpu_device_ids(db: Session, workspace: Workspace) -> tuple[str, ...]:
    """Return a workspace's exact assignment after proving its durable leases."""

    device_ids = parse_gpu_device_ids_json(workspace.assigned_gpu_device_ids_json)
    expected_scalar = device_ids[0] if device_ids else None
    if workspace.assigned_gpu_device_id != expected_scalar:
        raise ValueError("workspace GPU assignment mirror does not match")
    lease_ids = tuple(
        db.scalars(
            select(WorkspaceGpuLease.gpu_device_id)
            .where(WorkspaceGpuLease.workspace_id == workspace.id)
            .order_by(WorkspaceGpuLease.gpu_device_id)
        ).all()
    )
    if lease_ids != device_ids:
        raise ValueError("workspace GPU lease set does not match")
    return device_ids


def spawn_authorization_gpu_device_ids(
    authorization: SpawnAuthorization,
) -> tuple[str, ...]:
    device_ids = parse_gpu_device_ids_json(authorization.gpu_device_ids_json)
    expected_scalar = device_ids[0] if device_ids else None
    if authorization.gpu_device_id != expected_scalar:
        raise ValueError("spawn GPU assignment mirror does not match")
    if authorization.gpu_count != len(device_ids):
        raise ValueError("spawn GPU count does not match assignment")
    return device_ids


def assign_workspace_gpu_devices(
    db: Session, workspace: Workspace, device_ids: tuple[str, ...]
) -> None:
    if workspace_gpu_device_ids(db, workspace):
        raise ValueError("workspace already has a GPU assignment")
    serialized = gpu_device_ids_json(device_ids)
    now = datetime.utcnow()
    for device_id in device_ids:
        db.add(
            WorkspaceGpuLease(
                gpu_device_id=device_id,
                workspace_id=workspace.id,
                created_at=now,
            )
        )
    workspace.assigned_gpu_device_id = device_ids[0]
    workspace.assigned_gpu_device_ids_json = serialized


def release_workspace_gpu_devices(db: Session, workspace: Workspace) -> None:
    db.execute(
        delete(WorkspaceGpuLease).where(
            WorkspaceGpuLease.workspace_id == workspace.id
        )
    )
    workspace.assigned_gpu_device_id = None
    workspace.assigned_gpu_device_ids_json = None
