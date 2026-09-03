# JupyterHub execution infrastructure

이 디렉터리는 JupyterHub 5.5 실행면과 execution-network 정책을 소유한다. 정적
Compose 서비스 정의는 상위 통합 Compose가 담당하고, 사용자 Jupyter container는
DockerSpawner가 동적으로 만든다. `infra/host`의 firewall 도구는 명시적인 legacy 강화
모드용으로 보존하지만 기본 로컬·domain-test·운영 설계에서는 호출하지 않는다.

운영 기본값은 의도적으로 실행 불가능하다. image digest, resource 설정, DNS/URL,
secret과 명시적인 network/storage policy mode를 확정해야 Hub가 시작되거나 spawn이
허용된다. Legacy host-health mode만 해당 digest도 요구한다. `local-dev` 예외는 운영
설정과 공유하지 않는다.

## 이미지와 버전

- production Docker Engine: 기능 호환 하한 27.1.2; 27.5.1 이상 또는 현재 지원되는 최신 보안
  patch를 강력 권장. Engine 28+는 base isolated-gateway 계약, Engine 27은 Compose
  2.24.4+ compatibility overlay의 `inhibit_ipv4` 계약을 사용
- Hub: `jupyterhub==5.5.0`
- NativeAuthenticator: `1.3.0`
- DockerSpawner: `14.0.0`
- `nativeauth_compat.py`는 NativeAuthenticator 1.3의 구형 login `_render`
  signature에만 적용된다. 만료된 XSRF form의 동기 오류 화면은 Hub 기본 login
  template으로 위임하고, upstream이 `**kwargs`를 지원하면 자동으로 비활성화된다.
- 운영 build: `JUPYTERHUB_BASE_IMAGE`와 `SQUID_BASE_IMAGE` 모두
  `repository@sha256:<64 hex>` 형식이어야 한다.
- 로컬 build만 `ALLOW_MUTABLE_BASE_IMAGE=true`를 명시할 수 있다.
- single-user image에는 Hub와 호환되는 `jupyterhub-singleuser` 5.5가 있어야 한다.
  stock `quay.io/jupyterhub/singleuser:5.5`의 검증된 경로는
  `/opt/conda/bin/jupyterhub-singleuser`다. 운영 profile은 image digest를 요구하며
  `pull_policy=never`이므로 host에 사전 pull/load하고 실제 경로도 검사한다.
- 로컬 `infra/singleuser` image는 Python 3.13.14 system kernel과 격리된 Python
  3.12.13 conda kernel을 포함한다. 시작 wrapper는 digest-bound profile에 선언된 모든
  kernelspec의 system resource path, display/language, reviewed ipykernel argv, 절대
  interpreter와 실제 patch version을 검사한 다음에만 `jupyterhub-singleuser`를 exec한다.
  이 문서의 kernel은 Jupyter kernelspec 의미이며 container가 공유하는 host Linux
  kernel version 선택을 의미하지 않는다. default kernelspec의 digest-bound executable
  directory는 terminal `PATH`의 첫 항목에도 고정한다.
- profile policy schema v2는 `enabled`(기존 workspace 실행 허용)와 `selectable`(신규
  catalog 노출)을 분리한다. `python_version`, kernelspec별 `name`, `display_name`,
  `language`, `python_version`, `executable`, `default_kernel`, CPU/RAM/disk 및
  `private_disk_quota_enforced`는 immutable config digest에 포함된다. legacy schema v1
  실행값과 digest는 변경하지 않으며 v2 문서에서 legacy row는 selectable일 수 없다.

`profiles.production.example.json`은 출시 차단 placeholder다. 검토한 값을 채운 뒤
다음 명령으로 digest를 계산해 `config_digest`에 반영한다.

```bash
python3 infra/jupyterhub/profile_digest.py /etc/platform/profiles.json
```

## Compose 계약

### JupyterHub environment

