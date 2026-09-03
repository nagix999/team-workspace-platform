# cyberailabs.team 운영 서버 배포 가이드

이 문서는 검토된 release의 소스코드를 내부망 단일 Linux 서버로 옮긴 뒤,
`cyberailabs.team` 운영 서비스를 처음 설치하고 유지하는 전체 절차다. 대상 구성은 다음으로
고정한다.

| 항목 | 값 |
| --- | --- |
| 포털/API | `https://platform.cyberailabs.team` |
| JupyterHub | `https://cyberailabs.team` |
| 사용자 Jupyter | `https://<username>.cyberailabs.team` |
| 공인 VIP | `123.214.65.254:443` |
| 내부 운영 서버 | `10.155.1.24:3030` |
| TLS 종료 | 운영 서버의 Gateway Nginx |
| 배포 방식 | 단일 호스트 Docker Compose |

VIP는 HTTP를 해석하지 않는 L4 TCP passthrough/Static NAT로 구성한다. 플랫폼은 운영 호스트의
방화벽 규칙을 추가하거나 변경하지 않는다. 회사/VPN 접근 제한은 VIP·상위 네트워크 ACL과
Gateway의 source CIDR allowlist로 적용한다.

> **중요:** 이 절차는 새 운영 환경 설치 기준이다. 개발 PC의 SQLite 파일, Docker volume,
> `.runtime` 또는 secret을 임의로 복사하지 않는다. 기존 사용자 데이터 이전은 DB 두 개,
> profile policy, 암호화 key, private/shared volume을 한 시점에 결속해야 하는 별도 작업이다.
> 현재 저장소에는 local/domain-test에서 production으로 자동 이전하는 명령이 없다.

## 1. 배포 전 담당자 확인

진행 전에 다음 담당자와 값을 확정한다.

- 서버 담당자: `10.155.1.24` Linux/Docker/디스크/backup
- 네트워크 담당자: `123.214.65.254:443 → 10.155.1.24:3030` TCP 전달
- DNS 담당자: HostingKR의 apex, `platform`, `*` A record와 DNS-01 TXT
- 인증서 담당자: 발급, 만료 알림, 갱신과 개인키 보관
- 서비스 관리자: `PLATFORM_ADMIN_USERNAME`과 사용자 등록/퇴사 처리
- 접근정책 담당자: 회사/VPN 원본 CIDR 또는 VIP의 정확한 SNAT CIDR

다음 조건이 충족되지 않으면 외부 공개를 진행하지 않는다.

- 운영 서버와 Docker data root의 disk/inode 모니터링 및 경보
- Platform DB, Hub DB, private/shared volume과 secret의 off-host 암호화 backup
- 단일 호스트 장애 시 복구 담당자와 목표 복구시간
- 회사/VPN 밖 접근 차단과 허용 egress 목적지 검토
- 기존 workspace가 없는 새 설치이거나, 별도로 검증한 데이터 이전 계획

private/shared Docker volume에는 개별 hard quota가 없다. 호스트 가용 공간을 공동으로 사용하므로
disk 고갈 감시와 새 환경 생성 중단 절차가 필수다.

## 2. 운영 서버 요구사항

운영 서버에 다음이 필요하다.

- Linux 전용 호스트
- Docker Engine 28 이상
- Docker Compose v2
- Git, Make, Python 3.10 이상, OpenSSL, curl, `flock`(util-linux)
- DNS 확인용 `dig` 또는 동등 도구
- 인증서 발급용 Certbot 또는 호환 ACME client
- Docker image, 공식 SQLite 소스(`www.sqlite.org`)와 Python/npm 의존성을 내려받을 수 있는
  build-time 외부 통신
- 운영자가 `/var/run/docker.sock`을 사용할 수 있는 권한

버전을 확인한다.

```bash
docker version
docker compose version
python3 --version
openssl version
git --version
```

