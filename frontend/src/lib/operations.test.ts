import { describe, expect, it } from "vitest";
import type { Operation, OperationStatus, Workspace } from "../api/types";
import { selectLatestOperationsByWorkspace } from "./operations";

function operation(
  id: string,
  status: OperationStatus,
  requestedAt: string | null,
): Operation {
  return {
    id,
    workspaceId: "workspace-1",
    operationType: "START",
    status,
    progressPercent: null,
    message: null,
    errorCode: null,
    errorSummary: null,
    requestedAt,
    completedAt: null,
  };
}

describe("latest workspace operation selection", () => {
  it("shows a newer successful retry instead of an older failure", () => {
    const failed = operation("failed", "FAILED", "2026-08-12T01:00:00Z");
    const retried = operation("retried", "SUCCEEDED", "2026-08-12T01:01:00Z");

    expect(selectLatestOperationsByWorkspace([], {
      [failed.id]: failed,
      [retried.id]: retried,
    })["workspace-1"]).toBe(retried);
  });

  it("keeps the server-restored active operation authoritative", () => {
    const active = operation("active", "RUNNING", "2026-08-12T01:00:00Z");
    const locallyObserved = operation(
      "local-terminal",
      "FAILED",
      "2026-08-12T01:02:00Z",
    );
    const workspace = {
      id: "workspace-1",
      activeOperation: active,
    } satisfies Pick<Workspace, "id" | "activeOperation">;

    expect(selectLatestOperationsByWorkspace([workspace], {
      [locallyObserved.id]: locallyObserved,
    })["workspace-1"]).toBe(active);
  });
});
