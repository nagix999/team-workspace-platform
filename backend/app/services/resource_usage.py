from __future__ import annotations

from ..models import Workspace


def clear_workspace_resource_usage(workspace: Workspace) -> None:
    """Clear the observational cache without changing business row versions."""

    workspace.cpu_usage_millicores = None
    workspace.memory_usage_bytes = None
    workspace.memory_limit_bytes = None
    workspace.resource_usage_observed_at = None
