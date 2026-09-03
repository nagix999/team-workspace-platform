# 10.155.1.24 운영 서버 Git 업데이트 절차

이 문서는 이미 초기 설치가 끝난 `10.155.1.24` 단일 운영 서버에서 검토된 새 release를
적용하는 절차다. DNS, VIP, 인증서, `.env.production`, secret과 Docker volume은 기존 값을
보존한다. 처음 설치하는 서버라면 먼저
[운영 서버 전체 배포 가이드](production-deployment-ko.md)를 따른다.

## 핵심 원칙

- 운영 서버에서는 임의의 최신 commit보다 검토하고 고정한 release tag를 배포한다.
- `git pull` 전에 모든 workspace를 정상 중지하고 진행 중 생성·시작·삭제 작업을 끝낸다.
- `.env.production`, `secrets/production`, `.runtime/production/profiles.json`과 Docker volume을
  삭제하거나 Git 파일로 덮어쓰지 않는다.
- 배포는 raw `docker compose up`이 아니라 `make production-preflight`와
  `make production-up`으로만 수행한다.
- `production-up` 직전에 production stack을 수동으로 내리지 않는다. 배포 스크립트가 기존
  실행 container를 기록한 뒤 writer를 중지해야 DB 변경 전 실패 시 원래 service를 되살릴 수
  있다.
- `docker compose down -v`, `docker volume prune`, `docker image prune -a`는 실행하지 않는다.

## 1. Git 변경 전 유지보수 준비

포털 관리자 화면에서 모든 환경을 중지하고 생성·시작·중지·재시작·삭제·개인공간 준비 작업이
끝날 때까지 기다린다. 사용자에게 유지보수 시간을 공지하고 새 작업을 시작하지 않게 한다.

운영 코드 경로와 현재 revision을 기록한다.

```bash
cd /opt/team-workspace-platform
git status --short
git rev-parse HEAD
git describe --tags --always --dirty
```

`git status --short` 출력이 있으면 pull하지 않는다. 운영 서버의 수동 수정 사항을 먼저 별도
검토한다. 애플리케이션 배포 전에도 조직의 off-host backup 정책에 따라 Platform DB, Hub DB,
secret, profile policy, private/shared volume backup이 최신이고 복구 가능한지 확인한다.

## 2. 검토된 release 받기

권장 방식은 release tag 고정이다.

```bash
git fetch --tags --prune origin
git checkout --detach REVIEWED_RELEASE_TAG
git status --short
git rev-parse HEAD
```

`REVIEWED_RELEASE_TAG`는 실제 검토된 tag로 바꾼다. 현재 운영 branch를 계속 추적해야 하는 조직
정책이라면 fast-forward만 허용한다.

```bash
git checkout main
git pull --ff-only origin main
```

merge commit을 운영 서버에서 직접 만들거나 `git reset --hard`, `git clean`, `git stash`로
문제를 숨기지 않는다. pull을 사용했다면 변경 범위를 확인한다.

```bash
git diff --stat ORIG_HEAD HEAD
git diff ORIG_HEAD HEAD -- \
  .env.production.example compose.production.yaml compose.production.docker27.yaml \
  scripts/init-production.sh scripts/production.sh scripts/validate_production_network.py \
  backend/alembic infra/jupyterhub gateway
```

배포 tag에는 최소한 `compose.production.yaml`, `compose.production.docker27.yaml`,
`scripts/production.sh`, `scripts/validate_production_network.py`와 이 문서가 포함되어야
한다. 과거 tag가 이 운영 구성을 포함하지 않으면 그 tag를 운영에 배포하지 않는다.

## 3. 운영 설정 병합

기존 운영 설정을 지우지 않고 초기화 도구를 다시 실행한다. 이 명령은 없는 secret만 만들고
현재 Docker socket/운영자 group GID를 갱신하며 기존 secret 값을 덮어쓰지 않는다.

```bash
make production-init
chmod 0600 .env.production
```

새 `.env.production.example`과 기존 `.env.production`을 비교해 신규 key를 수동 반영한다.

```bash
diff -u .env.production.example .env.production || true
```

