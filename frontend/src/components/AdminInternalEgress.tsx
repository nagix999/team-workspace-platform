import { useCallback, useEffect, useState } from "react";
import { portalApi } from "../api/client";
import type { InternalEgressPolicySnapshot, InternalEgressRule } from "../api/types";
import { presentError } from "../lib/display";


const BLOCKED_PORTS = new Set([2375, 2376, 2377, 3128, 4243, 6443, 10250]);

function isCanonicalPrivateHostRoute(value: string): boolean {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/32$/.exec(value);
  if (!match) return false;
  const octets = match.slice(1).map(Number);
  if (octets.some((octet) => octet < 0 || octet > 255)) return false;
  if (`${octets.join(".")}/32` !== value) return false;
  const [first, second] = octets;
  return first === 10 || (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168);
}

function statusLabel(policy: InternalEgressPolicySnapshot): string {
  if (policy.applyStatus === "APPLIED") return "적용 완료";
  if (policy.applyStatus === "APPLYING") return "적용 중";
  if (policy.applyStatus === "FAILED") {
    return policy.appliedRevision === null
      ? "적용 실패 · 기본 차단 정책 유지 중"
      : "적용 실패 · 기존 적용 정책 유지 중";
  }
  return "적용 대기";
}

export function AdminInternalEgress() {
  const [snapshot, setSnapshot] = useState<InternalEgressPolicySnapshot | null>(null);
  const [cidr, setCidr] = useState("");
  const [port, setPort] = useState("8000");
  const [editing, setEditing] = useState<InternalEgressRule | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await portalApi.adminInternalEgressPolicy(signal);
      setSnapshot(result);
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
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!snapshot || snapshot.applyStatus === "APPLIED" || snapshot.applyStatus === "FAILED") {
      return;
    }
    const timer = window.setInterval(() => void load(), 1_500);
    return () => window.clearInterval(timer);
  }, [load, snapshot]);

  const reset = () => {
    setEditing(null);
    setCidr("");
    setPort("8000");
  };

  const edit = (rule: InternalEgressRule) => {
    setEditing(rule);
    setCidr(rule.destinationCidr);
    setPort(String(rule.port));
    setError(null);
    setNotice(null);
  };

  const save = async () => {
    if (!snapshot) {
      setError("정책을 먼저 불러와 주세요.");
      return;
    }
    const destinationCidr = cidr.trim();
    const parsedPort = Number(port);
    if (
      !isCanonicalPrivateHostRoute(destinationCidr) ||
      !Number.isSafeInteger(parsedPort) || parsedPort < 1024 || parsedPort > 65535 ||
      BLOCKED_PORTS.has(parsedPort)
    ) {
      setError("사설 IPv4 단일 호스트를 /32로 입력하고 올바른 TCP 포트를 지정해 주세요.");
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = editing
        ? await portalApi.updateAdminInternalEgressRule({
            id: editing.id,
            destinationCidr,
            port: parsedPort,
            expectedVersion: editing.rowVersion,
            expectedRevision: snapshot.desiredRevision,
          })
        : await portalApi.createAdminInternalEgressRule(
            destinationCidr,
            parsedPort,
            snapshot.desiredRevision,
          );
      setSnapshot(result);
      setNotice(editing ? "허용 대상을 수정하고 적용을 요청했습니다." : "허용 대상을 추가하고 적용을 요청했습니다.");
      reset();
    } catch (requestError) {
      setError(presentError(requestError));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (rule: InternalEgressRule) => {
    if (!snapshot) return;
    if (!window.confirm(`${rule.destinationCidr}:${rule.port} 허용을 삭제할까요?`)) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      setSnapshot(await portalApi.deleteAdminInternalEgressRule(
        rule.id,
        rule.rowVersion,
        snapshot.desiredRevision,
      ));
      if (editing?.id === rule.id) reset();
      setNotice("허용 대상을 삭제하고 적용을 요청했습니다.");
    } catch (requestError) {
      setError(presentError(requestError));
    } finally {
      setSaving(false);
    }
  };

  const retry = async () => {
    if (!snapshot) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      setSnapshot(await portalApi.retryAdminInternalEgressPolicy(snapshot.desiredRevision));
      setNotice("같은 정책의 적용을 다시 요청했습니다.");
    } catch (requestError) {
      setError(presentError(requestError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="admin-egress" aria-labelledby="admin-egress-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Proxy exception</p>
          <h2 id="admin-egress-heading">내부 서비스 통신</h2>
          <p>노트북이 Squid를 통해 접근할 사설 IPv4 단일 호스트와 TCP 포트만 등록합니다.</p>
        </div>
      </div>

      <div className="alert alert--warning">
        <span className="alert__icon">!</span>
        <div>
          <strong>직접 사내망 접근은 계속 차단됩니다.</strong>
          <p>
            대상 서비스는 운영 호스트의 LAN 주소 또는 0.0.0.0에 bind되어야 하며
            127.0.0.1에만 bind하면 proxy container에서 접근할 수 없습니다.
          </p>
        </div>
      </div>

      {error && <p className="workspace-error" role="alert">{error}</p>}
      {notice && <p className="inline-result" role="status">{notice}</p>}

      {snapshot && (
        <div className={`admin-egress__status admin-egress__status--${snapshot.applyStatus.toLowerCase()}`}>
          <strong>{statusLabel(snapshot)}</strong>
          <span>요청 {snapshot.desiredRevision} · 적용 {snapshot.appliedRevision ?? "-"}</span>
          {snapshot.lastErrorCode && <code>{snapshot.lastErrorCode}</code>}
          {snapshot.lastErrorSummary && <p>{snapshot.lastErrorSummary}</p>}
          {snapshot.appliedRevision !== snapshot.desiredRevision && (
            <p>
              아래 규칙은 요청(desired) 정책이며 현재 proxy의 실제 적용 목록이 아닙니다.
              {snapshot.appliedRevision === null
                ? " 현재는 기본 전체 차단 정책이 유지됩니다."
                : ` proxy에는 이전 적용 revision ${snapshot.appliedRevision}의 규칙이 유지됩니다.`}
            </p>
          )}
          {snapshot.applyStatus === "FAILED" && (
            <button type="button" className="button button--secondary" onClick={() => void retry()} disabled={saving}>
              적용 재시도
            </button>
          )}
        </div>
      )}

      <div className="admin-egress__editor">
        <label>
          <span>대상 CIDR</span>
          <input
            value={cidr}
            onChange={(event) => setCidr(event.target.value)}
            placeholder="<PRIVATE_IPV4>/32"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <label>
          <span>TCP 포트</span>
          <input
            type="number"
            min="1024"
            max="65535"
            value={port}
            onChange={(event) => setPort(event.target.value)}
          />
        </label>
        <div className="admin-egress__actions">
          <button type="button" className="button button--primary" onClick={() => void save()} disabled={saving || !snapshot}>
            {editing ? "수정 적용" : "추가 적용"}
          </button>
          {editing && (
            <button type="button" className="button button--secondary" onClick={reset} disabled={saving}>취소</button>
          )}
        </div>
      </div>

      {loading ? (
        <p className="empty-copy">정책을 불러오는 중입니다.</p>
      ) : snapshot?.rules.length ? (
        <div className="admin-egress__rules">
          {snapshot.rules.map((rule) => (
            <article key={rule.id}>
              <div>
                <code>{rule.destinationCidr}:{rule.port}</code>
                <span>버전 {rule.rowVersion}</span>
              </div>
              <div className="admin-egress__actions">
                <button type="button" className="button button--secondary" onClick={() => edit(rule)} disabled={saving}>수정</button>
                <button type="button" className="button button--danger" onClick={() => void remove(rule)} disabled={saving}>삭제</button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <p className="empty-copy">등록된 내부 서비스가 없습니다. 기본 정책은 모든 사설 목적지를 차단합니다.</p>
      )}

      <p className="admin-resource-note">
        정책 변경은 재시작 없이 새 요청에 반영됩니다. 이미 열린 HTTP/CONNECT 연결을 강제로
        종료하지는 않으므로 즉시 폐기가 필요하면 운영자가 egress 연결을 drain해야 합니다.
      </p>
    </section>
  );
}