Docker daemon 접근을 위해 운영자를 `docker` 그룹에 추가했다면 완전히 로그아웃한 뒤 다시
로그인한다. Docker socket 권한은 사실상 host root 권한이므로 일반 사용자에게 부여하지 않는다.

서버 주소와 3030 port도 확인한다.

```bash
ip -4 -o address show
ss -ltnp
```

`10.155.1.24`가 서버에 실제로 설정되어 있어야 하며 첫 배포 전 `10.155.1.24:3030`은 비어
있어야 한다.

## 3. 검토된 소스코드 배치

임의의 최신 `main` 대신 검토한 release tag 또는 commit을 사용한다. 배포 tag는
`compose.production.yaml`과 이 문서를 포함해야 한다. 아래
`REVIEWED_RELEASE_TAG` 문자열을 그대로 실행하지 않는다.

```bash
sudo install -d -m 0750 -o "$USER" -g "$(id -gn)" /opt/team-workspace-platform
git clone https://github.com/nagix999/team-workspace-platform.git \
  /opt/team-workspace-platform
cd /opt/team-workspace-platform
git checkout --detach REVIEWED_RELEASE_TAG
git status --short
```

`git status --short` 출력이 없어야 한다. 운영 서버에서 소스코드를 직접 수정하지 않는다.
release에 서명이나 조직 checksum이 있다면 이 단계에서 함께 검증한다.

운영용 Compose는 `compose.production.yaml` 단독으로 사용한다. `compose.yaml`이나
`compose.domain-test.yaml`과 합치지 않는다.

## 4. DNS와 VIP 설정

HostingKR 권한 DNS에 다음 A record를 둔다.

| 이름 | 유형 | 값 |
| --- | --- | --- |
| `@` 또는 빈 이름 | A | `123.214.65.254` |
| `platform` | A | `123.214.65.254` |
| `*` | A | `123.214.65.254` |

이 구성에서는 Hub가 apex를 사용하므로 `hub` 또는 `*.hub` record가 필요하지 않다. 임의 사용자
ID가 한 단계 wildcard에 매칭된다.

```bash
dig +short A cyberailabs.team
dig +short A platform.cyberailabs.team
dig +short A dns-check-user.cyberailabs.team
```

세 결과가 모두 `123.214.65.254`여야 한다. 변경 직전에는 TTL을 낮추고, 검증이 끝난 뒤 조직
정책값으로 되돌린다.

네트워크 장비는 다음 계약으로 설정한다.

```text
123.214.65.254:443/TCP  ->  10.155.1.24:3030/TCP
```

- VIP에서 TLS를 종료하거나 인증서를 교체하지 않는다.
- HTTP Host/SNI를 rewrite하지 않는다.
- WebSocket과 장기 TCP 연결을 허용한다.
- 가능하면 원본 client IP를 보존한다.
- SNAT가 필수면 운영 서버에 실제로 보이는 SNAT CIDR을 네트워크 담당자가 제공한다.
- 운영 서버의 3030 port를 사용자망에 직접 개방하지 않고 VIP 경로에서만 도달하게 한다.

최초 관리자 bootstrap과 공개 전 검증이 끝나기 전에는 VIP를 사용자에게 개방하지 않거나
상위 ACL을 배포 담당자의 검증 source로만 제한한다. DNS-01 인증서 발급은 inbound VIP가 아직
열리지 않아도 가능하다.

## 5. 운영 secret과 runtime 초기화

최종 배포 운영자 계정으로 저장소에서 실행한다.

```bash
cd /opt/team-workspace-platform
make production-init
```

다음 항목이 생성된다.

- `.env.production`: 운영 설정, mode `0600`
- `secrets/production/`: OAuth, Hub, HMAC, 암호화 key
- `.runtime/production/`: profile policy와 배포 backup

secret은 Git, 메신저, 티켓, 일반 파일공유에 올리지 않는다. 특히
`token_encryption_key`를 잃으면 DB에 저장된 secret 환경변수와 token을 복호화할 수 없다.
`secrets/production`과 `.runtime/production/profiles.json`은 운영 DB와 같은 복구 세대로
암호화해 off-host에 보관한다.

