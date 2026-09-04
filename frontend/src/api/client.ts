import type {
  AdminCapacity,
  AdminAuditEvent,
  AdminOperationPage,
  AdminProfileCatalog,
  AdminWorkspaceProfile,
  AdminWorkspacePage,
  ApiErrorBody,
  Capacity,
  EnvironmentScope,
  EnvironmentVariable,
  EnvironmentVariableList,
  EnvironmentVariableMutation,
  InternalEgressPolicySnapshot,
  InternalEgressRule,
  Operation,
  PortalUser,
  ProvisioningStatus,
  ResourcePolicy,
  ResourcePolicyUpdate,
  RuntimeProfileTemplate,
  UserProvisioning,
  UserStatus,
  Workspace,
  WorkspaceMutationResult,
  WorkspaceProfile,
  WorkspaceResourceUsage,
} from "./types";
import { cpuLimitToMillicores } from "../lib/profiles";

const MAX_NVIDIA_GPU_COUNT = 64;

type JsonRecord = Record<string, unknown>;

const configuredBase = import.meta.env.VITE_API_BASE_URL?.trim();
export const API_BASE = (configuredBase || "/api/v1").replace(/\/$/, "");
let csrfToken: string | null = null;
const ADMIN_PAGE_LIMIT = 100;
const MAX_ADMIN_PAGED_ITEMS = 10_000;
const KERNEL_IDLE_TIMEOUT_MIN_SECONDS = 300;
const KERNEL_IDLE_TIMEOUT_MAX_SECONDS = 604_800;
const KERNEL_IDLE_TIMEOUT_STEP_SECONDS = 60;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(status: number, body: ApiErrorBody = {}) {
    super(body.message || defaultErrorMessage(status));
    this.name = "ApiError";
    this.status = status;
    this.code = body.code || defaultErrorCode(status);
    this.requestId = body.requestId ?? null;
  }
}

function defaultErrorCode(status: number): string {
  if (status === 401) return "AUTH_REQUIRED";
  if (status === 403) return "FORBIDDEN";
  if (status === 404) return "NOT_FOUND";
  if (status === 409) return "CONFLICT";
  if (status === 429) return "CAPACITY_LIMIT";
  if (status >= 500) return "SERVICE_UNAVAILABLE";
  return "REQUEST_FAILED";
}

function defaultErrorMessage(status: number): string {
  if (status === 401) return "로그인이 필요합니다.";
  if (status === 403) return "이 작업을 수행할 권한이 없습니다.";
  if (status === 404) return "요청한 항목을 찾을 수 없습니다.";
  if (status === 409) return "현재 상태에서는 요청을 처리할 수 없습니다.";
  if (status === 429) return "현재 실행 가능한 환경 수가 모두 사용 중입니다.";
  if (status >= 500) return "서비스에 일시적으로 연결할 수 없습니다.";
  return "요청을 처리하지 못했습니다.";
}

function asRecord(value: unknown): JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function optionalRecord(value: unknown): JsonRecord | null {
  const record = asRecord(value);
  return Object.keys(record).length > 0 ? record : null;
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function normalizeLogoutRedirect(payload: unknown): string {
  const record = asRecord(payload);
  const candidate = nullableString(record.redirect_url);
  if (!candidate) throw invalidMutationResponse();
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw invalidMutationResponse();
  }
  const localHttp = parsed.protocol === "http:" &&
    (parsed.hostname === "localhost" || parsed.hostname.endsWith(".localhost"));
  if (
    (parsed.protocol !== "https:" && !localHttp) ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/hub/logout" ||
    parsed.search ||
    parsed.hash
  ) {
    throw invalidMutationResponse();
  }
  return parsed.toString();
}

function numberValue(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value > 0
    ? value
    : null;
}

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0
    ? value
    : null;
}

function utcInstant(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?Z$/.exec(
    value,
  );
  const parsed = new Date(value);
  if (!match || !Number.isFinite(parsed.getTime()) ||
    parsed.getUTCFullYear() !== Number(match[1]) ||
    parsed.getUTCMonth() + 1 !== Number(match[2]) ||
    parsed.getUTCDate() !== Number(match[3]) ||
    parsed.getUTCHours() !== Number(match[4]) ||
    parsed.getUTCMinutes() !== Number(match[5]) ||
    parsed.getUTCSeconds() !== Number(match[6])) {
    return null;
  }
  return value;
}

function normalizeWorkspaceResourceUsage(
  value: unknown,
): WorkspaceResourceUsage | null {
  if (value === null || value === undefined) return null;
  const usage = optionalRecord(value);
  if (usage === null) return null;
  const cpuMillicores = nonNegativeInteger(usage.cpu_millicores);
  const memoryBytes = nonNegativeInteger(usage.memory_bytes);
  const memoryLimitBytes = positiveInteger(usage.memory_limit_bytes);
  const observedAt = utcInstant(usage.observed_at);
  const expiresAt = utcInstant(usage.expires_at);
  if (cpuMillicores === null || memoryBytes === null || memoryLimitBytes === null ||
    memoryBytes > memoryLimitBytes || observedAt === null || expiresAt === null ||
    Date.parse(expiresAt) <= Date.parse(observedAt) ||
    typeof usage.stale !== "boolean") {
    return null;
  }
  return {
    cpuMillicores,
    memoryBytes,
    memoryLimitBytes,
    observedAt,
    expiresAt,
    stale: usage.stale,
  };
}

function gpuPoolCount(value: unknown): number {
  const count = nonNegativeInteger(value);
  return count !== null && count <= MAX_NVIDIA_GPU_COUNT ? count : 0;
}

function kernelIdleTimeout(value: unknown): number | null {
  const seconds = nonNegativeInteger(value);
  return seconds !== null && (
      seconds === 0 || (
        seconds >= KERNEL_IDLE_TIMEOUT_MIN_SECONDS &&
        seconds <= KERNEL_IDLE_TIMEOUT_MAX_SECONDS &&
        seconds % KERNEL_IDLE_TIMEOUT_STEP_SECONDS === 0
      )
    )
    ? seconds
    : null;
}

function integerArray(value: unknown, positive = true): number[] {
  if (!Array.isArray(value)) return [];
  const values = value.filter((item): item is number =>
    typeof item === "number" &&
    Number.isSafeInteger(item) &&
    (positive ? item > 0 : item >= 0),
  );
  return [...new Set(values)].sort((left, right) => left - right);
}

function normalizeAccelerator(value: JsonRecord): Pick<
  WorkspaceProfile,
  | "acceleratorKind"
  | "gpuCount"
  | "cudaVersion"
  | "gpuFramework"
  | "gpuFrameworkVersion"
