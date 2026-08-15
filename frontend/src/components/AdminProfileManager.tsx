import { useCallback, useEffect, useMemo, useState } from "react";
import { portalApi } from "../api/client";
import type {
  AdminWorkspaceProfile,
  ResourcePolicy,
  RuntimeProfileTemplate,
} from "../api/types";
import { formatMegabytes, presentError } from "../lib/display";
import { cpuLimitToMillicores } from "../lib/profiles";

interface AdminProfileManagerProps {
  onProfilesChanged: () => Promise<void>;
  resourcePolicy: ResourcePolicy | null;
}

function templateKey(template: RuntimeProfileTemplate): string {
  return `${template.id}:${template.version}`;
}

interface AdminKernelGroup {
  key: string;
  kernelName: string;
  displayName: string;
  pythonVersion: string;
  profiles: AdminWorkspaceProfile[];
}

export function groupAdminProfilesByKernel(
  profiles: AdminWorkspaceProfile[],
): AdminKernelGroup[] {
  const groups = new Map<string, AdminKernelGroup>();
  for (const profile of profiles) {
    const runtime = profile.runtimeProfile;
    const key = `${runtime.kernelName}:${runtime.pythonVersion}`;
    const group = groups.get(key) ?? {
      key,
      kernelName: runtime.kernelName,
      displayName: runtime.kernelDisplayName,
      pythonVersion: runtime.pythonVersion,
      profiles: [],
    };
    group.profiles.push(profile);
    groups.set(key, group);
  }
  return [...groups.values()]
    .map((group) => ({
      ...group,
      profiles: [...group.profiles].sort((left, right) =>
        Number(left.runtimeProfile.cpuLimit) - Number(right.runtimeProfile.cpuLimit) ||
        left.runtimeProfile.memoryLimitMb - right.runtimeProfile.memoryLimitMb ||
        left.id.localeCompare(right.id)),
    }))
    .sort((left, right) =>
      left.pythonVersion.localeCompare(right.pythonVersion) || left.key.localeCompare(right.key));
}