| 이름 | 계약 |
|---|---|
| `PLATFORM_ENV` | 운영 `production`, 로컬만 `local-dev` |
| `ALLOW_UNSAFE_LOCAL_DEV` | 운영 `false`; 로컬에서만 반드시 `true` |
| `JUPYTERHUB_PROFILE_ALLOWLIST_FILE` | container 내부 read-only JSON 경로 |
| `JUPYTERHUB_ADMIN_USERS` | 쉼표 구분 실제 bootstrap ID; placeholder 금지 |
| `JUPYTERHUB_BLOCKED_USERS_FILE` | 보호된 JSON 배열 파일 경로 |
| `NATIVE_ENABLE_SIGNUP` | 초기 private onboarding 때만 `true`; 이후 `false` |
| `JUPYTERHUB_BIND_URL` | 예: `http://:8000` (public proxy 내부 bind) |
| `JUPYTERHUB_HUB_BIND_URL` | 예: `http://0.0.0.0:8081` |
| `JUPYTERHUB_HUB_CONNECT_URL` | user network에서 도달할 정적 Hub 주소, 예: `http://172.30.0.10:8081` |
| `JUPYTERHUB_SUBDOMAIN_HOST` | 운영 `https://hub.example.net`; wildcard DNS/TLS 필요 |
| `PLATFORM_OAUTH_REDIRECT_URI` | 운영 exact HTTPS callback |
| `JUPYTER_NETWORK_NAME` | Compose가 소유하는 exact internal bridge 이름 |
| `JUPYTER_NETWORK_POLICY_MODE` | 기본/운영 `compose-internal-trusted-v1`; 이전 수동 강화 모드만 `legacy-host-firewall-v1` |
| `JUPYTER_NETWORK_SUBNET` | inspect 결과와 대조할 exact IPv4 subnet |
| `JUPYTER_NETWORK_DYNAMIC_IP_RANGE` | single-user에 할당할 exact IPv4 `ip_range` |
| `JUPYTER_STORAGE_POLICY_MODE` | 현재 `docker-volume-unlimited-v1`; 이전 XFS quota mode만 `legacy-xfs-project-quota-v1` |
| `JUPYTER_EGRESS_PROXY_URL` | 예: `http://172.30.0.20:3128` |
| `JUPYTER_SINGLEUSER_COMMAND` | image에서 검증한 절대 경로; stock 5.5는 `/opt/conda/bin/jupyterhub-singleuser` |
| `JUPYTERHUB_CONCURRENT_SPAWN_LIMIT` | 부하시험 값 `1..15`; 무제한 금지 |
| `SPAWN_VALIDATOR_CONSUME_URL` | `/internal/` 경로의 FastAPI consume endpoint |
| `SPAWN_VALIDATOR_CHECK_URL` | `/internal/` 경로의 FastAPI non-consuming endpoint |
| `SPAWN_VALIDATOR_TIMEOUT_SECONDS` | 기본 5초 |
| `PLATFORM_WEB_PROVISIONING_ENABLED` | 로컬 웹 프로비저닝 `true`; 운영에서는 반드시 `false` |
| `PLATFORM_PROVISIONING_{CLAIM,COMPLETE,FAIL}_URL` | 로컬에서만 exact `http://api:8000/internal/v1/user-provisioning/...` |
| `PLATFORM_WORKSPACE_DELETION_ENABLED` | workspace 영구 삭제의 별도 opt-in; API와 Hub agent 양쪽에서 일치해야 함 |
| `PLATFORM_DELETION_{CLAIM,COMPLETE,FAIL}_URL` | 삭제가 켜진 경우 exact `http://api:8000/internal/v1/workspace-deletions/...` |
| `PLATFORM_LOCAL_PROVISIONING_STATE_DIR` | 로컬 고정 shared-state bind `/srv/jupyterhub/local-provisioning` |
| `PLATFORM_LOCAL_PROJECT_ID_START` | 로컬 project-ID 5개 block allocator 시작값, 기본 `10000` |
| `PLATFORM_HEALTH_DIR` | 기본 `/run/platform-health` |
| `EXPECTED_NETWORK_POLICY_SHA256` | `legacy-host-firewall-v1`에서만 필요한 host network digest; Compose mode에서는 설정 금지 |
| `EXPECTED_STORAGE_POLICY_SHA256` | legacy XFS mode의 canonical storage digest; unlimited mode에서는 설정 금지 |
| `JUPYTERHUB_DB_URL` | 기본 `sqlite:////srv/jupyterhub/jupyterhub.sqlite` |