`production-init`은 Docker socket GID와 운영자 기본 GID를 `.env.production`에 기록한다.
다른 사용자나 root로 번갈아 실행하지 않는다.

## 6. 공인 TLS 인증서 발급

개인키는 HostingKR에서 내려받는 파일이 아니다. 운영 서버의 Certbot이 직접 생성하며 서버
밖으로 내보내지 않는다. 와일드카드 인증서이므로 DNS-01을 사용한다.

```bash
sudo certbot certonly \
  --manual \
  --preferred-challenges dns \
  --cert-name cyberailabs.team \
  --agree-tos \
  -m YOUR_REAL_EMAIL \
  -d cyberailabs.team \
  -d platform.cyberailabs.team \
  -d '*.cyberailabs.team'
```

Certbot이 멈추면 HostingKR의 **네임서버/DNS → 새 DNS 레코드 추가**에서 TXT를 선택하고
화면에 표시된 이름과 값을 정확히 등록한다. 보통 다음 이름을 사용한다.

```text
_acme-challenge.cyberailabs.team
_acme-challenge.platform.cyberailabs.team
```

apex와 wildcard가 서로 다른 TXT 값을 같은 `_acme-challenge` 이름에 요구할 수 있다. 이때
기존 값을 덮어쓰지 말고 두 TXT 값을 동시에 유지한다. HostingKR가 zone suffix를 자동으로
붙이는 화면이면 이름 칸에는 `_acme-challenge` 또는 `_acme-challenge.platform`만 입력한다.

```bash
dig +short TXT _acme-challenge.cyberailabs.team
dig +short TXT _acme-challenge.platform.cyberailabs.team
```

Certbot이 요청한 모든 값이 보인 뒤 Enter를 누른다. 발급 성공 후 challenge TXT는 제거할 수
있다. 생성 위치는 다음 명령으로 확인한다.

```bash
sudo certbot certificates
```

일반적으로 다음 경로다.

```text
/etc/letsencrypt/live/cyberailabs.team/fullchain.pem
/etc/letsencrypt/live/cyberailabs.team/privkey.pem
```

Certbot `live` 파일은 symlink이고 현재 production preflight는 symlink bind를 거부한다. 전용
그룹과 일반 파일 복사본을 만든다.

```bash
getent group team-workspace-tls >/dev/null \
  || sudo groupadd --system team-workspace-tls
sudo usermod -aG team-workspace-tls "$USER"
```

그룹 추가 후 로그아웃/로그인을 완료하고 다음을 실행한다.

```bash
sudo install -d -m 0750 -o root -g team-workspace-tls \
  /etc/team-workspace/tls
sudo install -m 0644 -o root -g root \
  /etc/letsencrypt/live/cyberailabs.team/fullchain.pem \
  /etc/team-workspace/tls/fullchain.pem
sudo install -m 0640 -o root -g team-workspace-tls \
  /etc/letsencrypt/live/cyberailabs.team/privkey.pem \
  /etc/team-workspace/tls/privkey.pem
```

개인키를 `0644`로 만들거나 저장소 안으로 복사하지 않는다. 인증서와 SAN을 확인한다.

```bash
openssl x509 -in /etc/team-workspace/tls/fullchain.pem \
  -noout -subject -issuer -dates -ext subjectAltName
getent group team-workspace-tls
stat -c '%a %U:%G %n' /etc/team-workspace/tls/privkey.pem
```

SAN에는 다음 세 값이 모두 있어야 한다.

```text
DNS:cyberailabs.team
DNS:platform.cyberailabs.team
DNS:*.cyberailabs.team
```

## 7. 접근 CIDR 파일 생성

Gateway는 회사/VPN source만 허용하고 나머지는 연결을 닫는다. 네트워크 담당자가 확인한
IPv4 CIDR을 한 줄에 하나씩 작성한다.

