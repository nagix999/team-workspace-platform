import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Operation, OperationStatus } from "../api/types";
import {
  AdminOperationSummary,
  adminOperationNeedsPolling,
  adminWorkspaceOperationNeedsPolling,
  availableResourceSelection,
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
});
