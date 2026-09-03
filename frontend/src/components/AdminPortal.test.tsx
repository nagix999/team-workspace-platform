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
      "자원·커널 정책",
      "Python·자원 조합",
      "전체 개발환경",
      "내부 서비스 통신",
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

  it("renders internal proxy exceptions on a separate administrator tab", () => {
    const html = renderToStaticMarkup(
      <AdminPortal
        initialSection="egress"
        auditEvents={[]}
        auditWarning={null}
        refreshing={false}
        onPlatformChanged={async () => undefined}
      />,
    );

    expect(html).toContain("내부 서비스 통신");
    expect(html).toContain("&lt;PRIVATE_IPV4&gt;/32");
    expect(html).toContain("직접 사내망 접근은 계속 차단됩니다");
    expect(html).not.toContain("최근 감사 이벤트");
  });
});
