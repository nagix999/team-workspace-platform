import { useCallback, useEffect, useState } from "react";
import { portalApi } from "../api/client";
import type { EnvironmentVariable, ObservedState } from "../api/types";
import { formatDate, presentError } from "../lib/display";

interface EnvironmentVariablesPanelProps {
  workspaceId?: string;
  workspaceState?: ObservedState;
  workspaceBusy?: boolean;
  onStart?: (workspaceId: string) => Promise<void>;
  onRestart?: (workspaceId: string) => Promise<void>;
  onEnvironmentChanged?: () => Promise<void>;
}

interface EnvironmentRestartDialogProps {
  workspaceId?: string;
  workspaceState?: ObservedState;
  action: "saved" | "deleted";
  busy: boolean;
  onClose: () => void;
  onRestart?: (workspaceId: string) => Promise<void>;
}

const environmentNamePattern = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function canReplaceEnvironmentVariable(
  item: EnvironmentVariable,
  replacementValue: string | undefined,
): boolean {
  // Secret values are deliberately never read back.  Treating the empty input
  // as a replacement would therefore make an innocent click erase the stored
  // value (and changing secret -> plain would have the same problem).  A new
  // value must be explicit; plaintext variables can continue to reuse their
  // currently visible value.
  return !item.isSecret || (replacementValue !== undefined && replacementValue.length > 0);
}