로컬 override는 image digest만 우회하고, one-time ticket,
validator HMAC, default-server 거부, 5/15 quota, profile allowlist, volume label 검증은
우회하지 않는다. 로컬 HTTP에서는 `cookie_host_prefix_enabled=False`; 운영 HTTPS에서는
항상 `True`다. unlimited storage mode는 local/production 모두 XFS health manifest와
writable-layer `storage_opt=size`를 요구하지 않지만 exact volume identity/label/mount와
single-user 내부 shared root 권한·쓰기 검증은 유지한다.

### Portal OAuth RBAC

NativeAuthenticator의 가입 승인과 외부 OAuth service 접근 권한은 서로 다른 검사다.
기본 `user` role은 표준 자기 리소스 권한인 `self`를 유지하고,
`access:services!service=platform-api`만 추가한다. 일반 사용자에게 필터 없는
`access:services` 또는 `admin:services`를 부여하지 않는다. 이 역할이 없으면 승인된
사용자도 `/hub/api/oauth2/authorize`에서 `platform-api` 접근 403을 받는다.

`platform-reconciler`는 별도 service-bound role의 `list:users`, `read:servers`만
가지며 서버 시작·중지 권한을 갖지 않는다. JupyterHub 5.5의 effective scope에는 필터
적용을 위한 `read:users:name`이 함께 나타나며 dedicated `reconciler`가 이 exact set을 매
snapshot 전에 검증한다. 이 process는 `control` network, platform DB RW,
`JUPYTERHUB_RECONCILER_TOKEN_FILE` RO만 받고 portal OAuth/session, 환경 암호화/HMAC,
admin lifecycle secret은 받지 않는다.

reconciler는 성공 snapshot의 관측 시작시각을 `/tmp/platform-reconciler-health.json`에
0600 atomic heartbeat로 남긴다. `python -m app.reconciler --healthcheck`는 이 시각이
`PLATFORM_RECONCILIATION_FRESHNESS_SECONDS` 안일 때만 성공한다. body 중간 연결 종료,
schema 오류, 느린 snapshot, DB 실패는 heartbeat를 제거하고 workspace를 stale 처리한다.

`platform-admin-lifecycle`는 `admin:servers` 하나만 갖고, credential은 Hub와 HTTP
listener가 없는 worker에만 전달한다. 다른 사용자 환경의 start/stop/restart/Hub record
remove에 사용하며 notebook 접속에는 사용하지 않는다. 관리자 launch는 token 없는 URL로
redirect한 뒤 관리자의 Hub browser session과 built-in `access:servers`로 인증한다.

### Secret file

다음은 환경변수 원문이 아니라 container 내부 file path로 전달한다. 표에 명시된 최소
service에만 mount하고 image/Git에 넣지 않는다.

| environment | 내용 |
|---|---|
| `JUPYTERHUB_COOKIE_SECRET_FILE` | Hub cookie secret |
| `CONFIGPROXY_AUTH_TOKEN_FILE` | configurable-http-proxy token, ASCII 32 bytes 이상 |
| `PLATFORM_OAUTH_CLIENT_SECRET_FILE` | 외부 `platform-api` OAuth client secret |
| `PLATFORM_RECONCILER_TOKEN_FILE` | `list:users + read:servers` 전용 token |
| reconciler의 `JUPYTERHUB_RECONCILER_TOKEN_FILE` | 같은 read-only token의 dedicated process 전용 mount path |
| `PLATFORM_ADMIN_LIFECYCLE_TOKEN_FILE` | Hub가 등록하는 exact `admin:servers` service token |
| worker의 `JUPYTERHUB_ADMIN_LIFECYCLE_TOKEN_FILE` | 같은 token의 worker 전용 mount path; API/frontend에는 금지 |
| `SPAWN_VALIDATOR_HMAC_KEY_FILE` | Hub/API 전용 임의 byte key, 32 bytes 이상 |

