import { useEffect, useMemo, useState } from "react";
import type {
  Capacity,
  UserStatus,
  WorkspaceProfile,
} from "../api/types";
import { formatMegabytes } from "../lib/display";
import { cpuLimitToMillicores } from "../lib/profiles";

interface CreateWorkspaceProps {
  profiles: WorkspaceProfile[];
  profilesLoading: boolean;
  profilesError: string | null;
  capacity: Capacity | null;
  capacityError: string | null;
  userStatus: UserStatus;
  creating: boolean;
  onRetry: () => Promise<void>;
  onCreate: (
    profileId: string,
    profileVersion: number,
    name?: string,
  ) => Promise<boolean>;
}

export function resolveWorkspaceProfile(
  profiles: WorkspaceProfile[],
  kernelKey: string,
  cpuLimit: string,
  memoryLimitMb: number | null,
  acceleratorKey?: string,
): WorkspaceProfile | null {
  const matches = profiles.filter((profile) =>
    workspaceKernelKey(profile) === kernelKey &&
    profile.cpuLimit === cpuLimit &&
    profile.memoryLimitMb === memoryLimitMb &&
    (acceleratorKey === undefined ||
      workspaceAcceleratorKey(profile) === acceleratorKey));
  // A selector tuple must resolve to one immutable server-side offer. Duplicate
  // offers are ambiguous, so fail closed instead of depending on response order.
  return matches.length === 1 ? matches[0] : null;
}

export function workspaceAcceleratorKey(
  profile: Pick<
    WorkspaceProfile,
    | "acceleratorKind"
    | "gpuCount"
    | "cudaVersion"
    | "gpuFramework"
    | "gpuFrameworkVersion"
  >,
): string {
  return [
    profile.acceleratorKind,
    profile.gpuCount,
    profile.cudaVersion ?? "-",
    profile.gpuFramework ?? "-",
    profile.gpuFrameworkVersion ?? "-",
  ].join(":");
}

function defaultWorkspaceProfile(profiles: WorkspaceProfile[]): WorkspaceProfile | undefined {
  return profiles.find((profile) => profile.acceleratorKind === "none") ?? profiles[0];
}

export function filterWorkspaceProfiles(
  profiles: WorkspaceProfile[],
  selection: {
    acceleratorKey: string;
    kernelKey?: string;
    cpuLimit?: string;
  },
): WorkspaceProfile[] {
  return profiles.filter((profile) =>
    workspaceAcceleratorKey(profile) === selection.acceleratorKey &&
    (selection.kernelKey === undefined ||
      workspaceKernelKey(profile) === selection.kernelKey) &&
    (selection.cpuLimit === undefined || profile.cpuLimit === selection.cpuLimit));
}

function acceleratorLabel(profile: WorkspaceProfile): string {
  return profile.acceleratorKind === "nvidia"
    ? `NVIDIA GPU 1개 · CUDA ${profile.cudaVersion} · PyTorch ${profile.gpuFrameworkVersion}`
    : "CPU 전용";
}

export function workspaceKernelKey(
  profile: Pick<WorkspaceProfile, "kernelName" | "pythonVersion">,
): string {
  return `${profile.kernelName}:${profile.pythonVersion}`;
}