```bash
sudo install -d -m 0755 -o root -g root /etc/team-workspace
sudoedit /etc/team-workspace/ingress-cidrs.txt
sudo chown root:root /etc/team-workspace/ingress-cidrs.txt
sudo chmod 0644 /etc/team-workspace/ingress-cidrs.txt
```

예시는 다음과 같다. 실제 값으로 바꾸고 주석 외 공백 문자를 넣지 않는다.

```text
# Corporate network
203.0.113.0/24
# Corporate VPN
198.51.100.32/27
```

`0.0.0.0/0`, 빈 파일, hostname, IPv6는 거부된다. VIP가 SNAT한다면 사용자 CIDR이 아니라
Gateway에서 관측되는 정확한 SNAT CIDR을 사용한다. 잘못된 CIDR은 모든 정상 사용자를 444로
차단하거나 반대로 범위를 과도하게 열 수 있다.

## 8. `.env.production` 검토

```bash
cd /opt/team-workspace-platform
vi .env.production
chmod 0600 .env.production
```

최소한 다음 값을 확인한다.

```dotenv
PRODUCTION_COMPOSE_PROJECT_NAME=team-workspace-production
PLATFORM_ADMIN_USERNAME=platform-admin
PLATFORM_GATEWAY_BIND_IP=10.155.1.24
PLATFORM_TLS_CERT_FILE=/etc/team-workspace/tls/fullchain.pem
PLATFORM_TLS_KEY_FILE=/etc/team-workspace/tls/privkey.pem
PLATFORM_INGRESS_CIDRS_FILE=/etc/team-workspace/ingress-cidrs.txt
PLATFORM_TLS_GID=REPLACE_WITH_TLS_FILE_GROUP_GID
PLATFORM_WORKSPACE_CPU_BUDGET_MILLICORES=8000
PLATFORM_WORKSPACE_MEMORY_BUDGET_MB=4096
DOCKER_GID=REPLACE_WITH_DOCKER_SOCKET_GID
PLATFORM_SECRET_GID=REPLACE_WITH_PRODUCTION_SECRET_GROUP_GID
```

`PLATFORM_TLS_GID`는 다음 출력의 세 번째 필드다.

```bash
getent group team-workspace-tls
```

`production-init`이 `DOCKER_GID`와 `PLATFORM_SECRET_GID`를 이미 숫자로 바꿔 놓아야 한다.
두 값이 여전히 `REPLACE_...`이면 Docker socket과 실행 운영자 그룹을 다시 확인하고
`production-init`을 정상 운영자 계정으로 재실행한다. `PLATFORM_TLS_GID`만 위에서 확인한
TLS 그룹의 숫자 GID로 직접 교체한다.

CPU와 메모리는 호스트 전체 사양이 아니라 **모든 실행 workspace가 예약할 수 있는 aggregate
hard ceiling**이다. OS, Docker, Gateway, Hub, DB와 build 여유를 남긴다. 관리자는 배포 후 이
ceiling 안에서 사용자가 선택할 CPU·메모리 값을 추가할 수 있다. 처음부터 호스트 최대값으로
설정하지 않는다.

`.env.production`에는 공백, shell 명령, 따옴표나 임의 확장을 넣을 수 없다. `KEY=value`의
제한된 형식만 production script가 허용한다.

## 9. egress와 차단 사용자 정책 검토

사용자 container는 직접 인터넷에 연결되지 않고 Squid allowlist를 통한다. 다음 파일을
검토한다.

```text
infra/egress-proxy/approved-domains.production.txt
```

기본값은 PyPI, npm과 승인된 GitHub download 경로다. 사내망, metadata 주소, 임의 domain을
넣지 않는다. 변경은 code review 후 새 image build로 배포한다.

퇴사자/차단 사용자는 다음 JSON에 exact username으로 관리한다.

```text
infra/jupyterhub/blocked-users.production.json
```

