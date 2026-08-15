import type { ObservedState, OperationStatus } from "../api/types";
import { ApiError } from "../api/client";

export interface StatusPresentation {
  label: string;
  tone: "neutral" | "info" | "success" | "warning" | "danger";
}

const workspaceStatus: Record<ObservedState, StatusPresentation> = {
  NOT_FOUND: { label: "생성 대기", tone: "neutral" },
  STARTING: { label: "시작 중", tone: "info" },
  RUNNING: { label: "실행 중", tone: "success" },
  STOPPING: { label: "중지 중", tone: "warning" },
  STOPPED: { label: "중지됨", tone: "neutral" },
  DELETION_PENDING: { label: "삭제 대기", tone: "warning" },
  DELETING: { label: "삭제 중", tone: "warning" },
  FAILED: { label: "오류", tone: "danger" },
  UNKNOWN: { label: "확인 필요", tone: "warning" },
};

const operationStatus: Record<OperationStatus, StatusPresentation> = {
  PENDING: { label: "요청 대기", tone: "info" },
  RUNNING: { label: "처리 중", tone: "info" },
  WAITING_EXTERNAL: { label: "저장공간 삭제 중", tone: "warning" },
  SUCCEEDED: { label: "완료", tone: "success" },
  FAILED: { label: "실패", tone: "danger" },
  AUTH_REQUIRED: { label: "재로그인 필요", tone: "warning" },
  CANCELLED: { label: "취소됨", tone: "neutral" },
};

export function workspaceStatusPresentation(state: ObservedState): StatusPresentation {
  return workspaceStatus[state] ?? { label: state, tone: "neutral" };
}

export function operationStatusPresentation(state: OperationStatus): StatusPresentation {
  return operationStatus[state] ?? { label: state, tone: "neutral" };
}

const errorMessages: Record<string, string> = {
  AUTH_REQUIRED: "인증 정보가 만료되었습니다. 다시 로그인한 뒤 요청해 주세요.",
  PROVISIONING_REQUIRED: "개인 저장공간을 준비하고 있습니다. 관리자의 활성화를 기다려 주세요.",
  EXECUTION_HOST_UNHEALTHY: "실행 호스트의 네트워크 또는 저장공간 점검이 필요합니다.",
  CAPACITY_LIMIT: "전체 실행 한도에 도달했습니다. 다른 환경이 중지된 뒤 다시 시도해 주세요.",
  RESOURCE_CAPACITY_LIMIT:
    "CPU 또는 메모리 여유가 부족합니다. 다른 환경이 중지되거나 더 작은 설정을 선택한 뒤 다시 시도해 주세요.",
  WORKSPACE_LIMIT: "사용자당 환경 5개 한도에 도달했습니다.",
  WORKSPACE_QUOTA_EXCEEDED: "사용자당 환경 5개 한도에 도달했습니다.",
  PROFILE_DISABLED: "선택한 프로필은 현재 사용할 수 없습니다.",
  PROFILE_VERSION_CONFLICT: "다른 관리자가 프로필을 먼저 변경했습니다. 새로고침 후 다시 시도해 주세요.",
  RESOURCE_POLICY_VERSION_CONFLICT: "다른 관리자가 자원 정책을 먼저 변경했습니다. 새로고침 후 다시 시도해 주세요.",
  RESOURCE_BUDGET_BELOW_RESERVED: "현재 예약량보다 전체 자원 예산을 낮출 수 없습니다.",
  RESOURCE_BUDGET_EXCEEDS_HARD_CEILING: "호스트 기준 최대 자원 예산을 초과했습니다.",
  RESOURCE_SELECTION_INVALID: "검증된 실행 프로필에 없는 CPU 또는 메모리 값입니다.",
  RESOURCE_SELECTION_EMPTY: "선택한 CPU·메모리 조합으로 제공할 수 있는 프로필이 없습니다.",
  ENVIRONMENT_NAME_INVALID: "환경변수 이름은 영문자 또는 밑줄로 시작하는 ASCII 식별자여야 합니다.",
  ENVIRONMENT_NAME_RESERVED: "플랫폼이 관리하는 예약 환경변수 이름은 사용할 수 없습니다.",
  ENVIRONMENT_VERSION_CONFLICT: "환경변수가 다른 요청에서 변경되었습니다. 새로고침 후 다시 시도해 주세요.",
  ENVIRONMENT_VARIABLE_LIMIT: "이 범위에서 설정할 수 있는 환경변수 수를 초과했습니다.",
  NETWORK_ERROR: "포털 API에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요.",
  SERVICE_UNAVAILABLE: "서비스가 일시적으로 응답하지 않습니다. 잠시 후 다시 시도해 주세요.",
};

export function presentError(error: unknown): string {
  if (error instanceof ApiError) {
    const message = errorMessages[error.code] ?? error.message;
    return error.requestId ? `${message} (요청 ID: ${error.requestId})` : message;
  }
  return "예상하지 못한 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.";
}

export function formatMegabytes(value: number | null): string | null {
  if (value === null) return null;
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024} GB`;
  return `${value.toLocaleString("ko-KR")} MB`;
}

export function formatDate(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