### JupyterHub mounts와 권한

- `/srv/jupyterhub`: Hub SQLite 영속 volume, container UID/GID `10001:10001`, mode 0700
- `/var/run/docker.sock`: read-write, **Hub에만** mount; host socket GID를 `group_add`
- `/srv/jupyterhub/local-provisioning`: 로컬에서는 host `.runtime`을 read-write bind해
  host CLI와 agent가 동일한 registry/manifest를 사용한다. production deletion을 켤 경우도
  PREPARED/REMOVED checkpoint를 reboot/container 재생성 뒤 보존하는 별도 persistent mount가
  반드시 필요하다. 디렉터리는 `2770` setgid, 파일과 lock은 `0660`이며 API mount는 read-only다.
- profile/blocked-user JSON: read-only
- 위 secret files: read-only
- `/run/platform-health`: storage health 또는 명시적 legacy network mode에서만 read-only bind
- Hub는 `edge`, `control`, `jupyter`에 연결하고 `jupyter`에서는 `hub_ip`를 정적으로
  할당한다. `egress-proxy`는 `jupyter`의 정적 `egress_proxy_ip`와 별도
  `egress-out`에만 연결한다. user container는 `jupyter` 하나만 연결한다.

`compose-internal-trusted-v1`에서는 spawn ticket을 소비하기 전과 container create 직전에
Hub가 Docker API로 network를 inspect한다. 이름/bridge/local scope, `internal=true`, IPv6
off, policy label, subnet/ip-range와 reserved proxy endpoint를 fail-closed로 검증한다.
Option은 Engine 28+ base의 `enable_icc=true` + IPv4/IPv6 isolated-gateway exact set이거나
Engine 27 overlay의 `enable_icc=true` + `inhibit_ipv4=true` exact set이어야 한다. 현재
Engine major version과 다른 option 조합은 거부한다. 이 검사는 플랫폼이 host
iptables/nftables를 읽거나 수정하지 않는다. Docker daemon 자체가 bridge 구현을 위해 host
netfilter 규칙을 관리하는 것은 필수 정상 동작이며 플랫폼 전용 `DOCKER-USER` rule 설치와는
구분한다. Docker daemon의 firewall rule 관리를 비활성화해서는 안 된다.

Single-user의 Hub connect URL은 reserved literal IPv4를 사용하고 Docker host config의
DNS는 container loopback(`127.0.0.1`)으로 고정한다. 따라서 사용자 코드가 Docker embedded
DNS를 통해 외부 이름을 직접 조회하지 못하며 package hostname은 HTTP(S) proxy인 Squid만
해석한다. 출시 시험에서 직접 DNS와 direct IP egress 실패를 별도로 확인한다.

ICC on이므로 같은 execution bridge의 single-user container는 서로 열린 port를 볼 수 있다.
private volume의 mount 격리는 유지되지만 사용자별 network 강격리는 아니며, 상호 신뢰하는
팀이라는 현재 전제에서만 허용한다. 그 전제가 바뀌면 KubeSpawner/NetworkPolicy로 전환한다.

### 로컬 웹 사용자 프로비저닝

`PLATFORM_WEB_PROVISIONING_ENABLED=true`는 `PLATFORM_ENV=local-dev`와
`ALLOW_UNSAFE_LOCAL_DEV=true`가 동시에 설정된 경우에만 허용된다. 운영에서 이 값을
켜면 Hub와 API 설정 검증이 실패한다.