초기값 `[]`은 유효하다. 관리자 username을 차단 목록에 동시에 넣으면 Hub가 시작을 거부한다.

## 10. 운영 사전검사

배포 전 local/domain-test stack과 모든 Jupyter workspace를 중지한다. 운영 서버가 새 서버라면
해당 container가 없어야 한다.

```bash
cd /opt/team-workspace-platform
make production-preflight
```

사전검사는 다음을 fail-closed로 확인한다.

- 서버가 `10.155.1.24`를 실제 보유함
- 인증서/개인키 일치, key mode/GID, 24시간 이상 유효, 세 SAN
- 유효하고 비어 있지 않은 source CIDR allowlist
- 실행 중 single-user container와 local/domain-test stack 부재
- Platform/Hub DB volume이 둘 다 존재하거나 둘 다 존재하지 않음
- digest-pinned base image와 전체 Python/CPU/메모리 profile image 계약
- production Compose 렌더링과 Gateway `nginx -t`

첫 build는 image 다운로드와 Python/npm 설치 때문에 오래 걸릴 수 있다. 중간에 실패하면 원인을
고친 뒤 preflight를 다시 실행하되, digest pin을 임의 tag로 바꾸지 않는다.

성공 기준은 마지막의 다음 메시지다.

```text
production preflight passed
```

## 11. 최초 시작

```bash
make production-up
make production-ps
```

`production-up`은 preflight를 다시 실행하고 다음 순서로 진행한다.

1. 기존 DB가 있으면 Platform/Hub SQLite online backup 생성
2. production profile policy를 exact single-user image ID에 결속
3. Alembic migration과 profile import
4. API, Hub, reconciler, worker, frontend, proxy, Gateway 시작
5. container health와 서버 내부 HTTPS health 확인

새 설치면 DB volume 두 개를 새로 만든다. 기존 운영 DB 변경 뒤 실패하면 script가 출력한 backup
경로를 보존하고 무작정 `production-up`을 반복하지 않는다.

상태는 모든 장기 실행 service가 healthy이고 worker가 running이어야 한다. `migrate`,
`bootstrap-profile`, `singleuser-image`는 exit code 0인 one-shot container다.

상세 로그는 다음으로 본다.

```bash
docker compose --env-file .env.production -f compose.production.yaml \
  logs --tail=200 api jupyterhub reconciler worker gateway egress-proxy
```

## 12. 최초 관리자와 사용자 계정 생성

production에서는 웹 signup을 항상 닫는다. 새 DB에서 관리자를 만들기 위해 공개 Gateway와
Hub/worker/reconciler를 잠시 중지하고, 비밀번호를 argv·환경변수·로그에 남기지 않는 one-shot
도구를 사용한다.

```bash
make production-bootstrap-admin
```

프롬프트에서 `.env.production`의 `PLATFORM_ADMIN_USERNAME`용 비밀번호를 두 번 입력한다.
비밀번호는 12자 이상, common-password denylist 밖이어야 하며 UTF-8 72 byte를 넘지 않는다.
초기 관리자 bootstrap은 NativeAuthenticator 사용자 표가 비어 있을 때만 성공하고 기존 행을
덮어쓰지 않는다. 변경 전 Platform/Hub DB online backup을 만들며, 작업 후 control plane과
Gateway를 다시 healthy 상태로 시작한다.

일반 사용자는 운영자가 exact username을 지정해 사전 승인 계정으로 만든다.

```bash
make production-create-user USERNAME=alice
```

임시 비밀번호는 별도의 안전한 채널로 사용자에게 전달하고, 사용자는 첫 로그인 직후
`https://cyberailabs.team/hub/change-password`에서 변경한다. username은 소문자 영문으로
시작하고 소문자·숫자·단일 하이픈 조합의 최대 32자다. 계정 생성 명령도 잠깐의 control-plane
유지보수 구간을 사용하므로 사용자에게 공지한 뒤 실행한다.

