import { describe, expect, it } from "vitest";
import type { AdminCapacity, Workspace } from "../api/types";
import {
  markAdminUsageStale,
  markWorkspaceUsageStale,
  resourceUsageIsStale,
} from "./resourceUsage";

const usage = {
  cpuMillicores: 125,
  memoryBytes: 1024,
  memoryLimitBytes: 2048,
  observedAt: "2026-09-04T01:00:00Z",
  expiresAt: "2026-09-04T01:00:30Z",
  stale: false,
};

describe("resource usage freshness", () => {
  it("expires a cached sample using the server-provided deadline", () => {
    expect(resourceUsageIsStale(usage, Date.parse("2026-09-04T01:00:29Z"))).toBe(false);
    expect(resourceUsageIsStale(usage, Date.parse("2026-09-04T01:00:30Z"))).toBe(true);
    expect(resourceUsageIsStale({ ...usage, stale: true }, 0)).toBe(true);
  });

  it("marks retained user and administrator samples stale after refresh failure", () => {
    const workspace = {
      id: "workspace-1",
      resourceUsage: usage,
    } as Workspace;
    const marked = markWorkspaceUsageStale([workspace]);
    expect(marked[0].resourceUsage?.stale).toBe(true);
    expect(workspace.resourceUsage?.stale).toBe(false);

    const capacity = {
      usage: {
        runningTotal: 1,
        measured: 1,
        unavailable: 0,
        cpuMillicores: 125,
        memoryBytes: 1024,
        expiresAt: usage.expiresAt,
        stale: false,
      },
    } as AdminCapacity;
    expect(markAdminUsageStale(capacity)?.usage?.stale).toBe(true);
    expect(capacity.usage?.stale).toBe(false);
  });
});
