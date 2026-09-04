import type { ObservedState, WorkspaceResourceUsage as Usage } from "../api/types";
import { formatBytes, formatMillicores } from "../lib/display";
import { cpuLimitToMillicores } from "../lib/profiles";
import { resourceUsageIsStale } from "../lib/resourceUsage";

type UsageState = "live" | "stale" | "measuring" | "unavailable" | "stopped";

interface WorkspaceResourceUsageProps {
  observedState: ObservedState;
  resourceUsage: Usage | null;
  cpuLimit: string | null;
  label: string;
  compact?: boolean;
}

export function workspaceResourceUsageState(
  observedState: ObservedState,
  resourceUsage: Usage | null,
): UsageState {
  if (observedState === "RUNNING") {
    if (resourceUsage === null) return "unavailable";
    return resourceUsageIsStale(resourceUsage) ? "stale" : "live";
  }
  if (observedState === "STARTING" || observedState === "STOPPING") {
    return "measuring";
  }
  if (observedState === "FAILED" || observedState === "UNKNOWN") {
    return "unavailable";
  }
  return "stopped";
}

const stateLabels: Record<UsageState, string> = {
  live: "측정됨",
  stale: "이전 측정값",
  measuring: "측정 중",
  unavailable: "수집 불가",
  stopped: "중지됨",
};

const stateDescriptions: Record<Exclude<UsageState, "live" | "stale">, string> = {
  measuring: "환경 상태 전환 후 실사용량을 측정합니다.",
  unavailable: "현재 CPU·메모리 실사용량을 확인할 수 없습니다.",
  stopped: "중지된 환경은 실사용량을 측정하지 않습니다.",
};

export function WorkspaceResourceUsage({
  observedState,
  resourceUsage,
  cpuLimit,
  label,
  compact = false,
}: WorkspaceResourceUsageProps) {
  const state = workspaceResourceUsageState(observedState, resourceUsage);
  const visibleUsage = state === "live" || state === "stale" ? resourceUsage : null;
  const cpuLimitMillicores = cpuLimit ? cpuLimitToMillicores(cpuLimit) : null;
  return (
    <section
      className={`workspace-resource-usage workspace-resource-usage--${state}${compact ? " workspace-resource-usage--compact" : ""}`}
      aria-label={`${label} CPU 및 메모리 실사용량`}
    >
      <div className="workspace-resource-usage__heading">
        <strong>실사용량</strong>
        <span>{stateLabels[state]}</span>
      </div>
      {visibleUsage ? (
        <>
          <dl>
            <div>
              <dt>CPU</dt>
              <dd>
                {formatMillicores(visibleUsage.cpuMillicores)}
                {cpuLimitMillicores !== null && (
                  <small> / {formatMillicores(cpuLimitMillicores)}</small>
                )}
              </dd>
            </div>
            <div>
              <dt>메모리</dt>
              <dd>
                {formatBytes(visibleUsage.memoryBytes)}
                <small> / {formatBytes(visibleUsage.memoryLimitBytes)}</small>
              </dd>
            </div>
          </dl>
          {state === "stale" && (
            <p role="status">최신 상태가 아니므로 현재 사용량과 다를 수 있습니다.</p>
          )}
        </>
      ) : (
        <p role="status">{stateDescriptions[state as keyof typeof stateDescriptions]}</p>
      )}
    </section>
  );
}
