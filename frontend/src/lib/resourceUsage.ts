import type { AdminCapacity, Workspace, WorkspaceResourceUsage } from "../api/types";

export function resourceUsageIsStale(
  usage: Pick<WorkspaceResourceUsage, "expiresAt" | "stale">,
  nowMilliseconds = Date.now(),
): boolean {
  const expiresAt = Date.parse(usage.expiresAt);
  return usage.stale || !Number.isFinite(expiresAt) || expiresAt <= nowMilliseconds;
}

export function markWorkspaceUsageStale(workspaces: Workspace[]): Workspace[] {
  let changed = false;
  const next = workspaces.map((workspace) => {
    if (workspace.resourceUsage === null || workspace.resourceUsage.stale) {
      return workspace;
    }
    changed = true;
    return {
      ...workspace,
      resourceUsage: { ...workspace.resourceUsage, stale: true },
    };
  });
  return changed ? next : workspaces;
}

export function markAdminUsageStale(capacity: AdminCapacity | null): AdminCapacity | null {
  if (capacity === null || capacity.usage === null || capacity.usage.stale) return capacity;
  return { ...capacity, usage: { ...capacity.usage, stale: true } };
}