다음 값은 운영 계약과 일치해야 한다.

- `PLATFORM_GATEWAY_BIND_IP=10.155.1.24`
- TLS 인증서/개인키와 ingress CIDR 파일의 절대 경로
- host 용량에 맞는 CPU/RAM hard ceiling
- `PLATFORM_ADMIN_USERNAME`
- Docker socket, secret, TLS 파일의 숫자 GID

Docker Server Engine의 기능 호환 하한은 27.1.2다. 27.1.2는 현재 지원·보안 권장 release라는
뜻이 아니며 preflight는 27.5.1 미만에서 경고한다. 가능한 즉시 27.5.1 이상 또는 조직이 승인한
현재 지원 release로 올린다. 플랫폼은 host firewall을 설치하지 않지만 Docker daemon이
bridge용 firewall/netfilter rule을 관리하는 기능은 끄면 안 된다. Engine 27은
compatibility overlay의 `!override`를 위해 Docker Compose 2.24.4 이상을 요구한다.

```bash
docker version --format 'Server={{.Server.Version}} API={{.Server.APIVersion}}'
docker compose version
```

운영 스크립트는 Engine을 검사해 28+에서 base Compose의 exact ICC + IPv4/IPv6
isolated-gateway set, 27에서 base + compatibility overlay의 exact ICC + `inhibit_ipv4`
set을 자동 선택한다. Base의 isolated 정의를 유지하면 기존 Engine 28 network에 config
차이가 생기지 않아 불필요한 network 재생성과 network ID 변경을 피한다. Docker network
option은 제자리에서 바꿀 수 없으므로 운영 Compose를 수동 주석 처리하거나 실행 중
network를 삭제하지 않는다. 모든 운영 조작은 `make production-*`를 사용한다.

`PLATFORM_PUBLIC_VIP` 설정은 필요하지 않다. 공인 `123.214.65.254:443`에서
`10.155.1.24:3030`으로의 전달은 방화벽/VIP 장비의 L4 NAT 계약이며 애플리케이션은 공인 VIP를
bind하거나 라우팅 판단에 사용하지 않는다. 예전 `.env.production`에 해당 key가 남아 있다면
혼동을 피하려고 제거한다.

인증서와 source CIDR 파일은 Git으로 배포하지 않는다. 운영 leaf에
`DNS:cyberailabs.team`과 `DNS:*.cyberailabs.team` SAN이 모두 있는지와 만료, key 권한, 서버
주소를 확인한다. wildcard SAN이 `platform.cyberailabs.team`을 보호하며 포털 URL은 그대로
유지한다.

```bash
openssl x509 -in /etc/team-workspace/tls/fullchain.pem \
  -noout -dates -ext subjectAltName
stat -c '%a %u:%g %n' \
  /etc/team-workspace/tls/fullchain.pem \
  /etc/team-workspace/tls/privkey.pem \
  /etc/team-workspace/ingress-cidrs.txt
ip -4 -o address show | grep -F '10.155.1.24/'
```

## 4. 사전검사와 실제 배포

```bash
make production-preflight
```

이 단계는 host/TLS/CIDR, Docker volume 쌍, single-user image 계약, Compose/Nginx 설정을 검사한다.
기존 DB가 있으면 SQLite online snapshot을 이용해 다음 상태가 전부 비어 있는지도 확인한다.

- 실행 중 workspace 또는 single-user container
- PENDING/RUNNING/WAITING_EXTERNAL lifecycle operation
- 진행 중 사용자 provisioning
- 진행 중 workspace volume 삭제

통과한 경우에만 실제 배포를 실행한다.

```bash
make production-up
```

`production-up`은 preflight에서 image/Compose 계약을 다시 검사하고, writer를 중지한
직후 DB idle을 한 번 더 확인한 뒤 Platform/Hub DB online backup, migration과 profile
import를 수행한다. 그런 다음 내부 service를 먼저 시작하고 live execution-network,
host bridge·non-root probe를 검증한 뒤에만 Gateway를 publish/start하여 HTTPS health를
확인한다. Network 검증이 실패하면 Gateway는 시작되지 않는다.
출력된 backup bundle 경로와 배포한 Git commit을 변경 기록에 남긴다.

