import type { AdminAuditEvent } from "../api/types";
import { formatDate } from "../lib/display";

interface AdminAuditPanelProps {
  events: AdminAuditEvent[];
  warning: string | null;
  refreshing: boolean;
}

function resultTone(result: string): "success" | "danger" | "neutral" {
  const normalized = result.toUpperCase();
  if (["SUCCESS", "SUCCEEDED", "ALLOWED"].includes(normalized)) return "success";
  if (["FAILURE", "FAILED", "DENIED", "ERROR"].includes(normalized)) return "danger";
  return "neutral";
}

export function AdminAuditPanel({ events, warning, refreshing }: AdminAuditPanelProps) {
  return (
    <section
      className="audit-panel"
      aria-labelledby="audit-heading"
      aria-busy={refreshing}
    >
      <div className="section-heading section-heading--audit">
        <div>
          <p className="eyebrow">Admin audit</p>
          <h2 id="audit-heading">최근 감사 이벤트</h2>
          <p>작업 결과와 대상 환경만 표시합니다. 상세 metadata는 화면에 표시하지 않습니다.</p>
        </div>
        <span className="item-count" aria-label={`최근 감사 이벤트 ${events.length}개`}>
          {events.length.toString().padStart(2, "0")}
        </span>
      </div>

      {warning && (
        <p className="audit-warning" role="status">
          감사 이벤트를 갱신하지 못했습니다. {warning}
        </p>
      )}

      {events.length === 0 ? (
        <p className="audit-empty">
          {refreshing ? "감사 이벤트를 불러오는 중입니다." : "표시할 감사 이벤트가 없습니다."}
        </p>
      ) : (
        <div
          className="audit-table-wrap"
          role="region"
          aria-label="최근 감사 이벤트 표"
          tabIndex={0}
        >
          <table className="audit-table">
            <caption>최신순 감사 이벤트, 최대 20개</caption>
            <thead>
              <tr>
                <th scope="col">시간</th>
                <th scope="col">작업</th>
                <th scope="col">결과</th>
                <th scope="col">환경 식별자</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.id}>
                  <td>
                    {event.createdAt ? (
                      <time dateTime={event.createdAt}>{formatDate(event.createdAt) ?? "시간 미상"}</time>
                    ) : "시간 미상"}
                  </td>
                  <td><code>{event.action}</code></td>
                  <td>
                    <span className={`audit-result audit-result--${resultTone(event.result)}`}>
                      {event.result}
                    </span>
                  </td>
                  <td>
                    {event.workspaceId ? (
                      <code className="audit-workspace-id">{event.workspaceId}</code>
                    ) : (
                      <span className="audit-platform-event">플랫폼</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