export function AdminProfileManager({
  onProfilesChanged,
  resourcePolicy,
}: AdminProfileManagerProps) {
  const [profiles, setProfiles] = useState<AdminWorkspaceProfile[]>([]);
  const [templates, setTemplates] = useState<RuntimeProfileTemplate[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [selectedTemplateKey, setSelectedTemplateKey] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const catalog = await portalApi.adminProfiles(signal);
      setProfiles(catalog.items);
      setTemplates(catalog.runtimeTemplates);
      setSelectedTemplateKey((current) => current || (
        catalog.runtimeTemplates[0] ? templateKey(catalog.runtimeTemplates[0]) : ""
      ));
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

  const editingProfile = useMemo(
    () => profiles.find((profile) => profile.id === editingId) ?? null,
    [editingId, profiles],
  );
  const representedTemplateKeys = useMemo(
    () => new Set(profiles.map((profile) => templateKey(profile.runtimeProfile))),
    [profiles],
  );
  const missingTemplates = useMemo(
    () => templates.filter((template) => !representedTemplateKeys.has(templateKey(template))),
    [representedTemplateKeys, templates],
  );
  const kernelGroups = useMemo(
    () => groupAdminProfilesByKernel(profiles),
    [profiles],
  );
  const selectedTemplate = useMemo(() => {
    if (editingProfile) return editingProfile.runtimeProfile;
    return missingTemplates.find((template) => templateKey(template) === selectedTemplateKey) ?? null;
  }, [editingProfile, missingTemplates, selectedTemplateKey]);

  useEffect(() => {
    if (editingProfile) return;
    if (!missingTemplates.some((template) => templateKey(template) === selectedTemplateKey)) {
      setSelectedTemplateKey(missingTemplates[0] ? templateKey(missingTemplates[0]) : "");
    }
  }, [editingProfile, missingTemplates, selectedTemplateKey]);

  const resetForm = () => {
    setEditingId(null);
    setName("");
    setDescription("");
    setEnabled(true);
    setSelectedTemplateKey(missingTemplates[0] ? templateKey(missingTemplates[0]) : "");
  };

  const beginEdit = (profile: AdminWorkspaceProfile) => {
    setEditingId(profile.id);
    setName(profile.name);
    setDescription(profile.description ?? "");
    setEnabled(profile.enabled);
    setSelectedTemplateKey(templateKey(profile.runtimeProfile));
    setNotice(null);
    setError(null);
  };

  const save = async () => {
    const normalizedName = name.trim();
    const normalizedDescription = description.trim() || null;
    if (!normalizedName || normalizedName.length > 80 || !selectedTemplate) {
      setError("프로필 이름과 검증된 실행 템플릿을 확인해 주세요.");
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      if (editingProfile) {
        await portalApi.updateAdminProfile({
          id: editingProfile.id,
          version: editingProfile.version,
          name: normalizedName,
          description: normalizedDescription,
          enabled,
        });
        setNotice("프로필 이름, 설명, 공개 여부를 수정했습니다.");
      } else {
        await portalApi.createAdminProfile({
          name: normalizedName,
          description: normalizedDescription,
          runtimeProfileId: selectedTemplate.id,
          runtimeProfileVersion: selectedTemplate.version,
          enabled,
        });
        setNotice("검증된 실행 템플릿으로 새 프로필을 만들었습니다.");
      }
      resetForm();
      await Promise.all([load(), onProfilesChanged()]);
    } catch (requestError) {
      setError(presentError(requestError));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (profile: AdminWorkspaceProfile) => {
    const confirmed = window.confirm(
      `${profile.name} 프로필을 비공개 처리하시겠습니까? 기존 환경에는 영향을 주지 않으며 새 환경 생성 목록에서만 제거됩니다.`,
    );
    if (!confirmed) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      await portalApi.deleteAdminProfile(profile.id, profile.version);
      if (editingId === profile.id) resetForm();
      setNotice("프로필을 비공개 처리했습니다.");
      await Promise.all([load(), onProfilesChanged()]);
    } catch (requestError) {
      setError(presentError(requestError));
    } finally {
      setSaving(false);
    }
  };

  const setKernelVisibility = async (group: AdminKernelGroup, visible: boolean) => {
    const targets = group.profiles.filter((profile) => profile.enabled !== visible);
    if (targets.length === 0) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      for (const profile of targets) {
        await portalApi.updateAdminProfile({
          id: profile.id,
          version: profile.version,
          name: profile.name,
          description: profile.description,
          enabled: visible,
        });
      }
      setNotice(
        `${group.displayName}의 CPU·Memory 조합 ${targets.length}개를 ${visible ? "공개" : "비공개"}했습니다.`,
      );
      await Promise.all([load(), onProfilesChanged()]);
    } catch (requestError) {
      setError(presentError(requestError));
      await load();
    } finally {
      setSaving(false);
    }
  };

  const createMissingProfiles = async () => {
    if (missingTemplates.length === 0) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      for (const template of missingTemplates) {
        await portalApi.createAdminProfile({
          name: `${template.kernelDisplayName} · CPU ${template.cpuLimit} · ${formatMegabytes(template.memoryLimitMb)}`,
          description: `Python ${template.pythonVersion} 검증 실행 조합`,
          runtimeProfileId: template.id,
          runtimeProfileVersion: template.version,
          enabled: true,
        });
      }
      setNotice(`누락된 검증 조합 ${missingTemplates.length}개를 생성하고 공개했습니다.`);
      await Promise.all([load(), onProfilesChanged()]);
    } catch (requestError) {
      setError(presentError(requestError));
      await load();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="admin-profiles">
      <div className="admin-workspaces__heading">
        <div>
          <h3>개발환경 프로필</h3>
          <p>검증된 Python 커널 × CPU × Memory의 모든 조합을 먼저 생성하고, 사용자에게 공개할 조합만 선택합니다. 이미지와 실행 명령은 화면에서 입력할 수 없습니다.</p>
        </div>
        <span className="item-count" aria-label={`관리 프로필 ${profiles.length}개`}>
          {profiles.length}
        </span>
      </div>

      {error && <p className="admin-message admin-message--error" role="alert">{error}</p>}
      {notice && <p className="admin-message" role="status">{notice}</p>}

      <section className="admin-kernel-matrix" aria-labelledby="admin-kernel-matrix-heading">
        <div className="admin-kernel-matrix__heading">
          <div>
            <h4 id="admin-kernel-matrix-heading">Python 커널 공개 설정</h4>
            <p>
              검증 템플릿 {templates.length}개 중 {profiles.length}개 조합이 생성되어 있습니다.
              CPU와 Memory 값은 별도 자원 정책 메뉴에서 제한합니다.
            </p>
          </div>
          {loading ? (
            <span className="matrix-complete">조합 확인 중</span>
          ) : missingTemplates.length > 0 ? (
            <button
              className="button button--primary"
              type="button"
              disabled={saving}
              onClick={() => void createMissingProfiles()}
            >누락 조합 {missingTemplates.length}개 모두 생성</button>
          ) : (
            <span className="matrix-complete">모든 검증 조합 생성됨</span>
          )}
        </div>
        <div className="admin-kernel-grid">
          {kernelGroups.map((group) => {
            const enabledCount = group.profiles.filter((profile) => profile.enabled).length;
            return (
              <article key={group.key}>
                <div>
                  <strong>{group.displayName}</strong>
                  <span>Python {group.pythonVersion} · {enabledCount}/{group.profiles.length}개 공개</span>
                </div>
                <div className="admin-kernel-grid__actions">
                  <button
                    className="button button--secondary"
                    type="button"
                    disabled={saving || enabledCount === group.profiles.length}
                    onClick={() => void setKernelVisibility(group, true)}
                  >모든 조합 공개</button>
                  <button
                    className="button button--secondary"
                    type="button"
                    disabled={saving || enabledCount === 0}
                    onClick={() => void setKernelVisibility(group, false)}
                  >모든 조합 비공개</button>
                </div>
              </article>
            );
          })}
          {!loading && kernelGroups.length === 0 && (
            <p className="admin-table-empty">생성된 Python 실행 조합이 없습니다.</p>
          )}
        </div>
      </section>

      {(editingProfile || missingTemplates.length > 0) && <div className="admin-profile-editor">
        <div className="admin-profile-editor__fields">
          <label>
            <span>검증된 실행 템플릿</span>
            <select
              value={selectedTemplate ? templateKey(selectedTemplate) : ""}
              disabled={saving || Boolean(editingProfile)}
              onChange={(event) => setSelectedTemplateKey(event.target.value)}
            >
              {(editingProfile ? [editingProfile.runtimeProfile] : missingTemplates).map((template) => (
                <option key={templateKey(template)} value={templateKey(template)}>
                  {template.kernelDisplayName} · Python {template.pythonVersion} · CPU {template.cpuLimit} · {formatMegabytes(template.memoryLimitMb)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>프로필 이름</span>
            <input
              type="text"
              value={name}
              maxLength={80}
              placeholder="예: 데이터 분석 표준"
              disabled={saving}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="admin-profile-description">
            <span>설명</span>
            <input
              type="text"
              value={description}
              maxLength={512}
              placeholder="사용 목적과 포함된 도구를 설명하세요."
              disabled={saving}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          <label className="admin-profile-enabled">
            <input
              type="checkbox"
              checked={enabled}
              disabled={saving}
              onChange={(event) => setEnabled(event.target.checked)}
            />
            <span>사용자 생성 목록에 공개</span>
          </label>
        </div>
        {selectedTemplate && (
          <ul className="resource-list" aria-label="프로필 실행 템플릿 요약">
            <li>커널 {selectedTemplate.kernelDisplayName}</li>
            <li>Python {selectedTemplate.pythonVersion}</li>
            <li>CPU {selectedTemplate.cpuLimit}</li>
            <li>메모리 {formatMegabytes(selectedTemplate.memoryLimitMb)}</li>
          </ul>
        )}
        <div className="admin-profile-editor__actions">
          <button
            className="button button--primary"
            type="button"
            disabled={saving || loading || !selectedTemplate || !name.trim()}
            onClick={() => void save()}
          >
            {saving ? "저장 중" : editingProfile ? "프로필 수정" : "프로필 생성"}
          </button>
          {editingProfile && (
            <button className="button button--secondary" type="button" disabled={saving} onClick={resetForm}>
              취소
            </button>
          )}
        </div>
      </div>}

      {loading && profiles.length === 0 ? (
        <p className="admin-table-empty"><span className="spinner" aria-hidden="true" /> 프로필 불러오는 중</p>
      ) : (
        <div className="admin-profile-list">
          {profiles.map((profile) => (
            <article key={profile.id} className={profile.enabled ? "" : "is-disabled"}>
              <div>
                <strong>{profile.name}</strong>
                <span>{(() => {
                  const cpu = cpuLimitToMillicores(profile.runtimeProfile.cpuLimit);
                  const locallySelectable = Boolean(
                    profile.enabled && resourcePolicy && cpu !== null &&
                    resourcePolicy.selectableCpuMillicores.includes(cpu) &&
                    resourcePolicy.selectableMemoryMb.includes(
                      profile.runtimeProfile.memoryLimitMb,
                    ),
                  );
                  const effectivelySelectable = profile.effectiveSelectable ?? locallySelectable;
                  if (!profile.enabled) return "비공개";
                  return effectivelySelectable
                    ? "프로필 공개"
                    : "프로필 공개 · 자원 정책으로 비노출";
                })()} · v{profile.version}</span>
                {profile.description && <p>{profile.description}</p>}
              </div>
              <ul className="resource-list">
                <li>{profile.runtimeProfile.kernelDisplayName}</li>
                <li>Python {profile.runtimeProfile.pythonVersion}</li>
                <li>CPU {profile.runtimeProfile.cpuLimit}</li>
                <li>{formatMegabytes(profile.runtimeProfile.memoryLimitMb)}</li>
              </ul>
              <div className="admin-profile-list__actions">
                <button className="button button--secondary" type="button" disabled={saving} onClick={() => beginEdit(profile)}>
                  편집
                </button>
                {profile.enabled && (
                  <button className="button button--danger" type="button" disabled={saving} onClick={() => void remove(profile)}>
                    비공개 처리
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
