export type UserStatus = "PROVISIONING" | "ACTIVE" | "DISABLED";
export type UserRole = "USER" | "ADMIN" | string;
export type ProvisioningStatus =
  | "MANUAL_REQUIRED"
  | "NOT_REQUESTED"
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED";

export interface UserProvisioning {
  status: ProvisioningStatus;
  attempts: number;
  errorCode: string | null;
  errorSummary: string | null;
  requestedAt: string | null;
  completedAt: string | null;
}

export interface PortalUser {
  id: string;
  username: string;
  displayName: string | null;
  role: UserRole;
  status: UserStatus;
  provisioning: UserProvisioning;
}

export interface WorkspaceProfile {
  id: string;
  version: number;
  name: string;
  description: string | null;
  kernelName: string;
  kernelDisplayName: string;
  pythonVersion: string;
  acceleratorKind: "none" | "nvidia";
  gpuCount: number;
  cudaVersion: string | null;
  gpuFramework: "pytorch" | null;
  gpuFrameworkVersion: string | null;
  cpuLimit: string;
  memoryLimitMb: number;
  privateDiskLimitMb: number | null;
  privateDiskQuotaEnforced: boolean;
  enabled: boolean;
}

export interface Capacity {
  workspaceUsed: number;
  workspaceLimit: number;
  nextDefaultWorkspaceName: string | null;
  activeUsed: number;
  activeLimit: number;
  cpuReservedMillicores: number | null;
  cpuBudgetMillicores: number | null;
  memoryReservedMb: number | null;
  memoryBudgetMb: number | null;
  gpuReservedCount: number | null;
  gpuBudgetCount: number | null;
  kernelIdleTimeoutSeconds: number | null;
  executionHostHealthy: boolean | null;
}

export interface WorkspaceOwner {
  id: string;
  username: string;
  displayName: string | null;
  status: UserStatus;
}

export type DesiredState = "RUNNING" | "STOPPED" | "DELETED";
export type DeletionStatus = "PENDING" | "RUNNING" | "FAILED" | "SUCCEEDED";
export type ObservedState =
  | "NOT_FOUND"
  | "STARTING"
  | "RUNNING"
  | "STOPPING"
  | "STOPPED"
  | "DELETION_PENDING"
  | "DELETING"
  | "FAILED"
  | "UNKNOWN";

export interface WorkspaceResourceUsage {
  cpuMillicores: number;
  memoryBytes: number;
  memoryLimitBytes: number;
  observedAt: string;
  expiresAt: string;
  stale: boolean;
}

export interface Workspace {
  id: string;
  name: string;
  profileId: string;
  profileVersion: number | null;
  profileName: string | null;
  kernelName: string | null;
  kernelDisplayName: string | null;
  pythonVersion: string | null;
  acceleratorKind: "none" | "nvidia" | null;
  gpuCount: number | null;
  cudaVersion: string | null;
  gpuFramework: "pytorch" | null;
  gpuFrameworkVersion: string | null;
  cpuLimit: string | null;
  memoryLimitMb: number | null;
  privateDiskLimitMb: number | null;
  privateDiskQuotaEnforced: boolean | null;
  desiredState: DesiredState;
  observedState: ObservedState;
  progressPercent: number | null;
  stale: boolean;
  resourceUsage: WorkspaceResourceUsage | null;
  lastErrorCode: string | null;
  lastErrorSummary: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  owner: WorkspaceOwner | null;
  restartRequired: boolean;
  deletionCheckpoint: string | null;
  deletionStatus: DeletionStatus | null;
  canRetryDelete: boolean;
  activeOperation: Operation | null;
}

export type OperationStatus =
  | "PENDING"
  | "RUNNING"
  | "WAITING_EXTERNAL"
  | "SUCCEEDED"
  | "FAILED"
  | "AUTH_REQUIRED"
  | "CANCELLED";

export interface Operation {
  id: string;
  workspaceId: string;
  operationType: string;
  status: OperationStatus;
  progressPercent: number | null;
  message: string | null;
  errorCode: string | null;
  errorSummary: string | null;
  requestedAt: string | null;
  completedAt: string | null;
}