관리자와 사용자는 포털 `https://platform.cyberailabs.team`에서 로그인한다. 첫 OAuth 로그인 후
개인 개발공간 준비가 완료되어야 workspace를 생성할 수 있다.

## 13. 공개 전 기능 검증

회사/VPN 허용망의 실제 브라우저에서 다음을 순서대로 확인한다.

### 13.1 DNS와 TLS

```bash
curl --fail --silent --show-error https://platform.cyberailabs.team/healthz
curl --fail --silent --show-error https://cyberailabs.team/healthz
curl --fail --silent --show-error https://alice.cyberailabs.team/healthz
```

- 세 주소 모두 공개 CA 신뢰 오류 없이 200
- 인증서의 apex/portal/wildcard SAN 확인
- 등록하지 않은 nested hostname과 잘못된 Host/SNI 거부
- 회사/VPN 밖 source는 연결 거부
- Gateway 외 host published port 없음

### 13.2 인증과 관리자

- 관리자 로그인 후 관리자 전용 메뉴 표시
- 일반 사용자에게 관리자 메뉴/API가 보이지 않음
- 로그아웃 후 기존 계정으로 자동 재로그인되지 않고 ID/password를 다시 요구
- 잘못된 비밀번호 반복 시 rate limit 확인
- 관리자 화면에서 생성 환경 수, 실제 실행 수, CPU/RAM 예약과 ceiling 확인

### 13.3 workspace

- 사용자 개인공간 준비 완료
- Python, CPU, 메모리 선택 후 중지 상태 workspace 생성
- 생성 후 일반/secret 환경변수 저장
- 환경변수 변경 시 restart 필요 안내
- 시작 후 JupyterLab, Notebook kernel, terminal과 WebSocket 동작
- `/home/jovyan/work` private data 영속성
- `/home/jovyan/shared` 팀 공유 read/write와 재시작 후 보존
- 직접 사내망/인터넷 연결 차단 및 승인된 PyPI/Git만 proxy를 통해 성공
- 중지, 재시작, 관리자 cross-user 열기/중지와 감사 이벤트
- 삭제 후 named server 제거, private volume wipe/re-provision 및 UI 수렴

### 13.4 VIP 경로

- 외부 443에서 내부 3030으로 TLS passthrough
- OAuth callback이 `https://platform.cyberailabs.team/api/v1/auth/callback`
- 사용자 URL이 `https://<username>.cyberailabs.team/user/...`
- Notebook event/terminal WebSocket 장기 연결
- 조직 정책이 허용한다면 큰 file upload 경로와 상위 장비 timeout
- Gateway 감사 IP가 원본 client 또는 검토된 SNAT 주소와 일치

## 14. 일상 운영

```bash
make production-ps
docker compose --env-file .env.production -f compose.production.yaml \
  logs --since=30m api jupyterhub reconciler worker gateway
df -h
df -i
docker system df
```

최소 경보 대상은 다음과 같다.

- host disk 사용률과 inode
- Docker data root 사용률
- Platform/Hub DB backup 성공과 off-host 복제
- Gateway 4xx/5xx, 로그인 실패/429
- API/Hub/reconciler health
- workspace start/delete 반복 실패
- 인증서 만료 예정일
- aggregate CPU/RAM 예약률

`docker system prune`, `docker image prune -a`, `docker volume prune`를 자동화하지 않는다. 기존
workspace가 고정한 retained runtime image나 사용자 volume을 삭제할 수 있다.

`make production-down`은 실행 workspace가 있으면 거부하며 control-plane container/network만
내린다. DB와 사용자 volume은 보존한다. `docker compose down -v`는 사용하지 않는다.

## 15. 인증서 갱신

`--manual` DNS 방식은 hook 없이는 자동 갱신되지 않는다. 만료 경보를 두고 충분히 이른
유지보수 일자에 최초 발급 명령을 다시 실행해 새 TXT 값으로 갱신한다. 발급 성공 전 기존
인증서/개인키를 덮어쓰지 않는다.

