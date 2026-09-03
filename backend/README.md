# Platform backend MVP

FastAPI 제어면, Platform SQLite/Alembic schema, 단일 durable worker와 JupyterHub HTTP adapter의 첫 실행 가능한 골격이다. 비밀번호는 이 서비스로 전달되거나 저장되지 않는다.

## 실행 명령

모든 production secret 환경변수를 주입한 뒤 같은 image를 세 역할로 실행한다.

```sh
# migration one-shot
python -m app.migrate upgrade head

# API (SQLite 전제상 worker/process 1개 고정)
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers 1

# durable operation worker (replica 1)
python -m app.worker --poll-seconds 1
```

Compose healthcheck는 API의 `GET /healthz`를 사용한다. DB 접속까지 확인할 때는 `GET /readyz`를 사용한다. `/readyz`의 `execution_host_healthy=false`는 API 자체 장애가 아니라 spawn을 닫아 둔 상태이므로 200을 유지한다.

로컬 격리 demo에서만 `PLATFORM_ALLOW_INSECURE_DEV_SECRETS=1`을 명시할 수 있다. 운영에서는 `.env.example`의 세 독립 secret을 생성하고 Compose secret/file 기반 주입으로 바꾼다. AES key는 다음처럼 만들 수 있다.

```sh
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Migration은 profile이나 volume slot을 임의로 seed하지 않는다. 검증된 provisioner가
owner-bound private named volume과 정확한 label을 준비한 뒤 `workspace_volume_slots`
inventory 반영과 사용자 `ACTIVE` 전환을 한 transaction으로 수행한다. 현재
`docker-volume-unlimited-v1` 정책에서는 내부 호환용 hard-limit 값으로 quota 적용을
추정하지 않는다. 그 전 workspace create API는 의도적으로 `PROVISIONING_REQUIRED`로
실패한다.

명시적 로컬 개발 Compose에서는 승인된 사용자가 포털의 **개인 개발공간 준비** 버튼을
눌러 `POST /api/v1/me/provisioning`을 호출할 수 있다. API는 CSRF와 본인 상태를 검사하고
1-user/1-job을 durable queue에 기록할 뿐 Docker socket을 갖지 않는다. Hub 컨테이너의
로컬 전용 managed service가 HMAC-authenticated internal endpoint로 job을 lease하고,
정확히 5개의 named volume을 검증한 manifest만 완료 처리한다. `/api/v1/me`의
`provisioning.status`는 `NOT_REQUESTED`, `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED` 또는
`MANUAL_REQUIRED`이며 UI는 진행 중에 이를 polling한다.

`PLATFORM_WEB_PROVISIONING_ENABLED=true`는 `PLATFORM_INSECURE_LOCAL_DEV=true`일 때만
허용되며 운영에서 켜면 설정 검증이 실패한다. 운영 사용자 활성화는 별도로 통제된
provisioner의 manifest를 아래 명령으로 import한다. 현재 기본 운영 저장 정책도
`docker-volume-unlimited-v1`이며 legacy XFS quota를 적용했다고 주장하지 않는다.

JupyterHub와 같은 immutable profile policy를 import하고 사용자를 활성화하는 명령은 다음과 같다.

```sh
python -m app.admin import-profiles --policy /run/platform-config/profiles.json
python -m app.admin list-users
python -m app.admin provision-user --username alice \
  --manifest /run/platform-health/user-<platform-user-uuid>.json