> | null {
  const entirelyMissing = [
    value.accelerator_kind,
    value.gpu_count,
    value.cuda_version,
    value.gpu_framework,
    value.gpu_framework_version,
  ].every((item) => item === undefined);
  const kind = entirelyMissing ? "none" : value.accelerator_kind;
  const count = entirelyMissing ? 0 : nonNegativeInteger(value.gpu_count);
  if (
    kind === "none" && count === 0 &&
    (value.cuda_version === undefined || value.cuda_version === null) &&
    (value.gpu_framework === undefined || value.gpu_framework === null) &&
    (value.gpu_framework_version === undefined || value.gpu_framework_version === null)
  ) {
    return {
      acceleratorKind: "none",
      gpuCount: 0,
      cudaVersion: null,
      gpuFramework: null,
      gpuFrameworkVersion: null,
    };
  }
  const cudaVersion = trimmedString(value.cuda_version, 16);
  const frameworkVersion = trimmedString(value.gpu_framework_version, 32);
  if (
    kind !== "nvidia" || count === null || count < 1 ||
    count > MAX_NVIDIA_GPU_COUNT ||
    value.gpu_framework !== "pytorch" ||
    !cudaVersion || !/^(?:[1-9]\d*)\.(?:0|[1-9]\d*)$/.test(cudaVersion) ||
    !frameworkVersion ||
    !/^(?:[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$/.test(frameworkVersion)
  ) return null;
  return {
    acceleratorKind: "nvidia",
    gpuCount: count,
    cudaVersion,
    gpuFramework: "pytorch",
    gpuFrameworkVersion: frameworkVersion,
  };
}

function trimmedString(value: unknown, maxLength: number): string | null {
  if (typeof value !== "string") return null;
  const candidate = value.trim();
  return candidate.length > 0 && candidate.length <= maxLength ? candidate : null;
}

function positiveCpuLimit(value: unknown): string | null {
  const candidate = trimmedString(value, 32);
  return candidate && cpuLimitToMillicores(candidate) !== null ? candidate : null;
}

function booleanValue(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function nullableBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function pickArray(payload: unknown, ...keys: string[]): unknown[] {
  if (Array.isArray(payload)) return payload;
  const record = asRecord(payload);
  for (const key of keys) {
    if (Array.isArray(record[key])) return record[key] as unknown[];
  }
  return [];
}

function extractErrorBody(payload: unknown): ApiErrorBody {
  const root = asRecord(payload);
  const detail = optionalRecord(root.detail);
  const error = optionalRecord(root.error);
  const source = error ?? detail ?? root;

  return {
    code: nullableString(source.code) ?? undefined,
    message:
      nullableString(source.message) ??
      (typeof root.detail === "string" ? root.detail : undefined),
    requestId:
      nullableString(source.request_id) ??
      nullableString(root.request_id) ??
      undefined,
  };
}

async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.method && init.method !== "GET" && init.method !== "HEAD" && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, {
      code: "NETWORK_ERROR",
      message: "포털 API에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요.",
    });
  }

  const contentType = response.headers.get("content-type") ?? "";
  let payload: unknown = null;
  if (response.status !== 204) {
    try {
      if (contentType.includes("application/json")) {
        payload = await response.json();
      } else {
        const text = await response.text();
        if (response.ok) {
          throw new ApiError(0, {
            code: "INVALID_RESPONSE",
            message: "포털 API 응답 형식이 올바르지 않습니다. 같은 요청을 다시 시도해 주세요.",
          });
        }
        payload = text ? { message: text } : null;
      }
    } catch (error) {
      if (error instanceof ApiError) throw error;
      const errorStatus = response.ok ? 0 : response.status;
      if (errorStatus === 401) {
        csrfToken = null;
        clearPendingMutationKeys();
      }
      throw new ApiError(errorStatus, {
        code: response.ok ? "INVALID_RESPONSE" : defaultErrorCode(errorStatus),
        message: response.ok
          ? "포털 API 응답을 확인하지 못했습니다. 같은 요청을 다시 시도해 주세요."
          : defaultErrorMessage(errorStatus),
      });
    }
  }

  if (!response.ok) {
    if (response.status === 401) {
      csrfToken = null;
      clearPendingMutationKeys();
    }
    const body = extractErrorBody(payload);
    body.requestId ||= response.headers.get("x-request-id") ?? undefined;
    throw new ApiError(response.status, body);
  }
  return payload;
}

export function normalizeUser(payload: unknown): PortalUser {
  const root = asRecord(payload);
  const value = optionalRecord(root.user) ?? root;
  const username = stringValue(value.username, stringValue(value.hub_username));
  const status = stringValue(value.status, "PROVISIONING").toUpperCase() as UserStatus;
  return {
    id: stringValue(value.id, username),
    username,
    displayName: nullableString(value.display_name),
    role: stringValue(value.role, "USER").toUpperCase(),
    status,
    provisioning: normalizeProvisioning(payload, status),
  };
}

const provisioningStatuses = new Set<ProvisioningStatus>([
  "MANUAL_REQUIRED",
  "NOT_REQUESTED",
  "PENDING",
  "RUNNING",
  "SUCCEEDED",
  "FAILED",
]);

function fallbackProvisioningStatus(userStatus: UserStatus): ProvisioningStatus {
  return userStatus === "ACTIVE" ? "SUCCEEDED" : "MANUAL_REQUIRED";
}

export function normalizeProvisioning(
  payload: unknown,
  userStatus: UserStatus = "PROVISIONING",
): UserProvisioning {
  const root = asRecord(payload);
  const value = optionalRecord(root.provisioning);
  const candidate = value
    ? stringValue(value.status).toUpperCase() as ProvisioningStatus
    : fallbackProvisioningStatus(userStatus);
  const status = provisioningStatuses.has(candidate)
    ? candidate
    : fallbackProvisioningStatus(userStatus);
  return {
    status,
    attempts: value ? Math.max(0, Math.trunc(numberValue(value.attempts, 0))) : 0,
    errorCode: value ? nullableString(value.error_code) : null,
    errorSummary: value ? nullableString(value.error_summary) : null,
    requestedAt: value ? nullableString(value.requested_at) : null,
    completedAt: value ? nullableString(value.completed_at) : null,
  };
}

