# ADR-0002: 사용자별 로그인과 공통 identity

- 상태: Rejected; 대체 제안은 [ADR-0003](0003-jupyterhub-local-login.md)
- 날짜: 2026-08-06
- 결정 범위: React/FastAPI 포털과 JupyterHub의 사용자 인증·매핑

> 2026-08-06 추가 요구에서 팀원 약 10명이 로컬 ID/password를 사용한다고 확정되어 이 OIDC 제안은 채택하지 않는다. 아래 내용은 대안 검토 이력을 보존하기 위한 것이다.

## 맥락

플랫폼에는 사용자별 로그인이 필요하고 workspace 소유권은 로그인 identity에서 결정된다. 포털과 JupyterHub가 서로 다른 사용자로 인식하거나 브라우저에 관리 token을 저장하면 타인 환경 접근과 계정 수명주기 불일치가 발생한다.

## 결정

- 포털과 JupyterHub는 동일 OIDC IdP를 사용하되 별도 client로 등록한다.
- FastAPI가 Authorization Code + PKCE 흐름을 처리하는 BFF가 된다.
- React에는 OIDC access/refresh token을 노출하지 않고 opaque `Secure HttpOnly` 세션 cookie만 제공한다.
- 사용자의 정규 identity는 변경 가능한 이메일이 아니라 `(issuer, subject)`다.
- 최초 로그인 때 내부 `user.id`와 불변 `hub_username` 매핑을 만든다.
- Hub launch는 포털 cookie를 공유하지 않고 Hub 자체 OIDC handshake를 거친다.
- 포털 logout, Hub logout, workspace stop을 별도 동작으로 정의한다.
- 사용자 비활성화는 포털 세션 폐기, Hub 접근 차단, 실행 서버 처리, token 회수를 포함한 하나의 운영 절차로 정의한다.

## 대안

### React가 직접 OIDC token 보관

구현 예제가 많지만 XSS가 access/refresh token을 탈취할 수 있고 API 인증·갱신 로직이 브라우저에 퍼진다. FastAPI가 이미 있으므로 채택하지 않는다.

### FastAPI와 JupyterHub에 로컬 계정을 각각 구현

비밀번호 정책, MFA, reset, lockout, 퇴사자 처리와 두 계정의 동기화를 중복 구현한다. 채택하지 않는다.

### JupyterHub를 포털 OAuth provider로 사용

포털이 JupyterHub 전용 서비스라면 가장 단순할 수 있다. 향후 일반 인스턴스까지 관리하는 독립 플랫폼이라는 현재 방향에서는 identity가 Hub 수명주기에 결합되므로 채택하지 않는다. 회사 IdP가 없고 제품 범위가 Hub 전용으로 축소되면 재검토한다.

## 결과

장점:

- 사용자별 로그인과 workspace 소유권의 기준이 하나다.
- 포털과 Hub 모두 기존 SSO/MFA·부서 그룹 정책을 재사용할 수 있다.
- OIDC token이 React 저장소와 JavaScript에서 사라진다.

비용:

- IdP에 포털·Hub client를 각각 등록해야 한다.
- FastAPI session store, CSRF, callback 검증, logout/철회 처리가 필요하다.
- Hub 첫 이동 때 별도 redirect handshake가 발생할 수 있다.
- `hub_username` 정규화와 사용자명 변경 정책이 필요하다.

## 필수 검증

- 사용자 A/B의 `(issuer, sub)`와 `hub_username`이 충돌하지 않는다.
- 이메일·표시 이름 변경 후에도 같은 workspace 소유권이 유지된다.
- callback의 state/nonce/PKCE, issuer, audience, signature, 만료 검증이 실패 시 닫힌다.
- React storage, 브라우저 URL, API/proxy 로그에 access/refresh token이 없다.
- logout, session 만료, 관리자 비활성화 후 API와 Hub 접근이 모두 차단된다.
- 포털 로그인 사용자가 launch 후 자신의 Hub 계정으로만 매핑된다.

## 재검토 조건

- 회사 OIDC IdP를 사용할 수 없음
- 포털이 JupyterHub 전용 UI로 범위 축소
- 브라우저가 FastAPI를 거치지 않고 여러 독립 API를 직접 호출해야 함