새 Certbot 인증서를 production regular file로 교체한다.

```bash
sudo install -m 0644 -o root -g root \
  /etc/letsencrypt/live/cyberailabs.team/fullchain.pem \
  /etc/team-workspace/tls/fullchain.pem.new
sudo install -m 0640 -o root -g team-workspace-tls \
  /etc/letsencrypt/live/cyberailabs.team/privkey.pem \
  /etc/team-workspace/tls/privkey.pem.new
sudo mv /etc/team-workspace/tls/fullchain.pem.new \
  /etc/team-workspace/tls/fullchain.pem
sudo mv /etc/team-workspace/tls/privkey.pem.new \
  /etc/team-workspace/tls/privkey.pem
```

그다음 검증하고 Gateway만 재생성한다.

```bash
make production-preflight
docker compose --env-file .env.production -f compose.production.yaml \
  up -d --no-deps --force-recreate gateway
curl --fail --silent --show-error https://platform.cyberailabs.team/healthz
```

장기적으로는 등록업체를 이전하지 않고도 `_acme-challenge`와
`_acme-challenge.platform`만 DNS API가 있는 validation zone으로 CNAME/NS 위임해 갱신을
자동화할 수 있다. DNS API key는 전체 zone 권한이 아닌 최소 권한을 사용한다.

## 16. 애플리케이션 업그레이드

운영 서버에서 Git release를 받은 뒤 실행할 명령과 판정 기준은
[10.155.1.24 운영 서버 Git 업데이트 절차](production-update-after-git-ko.md)를 따른다.
운영 container를 이미 전부 수동 삭제해 DB의 실행 의도만 남은 장애는 같은 문서의
`모든 container를 이미 삭제한 경우` 절차로만 복구한다. DB나 workspace volume을 직접
삭제·수정하지 않는다.

1. 모든 workspace를 중지하고 진행 중 operation/deletion이 없음을 확인한다.
2. 운영 DB·Hub DB, secret, profile policy와 사용자 volume backup을 검증한다.
3. 검토한 새 release tag를 checkout한다.
4. release note와 migration, `.env.production.example`, egress 변경을 비교한다.
5. `make production-preflight`를 실행한다.
6. `make production-up`을 실행한다.
7. health, 로그인, 기존 중지 workspace restart를 확인한다.

`production-up`은 기존 DB가 있을 때 두 DB의 SQLite online backup을 먼저 만든다. 출력된 경로는
서버 내부 임시 안전망이지 off-host backup을 대신하지 않는다.

DB 변경 이후 배포가 실패하면 출력된 명령을 검토해 복구한다.

```bash
make production-restore BACKUP=/absolute/path/to/verified-backup-bundle
make production-up
```

backup bundle은 Platform/Hub DB만 복원한다. secret, profile policy와 private/shared volume은
별도 backup 세트에서 같은 시점의 자료를 복원해야 한다.

## 17. Backup 정책

최소 backup 세트는 다음과 같다.

| 대상 | 중요 내용 |
| --- | --- |
| production Platform DB volume | 사용자, workspace, 감사, 암호화된 환경변수 |
| production Hub DB volume | ID/password hash, OAuth, named-server 상태 |
| `secrets/production` | DB 암호문 복호화와 서비스 인증에 필요한 key |
| `.runtime/production/profiles.json` | 기존 workspace의 immutable runtime 결속 |
| `platform.managed=true` private volumes | 사용자 작업 데이터 |
| `jupyter-shared` | 팀 공유 데이터 |
| TLS/ACME 계정 자료 | 인증서 갱신 연속성 |

배포 script의 DB backup과 별도로 조직 backup agent, storage snapshot 또는 검토된 volume backup
도구를 사용한다. 실행 중 SQLite 파일을 raw `cp`하지 않고 SQLite online backup을 사용한다.
사용자 volume 일관성이 필요한 업무는 workspace를 모두 중지한 유지보수 창에서 snapshot한다.

다음 복구 시험을 정기적으로 수행한다.

