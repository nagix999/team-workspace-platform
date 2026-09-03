import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Capacity } from "../api/types";
import { CapacityPanel, formatKernelIdleTimeout } from "./CapacityPanel";

const capacity: Capacity = {
  workspaceUsed: 1,
  workspaceLimit: 5,
  nextDefaultWorkspaceName: "환경-2",
  activeUsed: 2,
  activeLimit: 15,
  cpuReservedMillicores: 2000,
  cpuBudgetMillicores: 8000,
  memoryReservedMb: 2048,
  memoryBudgetMb: 8192,
  gpuReservedCount: 0,
  gpuBudgetCount: 0,
  kernelIdleTimeoutSeconds: 3600,
  executionHostHealthy: true,
};

describe("CapacityPanel kernel idle policy", () => {
  it("warns users about memory state loss when automatic culling is enabled", () => {
    const html = renderToStaticMarkup(<CapacityPanel capacity={capacity} loading={false} />);
    expect(html).toContain("유휴 커널 자동 정리 1시간");
    expect(html).toContain("실행 중(busy)으로 인식하는 셀");
    expect(html).toContain("메모리 변수와 실행 상태는 사라집니다");
  });

  it("does not show the warning when automatic culling is disabled", () => {
    const html = renderToStaticMarkup(
      <CapacityPanel
        capacity={{ ...capacity, kernelIdleTimeoutSeconds: 0 }}
        loading={false}
      />,
    );
    expect(html).not.toContain("유휴 커널 자동 정리");
  });

  it("shows the exclusive GPU pool only when the server publishes a budget", () => {
    const html = renderToStaticMarkup(
      <CapacityPanel
        capacity={{ ...capacity, gpuReservedCount: 0, gpuBudgetCount: 1 }}
        loading={false}
      />,
    );
    expect(html).toContain("NVIDIA GPU 예약");
    expect(html).toContain("독점 할당");
    expect(html).toContain('aria-label="NVIDIA GPU 예약 0/1"');

    const cpuOnlyHtml = renderToStaticMarkup(
      <CapacityPanel capacity={capacity} loading={false} />,
    );
    expect(cpuOnlyHtml).not.toContain("NVIDIA GPU 예약");
  });

  it("formats whole days, hours, and minutes", () => {
    expect(formatKernelIdleTimeout(604800)).toBe("7일");
    expect(formatKernelIdleTimeout(7200)).toBe("2시간");
    expect(formatKernelIdleTimeout(300)).toBe("5분");
  });
});
