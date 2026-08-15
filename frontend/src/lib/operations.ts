import type { Operation, Workspace } from "../api/types";

function requestedAtMillis(operation: Operation): number | null {
  if (!operation.requestedAt) return null;
  const value = Date.parse(operation.requestedAt);
  return Number.isFinite(value) ? value : null;
}

function isAtLeastAsRecent(candidate: Operation, current: Operation): boolean {
  const candidateTime = requestedAtMillis(candidate);
  const currentTime = requestedAtMillis(current);
  if (candidateTime !== null && currentTime !== null) return candidateTime >= currentTime;
  if (candidateTime !== null) return true;
  if (currentTime !== null) return false;
  // Object insertion order tracks when locally observed operations were added.
  // Prefer the later iteration when the server omitted both timestamps.
  return true;
}

export function selectLatestOperationsByWorkspace(
  workspaces: Array<Pick<Workspace, "id" | "activeOperation">>,
  operations: Record<string, Operation>,
): Record<string, Operation> {
  const selected: Record<string, Operation> = {};
  const serverActiveWorkspaceIds = new Set<string>();

  for (const workspace of workspaces) {
    if (!workspace.activeOperation) continue;
    selected[workspace.id] = workspace.activeOperation;
    serverActiveWorkspaceIds.add(workspace.id);
  }

  for (const operation of Object.values(operations)) {
    if (!operation.workspaceId || serverActiveWorkspaceIds.has(operation.workspaceId)) continue;
    const current = selected[operation.workspaceId];
    if (!current || isAtLeastAsRecent(operation, current)) {
      selected[operation.workspaceId] = operation;
    }
  }
  return selected;
}
