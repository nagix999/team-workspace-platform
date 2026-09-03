import { useCallback, useEffect, useMemo, useState } from "react";
import { portalApi } from "../api/client";
import type { AdminCapacity, Operation, ResourcePolicy, Workspace } from "../api/types";
import {
  formatDate,
  formatMegabytes,
  operationStatusPresentation,
  presentError,
  workspaceStatusPresentation,
} from "../lib/display";
import { selectLatestOperationsByWorkspace } from "../lib/operations";
import { StatusBadge } from "./StatusBadge";
import { AdminProfileManager } from "./AdminProfileManager";

interface AdminConsoleProps {
  onPlatformChanged: () => Promise<void>;
  view?: AdminConsoleView;
}

export type AdminConsoleView = "overview" | "resources" | "profiles" | "workspaces";

function replaceWorkspace(current: Workspace[], incoming: Workspace): Workspace[] {
  const index = current.findIndex((workspace) => workspace.id === incoming.id);
  if (index < 0) return [incoming, ...current];
  const previous = current[index];
  const next = [...current];
  next[index] = {
    ...incoming,
    owner: incoming.owner ?? previous.owner,
    name: incoming.name || previous.name,
  };
  return next;
}

function percent(used: number, limit: number): number {
  return limit > 0 ? Math.min(100, Math.max(0, used / limit * 100)) : 0;
}

export function adminOperationNeedsPolling(operation: Operation): boolean {
  return ["PENDING", "RUNNING", "WAITING_EXTERNAL"].includes(operation.status);
}

export function adminWorkspaceOperationNeedsPolling(
  workspace: Pick<Workspace, "activeOperation">,
): boolean {
  return Boolean(
    workspace.activeOperation && adminOperationNeedsPolling(workspace.activeOperation),
  );
}

export function availableResourceSelection(
  selected: number[],
  available: number[],
): number[] {
  const allowed = new Set(available);
  return selected.filter((value) => allowed.has(value));
}

export function gpuPolicyControlState(
  policy: Pick<
    ResourcePolicy,
    "gpuBudgetCount" | "selectableGpuCounts" | "availableGpuCounts" | "maxGpuBudgetCount"
  >,
  gpuReservedCount: number,
) {
  const available = policy.maxGpuBudgetCount === 1 &&
    policy.availableGpuCounts.includes(1);
  const enabled = policy.gpuBudgetCount === 1 &&
    policy.selectableGpuCounts.includes(1);
  const lockedByReservation = gpuReservedCount > 0;
  return {
    available,
    enabled,
    lockedByReservation,
    // If a previously enabled GPU runtime disappears, administrators still
    // need a recovery path to turn the stale policy off.  Enabling remains
    // impossible until the verified runtime and hard ceiling return.
    canChange: !lockedByReservation && (available || enabled),
  };
}

export function resolveKernelIdleTimeoutSeconds(
  enabled: boolean,
  minutesInput: string,
  bounds: ResourcePolicy["kernelIdleTimeoutBounds"],
): number | null {
  if (!enabled) return 0;
  if (bounds === null || minutesInput.trim() === "") return null;
  const seconds = Number(minutesInput) * 60;
  return Number.isSafeInteger(seconds) &&
      seconds >= bounds.minSeconds &&
      seconds <= bounds.maxSeconds &&
      seconds % bounds.stepSeconds === 0
    ? seconds
    : null;
}

function editableKernelIdleMinutes(policy: ResourcePolicy): string {
  if (policy.kernelIdleTimeoutSeconds !== null && policy.kernelIdleTimeoutSeconds > 0) {
    return String(policy.kernelIdleTimeoutSeconds / 60);
  }
  const bounds = policy.kernelIdleTimeoutBounds;
  if (bounds === null) return "";
  const preferred = Math.min(bounds.maxSeconds, Math.max(bounds.minSeconds, 3_600));
  const aligned = Math.min(
    bounds.maxSeconds,
    Math.ceil(preferred / bounds.stepSeconds) * bounds.stepSeconds,
  );
  return String(aligned / 60);
}

