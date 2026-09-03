import type { Capacity } from "../api/types";
import { formatMegabytes } from "../lib/display";

interface CapacityPanelProps {
  capacity: Capacity | null;
  loading: boolean;
}

interface MeterProps {
  label: string;
  detail: string;
  used: number;
  limit: number;
  accent?: boolean;
  formatValue?: (value: number) => string;
}

function CapacityMeter({
  label,
  detail,
  used,
  limit,
  accent = false,
  formatValue = String,
}: MeterProps) {
  const safeLimit = Math.max(limit, 1);
  const percentage = Math.min(100, Math.max(0, (used / safeLimit) * 100));
  const full = used >= limit;

  return (
    <div className="capacity-meter">
      <div className="capacity-meter__head">
        <div>
          <p className="eyebrow">{label}</p>
          <p className="capacity-meter__value">
            <strong>{formatValue(used)}</strong>
            <span> / {formatValue(limit)}</span>
          </p>
        </div>
        <span className={`capacity-meter__state ${full ? "is-full" : ""}`}>
          {full ? "한도 도달" : detail}
        </span>
      </div>
      <div
        className="capacity-meter__track"
        role="progressbar"
        aria-label={`${label} ${used}/${limit}`}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-valuenow={Math.min(used, limit)}
      >
        <span
          className={accent ? "capacity-meter__fill is-accent" : "capacity-meter__fill"}
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  );
}

export function formatKernelIdleTimeout(seconds: number): string {
  if (seconds >= 86_400 && seconds % 86_400 === 0) return `${seconds / 86_400}일`;
  if (seconds >= 3_600 && seconds % 3_600 === 0) return `${seconds / 3_600}시간`;
  return `${seconds / 60}분`;
}

export function CapacityPanel({ capacity, loading }: CapacityPanelProps) {
  const resourcesKnown = capacity !== null &&
    capacity.cpuReservedMillicores !== null &&
    capacity.cpuBudgetMillicores !== null &&
    capacity.memoryReservedMb !== null &&
    capacity.memoryBudgetMb !== null;
  return (
    <section className="panel capacity-panel" aria-labelledby="capacity-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Capacity</p>
          <h2 id="capacity-heading">사용 현황</h2>
        </div>
        {capacity?.executionHostHealthy === false && (
          <span className="health-warning">실행 호스트 점검 중</span>
        )}
      </div>
      {loading && !capacity ? (
        <div className="skeleton-block" aria-label="사용 현황 불러오는 중" />
      ) : capacity ? (
        <>
          <div className="capacity-panel__meters">
            <CapacityMeter
              label="내 환경"
              detail="보유 중"
              used={capacity.workspaceUsed}
              limit={capacity.workspaceLimit}
            />
            <CapacityMeter
              label="전체 실행 환경"
              detail="가동 중"
              used={capacity.activeUsed}
              limit={capacity.activeLimit}
              accent
            />
            {resourcesKnown ? (
              <>
                <CapacityMeter
                  label="CPU 예약"
                  detail="전체 예산"
                  used={capacity.cpuReservedMillicores!}
                  limit={capacity.cpuBudgetMillicores!}
                  formatValue={(value) => `${value / 1000} core`}
                />
                <CapacityMeter
                  label="메모리 예약"
                  detail="전체 예산"
                  used={capacity.memoryReservedMb!}
                  limit={capacity.memoryBudgetMb!}
                  formatValue={(value) => formatMegabytes(value) ?? `${value} MB`}
                />
                {capacity.gpuReservedCount !== null &&
                  capacity.gpuBudgetCount !== null &&
                  capacity.gpuBudgetCount > 0 && (
                    <CapacityMeter
                      label="NVIDIA GPU 예약"
                      detail="독점 할당"
                      used={capacity.gpuReservedCount}
                      limit={capacity.gpuBudgetCount}
                      formatValue={(value) => `${value}개`}
                    />
                  )}
              </>
            ) : (
              <p className="form-hint capacity-resource-warning" role="status">
                CPU·메모리 예약 현황을 확인할 수 없습니다.
              </p>
            )}
          </div>
          {capacity.kernelIdleTimeoutSeconds !== null &&
            capacity.kernelIdleTimeoutSeconds > 0 && (
              <p className="capacity-kernel-policy" role="note">
                <strong>유휴 커널 자동 정리 {formatKernelIdleTimeout(
                  capacity.kernelIdleTimeoutSeconds,
                )}</strong>
                새로 시작하거나 재시작한 환경에서 Jupyter가 유휴로 판단한 커널은 자동으로
                종료됩니다. Jupyter가 실행 중(busy)으로 인식하는 셀과 저장한 파일은 보호되지만,
                메모리 변수와 실행 상태는 사라집니다. 셀 밖 background 작업은 보호 대상이 아닙니다.
              </p>
            )}
        </>
      ) : (
        <p className="empty-copy">사용 현황을 불러오지 못했습니다.</p>
      )}
    </section>
  );
}