export interface WorkspaceMutationResult {
  workspace: Workspace;
  operation: Operation;
}

export type EnvironmentScope = "USER" | "WORKSPACE";

export interface EnvironmentVariable {
  name: string;
  scope: EnvironmentScope;
  version: number;
  isSet: boolean;
  isSecret: boolean;
  value: string | null;
  updatedAt: string | null;
}

export interface EnvironmentVariableList {
  items: EnvironmentVariable[];
  restartRequired: boolean;
}

export interface EnvironmentVariableMutation {
  item: EnvironmentVariable | null;
  changed: boolean;
  restartRequired: boolean;
}

export interface AdminCapacity {
  users: number;
  workspaceCreated: number;
  workspaceRunning: number;
  workspaceReserved: number;
  workspaceLimit: number;
  cpuReservedMillicores: number;
  cpuBudgetMillicores: number;
  memoryReservedMb: number;
  memoryBudgetMb: number;
  gpuReservedCount: number;
  gpuBudgetCount: number;
  usage: AdminResourceUsage | null;
}

export interface AdminResourceUsage {
  runningTotal: number;
  measured: number;
  unavailable: number;
  cpuMillicores: number;
  memoryBytes: number;
  expiresAt: string;
  stale: boolean;
}

export interface ResourcePolicy {
  version: number;
  cpuBudgetMillicores: number;
  memoryBudgetMb: number;
  selectableCpuMillicores: number[];
  selectableMemoryMb: number[];
  gpuBudgetCount: number;
  selectableGpuCounts: number[];
  availableCpuMillicores: number[];
  availableMemoryMb: number[];
  availableGpuCounts: number[];
  maxCpuBudgetMillicores: number | null;
  maxMemoryBudgetMb: number | null;
  maxGpuBudgetCount: number;
  kernelIdleTimeoutSeconds: number | null;
  kernelIdleTimeoutBounds: {
    minSeconds: number;
    maxSeconds: number;
    stepSeconds: number;
  } | null;
  updatedAt: string | null;
}

export interface ResourcePolicyUpdate {
  version: number;
  cpuBudgetMillicores: number;
  memoryBudgetMb: number;
  selectableCpuMillicores: number[];
  selectableMemoryMb: number[];
  gpuBudgetCount: number;
  selectableGpuCounts: number[];
  kernelIdleTimeoutSeconds: number;
}

export interface AdminWorkspacePage {
  items: Workspace[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminOperationPage {
  items: Operation[];
  limit: number;
  offset: number;
}

export interface RuntimeProfileTemplate {
  id: string;
  version: number;
  kernelName: string;
  kernelDisplayName: string;
  pythonVersion: string;
  acceleratorKind: "none" | "nvidia";
  gpuCount: number;
  cudaVersion: string | null;
  gpuFramework: "pytorch" | null;
  gpuFrameworkVersion: string | null;
  cpuLimit: string;
  memoryLimitMb: number;
}

export interface AdminWorkspaceProfile {
  id: string;
  version: number;
  name: string;
  description: string | null;
  enabled: boolean;
  effectiveSelectable: boolean | null;
  runtimeProfile: RuntimeProfileTemplate;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface AdminProfileCatalog {
  items: AdminWorkspaceProfile[];
  runtimeTemplates: RuntimeProfileTemplate[];
}

export interface AdminAuditEvent {
  id: string;
  workspaceId: string | null;
  action: string;
  result: string;
  createdAt: string | null;
}

export type InternalEgressApplyStatus = "PENDING" | "APPLYING" | "APPLIED" | "FAILED";

export interface InternalEgressRule {
  id: string;
  destinationCidr: string;
  port: number;
  rowVersion: number;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface InternalEgressPolicySnapshot {
  desiredRevision: number;
  desiredDigest: string;
  appliedRevision: number | null;
  appliedDigest: string | null;
  applyStatus: InternalEgressApplyStatus;
  lastErrorCode: string | null;
  lastErrorSummary: string | null;
  updatedAt: string | null;
  rules: InternalEgressRule[];
}

export interface ApiErrorBody {
  code?: string;
  message?: string;
  requestId?: string;
}