export function CreateWorkspace({
  profiles,
  profilesLoading,
  profilesError,
  capacity,
  capacityError,
  userStatus,
  creating,
  onRetry,
  onCreate,
}: CreateWorkspaceProps) {
  const initialProfile = defaultWorkspaceProfile(profiles);
  const [selectedAccelerator, setSelectedAccelerator] = useState(() =>
    initialProfile ? workspaceAcceleratorKey(initialProfile) : "");
  const [selectedKernel, setSelectedKernel] = useState(() =>
    initialProfile ? workspaceKernelKey(initialProfile) : "");
  const [selectedCpu, setSelectedCpu] = useState(() =>
    initialProfile?.cpuLimit ?? "");
  const [selectedMemory, setSelectedMemory] = useState<number | null>(() =>
    initialProfile?.memoryLimitMb ?? null);
  const [workspaceName, setWorkspaceName] = useState("");

  const acceleratorOptions = useMemo(() => {
    const values = new Map<string, { key: string; label: string }>();
    for (const profile of profiles) {
      const key = workspaceAcceleratorKey(profile);
      values.set(key, { key, label: acceleratorLabel(profile) });
    }
    return [...values.values()].sort((left, right) => {
      const leftCpu = left.key.startsWith("none:");
      const rightCpu = right.key.startsWith("none:");
      if (leftCpu !== rightCpu) return leftCpu ? -1 : 1;
      return left.key.localeCompare(right.key);
    });
  }, [profiles]);

  useEffect(() => {
    if (!acceleratorOptions.some((option) => option.key === selectedAccelerator)) {
      setSelectedAccelerator(acceleratorOptions[0]?.key ?? "");
    }
  }, [acceleratorOptions, selectedAccelerator]);

  const kernelOptions = useMemo(() => {
    const values = new Map<string, { key: string; label: string; version: string }>();
    for (const profile of filterWorkspaceProfiles(profiles, {
      acceleratorKey: selectedAccelerator,
    })) {
      const key = workspaceKernelKey(profile);
      values.set(key, {
        key,
        label: profile.kernelDisplayName,
        version: profile.pythonVersion,
      });
    }
    return [...values.values()].sort((left, right) =>
      left.version.localeCompare(right.version) || left.key.localeCompare(right.key));
  }, [profiles, selectedAccelerator]);

  useEffect(() => {
    if (!kernelOptions.some((option) => option.key === selectedKernel)) {
      setSelectedKernel(kernelOptions[0]?.key ?? "");
    }
  }, [kernelOptions, selectedKernel]);

  const cpuOptions = useMemo(() => [...new Set(filterWorkspaceProfiles(profiles, {
    acceleratorKey: selectedAccelerator,
    kernelKey: selectedKernel,
  })
    .map((profile) => profile.cpuLimit))]
    .sort((left, right) =>
      (cpuLimitToMillicores(left) ?? Number.MAX_SAFE_INTEGER) -
      (cpuLimitToMillicores(right) ?? Number.MAX_SAFE_INTEGER)),
  [profiles, selectedAccelerator, selectedKernel]);

  useEffect(() => {
    if (!cpuOptions.includes(selectedCpu)) setSelectedCpu(cpuOptions[0] ?? "");
  }, [cpuOptions, selectedCpu]);

  const memoryOptions = useMemo(() => [...new Set(filterWorkspaceProfiles(profiles, {
    acceleratorKey: selectedAccelerator,
    kernelKey: selectedKernel,
    cpuLimit: selectedCpu,
  })
    .map((profile) => profile.memoryLimitMb))].sort((left, right) => left - right),
  [profiles, selectedAccelerator, selectedCpu, selectedKernel]);

  useEffect(() => {
    if (selectedMemory === null || !memoryOptions.includes(selectedMemory)) {
      setSelectedMemory(memoryOptions[0] ?? null);
    }
  }, [memoryOptions, selectedMemory]);

  const selected = useMemo(
    () => resolveWorkspaceProfile(
      profiles,
      selectedKernel,
      selectedCpu,
      selectedMemory,
      selectedAccelerator,
    ),
    [profiles, selectedAccelerator, selectedCpu, selectedKernel, selectedMemory],
  );
  const capacityUnknown = capacity === null;
  const userAtLimit = capacity
    ? capacity.workspaceUsed >= capacity.workspaceLimit
    : false;
  const controlsDisabled = creating || profilesLoading || profiles.length === 0;
  const trimmedWorkspaceName = workspaceName.trim();
  const workspaceNameValid = trimmedWorkspaceName.length <= 80;
  const canCreate =
    userStatus === "ACTIVE" &&
    Boolean(selected) &&
    !profilesLoading &&
    !profilesError &&
    !capacityUnknown &&
    !capacityError &&
    !userAtLimit &&
    workspaceNameValid &&
    !creating;

  let unavailableReason: string | null = null;
  if (userStatus === "PROVISIONING") unavailableReason = "계정 준비가 완료되면 생성할 수 있습니다.";
  else if (userStatus === "DISABLED") unavailableReason = "비활성화된 계정입니다.";
  else if (profilesLoading) unavailableReason = "사용 가능한 설정을 불러오고 있습니다.";
  else if (profilesError) unavailableReason = profilesError;
  else if (!selected) unavailableReason = "사용 가능한 설정 조합이 없습니다.";
  else if (capacityError) unavailableReason = capacityError;
  else if (capacityUnknown) unavailableReason = "용량 정보를 확인한 뒤 생성할 수 있습니다.";
  else if (userAtLimit) unavailableReason = "내 환경 한도에 도달했습니다.";
  else if (!workspaceNameValid) unavailableReason = "환경 이름은 80자 이하여야 합니다.";

  const retryAvailable = Boolean(profilesError || capacityError);
  const defaultWorkspaceName = capacity?.nextDefaultWorkspaceName ?? "환경-N";

  const submit = async () => {
    if (!selected || !canCreate) return;
    const created = await onCreate(
      selected.id,
      selected.version,
      trimmedWorkspaceName || undefined,
    );
    if (created) {
      setWorkspaceName("");
    }
  };

  return (
    <section
      className="panel create-panel"
      aria-labelledby="create-heading"
      aria-busy={profilesLoading || creating}
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">New workspace</p>
          <h2 id="create-heading">개발환경 만들기</h2>
        </div>
        <span className="step-chip">01</span>
      </div>

      {profiles.length > 0 ? (
        <div className="profile-axis-grid" aria-label="개발환경 설정 선택">
          <label>
            <span className="field-label">가속기</span>
            <div className="select-wrap">
              <select
                id="accelerator-select"
                value={selectedAccelerator}
                disabled={controlsDisabled}
                onChange={(event) => setSelectedAccelerator(event.target.value)}
              >
                {acceleratorOptions.map((option) => (
                  <option key={option.key} value={option.key}>{option.label}</option>
                ))}
              </select>
              <span aria-hidden="true">⌄</span>
            </div>
          </label>
          <label>
            <span className="field-label">Python 커널</span>
            <div className="select-wrap">
              <select
                id="kernel-select"
                value={selectedKernel}
                disabled={controlsDisabled}
                onChange={(event) => setSelectedKernel(event.target.value)}
              >
                {kernelOptions.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.label} · Python {option.version}
                  </option>
                ))}
              </select>
              <span aria-hidden="true">⌄</span>
            </div>
          </label>
          <label>
            <span className="field-label">CPU</span>
            <div className="select-wrap">
              <select
                id="cpu-select"
                value={selectedCpu}
                disabled={controlsDisabled}
                onChange={(event) => setSelectedCpu(event.target.value)}
              >
                {cpuOptions.map((value) => (
                  <option key={value} value={value}>{value} core</option>
                ))}
              </select>
              <span aria-hidden="true">⌄</span>
            </div>
          </label>
          <label>
            <span className="field-label">Memory</span>
            <div className="select-wrap">
              <select
                id="memory-select"
                value={selectedMemory ?? ""}
                disabled={controlsDisabled}
                onChange={(event) => setSelectedMemory(Number(event.target.value))}
              >
                {memoryOptions.map((value) => (
                  <option key={value} value={value}>{formatMegabytes(value)}</option>
                ))}
              </select>
              <span aria-hidden="true">⌄</span>
            </div>
          </label>
        </div>
      ) : (
        <div className="profile-empty" role={profilesError ? "alert" : "status"}>
          {profilesLoading && <span className="spinner" aria-hidden="true" />}
          <p>{profilesLoading
            ? "사용 가능한 설정을 불러오고 있습니다."
            : profilesError ?? "관리자가 사용 가능한 설정 조합을 준비하고 있습니다."}</p>
        </div>
      )}

      <div className="workspace-name-field">
        <label className="field-label" htmlFor="workspace-name">환경 이름</label>
        <input
          id="workspace-name"
          type="text"
          maxLength={80}
          value={workspaceName}
          placeholder={defaultWorkspaceName}
          disabled={creating}
          onChange={(event) => setWorkspaceName(event.target.value)}
        />
        <p>비워두면 <strong>{defaultWorkspaceName}</strong>으로 자동 지정합니다.</p>
      </div>

      <div className="profile-summary" aria-live="polite">
        {selected ? (
          <>
            <div>
              <strong>{selected.name}</strong>
              <span>v{selected.version}</span>
            </div>
            {selected.description && <p>{selected.description}</p>}
            <ul className="resource-list" aria-label="선택한 개발환경 설정">
              <li>기본 커널 {selected.kernelDisplayName}</li>
              <li>기본 노트북/터미널 Python {selected.pythonVersion}</li>
              {selected.acceleratorKind === "nvidia" ? (
                <li>
                  NVIDIA GPU 1개 · CUDA {selected.cudaVersion} · PyTorch {selected.gpuFrameworkVersion}
                </li>
              ) : (
                <li>가속기 CPU 전용</li>
              )}
              <li>CPU {selected.cpuLimit}</li>
              <li>메모리 {formatMegabytes(selected.memoryLimitMb)}</li>
              {selected.privateDiskQuotaEnforced ? (
                <li>저장공간 하드 제한 {formatMegabytes(selected.privateDiskLimitMb)}</li>
              ) : (
                <>
                  <li>저장공간 개별 하드 제한 없음 (호스트 가용량까지)</li>
                  <li className="resource-warning">
                    호스트 저장공간은 모든 개발환경이 함께 사용합니다.
                  </li>
                </>
              )}
            </ul>
          </>
        ) : (
          <p>설정 조합을 확인할 수 없습니다.</p>
        )}
      </div>

      <p className="form-hint">
        환경은 중지된 상태로 생성됩니다. 생성 후 환경 카드에서 환경변수를 설정한 다음 시작하세요.
      </p>

      <button
        className="button button--primary button--wide"
        type="button"
        disabled={!canCreate}
        onClick={() => void submit()}
      >
        {creating ? (
          <><span className="spinner" aria-hidden="true" /> 요청하는 중</>
        ) : (
          <>환경 생성 <span aria-hidden="true">→</span></>
        )}
      </button>
      {unavailableReason && <p className="form-hint" role="status">{unavailableReason}</p>}
      {retryAvailable && (
        <button
          className="text-button form-retry"
          type="button"
          disabled={profilesLoading}
          onClick={() => void onRetry()}
        >
          설정 다시 불러오기
        </button>
      )}
    </section>
  );
}
