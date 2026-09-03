import type { Operation, Workspace, WorkspaceProfile } from "../api/types";
import {
  formatDate,
  formatMegabytes,
  operationStatusPresentation,
  workspaceStatusPresentation,
} from "../lib/display";
import { StatusBadge } from "./StatusBadge";
import { EnvironmentVariablesPanel } from "./EnvironmentVariablesPanel";

interface WorkspaceCardProps {
  workspace: Workspace;
  profile: WorkspaceProfile | null;
  operation: Operation | null;
  busy: boolean;
  launchUrl: string;
  onStart: (workspaceId: string) => Promise<void>;
  onStop: (workspaceId: string) => Promise<void>;
  onRestart: (workspaceId: string) => Promise<void>;
  onDelete: (workspaceId: string) => Promise<void>;
  onEnvironmentChanged: () => Promise<void>;
}

const activeOperationStatuses = new Set(["PENDING", "RUNNING", "WAITING_EXTERNAL"]);

function operationLabel(operation: Operation): string {
  if (operation.status === "WAITING_EXTERNAL") return "저장공간 삭제 중";
  const operationType = operation.operationType;
  if (operationType === "CREATE") return "환경 생성";
  if (operationType === "START") return "환경 시작";
  if (operationType === "STOP") return "환경 중지";
  if (operationType === "RESTART") return "환경 재시작";
  if (operationType === "DELETE") return "환경 삭제";
  return "환경 작업";
}