로컬 Hub는 `platform-local-provisioner` managed service를 같은 container 안에서
실행한다. 별도 service나 API/worker에 Docker socket을 추가하지 않는다. agent는
`http://api:8000/internal/v1/user-provisioning/{claim,complete,fail}` 세 endpoint만
허용하고 기존 HMAC v1 timestamp/nonce/body-digest 계약으로 요청한다. claim에서 받은
UUID/정규화 username 외 image, command, path, Docker option은 받지 않는다.

agent는 read-only profile allowlist에서 실행 UID/GID, storage label과 initializer image를
결정하고 shared volume 1개 및 사용자 private volume 정확히 5개를 만든다. 기존 volume의
driver/options/label이 정확히 일치하지 않으면 실패하며, helper container는 고정된
volume 하나만 mount하고 network 없이 최소 chown/chmod capability로 실행된다. fresh
local host에 profile image가 없으면 allowlist의 그 image만 pull한 뒤 immutable local
image ID로 helper를 실행한다. 운영 image pull/provisioning 경로는 이 agent에 없다.

### Workspace 영구 삭제 agent

삭제는 가입 provisioning과 별도 capability다. 따라서
`PLATFORM_WEB_PROVISIONING_ENABLED=false`이고
`PLATFORM_WORKSPACE_DELETION_ENABLED=true`인 agent는 user-provisioning endpoint를 전혀
호출하지 않는다. 삭제 flag는 `docker-volume-unlimited-v1`에서만 허용된다.

backend claim은 exact workspace가 tombstone 상태이고 Hub server가 `NOT_FOUND`이며 lease/spec/
owner/slot이 일치할 때만 발급한다. retry에도 유지되는 `deletion_id`가 destructive checkpoint를
결속하고, retry 때 교체 가능한 UI `operation_id`는 checkpoint identity로 사용하지 않는다.
agent는 private volume name, local driver/빈 options, owner/username/slot/quota label을 exact
검증하고 shared volume/name을 거부한다.

agent는 Docker remove 전에 `PREPARED`를 atomic write와 fsync로 보존한다. PREPARED retry에서
volume이 남아 있으면 labels를 다시 검증하고 `force=false`로 제거하며, 없으면 이미 제거된
것으로 본 뒤 `REMOVED`를 보존한다. REMOVED에서만 같은 name/labels로 빈 volume을 재생성하고
root를 정책 UID/GID와 `0700`으로 초기화·검증한다. backend가 manifest를 검증해 complete한
후에만 archive/slot 재사용을 허용한다.

production에서는 profile image가 digest-pinned이고 host에 이미 존재해야 하며 agent가 pull하지
않는다. local/domain-test에서만 allowlist image의 제한 pull을 허용한다. Production
Compose와 persistent checkpoint mount가 구현돼 있지만, 실제 운영 host의
mount/backup/crash-recovery 시험과 승인은 별도 출시 gate다.

Docker socket은 `cap_drop`, non-root, read-only rootfs로 축소할 수 없는 host-root 상당의
권한이다. 이 구현은 이미 socket을 보유한 Hub trust boundary 안에 provisioning/deletion
agent를 두어 socket 보유 주체를 늘리지 않는 절충안이다. 현재 운영 storage 정책도 hard quota 없는
Docker named volume이며 host disk/inode 감시와 backup이 별도 출시 gate다.

`hub_ip`/`egress_proxy_ip`는 `dynamic_ip_range` 밖에 둔다. `aux-address`로 예약하면
Compose의 동일 static IP attach와 충돌할 수 있으므로 network 생성 시 사용하지 않는다.

## Spawn validator exact contract

성공 response는 아래 key를 정확히 가져야 하며 추가 key도 거부된다. 거부는 non-2xx로
응답한다. consume은 DB transaction에서 ticket hash의 미사용/만료 여부, ACTIVE user,
owner chain, desired RUNNING, `spec_version`, profile/slot을 검사하고 원자적으로
소비해야 한다.

Consume request:

