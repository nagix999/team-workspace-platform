import { portalApi } from "../api/client";

interface LoginScreenProps {
  reason?: "SIGNED_OUT" | "SESSION_EXPIRED";
}

export function LoginScreen({ reason = "SIGNED_OUT" }: LoginScreenProps) {
  return (
    <main className="login-shell" id="main-content">
      <div className="login-glow login-glow--one" aria-hidden="true" />
      <div className="login-glow login-glow--two" aria-hidden="true" />
      <section className="login-card" aria-labelledby="login-heading">
        <div className="brand-mark brand-mark--large" aria-hidden="true">
          <span>&gt;_</span>
        </div>
        <p className="eyebrow">Team development cloud</p>
        <h1 id="login-heading">코드에 집중할 수 있는<br />나만의 작업공간</h1>
        <p className="login-card__copy">
          격리된 Jupyter 개발환경을 만들고, 멈추고, 다시 이어서 작업하세요.
        </p>
        {reason === "SESSION_EXPIRED" && (
          <p className="login-card__expired" role="alert">
            세션이 만료되었습니다. 안전하게 계속하려면 다시 로그인해 주세요.
          </p>
        )}
        <a className="button button--primary button--wide button--large" href={portalApi.loginUrl}>
          ID와 비밀번호로 로그인 <span aria-hidden="true">→</span>
        </a>
        <p className="login-card__note">
          승인된 팀원만 사용할 수 있습니다. 비밀번호는 JupyterHub에서 안전하게 확인합니다.
        </p>
      </section>
      <p className="login-footer">Workspace Portal · Internal</p>
    </main>
  );
}