export function adminWorkspaceLifecycleControls(
  workspace: Pick<Workspace, "desiredState" | "observedState" | "stale">,
  locks: { busy: boolean; operationPending: boolean; deletionPending: boolean },
) {
  const lifecycleAvailable = workspace.desiredState !== "DELETED";
  const intentRecoveryStop = lifecycleAvailable &&
    workspace.desiredState === "RUNNING" &&
    ["NOT_FOUND", "STOPPED", "FAILED"].includes(workspace.observedState);
  return {
    running: lifecycleAvailable && workspace.observedState === "RUNNING" && !workspace.stale,
    canStart: lifecycleAvailable && workspace.desiredState !== "RUNNING" &&
      ["NOT_FOUND", "STOPPED", "FAILED"].includes(workspace.observedState),
    canStop: lifecycleAvailable && (
      intentRecoveryStop || ["STARTING", "RUNNING", "UNKNOWN"].includes(workspace.observedState)
    ),
    lifecycleBusy: locks.busy || locks.operationPending || locks.deletionPending || (
      workspace.stale && !intentRecoveryStop
    ),
  };
}

function adminOperationTypeLabel(operationType: string): string {
  const labels: Record<string, string> = {
    CREATE: "생성",
    START: "시작",
    STOP: "중지",
    RESTART: "재시작",
    DELETE: "삭제",
  };
  return labels[operationType] ?? operationType;
}

export function AdminOperationSummary({
  operation,
  workspaceProgress,
}: {
  operation: Operation;
  workspaceProgress: number | null;
}) {
  const presentation = operationStatusPresentation(operation.status);
  const progress = operation.progressPercent ?? workspaceProgress ??
    (operation.status === "SUCCEEDED" ? 100 : null);
  const error = operation.errorSummary ?? operation.message;
  return (
    <div
      className={`admin-operation admin-operation--${presentation.tone}`}
      aria-label={`${adminOperationTypeLabel(operation.operationType)} 작업 ${presentation.label}`}
    >
      <span className="admin-operation__heading">
        <strong>{adminOperationTypeLabel(operation.operationType)}</strong>
        <StatusBadge status={presentation} />
      </span>
      <span className="admin-operation__progress">
        진행률 {progress === null ? "-" : `${Math.max(0, Math.min(100, progress))}%`}
      </span>
      {(operation.errorCode || error) && (
        <span className="admin-operation__error">
          {operation.errorCode && <code>{operation.errorCode}</code>}
          {error && <span>{error}</span>}
        </span>
      )}
    </div>
  );
}

const viewCopy: Record<AdminConsoleView, { title: string; description: string }> = {
  overview: {
    title: "플랫폼 현황",
    description: "전체 사용자와 개발환경의 현재 예약·실행 상태를 확인합니다.",
  },
  resources: {
    title: "자원·커널 정책",
    description: "CPU·메모리·GPU 선택 정책과 유휴 커널 자동 정리 시간을 설정합니다.",
  },
  profiles: {
    title: "Python 실행 조합",
    description: "검증된 Python 커널·가속기·CPU·Memory 조합을 사용자에게 공개합니다.",
  },
  workspaces: {
    title: "전체 개발환경",
    description: "모든 사용자의 환경을 확인하고 시작·중지·열기·삭제합니다.",
  },
};