```json
{
  "schema_version": 1,
  "username": "alice",
  "server_name": "ws-0123456789abcdef0123456789abcdef",
  "profile_id": "python-standard",
  "profile_version": 1,
  "spawn_ticket": "base64url-random-value-at-least-32-characters"
}
```

Consume success response:

```json
{
  "schema_version": 1,
  "authorized": true,
  "authorization": {
    "spawn_authorization_id": "opaque-auth-id",
    "workspace_id": "opaque-workspace-id",
    "operation_id": "opaque-operation-id",
    "attempt_no": 1,
    "workspace_spec_version": 1,
    "username": "alice",
    "server_name": "ws-0123456789abcdef0123456789abcdef",
    "profile_id": "python-standard",
    "profile_version": 1,
    "profile_config_digest": "sha256:<64 lowercase hex>",
    "private_volume_slot_id": "opaque-slot-id",
    "private_volume_slot_number": 1,
    "private_volume_name": "jupyter-user-alice-slot-1",
    "private_disk_hard_limit_bytes": 5368709120,
    "uid": 1000,
    "gid": 100,
    "valid_until_unix": 1800000030,
    "environment": {"TEAM_API_URL":"https://example.invalid"},
    "environment_digest": "hmac-sha256:<64 lowercase hex>",
    "user_environment_generation": 1,
    "workspace_environment_generation": 1
  }
}
```

Check request는 `environment` 원문을 제외한
`{"schema_version":1, ...authorization의 나머지 모든 필드}`이고 ticket도 포함하지 않는다.
digest와 두 generation은 포함한다. API는 같은 mutable 불변식들을 non-consuming 방식으로 다시 검사한다.
성공 response는 정확히 다음 형태다.

```json
{"schema_version":1,"authorized":true,"spawn_authorization_id":"opaque-auth-id"}
```

Hub는 response의 image/resource/mount path를 받지 않는다. `(profile_id, version,
config_digest)`와 UID/GID/storage enforcement marker가 로컬 immutable allowlist와 일치할 때만 로컬
image/resource policy를 적용한다. volume 이름은 username/slot 공식과 Docker label까지
재검증한다. v2 profile의 image, 기본 kernel/Python, CPU, memory, command도 validator
response가 아니라 같은 local allowlist에서만 적용한다. 로컬 profile의
`private_disk_quota_enforced=false`는 volume label과 정확히 일치해야 한다. 명시적
`docker-volume-unlimited-v1`에서는 production도 이를 허용하되 volume 이름, owner/slot
label, private/shared RW mount mapping과 shared root의 `root:<gid>/2770` 계약이 어긋나면
spawn 또는 single-user 시작을 거부한다.

환경변수 effective map은 user-global 위에 workspace 값을 덮어쓴 snapshot이다. key 최대
64 ASCII identifier, 값 최대 16 KiB UTF-8/NUL 금지, effective 최대 128개/canonical JSON
64 KiB다. Jupyter/Python/loader/proxy/CA/platform/startup namespace는 대소문자 구분 없이
예약한다. platform HOME/proxy/bootstrap map이 마지막 merge로 우선한다. 원문은 consume
응답과 Docker create에만 필요하며 check, user_options, labels, 감사·오류 로그에는 넣지 않는다.
성공한 create 뒤 Hub의 임시 user snapshot은 지우지만 container `Config.Env`에는 process
수명 동안 값이 남으므로 host root/Docker daemon으로부터 숨기는 secret store는 아니다.

### HMAC v1

body는 UTF-8이 아닌 ASCII JSON(`sort_keys=True`, separator `,`/`:`, 공백 없음)이고
canonical 서명 입력은 마지막 줄바꿈 없이 다음 여섯 줄이다.

```text
v1
<decimal unix timestamp>
<single-use base64url nonce>
POST
<URL path only>
<lowercase hex SHA-256(body)>
```

