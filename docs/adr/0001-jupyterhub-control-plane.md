# ADR-0001: JupyterHub 제어면 연동 방식

- 상태: Accepted (2026-08-10)
- 날짜: 2026-08-06
- 결정 범위: 개인 Jupyter 개발환경 생성·상태·URL 연동
- 관련 결정: [ADR-0003](0003-jupyterhub-local-login.md), [ADR-0004](0004-single-host-compose.md)

> 2026-08-11 이후 관리자 cross-user lifecycle에 한한 별도 `admin:servers` worker
> credential 결정은 [ADR-0007](0007-admin-runtime-and-environment-management.md)이
> 아래의 “전역 lifecycle service token” 기각 결정을 대체한다. 일반 사용자 위임 OAuth와
> portal API 무권한 원칙은 계속 유효하다.

## 맥락

플랫폼은 React, FastAPI, SQLite를 사용하며 첫 기능으로 JupyterHub 기반 개인 개발환경을 제공한다. 등록 사용자는 10명, 사용자당 환경은 최대 5개, 전체 active server는 최대 15개이고 단일 호스트 Docker Compose/DockerSpawner를 사용한다. 포털 안에서 spawn 진행 상태를 보여 주는 것이 확정 요구다. 브라우저에 Hub credential을 노출하지 않으면서 URL, quota와 감사를 처리해야 한다.

## 결정

FastAPI를 독립 제어면으로 두고 JupyterHub를 실행면이자 실제 server 상태의 원천으로 사용한다.

- React는 FastAPI만 호출한다.
- FastAPI는 externally-managed JupyterHub OAuth service지만 Hub proxy나 process 관리에는 결합하지 않는다.
- 일반 사용자의 생성·조회·시작·중지는 service 자체 token이 아니라 `servers!user` 범위의 사용자 위임 OAuth token으로 호출한다.
- 대상 username은 `/hub/api/user`가 검증한 token owner에서 계산하며 request path/body에서 받지 않는다.
- `platform-api` service 자체에는 server lifecycle role을 주지 않는다.
- worker가 만든 짧은 one-time spawn ticket을 Hub `apply_user_options/pre_spawn_hook`가 FastAPI에 검증할 때만 container를 실행한다. default/missing ticket/임의 profile은 실행을 거부한다.
- Hub도 `named_server_limit_per_user=5`, `active_server_limit=15`를 강제한다.
- reconciler에는 전용 팀 Hub의 상태와 orphan을 볼 수 있는 `list:users` + `read:servers` service token을 별도로 둔다. 쓰기 권한과 `access:servers`는 주지 않는다.
- 플랫폼은 workspace의 희망 상태, 최근 관찰 상태, durable operation을 SQLite에 저장한다.
- JupyterHub가 실제 사용자 server 상태의 원천이며 worker/reconciler가 drift를 조정한다.
- launch endpoint는 소유권과 상태를 확인하고 token 없는 JupyterHub URL로 리다이렉트한다.
- JupyterHub 내부 DB와 Docker API를 FastAPI가 직접 접근하지 않는다.

## 채택 근거

- JupyterHub가 server 시작·중지, 비동기 진행, URL, RBAC와 사용자 위임 OAuth를 공식 API로 제공한다.
- 제품 권한, quota, 감사, idempotency를 FastAPI에 집중할 수 있다.
- 개별 사용자 token의 오용이나 IDOR가 다른 사용자의 server 변경으로 확대되지 않는다.
- 향후 일반 인스턴스 공급자를 추가해도 React와 플랫폼 사용자 모델을 유지할 수 있다.

## 강한 대안과 반례

### JupyterHub native spawn URL로 이동

포털이 사용자를 `/hub/spawn`으로 보내면 operation worker와 lifecycle credential이 필요 없고 Hub가 소유권을 직접 강제한다. 가장 작은 첫 슬라이스다. 그러나 포털 내 생성 진행률이라는 확정 요구를 충족하지 못하므로 현 MVP에서는 기각한다. 이 요구가 사라질 때만 재검토한다.

### 전역 lifecycle service token

worker 재시작, 강제 portal-only stop과 automatic idle culling이 단순하다. 반면 FastAPI/worker 침해나 IDOR 한 건이 모든 팀원 server 변경으로 확대된다. 약 10명 규모에서 이 편의를 위해 blast radius를 키울 이유가 약하므로 채택하지 않는다. automatic idle culler도 MVP에서 제외한다. 사용자 없이 수행하는 mutation이 실제 운영 요구가 될 때만 제한 scope와 별도 승인 절차를 재검토한다.