export function WorkspaceCard({
  workspace,
  profile,
  operation,
  busy,
  launchUrl,
  onStart,
  onStop,
  onRestart,
  onDelete,
  onEnvironmentChanged,
}: WorkspaceCardProps) {
  const status = workspaceStatusPresentation(workspace.observedState);
  const operationActive = operation
    ? activeOperationStatuses.has(operation.status)
    : false;
  const deletionPending = workspace.desiredState === "DELETED" &&
    ["PENDING", "RUNNING"].includes(workspace.deletionStatus ?? "");
  const deletionRetryAvailable = workspace.desiredState === "DELETED" &&
    workspace.canRetryDelete && (
      workspace.deletionStatus === "FAILED" || workspace.lastErrorCode === "AUTH_REQUIRED"
    );
  const lifecycleAvailable = workspace.desiredState !== "DELETED";
  const intentRecoveryStop = lifecycleAvailable &&
    workspace.desiredState === "RUNNING" &&
    ["NOT_FOUND", "STOPPED", "FAILED"].includes(workspace.observedState);
  const controlsLocked = busy || operationActive || deletionPending || (
    workspace.stale && !deletionRetryAvailable && !intentRecoveryStop
  );
  const canLaunch = lifecycleAvailable && workspace.observedState === "RUNNING" && !workspace.stale;
  const canStart = lifecycleAvailable && workspace.desiredState !== "RUNNING" &&
    ["NOT_FOUND", "STOPPED", "FAILED"].includes(workspace.observedState);
  const canStop = lifecycleAvailable && (
    intentRecoveryStop || ["STARTING", "RUNNING", "UNKNOWN"].includes(workspace.observedState)
  );
  const progress = Math.min(
    100,
    Math.max(0, operation?.progressPercent ?? workspace.progressPercent ?? 0),
  );
  const showProgress = operationActive || ["STARTING", "STOPPING"].includes(workspace.observedState);
  const title = workspace.name || "개발환경";
  const profileTitle = profile?.name ?? workspace.profileName ?? workspace.profileId;
  const updatedAt = formatDate(workspace.updatedAt ?? workspace.createdAt);
  const kernelName = profile?.kernelDisplayName ??
    workspace.kernelDisplayName ?? workspace.kernelName;
  const pythonVersion = profile?.pythonVersion ?? workspace.pythonVersion;
  const cpuLimit = profile?.cpuLimit ?? workspace.cpuLimit;
  const memoryLimitMb = profile?.memoryLimitMb ?? workspace.memoryLimitMb;
  const privateDiskLimitMb = profile?.privateDiskLimitMb ?? workspace.privateDiskLimitMb;
  const privateDiskQuotaEnforced = profile?.privateDiskQuotaEnforced ??
    workspace.privateDiskQuotaEnforced;
  const hasProfileDetails = Boolean(
    kernelName || pythonVersion || cpuLimit || memoryLimitMb ||
      privateDiskLimitMb || privateDiskQuotaEnforced !== null,
  );

  return (
    <article className="workspace-card">
      <div className="workspace-card__top">
        <div className="workspace-card__identity">
          <span className="workspace-icon" aria-hidden="true">
            <span>_</span>
          </span>
          <div>
            <h3>{title}</h3>
            <p>
              {profileTitle} {profile
                ? `v${profile.version}`
                : workspace.profileVersion ? `v${workspace.profileVersion}` : ""}
              <span aria-hidden="true"> · </span>
              <span className="workspace-id">{workspace.id.slice(0, 8)}</span>
            </p>
          </div>
        </div>
        <div className="workspace-card__status">
          <StatusBadge status={status} stale={workspace.stale} />
          {workspace.restartRequired && <span className="restart-chip">재시작 필요</span>}
        </div>
      </div>

      {hasProfileDetails && (
        <ul className="resource-list workspace-profile-details" aria-label="개발환경 설정">
          {kernelName && <li>기본 커널 {kernelName}</li>}
          {pythonVersion && <li>기본 노트북/터미널 Python {pythonVersion}</li>}
          {cpuLimit && <li>CPU {cpuLimit}</li>}
          {memoryLimitMb !== null && <li>메모리 {formatMegabytes(memoryLimitMb)}</li>}
          {privateDiskQuotaEnforced === true ? (
            privateDiskLimitMb !== null ? (
              <li>저장공간 하드 제한 {formatMegabytes(privateDiskLimitMb)}</li>
            ) : (
              <li className="resource-warning">저장공간 제한 정보 확인 필요</li>
            )
          ) : privateDiskQuotaEnforced === false ? (
            <>
              <li>저장공간 개별 하드 제한 없음 (호스트 가용량까지)</li>
              <li className="resource-warning">
                호스트 저장공간은 모든 개발환경이 함께 사용합니다.
              </li>
            </>
          ) : privateDiskLimitMb !== null ? (
            <li className="resource-warning">
              저장공간 정책값 {formatMegabytes(privateDiskLimitMb)} · 제한 적용 여부 확인 필요
            </li>
          ) : null}
        </ul>
      )}

      {showProgress && (
        <div className="workspace-progress" aria-live="polite">
          <div className="workspace-progress__text">
            <span>{operation ? operationLabel(operation) : status.label}</span>
            <strong>{progress}%</strong>
          </div>
          <div
            className="workspace-progress__track"
            role="progressbar"
            aria-label={`${title} ${operation ? operationLabel(operation) : status.label}`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={progress}
          >
            <span style={{ width: `${progress}%` }} />
          </div>
          {operation?.message && <p>{operation.message}</p>}
        </div>
      )}

      {operation && !operationActive && operation.status !== "SUCCEEDED" && (
        <div className={`inline-result inline-result--${operationStatusPresentation(operation.status).tone}`}>
          <strong>{operationStatusPresentation(operation.status).label}</strong>
          <span>{operation.errorSummary ?? operation.message ?? "요청을 완료하지 못했습니다."}</span>
        </div>
      )}

      {(workspace.lastErrorSummary || workspace.lastErrorCode) && !operationActive && (
        <div className="workspace-error">
          <strong>최근 오류</strong>
          <p>{workspace.lastErrorSummary ?? workspace.lastErrorCode}</p>
        </div>
      )}

      {deletionPending && (
        <div className="deletion-pending" role="status">
          <span className="spinner" aria-hidden="true" />
          <div>
            <strong>개인 환경 삭제 진행 중</strong>
            <p>개인 데이터 삭제와 환경 정리가 끝날 때까지 다른 작업을 잠급니다. 공유 디렉터리는 영향을 받지 않습니다.</p>
          </div>
        </div>
      )}

      <div className="workspace-card__footer">
        <p className="updated-at">
          {updatedAt ? `마지막 갱신 ${updatedAt}` : "상태를 확인하는 중"}
        </p>
        <div className="workspace-actions">
          {canLaunch ? (
            <a
              className="button button--primary"
              href={launchUrl}
              target="_blank"
              rel="noopener noreferrer"
            >
              Jupyter 열기 <span aria-hidden="true">↗</span>
            </a>
          ) : (
            <button className="button button--primary" type="button" disabled>
              Jupyter 열기
            </button>
          )}

          {canStop ? (
            <button
              className="button button--secondary"
              type="button"
              disabled={controlsLocked || workspace.observedState === "STOPPING"}
              onClick={() => void onStop(workspace.id)}
            >
              {busy ? "요청 중" : "중지"}
            </button>
          ) : (
            <button
              className="button button--secondary"
              type="button"
              disabled={controlsLocked || !canStart}
              onClick={() => void onStart(workspace.id)}
            >
              {busy ? "요청 중" : "시작"}
            </button>
          )}
          {workspace.restartRequired && canLaunch && (
            <button
              className="button button--primary"
              type="button"
              disabled={controlsLocked}
              onClick={() => void onRestart(workspace.id)}
            >
              변경사항 적용 (재시작)
            </button>
          )}
          <button
            className="button button--danger"
            type="button"
            disabled={controlsLocked || (workspace.desiredState === "DELETED" && !deletionRetryAvailable)}
            onClick={() => {
              const confirmed = window.confirm(
                `${deletionRetryAvailable ? "삭제에 실패한 " : ""}${title} 환경을 ${deletionRetryAvailable ? "다시 삭제" : "삭제"}하시겠습니까? 개인 작업 데이터는 영구 삭제되며 복구할 수 없습니다. 팀 공유 디렉터리의 데이터는 삭제되지 않습니다.`,
              );
              if (confirmed) void onDelete(workspace.id);
            }}
          >
            {deletionRetryAvailable ? "삭제 재시도" : "삭제"}
          </button>
        </div>
      </div>

      {workspace.desiredState !== "DELETED" && (
        <details className="workspace-environment-details">
          <summary>환경변수 관리</summary>
          <EnvironmentVariablesPanel
            workspaceId={workspace.id}
            workspaceState={workspace.observedState}
            workspaceBusy={busy || operationActive}
            onStart={onStart}
            onRestart={onRestart}
            onEnvironmentChanged={onEnvironmentChanged}
          />
        </details>
      )}
    </article>
  );
}