헤더는 `X-Platform-HMAC-Version: v1`, `X-Platform-Timestamp`,
`X-Platform-Nonce`, `X-Platform-Content-SHA256`,
`X-Platform-Signature: v1=<lowercase HMAC-SHA256 hex>`다. API는 content hash와
constant-time signature 비교, 짧은 clock skew, nonce replay 금지를 모두 적용한다.
고정 test vector는 `jupyterhub/tests/contract_vectors.json`에 있다.

## Compose network와 legacy host 도구

기본 `compose-internal-trusted-v1` network는 Compose가 생성한다. Production에서는
`make production-*`/`scripts/production.sh`가 Engine별 Compose 파일을 자동 선택한다. 별도 root
bootstrap, sudo 또는 host firewall health manifest가 필요하지 않으며 JupyterHub가 매
spawn마다 Docker inspect 결과를 검증한다. 로컬의 새 execution/egress subnet은 이전
host-firewall profile의 subnet과 겹치지 않아 기존 external network와 stale
`DOCKER-USER` rule을 운영자가 그대로 두어도 새 stack의 packet을 선택하지 않는다.

Production 운영 스크립트는 Docker Engine major version을 검사한다. Engine 28+은
base `compose.production.yaml`의 `internal + IPv6 off + ICC + IPv4/IPv6 isolated-gateway`를
그대로 사용한다. Engine 27은 `compose.production.docker27.yaml`을 자동으로
추가하고 Compose `!override`로 `driver_opts`를 `ICC + inhibit_ipv4`의 exact set으로
교체한다. Engine 27에서는 Docker Compose 2.24.4 이상을 사용하고 raw base Compose
명령 대신 `make production-*` 또는 `scripts/production.sh`를 사용한다.

Base의 isolated option을 유지하므로 기존 Engine 28 network은 upgrade 시 config 차이 없이
계속 사용할 수 있다. 이는 Compose가 network를 불필요하게 재생성하거나 network ID를
바꾸는 위험을 피한다. Network option은 immutable이므로 실행 중 network에 다른
version의 option을 덧붙이거나 network를 수동 삭제하지 않는다.

아래 network/firewall 명령은 host 수정이 가능한 이전 `legacy-host-firewall-v1` 배포를
재현하기 위해서만 보존한다. 기본 로컬, domain-test 및 새 운영 Compose에서 실행하지 않는다.
이 모드를 별도로 선택한다면 운영 config를 `/etc/platform/host.json`으로 복사하고 root
소유, mode 0600으로 만든 뒤 기존 전체 health 계약을 함께 운영해야 한다.

```bash
sudo python3 infra/host/rotate_docker_generation.py --config /etc/platform/host.json
sudo python3 infra/host/create_jupyter_network.py --config /etc/platform/host.json
sudo python3 infra/host/enforce_jupyter_bridge_policy.py --config /etc/platform/host.json
sudo python3 infra/host/apply_jupyter_firewall.py --config /etc/platform/host.json
sudo python3 infra/host/check_network_health.py --config /etc/platform/host.json
sudo python3 infra/host/print_policy_digests.py --config /etc/platform/host.json
```

Legacy `network_health`는 current boot ID와 Docker generation에 묶인 최대 5분(기본
60초) manifest다. 해당 mode에서 Hub는 false, 누락, stale, policy digest mismatch를
모두 거부한다. Compose mode는 이 manifest를 읽지 않으며 설정돼 있으면 운영 시작을
거부해 두 정책을 혼동하지 않게 한다.

Legacy `enforce_jupyter_bridge_policy.py`는 Docker network의 이름·label·isolated gateway를
먼저 검증한 뒤 정확한 execution bridge만 대상으로 한다. NetworkManager가 Docker
bridge를 외부 연결로 인식해 IPv6 link-local 주소를 다시 추가한 경우 해당 device를
현재 daemon 수명 동안 unmanaged로 전환하고, Docker 28 isolated mode의
`disable_ipv6=1` 상태를 복원한다. 검사를 link-local 허용으로 완화하지 않는다.