## 5. 배포 직후 확인

```bash
make production-ps
make production-logs
```

확인 기준은 다음과 같다.

- `api`, `jupyterhub`, `reconciler`, `frontend`, `egress-proxy`, `gateway`가 healthy
- `worker`가 healthy (PID 1 operation-worker liveness 검사)
- `migrate`, `bootstrap-profile`, `singleuser-image`가 exit code 0
- Gateway 이외의 host published port가 없음
- Gateway가 정확히 `10.155.1.24:3030`을 publish
- 반복 restart, migration error, profile digest error, OAuth/RBAC error가 없음

서버 내부에서 Gateway까지 확인한다.

```bash
curl --fail --silent --show-error \
  --connect-to platform.cyberailabs.team:443:10.155.1.24:3030 \
  https://platform.cyberailabs.team/healthz
curl --fail --silent --show-error \
  --connect-to cyberailabs.team:443:10.155.1.24:3030 \
  https://cyberailabs.team/healthz
```

실제 execution bridge도 확인한다.

```bash
docker network inspect platform-jupyter-compose-production \
  --format 'internal={{.Internal}} ipv6={{.EnableIPv6}} options={{json .Options}} labels={{json .Labels}} ipam={{json .IPAM.Config}}'
network_id="$(docker network inspect platform-jupyter-compose-production --format '{{.Id}}')"
bridge_name="br-${network_id:0:12}"
ip -4 -o address show dev "${bridge_name}"
ip -6 -o address show scope global dev "${bridge_name}"
```

두 address 명령의 출력은 비어 있어야 한다. option은 Engine 28+에서
`enable_icc+IPv4/IPv6 isolated`, Engine 27에서 `enable_icc+inhibit_ipv4` exact set이어야
한다. 실제 workspace에서도
host·사내망·metadata·direct IP/DNS egress가 실패하고 승인 proxy만 성공하는지 확인한다.

그다음 실제 허용된 사내 PC에서 다음 smoke test를 수행한다.

1. 포털 로그인과 명시적 로그아웃 후 재인증
2. 관리자 메뉴, capacity와 감사 이벤트 조회
3. 기존 중지 workspace 하나 시작
4. JupyterLab, kernel, terminal, WebSocket 확인
5. private `/home/jovyan/work`와 shared `/home/jovyan/shared` 확인
6. 일반/secret 환경변수 변경 후 restart 안내와 재시작 적용 확인
7. workspace 중지 및 테스트용 workspace 삭제 수렴 확인

## 6. 최초 설치에서만 수행할 작업

새 DB를 만든 첫 배포에만 최초 관리자 계정을 만든다.

```bash
make production-bootstrap-admin
```

기존 운영 DB upgrade에서는 이 명령을 다시 실행하지 않는다. 승인된 일반 사용자를 추가할 때만
다음을 사용한다.

```bash
make production-create-user USERNAME=alice
```

## 7. 실패 시 처리

DB mutation 전에 실패하면 배포 스크립트가 원래 실행 중이던 service를 재시작한다. 원인을
로그에서 확인한 뒤 수정한다.

`team-workspace-production-worker-1 has no healthcheck configured`가 나오면 worker 장애나
workspace busy 상태가 아니라, worker healthcheck가 빠진 과거 Compose 파일과 `--wait`가 함께
사용된 것이다. `v0.1.4` 이상에는 HTTP listener 대신 PID 1 operation-worker의 liveness를
검사하는 전용 healthcheck가 포함된다. 운영 파일을 수동 편집하지 말고 검토된 최신 release를
적용한 뒤 `docker compose ... config`의 `services.worker.healthcheck.test`가
`python -c "import os; os.kill(1, 0)"`인지 확인한다.

DB migration/profile import 이후 실패하면 같은 명령을 무작정 반복하지 않는다. 출력된
`production database backup` 경로와 `restore with` 안내를 보존하고, 실패 시점과 DB revision을
검토한 뒤 필요할 때만 복원한다.

```bash
make production-restore BACKUP=/absolute/path/to/verified-backup-bundle
make production-up
```