- 빈 격리 호스트에서 두 DB 복원 후 로그인
- secret과 profile policy를 함께 복원한 뒤 환경변수/restart 확인
- private volume 한 개와 shared volume 복원
- 인증서 없이 시작이 거부되고 올바른 인증서로만 Gateway healthy
- 운영 DB와 volume의 owner/label/slot binding 일치

## 18. 장애 대응 요약

| 증상 | 확인할 항목 |
| --- | --- |
| `this host does not own 10.155.1.24` | NIC 주소와 배포 대상 서버 확인 |
| TLS file이 symlink라 거부됨 | Certbot `live`를 직접 지정하지 말고 전용 regular file로 복사 |
| `PLATFORM_TLS_GID does not match` | key의 숫자 GID와 `.env.production` 비교 |
| 외부에서 444/timeout | source/SNAT CIDR, VIP ACL, ingress allowlist 확인 |
| 인증서 SAN 누락 | apex, explicit `platform`, wildcard 세 SAN으로 재발급 |
| preflight가 local stack을 발견 | local/domain-test workspace와 stack을 정상 종료 |
| image build/pull 실패 | 운영 host build-time DNS/HTTPS와 registry 접근 확인 |
| Hub/reconciler unhealthy | 해당 service log, secret mode/GID, DB migration 확인 |
| workspace start 실패 | API/worker/Hub log, profile image 보존, Docker network/volume label 확인 |
| 디스크 부족 | 새 생성/시작 중지, 보존정책에 따른 정리와 backup; volume prune 금지 |
| DB 변경 뒤 rollout 실패 | 반복 실행하지 말고 출력된 verified backup으로 restore 판단 |

비밀값, OAuth code/state, 환경변수 원문, 개인키를 문제 보고에 첨부하지 않는다. 로그를 공유할
때 query string, username, 내부 주소와 volume label도 조직 정책에 맞게 제거한다.

## 19. 최종 Go/No-Go 체크리스트

- [ ] 검토된 release tag/commit이며 working tree가 깨끗함
- [ ] Docker Engine 28+, Compose v2 확인
- [ ] 서버가 `10.155.1.24`를 보유하고 3030 충돌 없음
- [ ] DNS apex/portal/random wildcard가 VIP를 반환
- [ ] VIP 443→host 3030 L4 전달과 source IP/SNAT 계약 확인
- [ ] 인증서 SAN 세 개, key mode/GID, 만료 경보 확인
- [ ] 회사/VPN CIDR allowlist와 상위 ACL 확인
- [ ] CPU/RAM ceiling에 control-plane/OS 여유 포함
- [ ] egress domain과 blocked-user 정책 검토
- [ ] `make production-preflight` 성공
- [ ] `make production-up` 및 모든 health 성공
- [ ] 최초 관리자 bootstrap과 일반 사용자 첫 로그인 성공
- [ ] OAuth/logout, workspace 생성/환경변수/start/Jupyter/stop/delete 성공
- [ ] private/shared storage와 egress 격리 시험 성공
- [ ] DB·secret·profile·volume off-host backup 및 실제 restore 시험 성공
- [ ] 회사/VPN 밖 접근과 unknown Host/SNI 거부 확인

위 항목이 모두 확인된 뒤에만 일반 사용자에게 운영 URL을 공지한다.

## 공식 참고자료

- [Docker Engine 설치](https://docs.docker.com/engine/install/)
- [Let's Encrypt DNS-01 및 wildcard challenge](https://letsencrypt.org/ca/docs/challenge-types/)
- [Certbot manual DNS와 갱신 제한](https://eff-certbot.readthedocs.io/en/stable/using.html#manual)
- [HostingKR TXT 레코드 등록](https://help.hosting.kr/hc/ko/articles/5696985768217-TXT%EB%A0%88%EC%BD%94%EB%93%9C-%EB%93%B1%EB%A1%9D%ED%95%98%EA%B8%B0)
