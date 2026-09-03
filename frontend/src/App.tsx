import { useMemo, useState } from "react";
import { portalApi } from "./api/client";
import { AdminPortal } from "./components/AdminPortal";
import { CapacityPanel } from "./components/CapacityPanel";
import { CreateWorkspace } from "./components/CreateWorkspace";
import { LoginScreen } from "./components/LoginScreen";
import { ProvisioningBanner } from "./components/ProvisioningBanner";
import { WorkspaceCard } from "./components/WorkspaceCard";
import { EnvironmentVariablesPanel } from "./components/EnvironmentVariablesPanel";
import { usePortal } from "./hooks/usePortal";
import { isAdminUser } from "./lib/access";

function LoadingScreen() {
  return (
    <main className="loading-screen" id="main-content">
      <div className="brand-mark brand-mark--large" aria-hidden="true">
        <span>&gt;_</span>
      </div>
      <div>
        <h1>Workspace Portal</h1>
        <p><span className="spinner" aria-hidden="true" /> 세션을 안전하게 확인하고 있습니다.</p>
      </div>
    </main>
  );
}

export default function App() {
  const portal = usePortal();
  const [activeMenu, setActiveMenu] = useState<"workspaces" | "admin">("workspaces");
  const profileByWorkspace = useMemo(() => {
    return new Map(
      portal.profiles.map((profile) => [`${profile.id}:${profile.version}`, profile]),
    );
  }, [portal.profiles]);

  if (portal.session === "CHECKING" || portal.initialLoading) {
    return <LoadingScreen />;
  }

  if (portal.session === "ANONYMOUS" || !portal.user) {
    return <LoginScreen reason={portal.sessionExpired ? "SESSION_EXPIRED" : "SIGNED_OUT"} />;
  }

  const displayName = portal.user.displayName || portal.user.username;
  const adminUser = isAdminUser(portal.user);
  const userInitial = displayName.trim().slice(0, 1).toUpperCase() || "U";
  const updatedTime = portal.lastUpdatedAt
    ? new Intl.DateTimeFormat("ko-KR", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(portal.lastUpdatedAt)
    : null;

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">본문으로 건너뛰기</a>
      <header className="topbar">
        <a className="brand" href="/" aria-label="Workspace Portal 홈">
          <span className="brand-mark" aria-hidden="true"><span>&gt;_</span></span>
          <span>
            <strong>Workspace</strong>
            <small>Developer portal</small>
          </span>
        </a>
        <div className="topbar__actions">
          <nav className="topbar-nav" aria-label="주 메뉴">
            <button
              type="button"
              className={activeMenu === "workspaces" || !adminUser ? "is-active" : ""}
              aria-current={activeMenu === "workspaces" || !adminUser ? "page" : undefined}
              onClick={() => setActiveMenu("workspaces")}
            >
              개발환경
            </button>
            {adminUser && (
              <button
                type="button"
                className={activeMenu === "admin" ? "is-active" : ""}
                aria-current={activeMenu === "admin" ? "page" : undefined}
                onClick={() => setActiveMenu("admin")}
              >
                관리자
              </button>
            )}
          </nav>
          <div className="user-summary">
            <span className="avatar" aria-hidden="true">{userInitial}</span>
            <span className="user-summary__name">
              <strong>{displayName}</strong>
              <small>{portal.user.role === "ADMIN" ? "관리자" : portal.user.username}</small>
            </span>
          </div>
          <a className="text-button" href={portalApi.passwordChangeUrl}>
            비밀번호 변경
          </a>
          <button className="text-button" type="button" onClick={() => void portal.logout()}>
            로그아웃
          </button>
        </div>
      </header>

      <main className="dashboard" id="main-content">
        {portal.delegatedAuthRequired && (
          <section className="alert alert--warning" role="alert">
            <span className="alert__icon" aria-hidden="true">!</span>
            <div>
              <strong>JupyterHub 인증을 갱신해 주세요</strong>
              <p>환경 작업에 필요한 인증이 만료되었습니다. 로그인 후 요청을 다시 실행할 수 있습니다.</p>
            </div>
            <a className="button button--compact" href={portalApi.loginUrl}>다시 로그인</a>
          </section>
        )}

        {portal.user.status === "PROVISIONING" && (
          <ProvisioningBanner
            provisioning={portal.user.provisioning}
            requesting={portal.provisioningRequesting}
            onRequest={portal.requestProvisioning}
          />
        )}

        {portal.user.status === "DISABLED" ? (
          <section className="disabled-panel" aria-labelledby="disabled-heading">
            <span className="disabled-panel__icon" aria-hidden="true">×</span>
            <p className="eyebrow">Account disabled</p>
            <h1 id="disabled-heading">이 계정은 현재 사용할 수 없습니다</h1>
            <p>접근이 필요하면 플랫폼 관리자에게 계정 상태를 문의해 주세요.</p>
            <button className="button button--secondary" type="button" onClick={() => void portal.logout()}>
              로그아웃
            </button>
          </section>
        ) : adminUser && activeMenu === "admin" ? (
          <AdminPortal
            auditEvents={portal.auditEvents}
            auditWarning={portal.auditWarning}
            refreshing={portal.refreshing}
            onPlatformChanged={portal.refresh}
          />
        ) : (
          <>
            <section className="hero" aria-labelledby="page-heading">
              <div>
                <p className="eyebrow">Your development space</p>
                <h1 id="page-heading">안녕하세요, {displayName}님.</h1>
                <p>필요할 때 시작하고, 작업이 끝나면 멈추세요. 코드는 전용 저장공간에 유지됩니다.</p>
              </div>
              <div className="refresh-area">
                {portal.syncWarning ? (
                  <span className="sync-state sync-state--warning" title={portal.syncWarning}>
                    상태 갱신 지연
                  </span>
                ) : updatedTime ? (
                  <span className="sync-state">{updatedTime} 기준</span>
                ) : null}
                <button
                  className="icon-button"
                  type="button"
                  aria-label="상태 새로고침"
                  title="상태 새로고침"
                  disabled={portal.refreshing}
                  onClick={() => void portal.refresh()}
                >
                  <span className={portal.refreshing ? "refresh-icon is-spinning" : "refresh-icon"} aria-hidden="true">↻</span>
                </button>
              </div>
            </section>

            <div className="overview-grid">
              <CapacityPanel capacity={portal.capacity} loading={portal.refreshing} />
              <CreateWorkspace
                profiles={portal.profiles}
                profilesLoading={portal.profilesLoading}
                profilesError={portal.profilesError}
                capacity={portal.capacity}
                capacityError={portal.capacityError}
                userStatus={portal.user.status}
                creating={portal.creating}
                onRetry={portal.refresh}
                onCreate={portal.createWorkspace}
              />
            </div>

            <section className="workspaces-section" aria-labelledby="workspaces-heading">
              <div className="section-heading section-heading--workspace">
                <div>
                  <p className="eyebrow">My workspaces</p>
                  <h2 id="workspaces-heading">내 개발환경</h2>
                </div>
                <span className="item-count" aria-label={`환경 ${portal.workspaces.length}개`}>
                  {portal.workspaces.length.toString().padStart(2, "0")}
                </span>
              </div>

              {portal.workspaces.length === 0 ? (
                <div className="empty-state">
                  <span className="empty-state__art" aria-hidden="true">{"{ }"}</span>
                  <h3>아직 만든 환경이 없습니다</h3>
                  <p>위에서 프로필을 선택하면 격리된 첫 개발환경을 준비합니다.</p>
                </div>
              ) : (
                <div className="workspace-list">
                  {portal.workspaces.map((workspace) => {
                    const profile = profileByWorkspace.get(
                      `${workspace.profileId}:${workspace.profileVersion ?? 1}`,
                    ) ?? null;
                    return (
                      <WorkspaceCard
                        key={workspace.id}
                        workspace={workspace}
                        profile={profile}
                        operation={portal.latestOperationByWorkspace[workspace.id] ?? null}
                        busy={portal.busyWorkspaceIds.has(workspace.id)}
                        launchUrl={portalApi.launchUrl(workspace.id)}
                        onStart={portal.startWorkspace}
                        onStop={portal.stopWorkspace}
                        onRestart={portal.restartWorkspace}
                        onDelete={portal.deleteWorkspace}
                        onEnvironmentChanged={portal.refresh}
                      />
                    );
                  })}
                </div>
              )}
            </section>

            <div className="user-environment-section">
              <EnvironmentVariablesPanel onEnvironmentChanged={portal.refresh} />
            </div>

          </>
        )}
      </main>

      {portal.notice && (
        <div className="toast" role="status" aria-live="polite">
          <span>{portal.notice}</span>
          <button type="button" aria-label="알림 닫기" onClick={portal.dismissNotice}>×</button>
        </div>
      )}

      <footer className="app-footer">
        <span>Workspace Portal</span>
        <span>문제가 계속되면 플랫폼 관리자에게 문의하세요.</span>
      </footer>
    </div>
  );
}