export function EnvironmentRestartDialog({
  workspaceId,
  workspaceState,
  action,
  busy,
  onClose,
  onRestart,
}: EnvironmentRestartDialogProps) {
  const canRestartNow = Boolean(
    workspaceId && workspaceState === "RUNNING" && onRestart,
  );
  const scopeMessage = workspaceId
    ? "현재 실행 중인 개발환경에는 변경 전 값이 유지됩니다. 이 환경을 다시 실행하면 변경사항이 적용됩니다."
    : "현재 실행 중인 개발환경에는 변경 전 값이 유지됩니다. 변경사항을 적용하려면 실행 중인 환경을 각각 다시 실행해야 합니다.";

  return (
    <div className="environment-modal-backdrop" role="presentation">
      <section
        className="environment-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="environment-restart-dialog-title"
        aria-describedby="environment-restart-dialog-description"
      >
        <p className="eyebrow">Restart required</p>
        <h3 id="environment-restart-dialog-title">
          {action === "deleted"
            ? "환경변수 삭제는 재실행 후 적용됩니다"
            : "환경변수 변경은 재실행 후 적용됩니다"}
        </h3>
        <p id="environment-restart-dialog-description">{scopeMessage}</p>
        <div className="environment-modal__actions">
          <button
            className="button button--secondary"
            type="button"
            disabled={busy}
            onClick={onClose}
          >
            나중에
          </button>
          {canRestartNow && workspaceId && onRestart && (
            <button
              className="button button--primary"
              type="button"
              disabled={busy}
              onClick={() => void onRestart(workspaceId)}
            >
              {busy ? "재실행 요청 중" : "지금 다시 실행"}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}

export function EnvironmentVariablesPanel({
  workspaceId,
  workspaceState,
  workspaceBusy = false,
  onStart,
  onRestart,
  onEnvironmentChanged,
}: EnvironmentVariablesPanelProps) {
  const [items, setItems] = useState<EnvironmentVariable[]>([]);
  const [restartRequired, setRestartRequired] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyName, setBusyName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [isSecret, setIsSecret] = useState(true);
  const [replacementValues, setReplacementValues] = useState<Record<string, string>>({});
  const [replacementSecrets, setReplacementSecrets] = useState<Record<string, boolean>>({});
  const [restartNotice, setRestartNotice] = useState<"saved" | "deleted" | null>(null);
  const [restartActionBusy, setRestartActionBusy] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await portalApi.environmentVariables(workspaceId, signal);
      setItems(result.items);
      setRestartRequired(result.restartRequired);
      setError(null);
    } catch (requestError) {
      if (requestError instanceof DOMException && requestError.name === "AbortError") return;
      setError(presentError(requestError));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [workspaceId, workspaceState]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!restartNotice) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !restartActionBusy) setRestartNotice(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [restartActionBusy, restartNotice]);

  const save = async (
    variableName: string,
    variableValue: string,
    secret: boolean,
    expectedVersion?: number,
  ) => {
    const normalizedName = variableName.trim();
    if (!environmentNamePattern.test(normalizedName)) {
      setError("환경변수 이름을 확인해 주세요.");
      return;
    }
    setBusyName(normalizedName);
    setError(null);
    try {
      const result = await portalApi.putEnvironmentVariable(
        normalizedName,
        variableValue,
        secret,
        expectedVersion,
        workspaceId,
      );
      setRestartRequired((current) => current || result.restartRequired);
      setName("");
      setValue("");
      setIsSecret(true);
      setReplacementValues((current) => ({ ...current, [normalizedName]: "" }));
      await Promise.all([load(), onEnvironmentChanged?.() ?? Promise.resolve()]);
      if (result.restartRequired) setRestartNotice("saved");
    } catch (requestError) {
      setError(presentError(requestError));
    } finally {
      setBusyName(null);
    }
  };

  const remove = async (item: EnvironmentVariable) => {
    const confirmed = window.confirm(
      `${item.name} 환경변수를 삭제하시겠습니까? 실행 중인 환경에는 다음 재시작부터 반영됩니다.`,
    );
    if (!confirmed) return;
    setBusyName(item.name);
    setError(null);
    try {
      const result = await portalApi.deleteEnvironmentVariable(
        item.name,
        item.version,
        workspaceId,
      );
      setRestartRequired((current) => current || result.restartRequired);
      await Promise.all([load(), onEnvironmentChanged?.() ?? Promise.resolve()]);
      if (result.restartRequired) setRestartNotice("deleted");
    } catch (requestError) {
      setError(presentError(requestError));
    } finally {
      setBusyName(null);
    }
  };

  const isWorkspaceScope = Boolean(workspaceId);
  const canRestart = Boolean(
    workspaceId && workspaceState === "RUNNING" && onRestart,
  );
  const canStartForRestart = Boolean(
    workspaceId && ["STOPPED", "NOT_FOUND", "FAILED"].includes(workspaceState ?? "") && onStart,
  );
  const restartFromDialog = async (targetWorkspaceId: string) => {
    if (!onRestart) return;
    setRestartActionBusy(true);
    try {
      await onRestart(targetWorkspaceId);
      setRestartNotice(null);
    } finally {
      setRestartActionBusy(false);
    }
  };

  return (
    <section
      className={isWorkspaceScope ? "environment-panel environment-panel--workspace" : "panel environment-panel"}
      aria-labelledby={isWorkspaceScope ? `environment-${workspaceId}` : "user-environment-heading"}
      aria-busy={loading}
    >
      <div className="environment-panel__heading">
        <div>
          {!isWorkspaceScope && <p className="eyebrow">Default environment</p>}
          <h3 id={isWorkspaceScope ? `environment-${workspaceId}` : "user-environment-heading"}>
            {isWorkspaceScope ? "환경별 환경변수" : "모든 내 환경의 환경변수"}
          </h3>
          <p>
            {isWorkspaceScope
              ? "같은 이름의 사용자 공통 값을 이 환경에서 덮어씁니다."
              : "새로 생성하거나 다음에 시작하는 모든 내 환경에 적용됩니다."}
          </p>
        </div>
        <span className="item-count" aria-label={`환경변수 ${items.length}개`}>
          {items.length.toString().padStart(2, "0")}
        </span>
      </div>

      {restartRequired && (
        <div className="restart-required" role="status">
          <div>
            <strong>재시작 필요</strong>
            <p>
              {workspaceState === "RUNNING"
                ? "실행 중인 프로세스에는 값을 즉시 주입하지 않습니다. 명시적으로 재시작해야 적용됩니다."
                : isWorkspaceScope
                  ? "다음 시작 시 변경사항을 적용합니다."
                  : "실행 중인 각 환경에서 명시적으로 재시작해야 적용됩니다."}
            </p>
          </div>
          {canRestart && workspaceId && onRestart && (
            <button
              className="button button--primary"
              type="button"
              disabled={workspaceBusy}
              onClick={() => void onRestart(workspaceId)}
            >
              변경사항 적용 (재시작)
            </button>
          )}
          {canStartForRestart && workspaceId && onStart && (
            <button
              className="button button--primary"
              type="button"
              disabled={workspaceBusy}
              onClick={() => void onStart(workspaceId)}
            >
              변경사항 적용하며 시작
            </button>
          )}
        </div>
      )}

      {error && <p className="environment-error" role="alert">{error}</p>}

      {loading ? (
        <p className="environment-empty"><span className="spinner" aria-hidden="true" /> 불러오는 중</p>
      ) : items.length === 0 ? (
        <p className="environment-empty">설정된 환경변수가 없습니다.</p>
      ) : (
        <ul className="environment-list">
          {items.map((item) => (
            <li key={item.name}>
              <div className="environment-item__identity">
                <code>{item.name}</code>
                {item.isSecret ? (
                  <span>비밀 값 설정됨 · 값은 표시하지 않음</span>
                ) : (
                  <span className="environment-plain-value">일반 값: <code>{item.value ?? ""}</code></span>
                )}
                {item.updatedAt && <small>수정 {formatDate(item.updatedAt)}</small>}
              </div>
              <label className="environment-kind">
                <span className="sr-only">{item.name} 값 유형</span>
                <select
                  value={(replacementSecrets[item.name] ?? item.isSecret) ? "secret" : "plain"}
                  disabled={busyName !== null}
                  onChange={(event) => setReplacementSecrets((current) => ({
                    ...current,
                    [item.name]: event.target.value === "secret",
                  }))}
                >
                  <option value="secret">비밀 값</option>
                  <option value="plain">일반 값</option>
                </select>
              </label>
              <label className="environment-replace">
                <span className="sr-only">{item.name} 새 값</span>
                <input
                  type={(replacementSecrets[item.name] ?? item.isSecret) ? "password" : "text"}
                  value={replacementValues[item.name] ?? (item.isSecret ? "" : item.value ?? "")}
                  placeholder={item.isSecret ? "새 값 입력" : "일반 값"}
                  maxLength={16384}
                  autoComplete="new-password"
                  disabled={busyName !== null}
                  onChange={(event) => setReplacementValues((current) => ({
                    ...current,
                    [item.name]: event.target.value,
                  }))}
                />
                {item.isSecret && !canReplaceEnvironmentVariable(
                  item,
                  replacementValues[item.name],
                ) && <small>기존 비밀값을 변경하려면 새 값을 입력하세요.</small>}
              </label>
              <div className="environment-item__actions">
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={busyName !== null || !canReplaceEnvironmentVariable(
                    item,
                    replacementValues[item.name],
                  )}
                  onClick={() => void save(
                    item.name,
                    replacementValues[item.name] ?? (item.isSecret ? "" : item.value ?? ""),
                    replacementSecrets[item.name] ?? item.isSecret,
                    item.version,
                  )}
                >
                  {busyName === item.name ? "저장 중" : "값 변경"}
                </button>
                <button
                  className="text-button text-button--danger"
                  type="button"
                  disabled={busyName !== null}
                  onClick={() => void remove(item)}
                >
                  삭제
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="environment-create">
        <label>
          <span>이름</span>
          <input
            type="text"
            value={name}
            placeholder="예: MODEL_CACHE_DIR"
            maxLength={64}
            autoComplete="off"
            spellCheck={false}
            disabled={busyName !== null}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label>
          <span>값 유형</span>
          <select
            value={isSecret ? "secret" : "plain"}
            disabled={busyName !== null}
            onChange={(event) => setIsSecret(event.target.value === "secret")}
          >
            <option value="secret">비밀 값</option>
            <option value="plain">일반 값</option>
          </select>
        </label>
        <label>
          <span>값</span>
          <input
            type={isSecret ? "password" : "text"}
            value={value}
            placeholder="값 입력"
            maxLength={16384}
            autoComplete="new-password"
            disabled={busyName !== null}
            onChange={(event) => setValue(event.target.value)}
          />
        </label>
        <button
          className="button button--primary"
          type="button"
          disabled={busyName !== null || !environmentNamePattern.test(name.trim())}
          onClick={() => void save(name, value, isSecret)}
        >
          추가
        </button>
      </div>
      <p className="environment-policy-hint">
        비밀 값은 저장 후 다시 표시되지 않으며, 일반 값은 소유자 화면에서 조회·편집할 수 있습니다.
      </p>
      {restartNotice && (
        <EnvironmentRestartDialog
          workspaceId={workspaceId}
          workspaceState={workspaceState}
          action={restartNotice}
          busy={restartActionBusy}
          onClose={() => setRestartNotice(null)}
          onRestart={onRestart ? restartFromDialog : undefined}
        />
      )}
    </section>
  );
}
