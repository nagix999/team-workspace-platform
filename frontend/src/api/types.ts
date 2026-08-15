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

export interface Workspace {
  id: string;
  name: string;
  profileId: string;
  profileVersion: number | null;
  profileName: string | null;
  kernelName: string | null;
  kernelDisplayName: string | null;
  pythonVersion: string | null;
  cpuLimit: string | null;
  memoryLimitMb: number | null;
  privateDiskLimitMb: number | null;
  privateDiskQuotaEnforced: boolean | null;
  desiredState: DesiredState;
  observedState: ObservedState;
  progressPercent: number | null;
  stale: boolean;
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
}

export interface ResourcePolicy {
  version: number;
  cpuBudgetMillicores: number;
  memoryBudgetMb: number;
  selectableCpuMillicores: number[];
  selectableMemoryMb: number[];
  availableCpuMillicores: number[];
  availableMemoryMb: number[];
  maxCpuBudgetMillicores: number | null;
  maxMemoryBudgetMb: number | null;
  updatedAt: string | null;
}

export interface ResourcePolicyUpdate {
  version: number;
  cpuBudgetMillicores: number;
  memoryBudgetMb: number;
  selectableCpuMillicores: number[];
  selectableMemoryMb: number[];
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

export interface ApiErrorBody {
  code?: string;
  message?: string;
  requestId?: string;
}