### FastAPI가 Docker API를 직접 호출

실행 세부를 통제하기 쉽지만 JupyterHub가 제공하는 인증, proxy, spawn 상태, 사용자 URL과 복구를 다시 구현한다. Docker socket 권한까지 FastAPI로 확대되므로 기각한다.

### React가 JupyterHub REST API를 직접 호출

OAuth token이 JavaScript에 노출되고 소유권·감사·quota 검사가 분산된다. CORS와 token 수명주기도 복잡해져 기각한다.

## 결과와 비용

긍정적 결과:

- 책임과 최소 권한 경계가 명확하다.
- 사용자별 동작 감사와 중복 방지가 쉽다.
- 플랫폼 API가 Spawner보다 안정적인 추상화가 된다.

수용해야 할 비용:

- 사용자 OAuth token을 server-side로 암호화해 session과 함께 관리해야 한다.
- API/worker와 token 암호화 key가 함께 완전히 침해되면 활성 session의 여러 사용자 token이 노출될 수 있다. “한 사용자 blast radius”는 개별 token/IDOR에만 해당한다.
- Hub start가 FastAPI의 one-time ticket validator 가용성에 의존한다.
- JupyterHub 5.5에서 사용자는 자기 server를 직접 stop하거나 Hub named-server record를 remove할 권한도 가지므로 이를 `audit_events.action=EXTERNAL_CHANGE`로 수용·감사한다. remove는 platform workspace/private volume을 삭제하지 않으며 영구 삭제는 관리자 runbook만 수행한다.
- token 만료 시 pending 명령은 `AUTH_REQUIRED`로 끝나며 자동 복구가 제한된다.
- Hub와 플랫폼 상태 drift를 처리할 worker/reconciler가 필요하다.
- 포털 로그인 가용성이 JupyterHub에 의존한다.

## 안전 조건

- 로그인 callback과 각 Hub 명령 전에 token owner와 resolved scope를 검증한다.
- spawn ticket은 username/workspace/server/profile ID+version+config digest/operation+attempt/private-volume-slot/workspace `spec_version`/expiry에 결속하고 hash 저장, 단일 사용, 짧은 만료로 검증한다. composite FK와 apply/pre-spawn owner join으로 token owner, platform user, workspace owner와 slot owner가 같음을 강제한다. profile execution row는 `(id,version)`별 immutable이고 Hub local allowlist의 digest와 exact match해야 한다. 같은 operation을 재시도할 때는 이전 미소비 ticket을 원자적으로 revoke하고 새 attempt 행을 만든다.
- Hub→internal validator 요청은 Hub/API 전용 key로 method·path·body hash·timestamp·nonce를 HMAC 서명하고 clock skew·replay를 거부한다.
- Hub `apply_user_options`는 ticket/profile 외 raw option을 거부하고 local allowlist만 resource 설정으로 적용하며 platform user가 `ACTIVE`가 아니면 consume/pre-spawn 모두 실패한다.
- admin을 포함한 모든 사용자의 default server와 미등록 named server 실행을 pre-spawn에서 거부하며 예외 없는 5/15 Hub limit을 켠다. hook 전 생성될 수 있는 미승인 failed/stopped record는 read-only reconciler가 탐지하고 private admin runbook으로 정리한다.
- Hub가 반환한 normalized username을 불변 매핑으로 쓰고 username 변경·재사용을 금지한다.
- 사용자 token은 별도 key로 암호화하며 브라우저 storage, URL, 로그에 넣지 않는다.
- `platform-api`에 `admin:users`, `admin:servers`, `access:servers`, `inherit`를 부여하지 않는다.
- workspace 소유권은 모든 FastAPI endpoint에서 검사한다.
- Hub per-user-domain `full_url`은 scheme, exact expected host와 path를 엄격히 검증하고 credential을 URL에 넣지 않는다.
- Hub 장애는 stopped로 간주하지 않고 stale/unknown으로 남긴다.

## 재검토 조건

- 포털 진행 표시 요구가 사라지고 단순 launch 링크로 축소됨
- 사용자 없이 수행해야 하는 자동 start/stop이 필수화됨
- JupyterHub API가 필요한 lifecycle이나 scope를 제공하지 않음
- 다중 Hub/리전과 분산 worker로 현재 조정 비용이 과도해짐
- JupyterHub 외 여러 서비스가 공통 identity를 필요로 함
