import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ProvisioningStatus, UserProvisioning } from "../api/types";
import { ProvisioningBanner } from "./ProvisioningBanner";

function provisioning(
  status: ProvisioningStatus,
  overrides: Partial<UserProvisioning> = {},
): UserProvisioning {
  return {
    status,
    attempts: 0,
    errorCode: null,
    errorSummary: null,
    requestedAt: null,
    completedAt: null,
    ...overrides,
  };
}

describe("ProvisioningBanner", () => {
  const onRequest = async () => {};

  it("keeps manual provisioning informational and exposes no web action", () => {
    const html = renderToStaticMarkup(
      <ProvisioningBanner
        provisioning={provisioning("MANUAL_REQUIRED")}
        requesting={false}
        onRequest={onRequest}
      />,
    );
    expect(html).toContain("관리자가 전용 저장공간 5개를 검증");
    expect(html).not.toContain("<button");
  });

  it("offers the first web provisioning request and locks duplicate clicks", () => {
    const ready = renderToStaticMarkup(
      <ProvisioningBanner
        provisioning={provisioning("NOT_REQUESTED")}
        requesting={false}
        onRequest={onRequest}
      />,
    );
    expect(ready).toContain("개인 개발공간 준비");
    expect(ready).not.toContain("disabled");

    const requesting = renderToStaticMarkup(
      <ProvisioningBanner
        provisioning={provisioning("NOT_REQUESTED")}
        requesting
        onRequest={onRequest}
      />,
    );
    expect(requesting).toContain("요청하는 중");
    expect(requesting).toContain("disabled");
  });

  it.each([
    ["PENDING", "준비 요청을 접수했습니다"],
    ["RUNNING", "준비하고 있습니다"],
  ] as const)("renders %s as a busy status without a duplicate action", (status, copy) => {
    const html = renderToStaticMarkup(
      <ProvisioningBanner
        provisioning={provisioning(status)}
        requesting={false}
        onRequest={onRequest}
      />,
    );
    expect(html).toContain(copy);
    expect(html).toContain('aria-busy="true"');
    expect(html).not.toContain("<button");
  });

  it("shows a safe failure summary and retry action", () => {
    const html = renderToStaticMarkup(
      <ProvisioningBanner
        provisioning={provisioning("FAILED", {
          errorSummary: "저장공간 검증에 실패했습니다.",
        })}
        requesting={false}
        onRequest={onRequest}
      />,
    );
    expect(html).toContain("저장공간 검증에 실패했습니다.");
    expect(html).toContain("다시 준비 요청");
  });
});
