import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { adminNavigation, AdminPortal } from "./AdminPortal";

describe("AdminPortal", () => {
  it("keeps platform operations behind a dedicated admin menu", () => {
    const html = renderToStaticMarkup(
      <AdminPortal
        auditEvents={[]}
        auditWarning={null}
        refreshing={false}
        onPlatformChanged={async () => undefined}
      />,
    );

    expect(adminNavigation.map((item) => item.label)).toEqual([
      "운영 현황",
      "CPU·Memory 정책",
      "Python·자원 조합",
      "전체 개발환경",
      "감사 이벤트",
    ]);
    expect(html).toContain('aria-label="관리자 기능"');
    expect(html).toContain("플랫폼 관리자");
    expect(html).toContain("플랫폼 현황");
    expect(html).not.toContain("최근 감사 이벤트");
  });

  it("renders audit events only on the separate audit tab", () => {
    const html = renderToStaticMarkup(
      <AdminPortal
        initialSection="audit"
        auditEvents={[]}
        auditWarning={null}
        refreshing={false}
        onPlatformChanged={async () => undefined}
      />,
    );

    expect(html).toContain("최근 감사 이벤트");
    expect(html).not.toContain("플랫폼 현황");
  });
});
