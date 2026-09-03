# ADR-0007: 관리자 실행 제어, 환경변수와 파기 가능한 workspace

- 상태: Accepted (2026-08-11)
- 날짜: 2026-08-11
- 결정 범위: 관리자 cross-user lifecycle/launch, 환경변수, resource offer, workspace 영구 삭제
- 관련 결정: [ADR-0001](0001-jupyterhub-control-plane.md), [ADR-0004](0004-single-host-compose.md), [ADR-0006](0006-trusted-network-shared-storage.md)

> 2026-08-15: CPU/RAM 선택값을 정적 catalog로만 제한한 결정과 생성 요청의 초기
> 환경변수 입력 범위는 [ADR-0008](0008-derived-resource-profiles-and-stopped-creation.md)이
> 대체한다. 환경변수 암호화·snapshot 및 나머지 관리자/삭제 결정은 그대로 유효하다.

이 결정은 ADR-0001의 “전역 lifecycle credential을 사용하지 않는다”와 ADR-0004의
“영구 삭제는 관리자 수동 runbook만 사용한다”를 아래의 제한된 범위에서 대체한다.
일반 사용자 lifecycle의 사용자 위임 OAuth, portal API의 무권한 원칙과 단일 호스트
Docker trust boundary는 그대로 유지한다.

## 맥락

관리자는 포털에서 모든 workspace를 조회하고 다른 사용자의 환경을 시작·중지·재시작·접속·
삭제해야 한다. 사용자는 user-global 또는 workspace별 환경변수를 관리하고, 관리자는 전체
CPU/RAM 예산과 노출할 실행 조합을 정해야 한다. 실행 중 환경변수 변경을 즉시 process에
주입하면 notebook/kernel별 상태가 갈리고 감사 가능한 실행 사양이 사라진다. 영구 삭제는
Docker volume 제거 권한과 crash-safe checkpoint가 필요하다.

## 결정

### 관리자 lifecycle과 브라우저 접속

- JupyterHub service `platform-admin-lifecycle`에 정확히 `admin:servers`만 부여한다.
- token은 JupyterHub와 HTTP listener가 없는 operation worker에만 file로 mount한다.
  portal API, frontend, migration, profile bootstrap에는 file path와 값 모두 전달하지 않는다.
- 일반 사용자 작업은 계속 로그인 사용자의 `servers!user` 위임 token을 사용한다. 관리자
  작업만 worker가 서버 측 target username을 구성해 별도 lifecycle token으로 수행한다.
- lifecycle token은 server record의 조회·시작·중지·제거용이다. notebook 내용 접근에는
  사용하지 않는다. 관리자 launch는 fresh canonical URL을 확인한 뒤 token 없는 `303`을
  반환하고, 관리자 자신의 Hub browser cookie로 다시 인증한다.
- Hub 시작 때 built-in admin role에 `access:servers`가 있는지 검사한다. single-user OAuth
  client가 이를 target server scope로 축소하는 현재 JupyterHub 5.5 계약이 바뀌면 시작을
  거부한다. portal OAuth client의 scope는 `servers!user`에서 넓히지 않는다.

이 credential이 탈취되면 모든 사용자 server의 가용성에 영향을 줄 수 있지만 사용자 token
발급, 사용자 관리나 notebook 내용 접근 권한까지 주지는 않는다. worker isolation, 짧은
operation lease, target owner 검증과 감사 로그가 필수다.

### 환경변수 snapshot

- backend는 user-global 값 위에 workspace 값을 덮어쓴 effective map을 spawn authorization에
  암호화 snapshot으로 고정한다. `is_secret`은 backend 저장/API/idempotency에만 사용하고
  JupyterHub에는 보내지 않는다.
- consume 응답만 `environment` 원문, 두 scope generation과 HMAC digest를 포함한다. check
  요청, Docker label, Hub `user_options`, 감사·오류 로그에는 원문을 넣지 않는다.
- digest는
  `HMAC-SHA256(key, "platform-spawn-environment-v1\0" + canonical_json(map))`이고,
  canonical JSON은 ASCII, key 정렬, 공백 없는 separator를 사용한다.
- key는 portable ASCII identifier 최대 64자, 값은 유효 UTF-8 최대 16 KiB/NUL 금지,
  effective map은 최대 128개와 canonical 64 KiB다. startup, identity, loader, proxy/CA,
  Python/Conda/Jupyter/Docker/platform namespace는 대소문자 구분 없이 예약한다.
- Hub는 HMAC/generation을 검증하고 별도 in-memory map에 잠시 보관한다. platform의 HOME,
  proxy, Hub/bootstrap 값은 마지막에 merge해 항상 우선한다. JupyterHub 5.5에는
  `post_spawn_hook`이 없으므로 전용 DockerSpawner wrapper가 `create_object` 직후와 모든
  `start` 종료/예외의 `finally`에서 snapshot과 merged environment 사본을 지운다.
  `pre_spawn_hook` 거부와 `post_stop_hook`도 같은 정리를 반복해 실패 경로를 닫는다.
- container process가 환경변수를 사용하려면 Docker `Config.Env`에는 실행 중 원문이 남는다.
  host root/Docker daemon 권한자와 해당 process owner에게서 값을 숨기는 secret manager는
  아니다. 일반 값은 API에서 재표시할 수 있지만 secret 값은 write-only로 유지한다.