이 복원 명령은 Platform/Hub DB만 복원한다. secret, profile policy, private/shared volume까지
영향을 받은 장애라면 같은 backup 세대의 자료를 함께 복원해야 한다.

로그나 장애 티켓에 `.env.production`, secret, TLS 개인키, 환경변수 값, OAuth query를 첨부하지
않는다.

## 8. 모든 container를 이미 삭제한 경우

운영자가 production Compose container와 single-user container를 모두 수동 삭제했지만 Docker
volume은 남아 있는 경우에는 일반 `production-up`을 바로 반복하지 않는다. DB에
`desired=RUNNING`, `observed=STOPPED` 같은 실행 의도가 남아 있으면 idle gate가 배포를 막는 것이
정상이다. 이 절차는 **container만 전부 없어졌다는 사실을 확인한 비상 복구 전용**이며 평상시
workspace 중지 수단이 아니다.

먼저 volume을 삭제하지 말고 이름과 존재 여부를 확인한다.

```bash
docker volume inspect \
  team-workspace-production_platform_data \
  team-workspace-production_jupyterhub_data
```

`.env.production`에서 `PRODUCTION_COMPOSE_PROJECT_NAME`을 바꿨다면 위 이름의
`team-workspace-production` 부분도 그 값으로 바꾼다. private workspace와 `jupyter-shared`
volume 역시 그대로 둔다. 두 DB volume 중 하나만 없거나, `docker ps -a`에 production/static 또는
single-user container가 하나라도 남아 있으면 아래 명령을 실행하지 말고 상태를 먼저 조사한다.
복구 명령은 container를 대신 삭제하지 않고 fail closed한다.

변경 대상만 읽는 dry-run을 실행한다.

```bash
make production-offline-quiesce-dry-run
```

출력된 workspace ID와 대상 개수를 운영 장애 기록에 남긴다. 예를 들어 정확히 1개가 대상이면
그 개수를 명시해 적용한다.

```bash
make production-offline-quiesce EXPECTED_COUNT=1
```

적용 명령은 다음 순서를 자동으로 강제한다.

1. 다른 production 작업과 겹치지 않도록 nonblocking operator lock을 획득한다.
2. production Compose container와 모든 `platform.kind=jupyter-singleuser` container가 실행/중지
   상태를 막론하고 0개인지 확인한다.
3. Platform/Hub DB volume이 정확히 한 쌍이며, 이름이나 label과 무관하게 두 DB volume을
   mount한 container도 0개인지 확인한다.
4. 두 SQLite DB를 online-backup API로 복제하고 manifest와 digest가 있는 하나의 finalized backup
   bundle로 검증한다.
5. application-aware maintenance CLI를 다시 dry-run한다.
6. container 부재를 다시 확인하고 dry-run 대상 수가 `EXPECTED_COUNT`와 정확히 같을 때만 하나의
   DB transaction으로 실행 의도를 중지 상태로 수렴시킨다.
7. 배포 idle gate가 통과하는지 새 snapshot으로 확인한다.

운영자는 raw SQL을 실행하지 않으며 maintenance CLI만 검증된 단일 transaction으로 DB를
변경한다. CLI는 대상 workspace의 private/shared volume을 mount하거나 삭제하지 않는다. 적용
결과와 함께 출력되는 `production database backup` 절대 경로를 보존한다.
예상 개수가 다르면 transaction은 적용되지 않으므로 새 dry-run 결과를 조사한 뒤 운영자가 개수를
다시 승인해야 한다.

성공한 뒤에만 정상 배포 절차로 돌아간다.

```bash
make production-preflight
make production-up
make production-ps
```

복구 적용 뒤 검증이 실패하면 `production-up`을 반복하기 전에 출력된 backup과 감사 이벤트를
확인한다. 검증된 backup으로 되돌려야 할 때만 다음을 사용한다.

```bash
make production-restore BACKUP=/absolute/path/to/production-backup-bundle
```

이 상황에서도 `docker compose down -v`, `docker volume prune`, raw `sqlite3 UPDATE`는 사용하지
않는다.
