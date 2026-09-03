import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Operation, OperationStatus } from "../api/types";
import {
  AdminOperationSummary,
  adminOperationNeedsPolling,
  adminWorkspaceLifecycleControls,
  adminWorkspaceOperationNeedsPolling,
  availableResourceSelection,
  gpuPolicyControlState,
  resolveKernelIdleTimeoutSeconds,
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

  it("enables the GPU toggle only for a verified free one-GPU pool", () => {
    const configured = {
      gpuBudgetCount: 1,
      selectableGpuCounts: [0, 1],
      availableGpuCounts: [0, 1],
      maxGpuBudgetCount: 1,
    };
    expect(gpuPolicyControlState(configured, 0)).toEqual({
      available: true,
      enabled: true,
      lockedByReservation: false,
      canChange: true,
    });
    expect(gpuPolicyControlState(configured, 1)).toMatchObject({
      available: true,
      enabled: true,
      lockedByReservation: true,
      canChange: false,
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
      gpuBudgetCount: 1,
      selectableGpuCounts: [0, 1],
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
