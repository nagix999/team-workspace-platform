import type { StatusPresentation } from "../lib/display";

interface StatusBadgeProps {
  status: StatusPresentation;
  stale?: boolean;
}

export function StatusBadge({ status, stale = false }: StatusBadgeProps) {
  return (
    <span className={`status-badge status-badge--${status.tone}`}>
      <span className="status-badge__dot" aria-hidden="true" />
      {status.label}
      {stale && <span className="status-badge__stale"> · 오래된 상태</span>}
    </span>
  );
}
