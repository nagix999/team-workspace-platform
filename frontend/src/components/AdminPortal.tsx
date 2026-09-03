import { useState } from "react";
import type { AdminAuditEvent } from "../api/types";
import { AdminAuditPanel } from "./AdminAuditPanel";
import { AdminConsole, type AdminConsoleView } from "./AdminConsole";
import { AdminInternalEgress } from "./AdminInternalEgress";

export type AdminSection = AdminConsoleView | "egress" | "audit";

interface AdminPortalProps {
  auditEvents: AdminAuditEvent[];
  auditWarning: string | null;
  refreshing: boolean;
  onPlatformChanged: () => Promise<void>;
  initialSection?: AdminSection;
}

export const adminNavigation: ReadonlyArray<{
  id: AdminSection;
  label: string;
}> = [
  { id: "overview", label: "운영 현황" },
  { id: "resources", label: "자원·커널 정책" },
  { id: "profiles", label: "Python·자원 조합" },
  { id: "workspaces", label: "전체 개발환경" },
  { id: "egress", label: "내부 서비스 통신" },
  { id: "audit", label: "감사 이벤트" },
];

export function AdminPortal({
  auditEvents,
  auditWarning,
  refreshing,
  onPlatformChanged,
  initialSection = "overview",
}: AdminPortalProps) {
  const [section, setSection] = useState<AdminSection>(initialSection);

  return (
    <div className="admin-portal">
      <section className="admin-portal__hero" aria-labelledby="admin-page-heading">
        <div>
          <p className="eyebrow">Admin only</p>
          <h1 id="admin-page-heading">플랫폼 관리자</h1>
          <p>운영 설정과 전체 개발환경 제어는 관리자에게만 표시됩니다.</p>
        </div>
      </section>
      <nav className="admin-tabs" aria-label="관리자 기능">
        {adminNavigation.map((item) => (
          <button
            key={item.id}
            type="button"
            className={section === item.id ? "is-active" : ""}
            aria-current={section === item.id ? "page" : undefined}
            onClick={() => setSection(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      {section === "audit" ? (
        <AdminAuditPanel
          events={auditEvents}
          warning={auditWarning}
          refreshing={refreshing}
        />
      ) : section === "egress" ? (
        <AdminInternalEgress />
      ) : (
        <AdminConsole view={section} onPlatformChanged={onPlatformChanged} />
      )}
    </div>
  );
}
