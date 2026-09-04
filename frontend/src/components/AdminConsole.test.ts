import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { AdminCapacity, Operation, OperationStatus } from "../api/types";
import {
  AdminUsageOverview,
  AdminOperationSummary,
  adminOperationNeedsPolling,
  adminRuntimePollingInterval,
  adminWorkspaceLifecycleControls,
  adminWorkspaceOperationNeedsPolling,
  availableResourceSelection,
  gpuPolicyControlState,
  resolveKernelIdleTimeoutSeconds,
  scheduleAdminRuntimePolling,
} from "./AdminConsole";

const operation = (operationType: string, status: OperationStatus): Operation => ({
  id: `${operationType}-${status}`,
  workspaceId: "workspace-1",
  operationType,
  status,
  progressPercent: null,
  message: null,
  errorCode: null,
  errorSummary: null,
  requestedAt: null,
  completedAt: null,
});

describe("AdminConsole operation polling", () => {
  it("keeps live usage polling scoped to overview/workspaces and cleans up its timer", () => {
    expect(adminRuntimePollingInterval("overview", false, true)).toBe(15_000);
    expect(adminRuntimePollingInterval("workspaces", false, true)).toBe(15_000);
    expect(adminRuntimePollingInterval("resources", false, true)).toBeNull();
    expect(adminRuntimePollingInterval("profiles", false, true)).toBeNull();
    expect(adminRuntimePollingInterval("resources", true, false)).toBe(2_000);

    vi.useFakeTimers();
    try {
      const callback = vi.fn();
      const cleanup = scheduleAdminRuntimePolling(callback, 15_000);
      vi.advanceTimersByTime(30_000);
      expect(callback).toHaveBeenCalledTimes(2);
      cleanup();
      vi.advanceTimersByTime(30_000);
      expect(callback).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders aggregate actual usage, budget, and partial measurement coverage", () => {
    const capacity: AdminCapacity = {
      users: 4,
      workspaceCreated: 5,
      workspaceRunning: 3,
      workspaceReserved: 3,
      workspaceLimit: 10,
      cpuReservedMillicores: 6000,
      cpuBudgetMillicores: 12000,
      memoryReservedMb: 6144,
      memoryBudgetMb: 16384,
      gpuReservedCount: 0,
      gpuBudgetCount: 0,
      usage: {
        runningTotal: 3,
        measured: 2,
        unavailable: 1,
        cpuMillicores: 2375,
        memoryBytes: 7_516_192_768,
        expiresAt: "2099-09-04T01:03:03Z",
        stale: false,
      },
    };
    const html = renderToStaticMarkup(createElement(AdminUsageOverview, { capacity }));
    expect(html).toContain("CPU 실사용 / 전체 예산");
    expect(html).toContain("2.38 core");
    expect(html).toContain("12 core");
    expect(html).toContain("메모리 실사용 / 전체 예산");
    expect(html).toContain("7 GB");
    expect(html).toContain("16 GB");
    expect(html).toContain("2<small> / 3개 실행 환경</small>");
    expect(html).toContain("1개 환경은 현재 수집할 수 없습니다.");

    const stale = renderToStaticMarkup(createElement(AdminUsageOverview, {
      capacity: {
        ...capacity,
        usage: capacity.usage ? { ...capacity.usage, stale: true } : null,
      },
    }));
    expect(stale).toContain("이전 측정값입니다");

    const unavailable = renderToStaticMarkup(createElement(AdminUsageOverview, {
      capacity: { ...capacity, usage: null },
    }));
    expect(unavailable).toContain("확인 불가");
    expect(unavailable).not.toContain("0 mCPU");
  });

  it.each(["START", "STOP", "RESTART", "DELETE"])(
    "keeps polling an unclaimed %s operation",
    (operationType) => {
      expect(adminOperationNeedsPolling(operation(operationType, "PENDING"))).toBe(true);
    },
  );

  it("continues through worker and external deletion phases, then stops", () => {
    expect(adminOperationNeedsPolling(operation("DELETE", "RUNNING"))).toBe(true);
    expect(adminOperationNeedsPolling(operation("DELETE", "WAITING_EXTERNAL"))).toBe(true);
    for (const status of ["SUCCEEDED", "FAILED", "AUTH_REQUIRED", "CANCELLED"] as const) {
      expect(adminOperationNeedsPolling(operation("DELETE", status))).toBe(false);
    }
  });

  it("recognizes a server-restored operation after a browser refresh", () => {
    const activeOperation = operation("RESTART", "PENDING");
    expect(adminWorkspaceOperationNeedsPolling({ activeOperation })).toBe(true);
    expect(adminWorkspaceOperationNeedsPolling({ activeOperation: null })).toBe(false);
  });

  it("renders a terminal failure restored from admin history after refresh", () => {
    const failed: Operation = {
      ...operation("RESTART", "FAILED"),
      progressPercent: 73,
      errorCode: "ADMIN_LIFECYCLE_UNAVAILABLE",
      errorSummary: "관리자 실행 권한을 확인할 수 없습니다.",
      completedAt: "2026-08-12T01:00:03Z",
    };
    const html = renderToStaticMarkup(createElement(AdminOperationSummary, {
      operation: failed,
      workspaceProgress: null,
    }));
    expect(html).toContain("재시작 작업 실패");
    expect(html).toContain("진행률 73%");
    expect(html).toContain("ADMIN_LIFECYCLE_UNAVAILABLE");
    expect(html).toContain("관리자 실행 권한을 확인할 수 없습니다.");
  });

  it("escapes an operation error summary before rendering it", () => {
    const failed: Operation = {
      ...operation("START", "FAILED"),
      errorSummary: "<img src=x onerror=alert(1)>",
    };
    const html = renderToStaticMarkup(createElement(AdminOperationSummary, {
      operation: failed,
      workspaceProgress: null,
    }));
    expect(html).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(html).not.toContain("<img");
  });

  it("drops stale selected resources so an administrator can repair the policy", () => {
    expect(availableResourceSelection([500, 1000, 2000], [1000, 2000])).toEqual([
      1000,
      2000,
    ]);
    expect(availableResourceSelection([500], [1000])).toEqual([]);
  });

  it("validates the administrator kernel idle timeout and supports disabling it", () => {
    const bounds = { minSeconds: 300, maxSeconds: 604800, stepSeconds: 60 };
    expect(resolveKernelIdleTimeoutSeconds(true, "60", bounds)).toBe(3600);
    expect(resolveKernelIdleTimeoutSeconds(true, "5", bounds)).toBe(300);
    expect(resolveKernelIdleTimeoutSeconds(true, "10080", bounds)).toBe(604800);
    expect(resolveKernelIdleTimeoutSeconds(true, "4", bounds)).toBeNull();
    expect(resolveKernelIdleTimeoutSeconds(true, "5.5", bounds)).toBeNull();
    expect(resolveKernelIdleTimeoutSeconds(false, "", bounds)).toBe(0);
    expect(resolveKernelIdleTimeoutSeconds(true, "60", null)).toBeNull();
  });

  it("enables GPU controls for a verified multi-GPU pool", () => {
    const configured = {
      gpuBudgetCount: 3,
      selectableGpuCounts: [0, 1, 2],
      availableGpuCounts: [0, 1, 2, 3, 4],
      maxGpuBudgetCount: 4,
    };
    expect(gpuPolicyControlState(configured, 0)).toEqual({
      available: true,
      enabled: true,
      lockedByReservation: false,
      canChange: true,
    });
    expect(gpuPolicyControlState(configured, 2)).toMatchObject({
      available: true,
      enabled: true,
      lockedByReservation: true,
      canChange: false,
    });
    expect(gpuPolicyControlState({
      ...configured,
      gpuBudgetCount: 2,
      selectableGpuCounts: [0, 2],
      availableGpuCounts: [0, 2, 4],
    }, 0)).toMatchObject({
      available: true,
      enabled: true,
    });
    expect(gpuPolicyControlState({
      ...configured,
      gpuBudgetCount: 0,
      selectableGpuCounts: [0],
      availableGpuCounts: [0],
      maxGpuBudgetCount: 0,
    }, 0)).toEqual({
      available: false,
      enabled: false,
      lockedByReservation: false,
      canChange: false,
    });
  });

  it("allows a stale enabled GPU policy to be switched off after runtime removal", () => {
    const staleEnabled = {
      gpuBudgetCount: 2,
      selectableGpuCounts: [0, 2],
      availableGpuCounts: [0],
      maxGpuBudgetCount: 0,
    };
    expect(gpuPolicyControlState(staleEnabled, 0)).toEqual({
      available: false,
      enabled: true,
      lockedByReservation: false,
      canChange: true,
    });
    expect(gpuPolicyControlState(staleEnabled, 1)).toEqual({
      available: false,
      enabled: true,
      lockedByReservation: true,
      canChange: false,
    });
  });

  it.each(["NOT_FOUND", "STOPPED", "FAILED"] as const)(
    "lets an administrator stop stale RUNNING intent observed as %s",
    (observedState) => {
      expect(adminWorkspaceLifecycleControls({
        desiredState: "RUNNING",
        observedState,
        stale: true,
      }, {
        busy: false,
        operationPending: false,
        deletionPending: false,
      })).toMatchObject({
        running: false,
        canStart: false,
        canStop: true,
        lifecycleBusy: false,
      });
    },
  );

  it("keeps an intent-recovery stop locked while another operation is pending", () => {
    expect(adminWorkspaceLifecycleControls({
      desiredState: "RUNNING",
      observedState: "STOPPED",
      stale: true,
    }, {
      busy: false,
      operationPending: true,
      deletionPending: false,
    }).lifecycleBusy).toBe(true);
  });
});
