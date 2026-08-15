# ADR-0003: JupyterHub 기반 로컬 ID/password 단일 인증

- 상태: Accepted (2026-08-10)
- 날짜: 2026-08-06
- 대체 대상: [ADR-0002](0002-shared-oidc-login.md)
- 결정 범위: React/FastAPI 포털과 JupyterHub의 사용자 인증·매핑

## 맥락

사용자별 ID/password 로그인이 필요하고 등록 사용자는 팀원 약 10명이다. 첫 서비스는 JupyterHub이며 Docker Compose 한 호스트에서 운영한다. 포털과 Hub에 password 저장소를 각각 만들면 reset, lockout, 비활성화와 사용자 매핑이 어긋난다. React에 password나 Hub token을 오래 보관해서도 안 된다.

## 결정

JupyterHub NativeAuthenticator를 유일한 password 검증·저장 주체로 두고 JupyterHub를 FastAPI의 OAuth provider로 사용한다.

- ID/password form은 Hub origin의 NativeAuthenticator 화면에서 제공한다.
- password hash는 NativeAuthenticator가 Hub DB에 bcrypt 형식으로 저장한다.
- React와 FastAPI는 password를 받지 않고 플랫폼 SQLite에는 password/hash 컬럼을 만들지 않는다.
- FastAPI는 `platform-api`라는 externally-managed Hub OAuth service로 등록한다.
- Authorization Code, confidential client secret, exact redirect URI와 일회성 `state`를 사용한다.
- FastAPI는 forward-compatible PKCE S256 parameter를 보내지만 JupyterHub 5.5는 검증하지 않으므로 현재 보안 통제로 간주하지 않는다.
- callback은 사용자 token으로 `/hub/api/user`를 호출해 normalized username과 resolved scope를 확인한다.
- 플랫폼 identity는 `(hub deployment, normalized username)`에 대응하는 내부 UUID다. username 변경과 재사용은 MVP에서 금지한다.
- React에는 무작위 opaque `Secure; HttpOnly; SameSite` portal session cookie만 제공한다.
- 사용자 OAuth token은 server-side에서 별도 key/key ID의 인증된 암호화(AEAD)로 보관하고 portal session보다 오래 사용하지 않는다.

## 초기 계정 정책

```python
c.JupyterHub.authenticator_class = "native"
c.Authenticator.username_pattern = r"^(?!.*--)[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$"
c.Authenticator.allow_all = True
c.Authenticator.allow_existing_users = False
c.Authenticator.admin_users = {"platform-admin"}  # 실제 bootstrap ID로 교체
c.Authenticator.blocked_users = set()
c.NativeAuthenticator.open_signup = False
c.NativeAuthenticator.enable_signup = True       # 통제된 온보딩 동안만
c.NativeAuthenticator.minimum_password_length = 12
c.NativeAuthenticator.check_common_password = True
c.NativeAuthenticator.allowed_failed_logins = 5
c.NativeAuthenticator.seconds_before_next_try = 900
c.JupyterHub.oauth_token_expires_in = 8 * 60 * 60
c.JupyterHub.token_expires_in_max_seconds = 8 * 60 * 60
c.JupyterHub.cookie_max_age_days = 8 / 24
c.JupyterHub.subdomain_hook = "idna"
```

username은 기본 정규화된 소문자와 위 DNS-label 규칙(영문자로 시작, 1~32자, `--` 금지)을 만족해야 한다. Hub에는 `subdomain_hook="idna"`를 명시하고 launch host도 같은 hook 결과로 계산한다. 사용자별 web origin을 정확히 만들기 위해 username 변경과 퇴사자 ID 재사용은 MVP에서 금지한다.

JupyterHub 5에서 `allow_all=True`는 NativeAuthenticator의 password·승인 검사를 통과한 사용자를 후속 allow 검사에서 허용하기 위한 값이며 승인 절차를 우회하지 않는다. `admin_users`의 실제 ID를 공개 전에 고정하고 해당 사용자가 가장 먼저 signup한다. MVP는 `jupyterhub-nativeauthenticator==1.3.0`, Python 3.9 이상을 고정하고 JupyterHub 5.5 조합을 시험한다.