export function normalizeProfiles(payload: unknown): WorkspaceProfile[] {
  const candidates: WorkspaceProfile[] = [];
  for (const item of pickArray(payload, "items", "profiles")) {
    const value = asRecord(item);
    const id = trimmedString(value.id, 64);
    const version = positiveInteger(value.version);
    const name = trimmedString(value.name, 128) ?? id;
    const kernelName = trimmedString(value.kernel_name, 128);
    const kernelDisplayName = trimmedString(value.kernel_display_name, 128) ?? kernelName;
    const pythonVersion = trimmedString(value.python_version, 64);
    const cpuLimit = positiveCpuLimit(value.cpu_limit);
    const memoryLimitMb = positiveInteger(value.memory_limit_mb);
    const accelerator = normalizeAccelerator(value);
    const privateDiskQuotaEnforced = nullableBoolean(
      value.private_disk_quota_enforced,
    );
    const configuredPrivateDiskLimitMb = positiveInteger(
      value.private_disk_limit_mb,
    );
    const privateDiskLimitMb = privateDiskQuotaEnforced === false
      ? null
      : configuredPrivateDiskLimitMb;
    const privateDiskShapeValid = privateDiskQuotaEnforced === false
      ? value.private_disk_limit_mb === null || configuredPrivateDiskLimitMb !== null
      : configuredPrivateDiskLimitMb !== null;
    const enabled = value.enabled === undefined || value.enabled === true;
    if (!id || !/^[a-z][a-z0-9-]*$/.test(id) || !version || !name ||
      !kernelName || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(kernelName) ||
      !kernelDisplayName || !pythonVersion ||
      !/^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$/.test(pythonVersion) ||
      !cpuLimit || !memoryLimitMb || privateDiskQuotaEnforced === null ||
      !privateDiskShapeValid || !accelerator || !enabled) continue;
    candidates.push({
      id,
      version,
      name,
      description: nullableString(value.description),
      kernelName,
      kernelDisplayName,
      pythonVersion,
      ...accelerator,
      cpuLimit,
      memoryLimitMb,
      privateDiskLimitMb,
      privateDiskQuotaEnforced,
      enabled,
    });
  }

  const labelsByKernel = new Map<string, Set<string>>();
  for (const profile of candidates) {
    const labels = labelsByKernel.get(profile.kernelName) ?? new Set<string>();
    labels.add(profile.kernelDisplayName);
    labelsByKernel.set(profile.kernelName, labels);
  }
  const seen = new Set<string>();
  return candidates.filter((profile) => {
    const key = `${profile.id}:${profile.version}`;
    const unambiguousKernel = labelsByKernel.get(profile.kernelName)?.size === 1;
    if (!unambiguousKernel || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function normalizeCapacity(payload: unknown): Capacity {
  const root = asRecord(payload);
  const user = optionalRecord(root.user) ?? optionalRecord(root.workspaces) ?? {};
  const platform =
    optionalRecord(root.global) ??
    optionalRecord(root.platform) ??
    optionalRecord(root.active) ??
    {};
  const resources = optionalRecord(platform.resources) ?? {};
  const cpu = optionalRecord(resources.cpu_millicores) ?? {};
  const memory = optionalRecord(resources.memory_mb) ?? {};
  const gpu = optionalRecord(resources.gpu_count) ?? {};
  return {
    workspaceUsed: numberValue(
      root.workspace_used ?? root.workspace_count ?? user.used ?? user.count,
      0,
    ),
    workspaceLimit: numberValue(
      root.workspace_limit ?? user.limit,
      0,
    ),
    nextDefaultWorkspaceName: nullableString(
      root.next_default_workspace_name ?? user.next_default_workspace_name,
    ),
    activeUsed: numberValue(
      root.active_used ??
        root.active_count ??
        platform.active ??
        platform.used ??
        platform.count,
      0,
    ),
    activeLimit: numberValue(root.active_limit ?? platform.limit, 0),
    cpuReservedMillicores: nonNegativeInteger(cpu.reserved),
    cpuBudgetMillicores: positiveInteger(cpu.limit),
    memoryReservedMb: nonNegativeInteger(memory.reserved),
    memoryBudgetMb: positiveInteger(memory.limit),
    gpuReservedCount: nonNegativeInteger(gpu.reserved),
    gpuBudgetCount: nonNegativeInteger(gpu.limit),
    kernelIdleTimeoutSeconds: kernelIdleTimeout(
      platform.kernel_idle_timeout_seconds,
    ),
    executionHostHealthy:
      typeof root.execution_host_healthy === "boolean"
        ? root.execution_host_healthy
        : null,
  };
}

export function normalizeWorkspace(payload: unknown): Workspace {
  const root = asRecord(payload);
  const value = optionalRecord(root.workspace) ?? root;
  const workspaceId = stringValue(value.id);
  const privateDiskQuotaEnforced = nullableBoolean(
    value.private_disk_quota_enforced,
  );
  const privateDiskLimitMb = privateDiskQuotaEnforced === false
    ? null
    : nullableNumber(value.private_disk_limit_mb);
  const owner = optionalRecord(value.owner);
  const ownerStatus = owner
    ? stringValue(owner.status, "ACTIVE").toUpperCase() as UserStatus
    : "ACTIVE";
  const activeOperationValue = optionalRecord(value.active_operation);
  const activeOperation = activeOperationValue
    ? normalizeOperation({
      ...activeOperationValue,
      workspace_id: activeOperationValue.workspace_id ?? workspaceId,
    })
    : null;
  const acceleratorFieldsPresent = [
    value.accelerator_kind,
    value.gpu_count,
    value.cuda_version,
    value.gpu_framework,
    value.gpu_framework_version,
  ].some((item) => item !== undefined);
  const accelerator = acceleratorFieldsPresent ? normalizeAccelerator(value) : null;
  return {
    id: workspaceId,
    name: stringValue(value.name, "개발환경"),
    profileId: stringValue(value.profile_id),
    profileVersion: nullableNumber(value.profile_version),
    profileName: nullableString(value.profile_name),
    kernelName: nullableString(value.kernel_name),
    kernelDisplayName: nullableString(value.kernel_display_name),
    pythonVersion: nullableString(value.python_version),
    acceleratorKind: accelerator?.acceleratorKind ?? null,
    gpuCount: accelerator?.gpuCount ?? null,
    cudaVersion: accelerator?.cudaVersion ?? null,
    gpuFramework: accelerator?.gpuFramework ?? null,
    gpuFrameworkVersion: accelerator?.gpuFrameworkVersion ?? null,
    cpuLimit: nullableString(value.cpu_limit),
    memoryLimitMb: nullableNumber(value.memory_limit_mb),
    privateDiskLimitMb,
    privateDiskQuotaEnforced,
    desiredState: stringValue(value.desired_state, "STOPPED") as Workspace["desiredState"],
    observedState: stringValue(value.observed_state, "UNKNOWN") as Workspace["observedState"],
    progressPercent: nullableNumber(value.progress_percent),
    stale: booleanValue(value.stale, false),
    resourceUsage: normalizeWorkspaceResourceUsage(value.resource_usage),
    lastErrorCode: nullableString(value.last_error_code),
    lastErrorSummary: nullableString(value.last_error_summary),
    createdAt: nullableString(value.created_at),
    updatedAt: nullableString(value.updated_at),
    owner: owner ? {
      id: stringValue(owner.id),
      username: stringValue(owner.username),
      displayName: nullableString(owner.display_name),
      status: ownerStatus,
    } : null,
    restartRequired: booleanValue(value.restart_required, false),
    deletionCheckpoint: nullableString(value.deletion_checkpoint),
    deletionStatus: nullableString(value.deletion_status) as Workspace["deletionStatus"],
    canRetryDelete: booleanValue(value.can_retry_delete, false),
    activeOperation: activeOperation?.id ? activeOperation : null,
  };
}

export function normalizeWorkspaces(payload: unknown): Workspace[] {
  return pickArray(payload, "items", "workspaces")
    .map(normalizeWorkspace)
    .filter((workspace) => workspace.id);
}

export function normalizeOperation(payload: unknown): Operation {
  const root = asRecord(payload);
  const value = optionalRecord(root.operation) ?? root;
  return {
    id: stringValue(value.id),
    workspaceId: stringValue(value.workspace_id),
    operationType: stringValue(value.operation_type, "UNKNOWN").toUpperCase(),
    status: stringValue(value.status, "PENDING").toUpperCase() as Operation["status"],
    progressPercent: nullableNumber(value.progress_percent),
    message: nullableString(value.message),
    errorCode: nullableString(value.error_code),
    errorSummary: nullableString(value.error_summary),
    requestedAt: nullableString(value.requested_at),
    completedAt: nullableString(value.completed_at),
  };
}

const operationStatuses = new Set<Operation["status"]>([
  "PENDING",
  "RUNNING",
  "WAITING_EXTERNAL",
  "SUCCEEDED",
  "FAILED",
  "AUTH_REQUIRED",
  "CANCELLED",
]);

function boundedNullableString(value: unknown, maxLength: number): string | null | undefined {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || value.length > maxLength) return undefined;
  return value.length > 0 ? value : null;
}

function normalizeOperationStrict(payload: unknown): Operation {
  const root = asRecord(payload);
  const value = optionalRecord(root.operation) ?? root;
  const id = trimmedString(value.id, 64);
  const workspaceId = trimmedString(value.workspace_id, 64);
  const operationType = stringValue(value.operation_type).toUpperCase();
  const status = stringValue(value.status).toUpperCase() as Operation["status"];
  const progressPercent = value.progress_percent === null || value.progress_percent === undefined
    ? null
    : nonNegativeInteger(value.progress_percent);
  const message = boundedNullableString(value.message, 512);
  const errorCode = boundedNullableString(value.error_code, 64);
  const errorSummary = boundedNullableString(value.error_summary, 512);
  const requestedAt = boundedNullableString(value.requested_at, 64);
  const completedAt = boundedNullableString(value.completed_at, 64);
  if (
    !id
    || !workspaceId
    || !/^[A-Z][A-Z0-9_]{0,23}$/.test(operationType)
    || !operationStatuses.has(status)
    || progressPercent === undefined
    || (progressPercent !== null && progressPercent > 100)
    || message === undefined
    || errorCode === undefined
    || errorSummary === undefined
    || requestedAt === undefined
    || completedAt === undefined
    || (requestedAt !== null && !Number.isFinite(Date.parse(requestedAt)))
    || (completedAt !== null && !Number.isFinite(Date.parse(completedAt)))
  ) {
    throw invalidMutationResponse();
  }
  return {
    id,
    workspaceId,
    operationType,
    status,
    progressPercent,
    message,
    errorCode,
    errorSummary,
    requestedAt,
    completedAt,
  };
}

export function normalizeAuditEvents(payload: unknown): AdminAuditEvent[] {
  return pickArray(payload, "items", "events")
    .map((item) => {
      const value = asRecord(item);
      return {
        id: stringValue(value.id),
        workspaceId: nullableString(value.workspace_id),
        action: stringValue(value.action),
        result: stringValue(value.result),
        createdAt: nullableString(value.created_at),
      };
    })
    .filter((event) => event.id && event.action && event.result);
}

export function normalizeInternalEgressPolicy(
  payload: unknown,
): InternalEgressPolicySnapshot {
  const root = asRecord(payload);
  const policy = asRecord(root.policy);
  const rawRules = Array.isArray(root.rules) ? root.rules : [];
  const desiredRevision = positiveInteger(policy.desired_revision);
  const desiredDigest = stringValue(policy.desired_digest);
  const appliedRevision = positiveInteger(policy.applied_revision);
  const appliedDigest = nullableString(policy.applied_digest);
  const applyStatus = stringValue(policy.apply_status).toUpperCase();
  const lastErrorCode = nullableString(policy.last_error_code);
  const lastErrorSummary = nullableString(policy.last_error_summary);
  const digestPattern = /^sha256:[0-9a-f]{64}$/;
  const errorCodePattern = /^[A-Z][A-Z0-9_]{0,63}$/;
  if (
    desiredRevision === null ||
    !digestPattern.test(desiredDigest) ||
    !["PENDING", "APPLYING", "APPLIED", "FAILED"].includes(applyStatus) ||
    ((appliedRevision === null) !== (appliedDigest === null)) ||
    (appliedDigest !== null && !digestPattern.test(appliedDigest)) ||
    (appliedRevision !== null && appliedRevision > desiredRevision) ||
    ((applyStatus === "FAILED") !== (lastErrorCode !== null)) ||
    (lastErrorCode !== null && !errorCodePattern.test(lastErrorCode)) ||
    (applyStatus === "APPLIED" && (
      appliedRevision !== desiredRevision || appliedDigest !== desiredDigest
    ))
  ) {
    throw invalidMutationResponse();
  }
  if (rawRules.length > 32) throw invalidMutationResponse();
  const rules: InternalEgressRule[] = rawRules.map((value) => {
    const rule = asRecord(value);
    const id = stringValue(rule.id);
    const destinationCidr = stringValue(rule.destination_cidr);
    const port = positiveInteger(rule.port);
    const rowVersion = positiveInteger(rule.row_version);
    if (
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id) ||
      !isCanonicalPrivateHostCidr(destinationCidr) ||
      port === null || port < 1024 || port > 65535 ||
      [2375, 2376, 2377, 3128, 4243, 6443, 10250].includes(port) ||
      rowVersion === null
    ) {
      throw invalidMutationResponse();
    }
    return {
      id,
      destinationCidr,
      port,
      rowVersion,
      createdAt: nullableString(rule.created_at),
      updatedAt: nullableString(rule.updated_at),
    };
  });
  if (
    new Set(rules.map((rule) => rule.id)).size !== rules.length ||
    new Set(rules.map((rule) => `${rule.destinationCidr}:${rule.port}`)).size !== rules.length
  ) {
    throw invalidMutationResponse();
  }
  return {
    desiredRevision,
    desiredDigest,
    appliedRevision,
    appliedDigest,
    applyStatus: applyStatus as InternalEgressPolicySnapshot["applyStatus"],
    lastErrorCode,
    lastErrorSummary,
    updatedAt: nullableString(policy.updated_at),
    rules,
  };
}

function isCanonicalPrivateHostCidr(value: string): boolean {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/32$/.exec(value);
  if (!match) return false;
  const octets = match.slice(1).map(Number);
  if (octets.some((octet) => !Number.isInteger(octet) || octet < 0 || octet > 255)) {
    return false;
  }
  if (`${octets.join(".")}/32` !== value) return false;
  const [first, second] = octets;
  return first === 10 || (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168);
}

export function normalizeAdminCapacity(payload: unknown): AdminCapacity {
  const root = asRecord(payload);
  const workspaces = optionalRecord(root.workspaces) ?? {};
  const resources = optionalRecord(root.resources) ??
    optionalRecord(optionalRecord(root.global)?.resources) ?? {};
  const cpu = optionalRecord(resources.cpu_millicores) ?? {};
  const memory = optionalRecord(resources.memory_mb) ?? {};
  const gpu = optionalRecord(resources.gpu_count) ?? {};
  const workspaceRunning = Math.max(0, numberValue(workspaces.running, 0));
  const rawUsage = optionalRecord(root.usage);
  let usage: AdminCapacity["usage"] = null;
  if (rawUsage !== null) {
    const runningTotal = nonNegativeInteger(rawUsage.running_total);
    const measured = nonNegativeInteger(rawUsage.measured);
    const unavailable = nonNegativeInteger(rawUsage.unavailable);
    const cpuMillicores = nonNegativeInteger(rawUsage.cpu_millicores);
    const memoryBytes = nonNegativeInteger(rawUsage.memory_bytes);
    const expiresAt = utcInstant(rawUsage.expires_at);
    if (runningTotal !== null && measured !== null && unavailable !== null &&
      cpuMillicores !== null && memoryBytes !== null && expiresAt !== null &&
      typeof rawUsage.stale === "boolean" &&
      runningTotal === workspaceRunning && measured + unavailable === runningTotal &&
      (measured > 0 || (cpuMillicores === 0 && memoryBytes === 0))) {
      usage = {
        runningTotal,
        measured,
        unavailable,
        cpuMillicores,
        memoryBytes,
        expiresAt,
        stale: rawUsage.stale,
      };
    }
  }
  return {
    users: Math.max(0, numberValue(root.users, 0)),
    workspaceCreated: Math.max(0, numberValue(
      workspaces.created ?? root.workspaces,
      0,
    )),
    workspaceRunning,
    workspaceReserved: Math.max(0, numberValue(
      workspaces.reserved ?? optionalRecord(root.global)?.active,
      0,
    )),
    workspaceLimit: Math.max(0, numberValue(
      workspaces.limit ?? optionalRecord(root.global)?.limit,
      0,
    )),
    cpuReservedMillicores: Math.max(0, numberValue(cpu.reserved, 0)),
    cpuBudgetMillicores: Math.max(0, numberValue(cpu.limit, 0)),
    memoryReservedMb: Math.max(0, numberValue(memory.reserved, 0)),
    memoryBudgetMb: Math.max(0, numberValue(memory.limit, 0)),
    gpuReservedCount: Math.max(0, numberValue(gpu.reserved, 0)),
    gpuBudgetCount: Math.max(0, numberValue(gpu.limit, 0)),
    usage,
  };
}

export function normalizeResourcePolicy(payload: unknown): ResourcePolicy {
  const root = asRecord(payload);
  const value = optionalRecord(root.resource_policy) ?? root;
  const selectedCpu = integerArray(value.selectable_cpu_millicores);
  const selectedMemory = integerArray(value.selectable_memory_mb);
  const availableCpu = integerArray(value.available_cpu_millicores);
  const availableMemory = integerArray(value.available_memory_mb);
  const selectedGpu = integerArray(value.selectable_gpu_counts, false)
    .filter((item) => item <= MAX_NVIDIA_GPU_COUNT);
  const availableGpu = integerArray(value.available_gpu_counts, false)
    .filter((item) => item <= MAX_NVIDIA_GPU_COUNT);
  const hasAvailableCpu = Array.isArray(value.available_cpu_millicores);
  const hasAvailableMemory = Array.isArray(value.available_memory_mb);
  const hasSelectedGpu = Array.isArray(value.selectable_gpu_counts);
  const hasAvailableGpu = Array.isArray(value.available_gpu_counts);
  const hardCeiling = optionalRecord(value.hard_ceiling) ?? {};
  const rawKernelBounds = optionalRecord(value.kernel_idle_timeout_bounds);
  const kernelMinSeconds = positiveInteger(rawKernelBounds?.min_seconds);
  const kernelMaxSeconds = positiveInteger(rawKernelBounds?.max_seconds);
  const kernelStepSeconds = positiveInteger(rawKernelBounds?.step_seconds);
  const kernelIdleTimeoutBounds = kernelMinSeconds !== null &&
      kernelMaxSeconds !== null && kernelStepSeconds !== null &&
      kernelMinSeconds <= kernelMaxSeconds &&
      kernelMinSeconds % kernelStepSeconds === 0 &&
      kernelMaxSeconds % kernelStepSeconds === 0
    ? {
        minSeconds: kernelMinSeconds,
        maxSeconds: kernelMaxSeconds,
        stepSeconds: kernelStepSeconds,
      }
    : null;
  const rawKernelIdleTimeoutSeconds = nonNegativeInteger(
    value.kernel_idle_timeout_seconds,
  );
  const kernelIdleTimeoutSeconds = rawKernelIdleTimeoutSeconds !== null &&
      kernelIdleTimeoutBounds !== null &&
      (rawKernelIdleTimeoutSeconds === 0 || (
        rawKernelIdleTimeoutSeconds >= kernelIdleTimeoutBounds.minSeconds &&
        rawKernelIdleTimeoutSeconds <= kernelIdleTimeoutBounds.maxSeconds &&
        rawKernelIdleTimeoutSeconds % kernelIdleTimeoutBounds.stepSeconds === 0
      ))
    ? rawKernelIdleTimeoutSeconds
    : null;
  return {
    version: numberValue(value.version, 0),
    cpuBudgetMillicores: numberValue(value.cpu_budget_millicores, 0),
    memoryBudgetMb: numberValue(value.memory_budget_mb, 0),
    selectableCpuMillicores: selectedCpu,
    selectableMemoryMb: selectedMemory,
    // The platform budget may cover multiple verified physical GPUs. Runtime
    // offers determine how many of them one workspace may reserve exclusively.
    gpuBudgetCount: gpuPoolCount(value.gpu_budget_count),
    selectableGpuCounts: hasSelectedGpu ? selectedGpu : [0],
    // Older API responses omitted the available catalog.  Preserve that
    // compatibility, but do not turn an explicitly empty current catalog into
    // an apparently valid stale selection.
    availableCpuMillicores: hasAvailableCpu ? availableCpu : selectedCpu,
    availableMemoryMb: hasAvailableMemory ? availableMemory : selectedMemory,
    availableGpuCounts: hasAvailableGpu
      ? availableGpu
      : (hasSelectedGpu ? selectedGpu : [0]),
    maxCpuBudgetMillicores: positiveInteger(hardCeiling.cpu_millicores),
    maxMemoryBudgetMb: positiveInteger(hardCeiling.memory_mb),
    maxGpuBudgetCount: gpuPoolCount(hardCeiling.gpu_count),
    kernelIdleTimeoutSeconds,
    kernelIdleTimeoutBounds,
    updatedAt: nullableString(value.updated_at),
  };
}

function normalizeRuntimeTemplate(payload: unknown): RuntimeProfileTemplate | null {
  const value = asRecord(payload);
  const id = trimmedString(value.id, 64);
  const version = positiveInteger(value.version);
  const kernelName = trimmedString(value.kernel_name, 128);
  const kernelDisplayName = trimmedString(value.kernel_display_name, 128);
  const pythonVersion = trimmedString(value.python_version, 64);
  const cpuLimit = positiveCpuLimit(value.cpu_limit);
  const memoryLimitMb = positiveInteger(value.memory_limit_mb);
  const accelerator = normalizeAccelerator(value);
  if (!id || !version || !kernelName || !kernelDisplayName || !pythonVersion ||
    !cpuLimit || !memoryLimitMb || !accelerator) return null;
  return {
    id,
    version,
    kernelName,
    kernelDisplayName,
    pythonVersion,
    ...accelerator,
    cpuLimit,
    memoryLimitMb,
  };
}

function normalizeAdminProfile(payload: unknown): AdminWorkspaceProfile | null {
  const root = asRecord(payload);
  const value = optionalRecord(root.profile) ?? root;
  const runtimeProfile = normalizeRuntimeTemplate(value.runtime_profile);
  const id = trimmedString(value.id, 64);
  const version = positiveInteger(value.version);
  const name = trimmedString(value.name, 80);
  if (!id || !version || !name || !runtimeProfile) return null;
  return {
    id,
    version,
    name,
    description: nullableString(value.description),
    enabled: value.enabled === true,
    effectiveSelectable: nullableBoolean(value.effective_selectable),
    runtimeProfile,
    createdAt: nullableString(value.created_at),
    updatedAt: nullableString(value.updated_at),
  };
}

export function normalizeAdminProfileCatalog(payload: unknown): AdminProfileCatalog {
  const root = asRecord(payload);
  return {
    items: pickArray(payload, "items")
      .map(normalizeAdminProfile)
      .filter((item): item is AdminWorkspaceProfile => item !== null),
    runtimeTemplates: pickArray(root.runtime_templates)
      .map(normalizeRuntimeTemplate)
      .filter((item): item is RuntimeProfileTemplate => item !== null),
  };
}

export function normalizeEnvironmentVariable(payload: unknown): EnvironmentVariable {
  const root = asRecord(payload);
  const value = optionalRecord(root.item) ?? root;
  const scope = stringValue(value.scope, "WORKSPACE").toUpperCase();
  return {
    name: stringValue(value.name),
    scope: (scope === "USER" ? "USER" : "WORKSPACE") as EnvironmentScope,
    version: Math.max(0, numberValue(value.version, 0)),
    isSet: value.is_set === true,
    isSecret: value.is_secret !== false,
    value: value.is_secret === false ? nullableString(value.value) ?? "" : null,
    updatedAt: nullableString(value.updated_at),
  };
}

export function normalizeEnvironmentList(payload: unknown): EnvironmentVariableList {
  const root = asRecord(payload);
  return {
    items: pickArray(payload, "items")
      .map(normalizeEnvironmentVariable)
      .filter((item) => item.name && item.version > 0 && item.isSet),
    restartRequired: booleanValue(root.restart_required, false),
  };
}

function normalizeEnvironmentMutation(payload: unknown): EnvironmentVariableMutation {
  const root = asRecord(payload);
  const item = optionalRecord(root.item);
  return {
    item: item ? normalizeEnvironmentVariable(item) : null,
    changed: booleanValue(root.changed, false),
    restartRequired: booleanValue(root.restart_required, false),
  };
}

export function normalizeAdminWorkspacePage(payload: unknown): AdminWorkspacePage {
  const root = asRecord(payload);
  const items = normalizeWorkspaces(payload);
  return {
    items,
    total: Math.max(items.length, numberValue(root.total, items.length)),
    limit: Math.max(0, numberValue(root.limit, items.length)),
    offset: Math.max(0, numberValue(root.offset, 0)),
  };
}

function normalizeAdminWorkspacePageStrict(
  payload: unknown,
  expectedOffset: number,
  expectedLimit: number,
): AdminWorkspacePage {
  const root = asRecord(payload);
  if (
    !Array.isArray(root.items)
    || !Number.isSafeInteger(root.total)
    || (root.total as number) < 0
    || root.limit !== expectedLimit
    || root.offset !== expectedOffset
  ) {
    throw invalidMutationResponse();
  }
  const items = (root.items as unknown[]).map(normalizeWorkspace);
  if (
    items.some((workspace) => !workspace.id)
    || items.length > expectedLimit
    || new Set(items.map((workspace) => workspace.id)).size !== items.length
  ) {
    throw invalidMutationResponse();
  }
  return {
    items,
    total: root.total as number,
    limit: expectedLimit,
    offset: expectedOffset,
  };
}

function normalizeAdminOperationPageStrict(
  payload: unknown,
  expectedOffset: number,
  expectedLimit: number,
): AdminOperationPage {
  const root = asRecord(payload);
  if (
    !Array.isArray(root.items)
    || root.limit !== expectedLimit
    || root.offset !== expectedOffset
    || root.items.length > expectedLimit
  ) {
    throw invalidMutationResponse();
  }
  const items = (root.items as unknown[]).map(normalizeOperationStrict);
  if (new Set(items.map((operation) => operation.id)).size !== items.length) {
    throw invalidMutationResponse();
  }
  return { items, limit: expectedLimit, offset: expectedOffset };
}

function normalizeMutation(payload: unknown): WorkspaceMutationResult {
  const root = asRecord(payload);
  return {
    workspace: normalizeWorkspace(root.workspace),
    operation: normalizeOperation(root.operation),
  };
}

function invalidMutationResponse(): ApiError {
  return new ApiError(0, {
    code: "INVALID_RESPONSE",
    message: "작업 결과를 확인하지 못했습니다. 같은 요청을 다시 시도해 주세요.",
  });
}

function normalizeMutationStrict(payload: unknown): WorkspaceMutationResult {
  const result = normalizeMutation(payload);
  if (
    !result.workspace.id
    || !result.operation.id
    || result.operation.workspaceId !== result.workspace.id
  ) {
    throw invalidMutationResponse();
  }
  return result;
}

function normalizeEnvironmentMutationStrict(
  payload: unknown,
): EnvironmentVariableMutation {
  const root = asRecord(payload);
  if (root.deleted === true) {
    const name = trimmedString(root.name, 64);
    const scope = stringValue(root.scope).toUpperCase();
    const version = positiveInteger(root.version);
    if (
      !name
      || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)
      || !["USER", "WORKSPACE"].includes(scope)
      || !version
      || typeof root.restart_required !== "boolean"
    ) {
      throw invalidMutationResponse();
    }
    return {
      item: null,
      changed: true,
      restartRequired: root.restart_required,
    };
  }
  const item = optionalRecord(root.item);
  if (
    typeof root.changed !== "boolean"
    || typeof root.restart_required !== "boolean"
    || !item
    || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(stringValue(item.name))
    || !["USER", "WORKSPACE"].includes(stringValue(item.scope).toUpperCase())
    || !positiveInteger(item.version)
    || typeof item.is_secret !== "boolean"
    || item.is_set !== true
  ) {
    throw invalidMutationResponse();
  }
  const result = normalizeEnvironmentMutation(payload);
  if (result.item && (!result.item.name || result.item.version <= 0 || !result.item.isSet)) {
    throw invalidMutationResponse();
  }
  return result;
}

function normalizeAdminProfileStrict(payload: unknown): AdminWorkspaceProfile {
  const profile = normalizeAdminProfile(payload);
  if (!profile) throw invalidMutationResponse();
  return profile;
}

function normalizeResourcePolicyStrict(payload: unknown): ResourcePolicy {
  const policy = normalizeResourcePolicy(payload);
  if (
    policy.version <= 0
    || policy.cpuBudgetMillicores <= 0
    || policy.memoryBudgetMb <= 0
    || policy.selectableCpuMillicores.length === 0
    || policy.selectableMemoryMb.length === 0
    || policy.selectableGpuCounts.length === 0
    || policy.kernelIdleTimeoutSeconds === null
    || policy.kernelIdleTimeoutBounds === null
  ) {
    throw invalidMutationResponse();
  }
  return policy;
}

function idempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  if (!globalThis.crypto?.getRandomValues) {
    throw new ApiError(503, {
      code: "SECURE_BROWSER_REQUIRED",
      message: "안전한 중복 요청 방지를 지원하는 브라우저에서 다시 시도해 주세요.",
    });
  }
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

interface PendingMutationKey {
  key: string;
  expiresAt: number;
}

const pendingMutationKeys = new Map<string, PendingMutationKey>();
const PENDING_MUTATION_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_PENDING_MUTATION_KEYS = 128;
const PENDING_MUTATION_STORAGE_KEY = "workspace-portal.pending-mutations.v1";
const fingerprintPattern = /^[0-9a-f]{64}$/;
const generatedKeyPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
let hydratedCsrfToken: string | null = null;

function sessionStorageOrNull(): Storage | null {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

function persistPendingMutationKeys(now = Date.now()): void {
  const storage = sessionStorageOrNull();
  if (!storage) return;
  const entries = [...pendingMutationKeys.entries()]
    .filter(([fingerprint, entry]) =>
      fingerprintPattern.test(fingerprint)
      && generatedKeyPattern.test(entry.key)
      && Number.isSafeInteger(entry.expiresAt)
      && entry.expiresAt > now
      && entry.expiresAt <= now + PENDING_MUTATION_TTL_MS)
    .slice(-MAX_PENDING_MUTATION_KEYS)
    .map(([fingerprint, entry]) => ({ fingerprint, ...entry }));
  try {
    if (entries.length === 0) storage.removeItem(PENDING_MUTATION_STORAGE_KEY);
    else storage.setItem(PENDING_MUTATION_STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // Storage can be blocked or full. The in-memory retry protection remains.
  }
}

function hydratePendingMutationKeys(now = Date.now()): void {
  if (!csrfToken || hydratedCsrfToken === csrfToken) return;
  pendingMutationKeys.clear();
  hydratedCsrfToken = csrfToken;
  const storage = sessionStorageOrNull();
  if (!storage) return;
  try {
    const raw = storage.getItem(PENDING_MUTATION_STORAGE_KEY);
    if (!raw) return;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length > MAX_PENDING_MUTATION_KEYS) {
      storage.removeItem(PENDING_MUTATION_STORAGE_KEY);
      return;
    }
    for (const candidate of parsed) {
      const entry = asRecord(candidate);
      const fingerprint = stringValue(entry.fingerprint);
      const key = stringValue(entry.key);
      const expiresAt = entry.expiresAt;
      if (
        !fingerprintPattern.test(fingerprint)
        || !generatedKeyPattern.test(key)
        || typeof expiresAt !== "number"
        || !Number.isSafeInteger(expiresAt)
        || expiresAt > now + PENDING_MUTATION_TTL_MS
      ) {
        storage.removeItem(PENDING_MUTATION_STORAGE_KEY);
        pendingMutationKeys.clear();
        return;
      }
      if (expiresAt <= now) continue;
      pendingMutationKeys.set(fingerprint, { key, expiresAt });
    }
  } catch {
    try {
      storage.removeItem(PENDING_MUTATION_STORAGE_KEY);
    } catch {
      // Ignore an unavailable storage implementation.
    }
    pendingMutationKeys.clear();
  }
}

function clearPendingMutationKeys(): void {
  pendingMutationKeys.clear();
  hydratedCsrfToken = null;
  const storage = sessionStorageOrNull();
  if (!storage) return;
  try {
    storage.removeItem(PENDING_MUTATION_STORAGE_KEY);
  } catch {
    // Ignore an unavailable storage implementation.
  }
}

async function mutationFingerprint(path: string, init: RequestInit): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new ApiError(503, {
      code: "SECURE_BROWSER_REQUIRED",
      message: "안전한 중복 요청 방지를 지원하는 브라우저에서 다시 시도해 주세요.",
    });
  }
  const method = (init.method ?? "POST").toUpperCase();
  const body = typeof init.body === "string" ? init.body : "";
  const encoded = new TextEncoder().encode(`${method}\0${path}\0${body}`);
  // Mutating API calls require the per-session CSRF token. Use it as an HMAC
  // key so a persisted fingerprint cannot be used as an offline oracle for a
  // low-entropy secret environment value. Tests or callers that have not
  // loaded /me still get in-memory SHA-256 matching, but are not persisted.
  const digest = csrfToken
    ? await globalThis.crypto.subtle.sign(
        "HMAC",
        await globalThis.crypto.subtle.importKey(
          "raw",
          new TextEncoder().encode(csrfToken),
          { name: "HMAC", hash: "SHA-256" },
          false,
          ["sign"],
        ),
        encoded,
      )
    : await globalThis.crypto.subtle.digest("SHA-256", encoded);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function mutationRequest<T>(
  path: string,
  init: RequestInit,
  normalize: (payload: unknown) => T,
): Promise<T> {
  const fingerprint = await mutationFingerprint(path, init);
  const now = Date.now();
  hydratePendingMutationKeys(now);
  for (const [candidate, entry] of pendingMutationKeys) {
    if (entry.expiresAt <= now) pendingMutationKeys.delete(candidate);
  }
  const pending = pendingMutationKeys.get(fingerprint);
  if (!pending && pendingMutationKeys.size >= MAX_PENDING_MUTATION_KEYS) {
    const oldest = pendingMutationKeys.keys().next().value;
    if (typeof oldest === "string") pendingMutationKeys.delete(oldest);
  }
  const key = pending?.key ?? idempotencyKey();
  pendingMutationKeys.set(fingerprint, {
    key,
    expiresAt: now + PENDING_MUTATION_TTL_MS,
  });
  if (csrfToken) persistPendingMutationKeys(now);
  try {
    const headers = new Headers(init.headers);
    headers.set("Idempotency-Key", key);
    const payload = await request(path, {
      ...init,
      headers,
    });
    const result = normalize(payload);
    pendingMutationKeys.delete(fingerprint);
    if (csrfToken) persistPendingMutationKeys();
    return result;
  } catch (error) {
    // A network abort or 5xx response may occur after the server committed. Keep
    // the key so an identical retry replays the durable receipt/operation. A
    // definitive 4xx response did not leave an unknown outcome and may rotate.
    if (error instanceof ApiError && error.status > 0 && error.status < 500) {
      pendingMutationKeys.delete(fingerprint);
      if (csrfToken) persistPendingMutationKeys();
    }
    throw error;
  }
}

export const portalApi = {
  loginUrl: `${API_BASE}/auth/login`,
  passwordChangeUrl: `${API_BASE}/auth/change-password`,
  launchUrl(workspaceId: string): string {
    return `${API_BASE}/workspaces/${encodeURIComponent(workspaceId)}/launch`;
  },
  adminLaunchUrl(workspaceId: string): string {
    return `${API_BASE}/admin/workspaces/${encodeURIComponent(workspaceId)}/launch`;
  },
  async me(signal?: AbortSignal): Promise<PortalUser> {
    const payload = await request("/me", { signal });
    const root = asRecord(payload);
    csrfToken = nullableString(root.csrf_token);
    return normalizeUser(payload);
  },
  async profiles(signal?: AbortSignal): Promise<WorkspaceProfile[]> {
    return normalizeProfiles(await request("/workspace-profiles", { signal }));
  },
  async capacity(signal?: AbortSignal): Promise<Capacity> {
    return normalizeCapacity(await request("/capacity", { signal }));
  },
  async workspaces(signal?: AbortSignal): Promise<Workspace[]> {
    return normalizeWorkspaces(await request("/workspaces", { signal }));
  },
  async workspace(workspaceId: string, signal?: AbortSignal): Promise<Workspace> {
    return normalizeWorkspace(
      await request(`/workspaces/${encodeURIComponent(workspaceId)}`, { signal }),
    );
  },
  async operation(operationId: string, signal?: AbortSignal): Promise<Operation> {
    return normalizeOperationStrict(
      await request(`/operations/${encodeURIComponent(operationId)}`, { signal }),
    );
  },
  async adminAuditEvents(signal?: AbortSignal): Promise<AdminAuditEvent[]> {
    return normalizeAuditEvents(
      await request("/admin/audit-events?limit=20&offset=0", { signal }),
    );
  },
  async requestProvisioning(): Promise<UserProvisioning> {
    const payload = await request("/me/provisioning", { method: "POST" });
    return normalizeProvisioning(payload);
  },
  async createWorkspace(
    profileId: string,
    profileVersion: number,
    options: {
      name?: string;
    } = {},
  ): Promise<WorkspaceMutationResult> {
    const name = options.name?.trim();
    const body: Record<string, unknown> = {
      profile_id: profileId,
      profile_version: profileVersion,
    };
    if (name) body.name = name;
    return mutationRequest(
      "/workspaces",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
      normalizeMutationStrict,
    );
  },
  async startWorkspace(workspaceId: string): Promise<WorkspaceMutationResult> {
    return mutationRequest(
      `/workspaces/${encodeURIComponent(workspaceId)}/actions/start`,
      {
        method: "POST",
      },
      normalizeMutationStrict,
    );
  },
  async stopWorkspace(workspaceId: string): Promise<WorkspaceMutationResult> {
    return mutationRequest(
      `/workspaces/${encodeURIComponent(workspaceId)}/actions/stop`,
      {
        method: "POST",
      },
      normalizeMutationStrict,
    );
  },
  async restartWorkspace(workspaceId: string): Promise<WorkspaceMutationResult> {
    return mutationRequest(
      `/workspaces/${encodeURIComponent(workspaceId)}/actions/restart`,
      {
        method: "POST",
      },
      normalizeMutationStrict,
    );
  },
  async deleteWorkspace(workspaceId: string): Promise<WorkspaceMutationResult> {
    return mutationRequest(
      `/workspaces/${encodeURIComponent(workspaceId)}`,
      {
        method: "DELETE",
      },
      normalizeMutationStrict,
    );
  },
  async environmentVariables(
    workspaceId?: string,
    signal?: AbortSignal,
  ): Promise<EnvironmentVariableList> {
    const path = workspaceId
      ? `/workspaces/${encodeURIComponent(workspaceId)}/environment-variables`
      : "/me/environment-variables";
    return normalizeEnvironmentList(await request(path, { signal }));
  },
  async putEnvironmentVariable(
    name: string,
    value: string,
    isSecret: boolean,
    expectedVersion?: number,
    workspaceId?: string,
  ): Promise<EnvironmentVariableMutation> {
    const base = workspaceId
      ? `/workspaces/${encodeURIComponent(workspaceId)}/environment-variables`
      : "/me/environment-variables";
    return mutationRequest(
      `${base}/${encodeURIComponent(name)}`,
      {
        method: "PUT",
        body: JSON.stringify({
          value,
          is_secret: isSecret,
          ...(expectedVersion === undefined ? {} : { expected_version: expectedVersion }),
        }),
      },
      normalizeEnvironmentMutationStrict,
    );
  },
  async deleteEnvironmentVariable(
    name: string,
    expectedVersion: number,
    workspaceId?: string,
  ): Promise<EnvironmentVariableMutation> {
    const base = workspaceId
      ? `/workspaces/${encodeURIComponent(workspaceId)}/environment-variables`
      : "/me/environment-variables";
    const query = new URLSearchParams({ expected_version: String(expectedVersion) });
    return mutationRequest(
      `${base}/${encodeURIComponent(name)}?${query.toString()}`,
      {
        method: "DELETE",
      },
      normalizeEnvironmentMutationStrict,
    );
  },
  async adminCapacity(signal?: AbortSignal): Promise<AdminCapacity> {
    return normalizeAdminCapacity(await request("/admin/capacity", { signal }));
  },
  async adminSettings(signal?: AbortSignal): Promise<ResourcePolicy> {
    return normalizeResourcePolicy(await request("/admin/settings", { signal }));
  },
  async adminInternalEgressPolicy(
    signal?: AbortSignal,
  ): Promise<InternalEgressPolicySnapshot> {
    return normalizeInternalEgressPolicy(
      await request("/admin/internal-egress-policy", { signal }),
    );
  },
  async createAdminInternalEgressRule(
    destinationCidr: string,
    port: number,
    expectedRevision: number,
  ): Promise<InternalEgressPolicySnapshot> {
    return mutationRequest(
      "/admin/internal-egress-policy/rules",
      {
        method: "POST",
        body: JSON.stringify({
          destination_cidr: destinationCidr,
          port,
          expected_revision: expectedRevision,
        }),
      },
      normalizeInternalEgressPolicy,
    );
  },
  async updateAdminInternalEgressRule(input: {
    id: string;
    destinationCidr: string;
    port: number;
    expectedVersion: number;
    expectedRevision: number;
  }): Promise<InternalEgressPolicySnapshot> {
    return mutationRequest(
      `/admin/internal-egress-policy/rules/${encodeURIComponent(input.id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          destination_cidr: input.destinationCidr,
          port: input.port,
          expected_version: input.expectedVersion,
          expected_revision: input.expectedRevision,
        }),
      },
      normalizeInternalEgressPolicy,
    );
  },
  async deleteAdminInternalEgressRule(
    id: string,
    expectedVersion: number,
    expectedRevision: number,
  ): Promise<InternalEgressPolicySnapshot> {
    const query = new URLSearchParams({
      expected_revision: String(expectedRevision),
      expected_version: String(expectedVersion),
    });
    return mutationRequest(
      `/admin/internal-egress-policy/rules/${encodeURIComponent(id)}?${query.toString()}`,
      { method: "DELETE" },
      normalizeInternalEgressPolicy,
    );
  },
  async retryAdminInternalEgressPolicy(
    expectedRevision: number,
  ): Promise<InternalEgressPolicySnapshot> {
    return mutationRequest(
      "/admin/internal-egress-policy/retry",
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      },
      normalizeInternalEgressPolicy,
    );
  },
  async adminProfiles(signal?: AbortSignal): Promise<AdminProfileCatalog> {
    return normalizeAdminProfileCatalog(await request("/admin/profiles", { signal }));
  },
  async createAdminProfile(input: {
    name: string;
    description: string | null;
    runtimeProfileId: string;
    runtimeProfileVersion: number;
    enabled: boolean;
  }): Promise<AdminWorkspaceProfile> {
    return mutationRequest(
      "/admin/profiles",
      {
        method: "POST",
        body: JSON.stringify({
          name: input.name,
          description: input.description,
          runtime_profile_id: input.runtimeProfileId,
          runtime_profile_version: input.runtimeProfileVersion,
          enabled: input.enabled,
        }),
      },
      normalizeAdminProfileStrict,
    );
  },
  async updateAdminProfile(input: {
    id: string;
    version: number;
    name: string;
    description: string | null;
    enabled: boolean;
  }): Promise<AdminWorkspaceProfile> {
    return mutationRequest(
      `/admin/profiles/${encodeURIComponent(input.id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          version: input.version,
          name: input.name,
          description: input.description,
          enabled: input.enabled,
        }),
      },
      normalizeAdminProfileStrict,
    );
  },
  async deleteAdminProfile(id: string, expectedVersion: number): Promise<AdminWorkspaceProfile> {
    const query = new URLSearchParams({ expected_version: String(expectedVersion) });
    return mutationRequest(
      `/admin/profiles/${encodeURIComponent(id)}?${query.toString()}`,
      {
        method: "DELETE",
      },
      normalizeAdminProfileStrict,
    );
  },
  async updateAdminSettings(update: ResourcePolicyUpdate): Promise<ResourcePolicy> {
    return mutationRequest(
      "/admin/settings",
      {
        method: "PATCH",
        body: JSON.stringify({
          version: update.version,
          cpu_budget_millicores: update.cpuBudgetMillicores,
          memory_budget_mb: update.memoryBudgetMb,
          selectable_cpu_millicores: update.selectableCpuMillicores,
          selectable_memory_mb: update.selectableMemoryMb,
          gpu_budget_count: update.gpuBudgetCount,
          selectable_gpu_counts: update.selectableGpuCounts,
          kernel_idle_timeout_seconds: update.kernelIdleTimeoutSeconds,
        }),
      },
      normalizeResourcePolicyStrict,
    );
  },
  async adminWorkspaces(signal?: AbortSignal): Promise<AdminWorkspacePage> {
    const items: Workspace[] = [];
    const seenIds = new Set<string>();
    let expectedTotal: number | null = null;
    for (let offset = 0; offset < MAX_ADMIN_PAGED_ITEMS; offset += ADMIN_PAGE_LIMIT) {
      const page = normalizeAdminWorkspacePageStrict(
        await request(
          `/admin/workspaces?limit=${ADMIN_PAGE_LIMIT}&offset=${offset}`,
          { signal },
        ),
        offset,
        ADMIN_PAGE_LIMIT,
      );
      if (expectedTotal === null) {
        expectedTotal = page.total;
        if (expectedTotal > MAX_ADMIN_PAGED_ITEMS) throw invalidMutationResponse();
      } else if (page.total !== expectedTotal) {
        throw invalidMutationResponse();
      }
      for (const workspace of page.items) {
        if (seenIds.has(workspace.id)) throw invalidMutationResponse();
        seenIds.add(workspace.id);
        items.push(workspace);
      }
      if (items.length > expectedTotal) throw invalidMutationResponse();
      if (items.length === expectedTotal) {
        return {
          items,
          total: expectedTotal,
          limit: ADMIN_PAGE_LIMIT,
          offset: 0,
        };
      }
      // A full page is required while the stable total says more rows exist.
      // This prevents a malformed/no-progress response from silently truncating inventory.
      if (page.items.length !== ADMIN_PAGE_LIMIT) throw invalidMutationResponse();
    }
    throw invalidMutationResponse();
  },
  async adminOperations(signal?: AbortSignal): Promise<Operation[]> {
    const items: Operation[] = [];
    const seenIds = new Set<string>();
    for (let offset = 0; offset < MAX_ADMIN_PAGED_ITEMS; offset += ADMIN_PAGE_LIMIT) {
      const page = normalizeAdminOperationPageStrict(
        await request(
          `/admin/operations?limit=${ADMIN_PAGE_LIMIT}&offset=${offset}`,
          { signal },
        ),
        offset,
        ADMIN_PAGE_LIMIT,
      );
      for (const operation of page.items) {
        if (seenIds.has(operation.id)) throw invalidMutationResponse();
        seenIds.add(operation.id);
        items.push(operation);
      }
      if (page.items.length < ADMIN_PAGE_LIMIT) return items;
    }
    // We cannot prove there is no next page at the defensive cap, so fail closed.
    throw invalidMutationResponse();
  },
  async adminWorkspaceAction(
    workspaceId: string,
    action: "start" | "stop" | "restart",
  ): Promise<WorkspaceMutationResult> {
    return mutationRequest(
      `/admin/workspaces/${encodeURIComponent(workspaceId)}/actions/${action}`,
      {
        method: "POST",
      },
      normalizeMutationStrict,
    );
  },
  async adminDeleteWorkspace(workspaceId: string): Promise<WorkspaceMutationResult> {
    return mutationRequest(
      `/admin/workspaces/${encodeURIComponent(workspaceId)}`,
      {
        method: "DELETE",
      },
      normalizeMutationStrict,
    );
  },
  async logout(): Promise<string> {
    const redirectUrl = normalizeLogoutRedirect(
      await request("/auth/logout", { method: "POST" }),
    );
    csrfToken = null;
    clearPendingMutationKeys();
    return redirectUrl;
  },
};