export function AdminConsole({
  onPlatformChanged,
  view = "overview",
}: AdminConsoleProps) {
  const [capacity, setCapacity] = useState<AdminCapacity | null>(null);
  const [policy, setPolicy] = useState<ResourcePolicy | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [operations, setOperations] = useState<Record<string, Operation>>({});
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyWorkspaceIds, setBusyWorkspaceIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [cpuBudgetCores, setCpuBudgetCores] = useState("");
  const [memoryBudgetMb, setMemoryBudgetMb] = useState("");
  const [selectedCpu, setSelectedCpu] = useState<Set<number>>(new Set());
  const [selectedMemory, setSelectedMemory] = useState<Set<number>>(new Set());
  const [gpuEnabled, setGpuEnabled] = useState(false);
  const [cpuToAdd, setCpuToAdd] = useState("");
  const [memoryToAdd, setMemoryToAdd] = useState("");
  const [kernelCullingEnabled, setKernelCullingEnabled] = useState(false);
  const [kernelIdleMinutes, setKernelIdleMinutes] = useState("");

  const loadRuntime = useCallback(async (signal?: AbortSignal) => {
    const [capacityResult, workspaceResult] = await Promise.all([
      portalApi.adminCapacity(signal),
      portalApi.adminWorkspaces(signal),
    ]);
    setCapacity(capacityResult);
    setWorkspaces(workspaceResult.items);
    setTotal(workspaceResult.total);
  }, []);

  const loadAll = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const [capacityResult, workspaceResult, policyResult, operationResults] = await Promise.all([
        portalApi.adminCapacity(signal),
        portalApi.adminWorkspaces(signal),
        portalApi.adminSettings(signal),
        portalApi.adminOperations(signal),
      ]);
      setCapacity(capacityResult);
      setWorkspaces(workspaceResult.items);
      setTotal(workspaceResult.total);
      setOperations(Object.fromEntries(
        // The endpoint is newest-first. Insert oldest-first so timestamp ties
        // still leave the newest server result authoritative.
        [...operationResults].reverse().map((operation) => [operation.id, operation]),
      ));
      setPolicy(policyResult);
      setCpuBudgetCores(String(policyResult.cpuBudgetMillicores / 1000));
      setMemoryBudgetMb(String(policyResult.memoryBudgetMb));
      setSelectedCpu(new Set(availableResourceSelection(
        policyResult.selectableCpuMillicores,
        policyResult.availableCpuMillicores,
      )));
      setSelectedMemory(new Set(availableResourceSelection(
        policyResult.selectableMemoryMb,
        policyResult.availableMemoryMb,
      )));
      setGpuEnabled(
        gpuPolicyControlState(policyResult, capacityResult.gpuReservedCount).enabled,
      );
      setKernelCullingEnabled((policyResult.kernelIdleTimeoutSeconds ?? 0) > 0);
      setKernelIdleMinutes(editableKernelIdleMinutes(policyResult));
      setError(null);
    } catch (requestError) {
      if (requestError instanceof DOMException && requestError.name === "AbortError") return;
      setError(presentError(requestError));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadAll(controller.signal);
    return () => controller.abort();
  }, [loadAll]);

  const activeOperationIds = useMemo(() => [...new Set([
    ...Object.values(operations)
      .filter(adminOperationNeedsPolling)
      .map((operation) => operation.id),
    ...workspaces
      .filter(adminWorkspaceOperationNeedsPolling)
      .map((workspace) => workspace.activeOperation)
      .filter((operation): operation is Operation => operation !== null)
      .map((operation) => operation.id),
  ])], [operations, workspaces]);
  const operationsByWorkspace = useMemo(
    () => selectLatestOperationsByWorkspace(workspaces, operations),
    [operations, workspaces],
  );
  const pollingNeeded = useMemo(() => activeOperationIds.length > 0 ||
    workspaces.some((workspace) =>
      ["STARTING", "STOPPING", "DELETION_PENDING", "DELETING"].includes(
        workspace.observedState,
      ) || (workspace.stale && workspace.desiredState !== "DELETED") ||
      (workspace.desiredState === "DELETED" &&
        ["PENDING", "RUNNING"].includes(workspace.deletionStatus ?? ""))),
  [activeOperationIds.length, workspaces]);

  useEffect(() => {
    if (!pollingNeeded) return;
    const interval = window.setInterval(() => {
      void Promise.allSettled([
        loadRuntime(),
        ...activeOperationIds.map((operationId) => portalApi.operation(operationId)),
      ]).then(([runtimeResult, ...operationResults]) => {
        const completedOperations = operationResults
          .filter((result): result is PromiseFulfilledResult<Operation> =>
            result.status === "fulfilled")
          .map((result) => result.value);
        if (completedOperations.length > 0) {
          setOperations((current) => {
            const next = { ...current };
            for (const operation of completedOperations) next[operation.id] = operation;
            return next;
          });
        }
        const rejected = [runtimeResult, ...operationResults].find(
          (result) => result.status === "rejected",
        );
        if (rejected?.status === "rejected") setError(presentError(rejected.reason));
      });
    }, 2_000);
    return () => window.clearInterval(interval);
  }, [activeOperationIds, loadRuntime, pollingNeeded]);

  const addResourceValue = (
    value: number,
    maximum: number,
    setter: React.Dispatch<React.SetStateAction<Set<number>>>,
    clear: React.Dispatch<React.SetStateAction<string>>,
  ) => {
    if (!Number.isSafeInteger(value) || value <= 0 || value > maximum) {
      setError("추가할 값은 현재 전체 예산과 호스트 최대 예산 이하여야 합니다.");
      return;
    }
    setter((current) => new Set(current).add(value));
    clear("");
    setError(null);
  };

  const removeResourceValue = (
    value: number,
    setter: React.Dispatch<React.SetStateAction<Set<number>>>,
  ) => setter((current) => {
    const next = new Set(current);
    next.delete(value);
    return next;
  });

  const unavailablePolicySelections = useMemo(() => {
    if (!policy) return { cpu: 0, memory: 0 };
    return {
      cpu: policy.selectableCpuMillicores.length - availableResourceSelection(
        policy.selectableCpuMillicores,
        policy.availableCpuMillicores,
      ).length,
      memory: policy.selectableMemoryMb.length - availableResourceSelection(
        policy.selectableMemoryMb,
        policy.availableMemoryMb,
      ).length,
    };
  }, [policy]);

  const savePolicy = async () => {
    if (!policy || !capacity) return;
    const cpuBudgetMillicores = Number(cpuBudgetCores) * 1000;
    const memoryBudget = Number(memoryBudgetMb);
    const gpuBudgetCount = gpuEnabled ? 1 : 0;
    if (!Number.isSafeInteger(cpuBudgetMillicores) || cpuBudgetMillicores <= 0 ||
      !Number.isSafeInteger(memoryBudget) || memoryBudget <= 0) {
      setError("CPU 전체 예산과 메모리 전체 예산을 올바르게 입력해 주세요.");
      return;
    }
    if (cpuBudgetMillicores < capacity.cpuReservedMillicores ||
      memoryBudget < capacity.memoryReservedMb ||
      gpuBudgetCount < capacity.gpuReservedCount) {
      setError("현재 예약량보다 전체 예산을 낮출 수 없습니다.");
      return;
    }
    if ((policy.maxCpuBudgetMillicores !== null &&
      cpuBudgetMillicores > policy.maxCpuBudgetMillicores) ||
      (policy.maxMemoryBudgetMb !== null && memoryBudget > policy.maxMemoryBudgetMb)) {
      setError("호스트 기준 최대 CPU 또는 메모리 예산을 초과했습니다.");
      return;
    }
    if (gpuBudgetCount > policy.maxGpuBudgetCount ||
      (gpuEnabled && !policy.availableGpuCounts.includes(1))) {
      setError("검증된 NVIDIA GPU 및 CUDA 실행 프로필이 없어 GPU를 공개할 수 없습니다.");
      return;
    }
    if (selectedCpu.size === 0 || selectedMemory.size === 0) {
      setError("사용자가 선택할 CPU와 메모리 값을 각각 하나 이상 선택해 주세요.");
      return;
    }
    const kernelIdleTimeoutSeconds = resolveKernelIdleTimeoutSeconds(
      kernelCullingEnabled,
      kernelIdleMinutes,
      policy.kernelIdleTimeoutBounds,
    );
    if (kernelIdleTimeoutSeconds === null || policy.kernelIdleTimeoutSeconds === null) {
      setError("유휴 커널 자동 정리 시간을 허용 범위의 분 단위로 입력해 주세요.");
      return;
    }
    if (kernelIdleTimeoutSeconds !== policy.kernelIdleTimeoutSeconds && !window.confirm(
      `유휴 커널 정책을 ${policy.kernelIdleTimeoutSeconds === 0
        ? "사용 안 함"
        : `${policy.kernelIdleTimeoutSeconds / 60}분`}에서 ${kernelIdleTimeoutSeconds === 0
        ? "사용 안 함"
        : `${kernelIdleTimeoutSeconds / 60}분`}으로 변경합니다. ` +
      "현재 실행 중인 환경을 재시작한 뒤 적용됩니다. " +
      "유휴 커널이 종료되면 메모리 변수와 실행 상태는 사라지고 저장된 파일은 유지됩니다. " +
      "계속하시겠습니까?",
    )) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await portalApi.updateAdminSettings({
        version: policy.version,
        cpuBudgetMillicores,
        memoryBudgetMb: memoryBudget,
        selectableCpuMillicores: [...selectedCpu].sort((left, right) => left - right),
        selectableMemoryMb: [...selectedMemory].sort((left, right) => left - right),
        gpuBudgetCount,
        selectableGpuCounts: gpuEnabled ? [0, 1] : [0],
        kernelIdleTimeoutSeconds,
      });
      setPolicy(updated);
      setKernelCullingEnabled((updated.kernelIdleTimeoutSeconds ?? 0) > 0);
      setKernelIdleMinutes(editableKernelIdleMinutes(updated));
      setNotice("자원 및 유휴 커널 정책을 저장했습니다. 실행 중인 환경에는 재시작 후 적용됩니다.");
      await Promise.all([loadRuntime(), onPlatformChanged()]);
    } catch (requestError) {
      setError(presentError(requestError));
    } finally {
      setSaving(false);
    }
  };

  const runAction = async (
    workspace: Workspace,
    action: "start" | "stop" | "restart" | "delete",
  ) => {
    if (action === "delete") {
      const retry = workspace.desiredState === "DELETED" && workspace.canRetryDelete;
      const confirmed = window.confirm(
        `${workspace.owner?.username ?? "사용자"}의 ${workspace.name} 환경 삭제를 ${retry ? "재시도" : "요청"}하시겠습니까? 개인 작업 데이터는 영구 삭제되며 복구할 수 없습니다. 팀 공유 디렉터리의 데이터는 삭제되지 않습니다.`,
      );
      if (!confirmed) return;
    }
    setBusyWorkspaceIds((current) => new Set(current).add(workspace.id));
    setError(null);
    setNotice(null);
    try {
      const result = action === "delete"
        ? await portalApi.adminDeleteWorkspace(workspace.id)
        : await portalApi.adminWorkspaceAction(workspace.id, action);
      setWorkspaces((current) => replaceWorkspace(current, result.workspace));
      setOperations((current) => ({
        ...current,
        [result.operation.id]: result.operation,
      }));
      setNotice(
        action === "delete"
          ? "관리자 환경 삭제 요청을 접수했습니다."
          : action === "restart"
            ? "환경변수 변경사항을 적용하도록 재시작을 요청했습니다."
            : `관리자 환경 ${action === "start" ? "시작" : "중지"} 요청을 접수했습니다.`,
      );
      await Promise.all([loadRuntime(), onPlatformChanged()]);
    } catch (requestError) {
      setError(presentError(requestError));
    } finally {
      setBusyWorkspaceIds((current) => {
        const next = new Set(current);
        next.delete(workspace.id);
        return next;
      });
    }
  };

  return (
    <section className="admin-console" aria-labelledby={`admin-console-${view}-heading`} aria-busy={loading}>
      <div className="section-heading section-heading--admin">
        <div>
          <p className="eyebrow">Platform administration</p>
          <h2 id={`admin-console-${view}-heading`}>{viewCopy[view].title}</h2>
          <p>{viewCopy[view].description}</p>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label="관리자 현황 새로고침"
          disabled={loading}
          onClick={() => void loadAll()}
        >↻</button>
      </div>

      {error && <p className="admin-message admin-message--error" role="alert">{error}</p>}
      {notice && <p className="admin-message" role="status">{notice}</p>}

      {view === "overview" && capacity && (
        <div className="admin-stats" aria-label="전체 플랫폼 현황">
          <div><span>등록 사용자</span><strong>{capacity.users}</strong></div>
          <div><span>생성된 환경</span><strong>{capacity.workspaceCreated}</strong></div>
          <div><span>실제 실행 중</span><strong>{capacity.workspaceRunning}</strong></div>
          <div>
            <span>예약 환경</span>
            <strong>{capacity.workspaceReserved}<small> / {capacity.workspaceLimit}</small></strong>
          </div>
          <div className="admin-stat--resource">
            <span>CPU 예약 / 전체 예산</span>
            <strong>{capacity.cpuReservedMillicores / 1000}<small> / {capacity.cpuBudgetMillicores / 1000} core</small></strong>
            <i style={{ width: `${percent(capacity.cpuReservedMillicores, capacity.cpuBudgetMillicores)}%` }} />
          </div>
          {(capacity.gpuBudgetCount > 0 || capacity.gpuReservedCount > 0) && (
            <div className="admin-stat--resource">
              <span>NVIDIA GPU 예약 / 전체 예산</span>
              <strong>{capacity.gpuReservedCount}<small> / {capacity.gpuBudgetCount}개</small></strong>
              <i style={{ width: `${percent(capacity.gpuReservedCount, capacity.gpuBudgetCount)}%` }} />
            </div>
          )}
          <div className="admin-stat--resource">
            <span>메모리 예약 / 전체 예산</span>
            <strong>{formatMegabytes(capacity.memoryReservedMb)}<small> / {formatMegabytes(capacity.memoryBudgetMb)}</small></strong>
            <i style={{ width: `${percent(capacity.memoryReservedMb, capacity.memoryBudgetMb)}%` }} />
          </div>
        </div>
      )}

      {view === "resources" && policy && capacity && (
        <div className="admin-settings">
          <div className="admin-settings__heading">
            <div>
              <h3>CPU·메모리·GPU 및 커널 정책</h3>
              <p>호스트 자원 선택값과 Jupyter 유휴 커널 정리 기준을 관리합니다.</p>
            </div>
            <span>정책 v{policy.version}</span>
          </div>
          <section className="admin-kernel-policy" aria-labelledby="admin-gpu-policy-heading">
            <div>
              <h4 id="admin-gpu-policy-heading">NVIDIA GPU 환경</h4>
              <p>
                운영 사전점검을 통과한 물리 GPU 1개를 한 환경에 독점 할당합니다.
                GPU 메모리 용량 제한이나 공유 할당은 지원하지 않습니다.
              </p>
            </div>
            <div className="admin-kernel-policy__controls">
              <label className="admin-kernel-policy__toggle">
                <input
                  type="checkbox"
                  checked={gpuEnabled}
                  disabled={saving || !gpuPolicyControlState(
                    policy,
                    capacity.gpuReservedCount,
                  ).canChange}
                  onChange={(event) => setGpuEnabled(event.target.checked)}
                />
                <span>사용자에게 NVIDIA GPU 1개 선택 허용</span>
              </label>
            </div>
            {!gpuPolicyControlState(policy, capacity.gpuReservedCount).available ? (
              <p className="admin-message admin-message--warning" role="status">
                검증된 호스트 GPU UUID와 CUDA PyTorch 실행 프로필이 배포되지 않았습니다.
              </p>
            ) : (
              <p className="admin-hard-ceiling">
                호스트 GPU: 1개 · 현재 독점 예약 {capacity.gpuReservedCount}개
              </p>
            )}
          </section>
          <div className="admin-budget-fields">
            <label>
              <span>CPU 전체 예산 (core)</span>
              <input
                type="number"
                min={capacity.cpuReservedMillicores / 1000}
                max={policy.maxCpuBudgetMillicores !== null
                  ? policy.maxCpuBudgetMillicores / 1000
                  : undefined}
                step="0.1"
                value={cpuBudgetCores}
                disabled={saving}
                onChange={(event) => setCpuBudgetCores(event.target.value)}
              />
            </label>
            <label>
              <span>메모리 전체 예산 (MB)</span>
              <input
                type="number"
                min={capacity.memoryReservedMb}
                max={policy.maxMemoryBudgetMb ?? undefined}
                step="1"
                value={memoryBudgetMb}
                disabled={saving}
                onChange={(event) => setMemoryBudgetMb(event.target.value)}
              />
            </label>
          </div>
          {(policy.maxCpuBudgetMillicores !== null || policy.maxMemoryBudgetMb !== null) && (
            <p className="admin-hard-ceiling">
              호스트 최대 예산: CPU {policy.maxCpuBudgetMillicores !== null
                ? `${policy.maxCpuBudgetMillicores / 1000} core`
                : "확인 불가"}, 메모리 {formatMegabytes(policy.maxMemoryBudgetMb) ?? "확인 불가"}
            </p>
          )}
          {(unavailablePolicySelections.cpu > 0 || unavailablePolicySelections.memory > 0) && (
            <p className="admin-message admin-message--warning" role="status">
              현재 프로필 카탈로그에서 제거된 이전 선택값은 저장 시 제외됩니다.
              사용 가능한 CPU와 메모리를 각각 하나 이상 다시 선택해 주세요.
            </p>
          )}
          <p className="admin-resource-note">
            배포 호스트의 최대 예산 안에서 값을 직접 추가할 수 있습니다. 저장 시 검증된
            Python 런타임마다 CPU × Memory 조합이 생성되며, 이미지와 실행 명령은 바뀌지 않습니다.
          </p>
          <div className="admin-resource-selectors">
            <section aria-labelledby="admin-cpu-values-heading">
              <h4 id="admin-cpu-values-heading">사용자 CPU 선택값</h4>
              <div className="admin-resource-add">
                <input
                  type="number"
                  aria-label="추가할 CPU 값"
                  value={cpuToAdd}
                  min="0.001"
                  max={Math.min(
                    Number(cpuBudgetCores || 0),
                    (policy.maxCpuBudgetMillicores ?? Number.MAX_SAFE_INTEGER) / 1000,
                  )}
                  step="0.001"
                  placeholder="예: 4"
                  disabled={saving}
                  onChange={(event) => setCpuToAdd(event.target.value)}
                />
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={saving || cpuToAdd === ""}
                  onClick={() => addResourceValue(
                    Number(cpuToAdd) * 1000,
                    Math.min(
                      Number(cpuBudgetCores || 0) * 1000,
                      policy.maxCpuBudgetMillicores ?? Number.MAX_SAFE_INTEGER,
                    ),
                    setSelectedCpu,
                    setCpuToAdd,
                  )}
                >CPU 선택값 추가</button>
              </div>
              <div className="admin-resource-values" aria-label="선택된 CPU 값">
                {[...selectedCpu].sort((left, right) => left - right).map((value) => (
                  <span key={value}>
                    {value / 1000} core
                    <button
                      type="button"
                      aria-label={`CPU ${value / 1000} core 제거`}
                      disabled={saving}
                      onClick={() => removeResourceValue(value, setSelectedCpu)}
                    >×</button>
                  </span>
                ))}
              </div>
            </section>
            <section aria-labelledby="admin-memory-values-heading">
              <h4 id="admin-memory-values-heading">사용자 메모리 선택값</h4>
              <div className="admin-resource-add">
                <input
                  type="number"
                  aria-label="추가할 메모리 값"
                  value={memoryToAdd}
                  min="1"
                  max={Math.min(
                    Number(memoryBudgetMb || 0),
                    policy.maxMemoryBudgetMb ?? Number.MAX_SAFE_INTEGER,
                  )}
                  step="1"
                  placeholder="예: 4096"
                  disabled={saving}
                  onChange={(event) => setMemoryToAdd(event.target.value)}
                />
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={saving || memoryToAdd === ""}
                  onClick={() => addResourceValue(
                    Number(memoryToAdd),
                    Math.min(
                      Number(memoryBudgetMb || 0),
                      policy.maxMemoryBudgetMb ?? Number.MAX_SAFE_INTEGER,
                    ),
                    setSelectedMemory,
                    setMemoryToAdd,
                  )}
                >메모리 선택값 추가</button>
              </div>
              <div className="admin-resource-values" aria-label="선택된 메모리 값">
                {[...selectedMemory].sort((left, right) => left - right).map((value) => (
                  <span key={value}>
                    {formatMegabytes(value)}
                    <button
                      type="button"
                      aria-label={`메모리 ${formatMegabytes(value)} 제거`}
                      disabled={saving}
                      onClick={() => removeResourceValue(value, setSelectedMemory)}
                    >×</button>
                  </span>
                ))}
              </div>
            </section>
          </div>
          <section className="admin-kernel-policy" aria-labelledby="admin-kernel-policy-heading">
            <div>
              <h4 id="admin-kernel-policy-heading">유휴 커널 자동 정리</h4>
              <p>
                Jupyter가 설정 시간 동안 유휴로 판단한 커널 프로세스만 종료합니다.
                Jupyter가 실행 중(busy)으로 인식하는 셀은 종료하지 않습니다.
              </p>
            </div>
            {policy.kernelIdleTimeoutBounds === null ||
              policy.kernelIdleTimeoutSeconds === null ? (
                <p className="admin-message admin-message--warning" role="alert">
                  서버에서 유휴 커널 정책 범위를 확인할 수 없어 저장할 수 없습니다.
                </p>
              ) : (
                <div className="admin-kernel-policy__controls">
                  <label className="admin-kernel-policy__toggle">
                    <input
                      type="checkbox"
                      checked={kernelCullingEnabled}
                      disabled={saving}
                      onChange={(event) => setKernelCullingEnabled(event.target.checked)}
                    />
                    <span>자동 정리 사용</span>
                  </label>
                  <label>
                    <span>유휴 시간 (분)</span>
                    <input
                      type="number"
                      aria-label="커널 유휴 시간"
                      min={policy.kernelIdleTimeoutBounds.minSeconds / 60}
                      max={policy.kernelIdleTimeoutBounds.maxSeconds / 60}
                      step={policy.kernelIdleTimeoutBounds.stepSeconds / 60}
                      value={kernelIdleMinutes}
                      disabled={saving || !kernelCullingEnabled}
                      onChange={(event) => setKernelIdleMinutes(event.target.value)}
                    />
                  </label>
                </div>
              )}
            <p className="admin-kernel-policy__warning">
              커널이 종료되면 메모리의 변수·모델·실행 상태는 사라지지만 Notebook과 저장한
              파일은 유지됩니다. 브라우저 탭이 열려 있어도 유휴 상태면 정리되며, 현재 실행
              중인 환경에는 재시작 후 새 설정이 적용됩니다. 셀이 반환된 뒤 별도 background
              process로 실행한 작업은 busy 보호 대상이 아닙니다.
            </p>
          </section>
          <button
            className="button button--primary"
            type="button"
            disabled={saving || policy.kernelIdleTimeoutBounds === null ||
              policy.kernelIdleTimeoutSeconds === null}
            onClick={() => void savePolicy()}
          >
            {saving ? "정책 저장 중" : "관리 정책 저장"}
          </button>
        </div>
      )}

      {view === "profiles" && (
        <AdminProfileManager
          resourcePolicy={policy}
          onProfilesChanged={onPlatformChanged}
        />
      )}

      {view === "workspaces" && <div className="admin-workspaces">
        <div className="admin-workspaces__heading">
          <div>
            <h3>전체 사용자 환경</h3>
            <p>개인 환경 데이터 삭제는 되돌릴 수 없습니다. 공유 디렉터리는 삭제 대상이 아닙니다.</p>
          </div>
          <span className="item-count" aria-label={`전체 환경 ${total}개`}>{total}</span>
        </div>
        {loading && workspaces.length === 0 ? (
          <p className="admin-table-empty"><span className="spinner" aria-hidden="true" /> 불러오는 중</p>
        ) : workspaces.length === 0 ? (
          <p className="admin-table-empty">생성된 환경이 없습니다.</p>
        ) : (
          <div className="admin-workspace-table-wrap" role="region" aria-label="전체 사용자 환경 표" tabIndex={0}>
            <table className="admin-workspace-table">
              <thead>
                <tr>
                  <th scope="col">환경</th>
                  <th scope="col">사용자</th>
                  <th scope="col">상태</th>
                  <th scope="col">자원</th>
                  <th scope="col">갱신</th>
                  <th scope="col">작업</th>
                </tr>
              </thead>
              <tbody>
                {workspaces.map((workspace) => {
                  const busy = busyWorkspaceIds.has(workspace.id);
                  const trackedOperation = operationsByWorkspace[workspace.id];
                  const operationPending = Boolean(
                    trackedOperation && adminOperationNeedsPolling(trackedOperation),
                  );
                  const deletionPending = workspace.desiredState === "DELETED" &&
                    (["PENDING", "RUNNING"].includes(workspace.deletionStatus ?? "") ||
                      (operationPending && trackedOperation.operationType === "DELETE"));
                  const deletionRetryAvailable = workspace.desiredState === "DELETED" &&
                    workspace.canRetryDelete && (
                      workspace.deletionStatus === "FAILED" || workspace.lastErrorCode === "AUTH_REQUIRED"
                    );
                  const { running, canStart, canStop, lifecycleBusy } =
                    adminWorkspaceLifecycleControls(workspace, {
                      busy,
                      operationPending,
                      deletionPending,
                    });
                  return (
                    <tr key={workspace.id}>
                      <td>
                        <strong>{workspace.name}</strong>
                        <code>{workspace.id.slice(0, 8)}</code>
                      </td>
                      <td>
                        <strong>{workspace.owner?.displayName ?? workspace.owner?.username ?? "알 수 없음"}</strong>
                        {workspace.owner?.username && <span>{workspace.owner.username}</span>}
                      </td>
                      <td>
                        <StatusBadge
                          status={workspaceStatusPresentation(workspace.observedState)}
                          stale={workspace.stale}
                        />
                        {workspace.restartRequired && <span className="restart-chip">재시작 필요</span>}
                        {deletionPending && <span className="deletion-chip">삭제 진행 중</span>}
                        {deletionRetryAvailable && <span className="deletion-chip deletion-chip--failed">삭제 실패</span>}
                        {trackedOperation && (
                          <AdminOperationSummary
                            operation={trackedOperation}
                            workspaceProgress={workspace.progressPercent}
                          />
                        )}
                      </td>
                      <td>
                        <span>CPU {workspace.cpuLimit ?? "-"}</span>
                        <span>메모리 {formatMegabytes(workspace.memoryLimitMb) ?? "-"}</span>
                      </td>
                      <td>{formatDate(workspace.updatedAt ?? workspace.createdAt) ?? "-"}</td>
                      <td>
                        <div className="admin-row-actions">
                          {running ? (
                            <a
                              className="button button--primary"
                              href={portalApi.adminLaunchUrl(workspace.id)}
                              target="_blank"
                              rel="noopener noreferrer"
                            >열기</a>
                          ) : (
                            <button className="button button--primary" type="button" disabled>열기</button>
                          )}
                          {workspace.restartRequired && running && (
                            <button
                              className="button button--primary"
                              type="button"
                              disabled={lifecycleBusy}
                              onClick={() => void runAction(workspace, "restart")}
                            >변경사항 적용 (재시작)</button>
                          )}
                          {canStop ? (
                            <button
                              className="button button--secondary"
                              type="button"
                              disabled={lifecycleBusy}
                              onClick={() => void runAction(workspace, "stop")}
                            >중지</button>
                          ) : (
                            <button
                              className="button button--secondary"
                              type="button"
                              disabled={lifecycleBusy || !canStart}
                              onClick={() => void runAction(workspace, "start")}
                            >시작</button>
                          )}
                          <button
                            className="button button--danger"
                            type="button"
                            disabled={busy || deletionPending || (
                              workspace.desiredState === "DELETED" && !deletionRetryAvailable
                            )}
                            onClick={() => void runAction(workspace, "delete")}
                          >{deletionRetryAvailable ? "삭제 재시도" : "삭제"}</button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>}
    </section>
  );
}