1. gateway 공개 전 private 경로에서 admin ID를 가장 먼저 signup한다.
2. 팀원은 제한된 기간에 signup하고 admin이 실제 구성원인지 확인해 승인한다.
3. 승인된 사용자가 포털에 처음 로그인하면 OAuth callback이 아직 없는 normalized username만 platform user `PROVISIONING`으로 insert한다. 기존 `ACTIVE/PROVISIONING`은 보존하고 `DISABLED`는 session 발급을 거부해 자동 재활성화하지 않는다. workspace create는 `PROVISIONING_REQUIRED`로 거부한다.
4. 관리자가 root-owned one-shot provisioner로 그 user UUID/username의 private slot 5개를 만들고 manifest·실제 quota 검증과 DB inventory import를 마친 뒤 conditional `PROVISIONING → ACTIVE` update만 허용한다. 중간에 `DISABLED`가 됐으면 활성화하지 않는다.
5. 약 10명 온보딩 뒤 `enable_signup=False`로 재배포한다.
6. 신규 팀원 등록 때만 통제된 경로를 다시 열거나 관리자가 운영 절차를 수행한다.
7. 퇴사·이동 시 한 transaction에서 platform user `DISABLED` + session revoke + 모든 workspace `STOPPED`/`spec_version` 증가 + pending start 취소 + 미소비 ticket revoke → claimed worker drain → `Authenticator.blocked_users` 반영과 Hub 재시작 → 모든 server 중지 확인 → private break-glass admin으로 `/hub/api/users/{name}/tokens` 열거·전부 삭제 → Hub user 삭제 → volume 보존/삭제 → 과거 cookie/token/URL 재접근 실패 확인을 하나의 runbook으로 수행한다.

NativeAuthenticator의 자체 로그인 실패 횟수는 현재 process memory에 있어 Hub 재시작 시 초기화될 수 있다. 따라서 gateway IP rate limit, 실패 로그인 감사·알림을 필수 보완책으로 둔다. Hub DB와 backup에는 password hash가 있으므로 플랫폼 DB보다 민감하게 취급한다.

2026-08 현재 production stable인 JupyterHub 5.5.0에는 Hub-side PKCE 검증이 없다. MVP는 5.5.0을 고정하고 위 통제를 적용한다. JupyterHub 6.x가 안정화되고 NativeAuthenticator, DockerSpawner, single-user image 호환 시험이 끝나면 `c.JupyterHub.oauth_require_pkce = True`를 추가한다. 그 전까지 PKCE 미강제는 명시적 잔여 위험이다.

OAuth token, Hub login cookie와 사용자가 API로 생성하는 token의 최대 수명은 초기 8시간으로 제한하고 portal absolute session도 그보다 길게 두지 않는다. portal logout만으로 Hub DB token이 항상 철회되는 것은 아니며 Hub logout redirect 실패 가능성도 있다. logout은 기존 WebSocket이나 다른 browser session의 즉시 종료를 보장하지 않는다. password reset/승인 해제만으로 기존 cookie/token이 즉시 무효화된다고 가정하지 않고 offboarding에서 Hub-side token 삭제, blocked-user startup 처리, server stop과 재접근 실패를 확인한다.

## 대안 비교

### FastAPI가 계정 원장

React 로그인 UI와 플랫폼 identity를 완전히 소유할 수 있다. 그러나 Hub에서 같은 계정을 쓰려면 FastAPI를 안전한 OAuth provider로 만들거나 custom JupyterHub Authenticator를 구현해야 한다. 인증 프로토콜, reset, brute-force 방어와 token 철회 범위가 크게 늘어 MVP에서는 채택하지 않는다. 두 DB에 password hash를 복제하는 절충안은 금지한다.

### PAMAuthenticator

JupyterHub 내장이고 기존 Linux 계정·PAM 정책이 있을 때 강하다. 현재는 DockerSpawner 사용자가 host OS 계정과 대응하지 않으므로 Compose container의 `/etc/passwd`·`/etc/shadow`, root 권한, backup 수명주기만 추가된다. 기존 조직 Linux 계정 원장이 없으므로 채택하지 않는다.

### 로컬 ID/password를 지원하는 별도 IdP

Hub와 독립된 공통 identity, MFA, 여러 애플리케이션 확장에 더 적합하다. 반면 지금은 인증 service, DB, 복구와 client 설정을 하나 더 운영해야 한다. JupyterHub 외 서비스가 늘거나 MFA·중앙 offboarding이 필요할 때 가장 강한 전환 대안이다.