명시적 unlimited volume bootstrap은 XFS quota를 가장하지 않고
`platform.quota.enforced=false` label을 남긴다.

```bash
python3 infra/host/provision_local_dev_volumes.py \
  --profile-policy infra/jupyterhub/profiles.local-dev.json \
  --user-id 00000000-0000-4000-8000-000000000001 \
  --username alice --output /tmp/local-user-00000000-0000-4000-8000-000000000001.json \
  --i-understand-this-is-unsafe-local-dev
```

로컬 provisioner는 output 디렉터리의 잠금과
`.local-project-id-allocations.json`을 이용해 사용자마다 10000부터 겹치지 않는 연속
5-ID block을 자동 예약한다. 예약은 Docker volume 생성보다 먼저 atomic write되므로 실패 후
같은 사용자를 재실행하면 같은 block을 재사용한다. 기존 `local-user-*.json` manifest도
검증 후 registry에 반영하며, manifest를 수동 삭제해도 예약은 자동 회수하지 않는다.

## Egress proxy

`egress-proxy/squid.conf`는 80/443만 허용하고 private, loopback, link-local,
documentation, multicast, metadata 대역을 IP로 재검사한 뒤 approved domain만 통과시킨다.
운영 allowlist example은 비어 있어 기본 deny다. Compose에서는 non-root,
`cap_drop: [ALL]`, `no-new-privileges`, read-only rootfs, `/tmp` tmpfs, 제한된 log driver로
실행하고 secret과 Docker socket을 주지 않는다. 승인된 HTTPS upload를 통한 반출까지
막는 DLP 경계는 아니다. 기본 Compose mode는 별도 host L3 rule에 의존하지 않고 Squid가
DNS 해석 결과를 포함해 RFC1918, loopback, link-local, metadata, documentation 및 특수
IPv4/IPv6 대역을 거부한다. 사용자 container에는 외부 route가 없는 execution network만
연결하고 `egress-out`에는 proxy만 연결한다. 이 application-layer 경계보다 강한 egress나
사용자별 network 격리가 필요하면 현재 신뢰 팀 전제와 Compose 방식을 재검토한다.

## 검증

네트워크 계약은 Linux의 실제 Docker Engine 27.1.2+에서 출시 spike를 수행해야 한다.
27.1.2는 기능 호환 하한이며 현재 지원·보안 권장 release라는 뜻이 아니다. 27.5.1 미만은
preflight 경고 대상으로 두고 가능한 경우 현재 지원되는 최신 patch를 사용한다.
저장소에서 가능한 정적 계약 시험은 다음과 같다.

```bash
python3 -m unittest discover -s infra/jupyterhub/tests -v
python3 -m unittest discover -s infra/host/tests -v
python3 -m py_compile infra/jupyterhub/*.py infra/host/*.py
```

정적 Compose 출력만으로 host의 live network 상태를 대신하지 않는다. production stack을
시작한 실제 host에서 다음을 확인한다.

```bash
docker network inspect platform-jupyter-compose-production \
  --format 'internal={{.Internal}} ipv6={{.EnableIPv6}} options={{json .Options}} labels={{json .Labels}} ipam={{json .IPAM.Config}}'
network_id="$(docker network inspect platform-jupyter-compose-production --format '{{.Id}}')"
bridge_name="br-${network_id:0:12}"
ip -4 -o address show dev "${bridge_name}"
ip -6 -o address show scope global dev "${bridge_name}"
```

두 `ip ... address` 명령은 아무 주소도 출력하지 않아야 한다. option은 Engine
28+에서 `enable_icc+gateway_mode_ipv4/ipv6=isolated`, Engine 27에서
`enable_icc+inhibit_ipv4` exact set이어야 한다. 이어 실제 single-user에서
host gateway·host 관리 주소·사내망·metadata·direct IP/DNS egress가 실패하고, Hub와 승인된
HTTP(S) proxy 목적지만 성공하는지 확인한다. Docker restart 후 같은 검사를 반복한다.