실행 중 변경은 hot-apply하지 않는다. `restart_required`만 표시하며 명시적 RESTART가
RUNNING → stop/NOT_FOUND → 새 ticket/snapshot으로 spawn한다. 실패 시 이전 container를
몰래 되살리지 않고 operation 상태로 드러낸다.

### CPU/RAM과 논리 profile

배포 설정의 digest-bound runtime profile tuple이 실행 가능한 최상위 allowlist다. 관리자가
만드는 profile offer는 기존 `(profile_id, version, config_digest)`를 참조하는 이름/설명/
노출 정책일 뿐 image, CPU, memory나 command를 새로 정의하지 않는다. spawn ticket에는
offer 값이 아니라 underlying immutable tuple만 들어간다. Hub는 local allowlist의 CPU/RAM,
image, kernel, mount/security 설정만 적용한다.

관리자 CPU/RAM budget과 selectable 값은 그 catalog 안에서만 줄이거나 선택할 수 있다.
backend가 SQLite transaction에서 active reservation 합계를 검사하고, Hub 15개 active limit가
마지막 동시성 방어선이다. 운영 hard ceiling이나 Hub allowlist를 UI로 확대할 수 없다.
로컬 검증 catalog는 Python 3.12/3.13, CPU 1/2/4 core, memory 1/2/4 GiB의
18개 조합을 미리 생성한다. 관리자는 이 중 CPU·memory 값을 사용자 선택 목록에 추가하거나
제거할 수 있으며, 임의 숫자를 runtime limit으로 직접 주입할 수는 없다.

### 영구 삭제와 volume wipe

`PLATFORM_WORKSPACE_DELETION_ENABLED`는 가입용
`PLATFORM_WEB_PROVISIONING_ENABLED`와 분리한다. production에서 가입 provisioning은 계속
금지한다. 삭제는 backend와 Hub agent 양쪽 flag가 켜지고 storage mode가 정확히
`docker-volume-unlimited-v1`일 때만 허용한다.

1. backend가 tombstone/spec 증가와 start/ticket 차단을 먼저 수행하고 Hub server가
   `NOT_FOUND`임을 확인한 뒤 HMAC 내부 claim을 발급한다.
2. retry 동안 유지되는 immutable `deletion_id`를 만든다. user-visible `operation_id`는 retry
   때 바뀔 수 있으므로 destructive checkpoint에는 결속하지 않는다.
3. privileged agent는 workspace/owner/username/spec/slot/name과 local driver, 빈 options,
   private-only exact label 및 정책 UID/GID/limit을 다시 검증한다. shared volume은 거부한다.
4. Docker remove 전에 exact binding과 `PREPARED`를 atomic write + file/directory fsync한다.
   PREPARED retry는 volume이 있으면 label을 다시 검증한 뒤 `force=false`로 제거하고, 없으면
   이미 제거된 것으로 본다. 그 뒤에만 `REMOVED`를 durable write한다.
5. REMOVED에서 같은 name/label의 빈 volume을 만들고 network 없는 최소권한 helper로 root를
   `uid:gid/0700`으로 초기화한 뒤 label과 `stat`을 exact 검증한다. backend가 manifest를
   검증해 complete해야만 workspace를 archive하고 slot을 재사용 가능하게 한다.

production agent는 digest-pinned profile allowlist와 host에 미리 존재하는 initializer image만
사용하고 자동 pull하지 않는다. local/domain-test만 allowlist image pull을 허용한다. 삭제
checkpoint 디렉터리는 host reboot/container 재생성에도 보존돼야 한다. wildcard, volume
prune, 강제 remove와 shared volume 삭제는 사용하지 않는다.

## 운영 상태와 잔여 위험

현재 저장소에는 loopback local integration용 `compose.yaml`과 별도 production Compose가
모두 있다. Production deletion은 persistent checkpoint mount와 digest-pinned/preloaded image를
포함하지만, 운영 배포 준비 완료를 자동으로 뜻하지는 않는다. Docker socket 경계,
DB/volume backup, disk/inode 경보와 실제 crash-recovery 시험을 별도로 승인해야 한다.

Docker socket은 여전히 host-root 상당 권한이며 삭제는 backup이 없으면 복구 불가능하다.
checkpoint는 remove 전/후 crash의 중복 파기를 막지만 악의적인 host root나 Docker daemon
침해를 막지 못한다.

## 검증 조건

- lifecycle role이 정확히 `admin:servers`이고 secret mount가 Hub/worker로만 제한됨
- built-in admin browser access 계약과 token 없는 cross-user launch 회귀 시험
- environment digest/generation/size/예약 key/precedence 및 check·label·user_options 무원문 시험
- Docker create/start 성공·실패 및 pre-spawn 거부 뒤 Hub plaintext가 제거되고, 새 ticket
  spawn이 새 snapshot을 다시 적용함
- offer/image/resource 주입이 local immutable tuple을 바꾸지 못함
- deletion-only agent가 provisioning endpoint를 호출하지 않음
- remove 직후 checkpoint 전 crash, recreate 직후 complete 전 retry가 안전하게 복구됨
- PREPARED 뒤 wrong-label volume 교체, shared/mounted/missing-policy volume을 삭제하지 않음
- production에서 mutable image와 자동 pull이 거부되고 local/domain-test에서만 제한 pull 허용