### Dummy/shared password 또는 두 개의 로컬 계정

사용자별 책임 추적과 비밀 격리를 깨거나 계정 수명주기를 중복한다. 개발 smoke test 외에는 금지한다.

## 결과와 비용

장점:

- password가 한 시스템에만 머문다.
- Hub 로그인과 portal identity가 같은 normalized username에서 나온다.
- 10명 규모의 승인·reset을 추가 identity service 없이 운영할 수 있다.
- 사용자 위임 OAuth로 자기 server만 제어할 수 있다.

비용과 잔여 위험:

- Hub 장애가 portal의 신규 로그인도 막는다.
- NativeAuthenticator의 선택형 2FA는 MVP에서 켜지 않으며 조직 directory 동기화와 자동 퇴사자 처리가 없다.
- username을 장기 identity로 사용하므로 변경·재사용을 제한해야 한다.
- 암호화된 사용자 OAuth token과 OAuth client secret을 운영해야 한다.
- NativeAuthenticator/JupyterHub 버전 호환성을 업그레이드마다 검증해야 한다.

## 필수 검증

- admin 이름을 private bootstrap 전에 외부 사용자가 선점할 수 없다.
- 승인 전 사용자는 로그인할 수 없고 승인 사용자는 `allow_all=True` 설정에서 실제 로그인할 수 있으며 onboarding 종료 후 signup이 닫힌다.
- 첫 portal login은 `PROVISIONING` user row를 만들고 slot 준비 전 workspace create는 `PROVISIONING_REQUIRED`, 검증 뒤에는 `ACTIVE`로 전환된다.
- 숫자-only·선두/후행 하이픈·`--` username은 거부되고 허용 경계 username의 IDNA user host가 Hub와 portal에서 동일하다.
- 약한/common password, 6번째 연속 실패, gateway rate limit이 예상대로 거부된다.
- callback의 redirect URI나 state가 틀리거나 재사용되면 실패 시 닫힌다.
- 6.x 전환 시험에서는 PKCE verifier 누락·불일치가 거부되는지 별도로 확인한다.
- 사용자 A의 token으로 B의 server 시작·조회·중지가 `403/404`다.
- React storage, 브라우저 URL, API/proxy 로그, platform DB에 password나 평문 OAuth token이 없다.
- portal logout, Hub logout, session 만료, 관리자 비승인 후 각각의 접근 결과가 명세와 일치한다.
- Hub logout redirect 중단, password reset/비승인 후 기존 token 접근과 offboarding 강제 철회를 시험한다.
- 초기 8시간이 지난 Hub login cookie와 OAuth/API token은 더 이상 유효하지 않고, offboarding 뒤 과거 cookie/token/URL/WebSocket 접근이 실패한다.
- password 변경/reset과 Hub DB backup/restore 후 로그인이 정상이다.

## 재검토 조건

- React 자체 ID/password form이 필수 요구가 됨
- JupyterHub 외 여러 독립 서비스가 같은 로그인을 요구함
- MFA, 중앙 인사 연동, 비밀번호 셀프서비스 정책이 필요함
- 인터넷에 직접 공개하거나 사용자 수·관리 부담이 크게 증가함
- Hub 장애와 무관한 portal 로그인이 필요함

## 근거 자료

- [JupyterHub services](https://jupyterhub.readthedocs.io/en/latest/reference/services.html)
- [JupyterHub and OAuth/PKCE](https://jupyterhub.readthedocs.io/en/latest/explanation/oauth.html)
- [JupyterHub application configuration](https://jupyterhub.readthedocs.io/en/stable/reference/api/app.html)
- [JupyterHub releases on PyPI](https://pypi.org/project/jupyterhub/)
- [NativeAuthenticator overview](https://native-authenticator.readthedocs.io/)
- [NativeAuthenticator 1.3.0 on PyPI](https://pypi.org/project/jupyterhub-nativeauthenticator/1.3.0/)
- [NativeAuthenticator options](https://native-authenticator.readthedocs.io/en/stable/options.html)
- [NativeAuthenticator quickstart](https://native-authenticator.readthedocs.io/en/latest/quickstart.html)