```

신규 workspace 요청은 catalog가 반환한 정확한 immutable tuple만 받는다.

```json
{"profile_id":"python312-cpu1-mem1024","profile_version":1}
```

image, command, volume 이름이나 CPU/RAM/disk 수치를 요청 body에 직접 넣을 수 없다.
`PLATFORM_WORKSPACE_CPU_BUDGET_MILLICORES`와
`PLATFORM_WORKSPACE_MEMORY_BUDGET_MB`는 각각 aggregate 실행 예약 한도이며 기본 로컬값은
8000m/4096MiB다. workspace 생성은 중지 상태로 storage slot만 배정하고, START admission과
15개 count 제한은 같은 SQLite `BEGIN IMMEDIATE` transaction 안에서 검사한다.
`GET /api/v1/capacity`와 admin capacity는
현재 CPU/RAM 예약량을 함께 반환한다.

프로필 응답의 `private_disk_quota_enforced=false`이면
`private_disk_limit_mb`는 `null`이다. DB, slot inventory와 profile digest에 남아 있는
byte 값은 기존 데이터 호환용 내부값일 뿐 사용자 제한이나 실제 quota가 아니다. 현재
local과 production의 `docker-volume-unlimited-v1` profile은 모두 명시적인 false를
사용하며, true는 별도로 선택한 legacy XFS quota mode에서만 유효하다.

Manifest는 user UUID/username, 정확히 5개 slot, 결정적 slot ID와 volume 이름,
UID/GID 및 owner/slot label을 검증할 수 있어야 한다. unlimited mode에서는
`platform.quota.enforced=false`가 profile과 일치해야 하며 project ID와 hard-limit 숫자는
quota 적용 증거로 취급하지 않는다. `infra/host/provision_user_volumes.py`는 명시적으로
선택한 legacy XFS mode용이고 현재 기본 운영 경로가 아니다. 모든 검증이 끝난 뒤에만
`PROVISIONING → ACTIVE`가 된다.

`infra/host/provision_local_dev_volumes.py`의 `unsafe_local_dev:true` manifest도 같은 `--manifest` 명령으로 import할 수 있지만 `PLATFORM_INSECURE_LOCAL_DEV=true`일 때만 허용한다. 이 경우 DB unique 검사에만 쓰는 deterministic synthetic project ID를 발급하고 quota가 검증됐다고 주장하지 않는다.

사용자별·shared hard quota가 없으므로 한 사용자의 쓰기가 host filesystem을 가득 채워
다른 workspace와 control plane을 중단시킬 수 있다. 운영자는 filesystem 여유 공간과
inode를 모니터링하고 임계치 경보, 새 spawn 차단·정리 runbook, control-plane 예약 공간,
bounded log, private/shared backup과 복구 시험을 운영 공개 전에 준비해야 한다.

Docker secret은 직접 env 대신 다음 `_FILE` 계약을 사용할 수 있다. 같은 값의 env와 `_FILE`을 동시에 지정하면 시작을 거부한다.

- `JUPYTERHUB_OAUTH_CLIENT_SECRET_FILE`
- `PLATFORM_TOKEN_ENCRYPTION_KEY_FILE`
- `PLATFORM_SESSION_HASH_KEY_FILE`
- `PLATFORM_INTERNAL_HMAC_KEY_FILE`

token 암호화 key와 key ID는 Platform DB backup과 별도로 같은 복구 정책에 포함해야 한다.
key를 잃으면 암호화된 Hub session token과 secret 환경변수를 복호화할 수 없고, 같은 key
ID에 다른 key를 넣으면 무결성 검사가 실패한다. 일반 환경변수는 DB에 평문, secret은
AES-GCM 암호문으로 저장되므로 DB backup 자체도 민감정보로 분류해 암호화·접근통제하고
key 교체는 명시적인 재암호화 migration과 restore 시험으로만 수행한다.

## 관리자·환경변수·삭제 API 계약

- workspace 생성은 offer의 정확한 `profile_id/profile_version`과 선택적인 NFC 정규화
  `name`만 받는다. 이름을 비우면 보유 slot에 따라 `환경-N`을 transaction 안에서 확정하고
  `STOPPED/NOT_FOUND`로 완료한다. 환경변수는 생성 후 workspace API로 설정한 다음 시작한다.
- user-global 환경변수는 `/api/v1/me/environment-variables`, workspace override는
  `/api/v1/workspaces/{id}/environment-variables`에서 관리한다. 일반 값만 GET에서 다시
  표시하고 secret은 항상 `value:null`이다. secret은 AES-GCM 암호문만, 일반 값은 평문만
  저장하며 mutation receipt/audit에는 어느 값도 넣지 않는다. effective map의 128개/64KiB
  한도는 저장 transaction 안에서 검증하므로 실패한 변경은 generation/spec/receipt까지
  모두 rollback된다.
- 실행 중 환경변수 변경은 process에 hot-apply하지 않는다. 응답의 `restart_required=true`와
  명시적 `POST .../actions/restart`를 사용한다. RESTART는 stop 뒤 Hub named-server record를
  JSON `{"remove":true}`로 제거하고 `NOT_FOUND`를 확인한 다음 새 1회성 snapshot으로 시작한다.
- 관리자 설정은 `/api/v1/admin/settings`, 논리 profile offer는
  `/api/v1/admin/profiles`, 전체 환경/집계는 `/api/v1/admin/workspaces|capacity`에서 관리한다.
  배포 CPU/RAM 값은 hard ceiling이다. 관리자는 그 이하의 positive whole millicore/MiB 값을
  선택 정책에 직접 추가할 수 있으며, 서버는 immutable Python runtime마다 전체 자원 조합을
  파생 profile로 materialize한다. Hub는 원본 runtime digest, 파생 digest와 같은 hard ceiling을
  재검증한다. offer는 image/command를 받지 않는다.
  같은 versioned singleton 정책의 `kernel_idle_timeout_seconds`는 `0` 또는 5분~7일의 분
  단위 값이다. worker가 시작 시점 값을 authorization에 snapshot하고 Hub가 다시 검증한 뒤
  Jupyter kernel culler argument로 적용한다. 변경은 이미 실행 중인 process에 hot-apply하지
  않으며 일반 사용자 capacity 응답에도 현재 정책을 안내용으로 포함한다.
  hard ceiling을 낮출 때는 먼저 이 API로 persisted budget을 새 ceiling 이하로 줄인 뒤
  배포 설정을 변경한다. 반대 순서에서는 admission/import가 의도적으로 fail-closed된다.
- workspace 응답의 `active_operation`은 최신 `PENDING|RUNNING|WAITING_EXTERNAL` lifecycle
  작업의 `{id,operation_type,status,progress_percent,requested_at}` 또는 `null`이다. 새 요청은
  이전 active lifecycle을 `CANCELLED/SUPERSEDED`로 종료하므로 새로고침 뒤에도 이 필드를
  기준으로 하나의 operation만 polling한다.
- 관리자 cross-user lifecycle만 worker 전용 `admin:servers` file credential을 사용한다.
  누락·scope 부족·401/403은 사용자 재로그인 문제가 아니므로 terminal
  `ADMIN_LIFECYCLE_UNAVAILABLE`이다. 브라우저 launch에는 token을 넣지 않으며 성공한 cross-user
  launch는 actor/workspace/owner ID만 감사한다.
- lifecycle은 `PLATFORM_LIFECYCLE_TIMEOUT_SECONDS`와 command attempt 상한으로 bounded다.
  삭제 실패는 tombstone/slot을 유지하고 새 idempotency key의 DELETE로 재시도한다. Hub가
  `NOT_FOUND`를 확인한 뒤에만 stable `deletion_id` volume wipe를 claim하며, 성공 callback이
  와야 workspace archive/slot 재사용 및 workspace 환경 secret 정리가 완료된다.
  unlimited Docker volume의 실제 allocator project ID는 DB의 collision 방지용 synthetic
  sentinel과 다를 수 있으므로 signed agent가 검증한 volume label의 양수 ID를 manifest
  기준으로 사용한다. quota enforcement가 켜진 profile만 DB project ID와 exact 일치를
  요구한다. signed completion이 결정적인 4xx로 거부되면 agent는 즉시 fail callback으로
  lease를 해제해 5분 만료를 기다리지 않는다.
- 관리자 running 집계와 cross-user launch는 `stale=false`뿐 아니라
  `PLATFORM_RECONCILIATION_FRESHNESS_SECONDS` 안의 `last_reconciled_at`을 요구한다.
- `python -m app.reconciler`는 별도 control-only process로 Hub의 stopped server 포함
  snapshot을 5초마다 읽는다. active lifecycle operation은 건너뛰고, 외부 stop/remove로
  실제 상태가 바뀔 때만 `EXTERNAL_CHANGE`를 1회 감사한다. Hub/stream/schema 오류 또는
  TTL을 넘긴 snapshot은 즉시 stale로 처리한다. 이 process에는 platform DB와 read-only
  reconciler token 외의 portal/admin/environment secret을 mount하지 않는다.
- reconciler health는 성공 DB commit 뒤 생성한 0600 atomic heartbeat의 snapshot 시작시각을
  검증한다. `python -m app.reconciler --healthcheck`가 실패하면 배포/복구 절차도 실패해야 한다.

## 테스트

```sh
python -m pip install -r requirements-dev.txt
pytest
```

테스트는 실제 Hub 대신 `FakeJupyterHubProvider`를 주입하며 OAuth state 단일 사용,
opaque cookie/암호문 저장, 소유권, transactional 5-slot/idempotency, provisioning
lease·manifest·재시도, worker 수렴, 관리자 actor/target 분리, 환경 secret/rollback,
restart/remove, crash-safe deletion과 populated 0003→0007 migration 데이터 보존을 확인한다.
