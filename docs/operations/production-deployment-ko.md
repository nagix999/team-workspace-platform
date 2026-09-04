# cyberailabs.team 운영 서버 배포 가이드

이 문서는 검토된 release의 소스코드를 내부망 단일 Linux 서버로 옮긴 뒤,
`cyberailabs.team` 운영 서비스를 처음 설치하고 유지하는 전체 절차다. 대상 구성은 다음으로
고정한다.

| 항목 | 값 |
| --- | --- |
| 포털/API | `https://platform.cyberailabs.team` |
| JupyterHub | `https://cyberailabs.team` |
| 사용자 Jupyter | `https://<username>.cyberailabs.team` |
| 공인 VIP | `<PUBLIC_VIP>:443` |
| 내부 운영 서버 | `<INTERNAL_SERVER_IP>:3030` |
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

- 서버 담당자: `<INTERNAL_SERVER_IP>` Linux/Docker/디스크/backup
- 네트워크 담당자: `<PUBLIC_VIP>:443 → <INTERNAL_SERVER_IP>:3030` TCP 전달
- DNS 담당자: HostingKR의 apex와 `*` A record, 선택적 `platform` A record, DNS-01 TXT
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
- Docker Engine 27.1.2 이상(기능 호환 하한)
- Docker Engine 27.5.1 이상 또는 현재 지원되는 최신 보안 패치 release 강력 권장
- Docker Compose v2; Engine 27은 compatibility overlay의 `!override`를 지원하는 2.24.4 이상
- Git, Make, Python 3.10 이상, OpenSSL, curl, `flock`(util-linux)
- DNS 확인용 `dig` 또는 동등 도구
- 인증서 발급용 Certbot 또는 호환 ACME client
- Docker image, 공식 SQLite 소스(`www.sqlite.org`)와 Python/npm 의존성을 내려받을 수 있는
  build-time 외부 통신
- 운영자가 `/var/run/docker.sock`을 사용할 수 있는 권한
- GPU 사용 시 x86_64 NVIDIA host, 호환 driver, NVIDIA Container Toolkit과 Docker `nvidia`
  runtime (CPU-only 배포에는 불필요)

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

플랫폼은 host firewall rule을 설치하지 않지만 Docker daemon의 bridge firewall/netfilter
관리는 필요하다. Docker daemon을 `iptables=false` 같은 firewall 비활성 설정으로 실행하지
않는다. 조직 표준 daemon 설정과 실제 packet 차단은 서버 담당자가 검토한다.

서버 주소와 3030 port도 확인한다.

```bash
ip -4 -o address show
ss -ltnp
```

`<INTERNAL_SERVER_IP>`가 서버에 실제로 설정되어 있어야 하며 첫 배포 전
`<INTERNAL_SERVER_IP>:3030`은 비어
있어야 한다.

## 3. 검토된 소스코드 배치

임의의 최신 `main` 대신 검토한 release tag 또는 commit을 사용한다. 배포 tag는
`compose.production.yaml`, `compose.production.docker27.yaml`, `scripts/production.sh`,
`scripts/validate_production_network.py`와 이 문서를 포함해야 한다. 아래
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

운영용 Compose는 `make production-*`/`scripts/production.sh`로만 조작한다. 스크립트가 Engine
28+에서 base `compose.production.yaml`만, Engine 27에서 base와
`compose.production.docker27.yaml`을 자동 선택한다. `compose.yaml`이나
`compose.domain-test.yaml`과 합치지 않고 Engine 27에서 base만 직접 실행하지 않는다.

## 4. DNS와 VIP 설정

HostingKR 권한 DNS에 다음 A record를 둔다.

| 이름 | 유형 | 값 | 필요 여부 |
| --- | --- | --- | --- |
| `@` 또는 빈 이름 | A | `<PUBLIC_VIP>` | 필수 |
| `*` | A | `<PUBLIC_VIP>` | 필수 |
| `platform` | A | `<PUBLIC_VIP>` | 선택 |

별도 `platform` A record는 없어도 된다. `platform` 이름에 다른 record가 전혀 없으면 root
wildcard가 `platform.cyberailabs.team`에도 응답한다. `platform` 이름에 record를 따로 둔다면
wildcard에 기대지 말고 그 이름도 같은 VIP로 해석되게 한다. 포털 URL은 record 유무와 무관하게
계속 `https://platform.cyberailabs.team`이다. Hub가 apex를 사용하므로 `hub` 또는 `*.hub`
record도 필요하지 않다. 임의 사용자 ID가 한 단계 wildcard에 매칭된다.

```bash
dig +short A cyberailabs.team
dig +short A platform.cyberailabs.team
dig +short A dns-check-user.cyberailabs.team
```

세 결과가 모두 `<PUBLIC_VIP>`여야 한다. 변경 직전에는 TTL을 낮추고, 검증이 끝난 뒤 조직
정책값으로 되돌린다.

네트워크 장비는 다음 계약으로 설정한다.

```text
<PUBLIC_VIP>:443/TCP  ->  <INTERNAL_SERVER_IP>:3030/TCP
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
  -d '*.cyberailabs.team'
```

Certbot이 멈추면 HostingKR의 **네임서버/DNS → 새 DNS 레코드 추가**에서 TXT를 선택하고
화면에 표시된 이름과 값을 정확히 등록한다. apex와 wildcard challenge 모두 보통 다음 이름을
사용한다.

```text
_acme-challenge.cyberailabs.team
```

apex와 wildcard가 서로 다른 TXT 값을 같은 `_acme-challenge` 이름에 요구할 수 있다. 이때
기존 값을 덮어쓰지 말고 두 TXT 값을 동시에 유지한다. HostingKR가 zone suffix를 자동으로
붙이는 화면이면 이름 칸에는 `_acme-challenge`만 입력한다.

```bash
dig +short TXT _acme-challenge.cyberailabs.team
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

SAN에는 다음 두 값이 모두 있어야 한다. `*.cyberailabs.team`이
`platform.cyberailabs.team`과 한 단계 사용자 hostname을 모두 보호하므로 portal SAN을 따로
요청하지 않는다.

```text
DNS:cyberailabs.team
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
PLATFORM_GATEWAY_BIND_IP=<INTERNAL_SERVER_IP>
PLATFORM_TLS_CERT_FILE=/etc/team-workspace/tls/fullchain.pem
PLATFORM_TLS_KEY_FILE=/etc/team-workspace/tls/privkey.pem
PLATFORM_INGRESS_CIDRS_FILE=/etc/team-workspace/ingress-cidrs.txt
PLATFORM_TLS_GID=REPLACE_WITH_TLS_FILE_GROUP_GID
PLATFORM_WORKSPACE_CPU_BUDGET_MILLICORES=8000
PLATFORM_WORKSPACE_MEMORY_BUDGET_MB=4096
PLATFORM_GPU_RUNTIME_CONFIG_FILE=disabled
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

`PLATFORM_GPU_RUNTIME_CONFIG_FILE=disabled`는 CPU-only 기본값이다. 이 상태에서는 GPU image를
빌드하거나 GPU profile을 노출하지 않으며, 일반 Python 3.12/3.13 kernel은 CUDA runtime이라고
간주하지 않는다. GPU를 사용할 때만 다음 절차를 완료한 뒤 이 값을 절대경로로 바꾼다.

`.env.production`에는 공백, shell 명령, 따옴표나 임의 확장을 넣을 수 없다. `KEY=value`의
제한된 형식만 production script가 허용한다.

### 8.1 선택: NVIDIA GPU runtime 활성화

현재 GPU 범위는 **x86_64 단일 host의 물리 NVIDIA GPU 풀에서 사용자가 선택한 1개 이상을
workspace 하나에 독점 할당**하는 계약이다. 제공하는 kernel은 Python 3.12.13의
`python312-cuda` 하나이며 PyTorch `2.7.1+cu126`/CUDA 12.6으로 고정된다. 일반 Python kernel을
선택하면 GPU가 보이지 않는다. GPU memory quota, MIG, MPS/time-slicing, 여러 workspace의 동일
GPU 공유, topology-aware 배치, 다중 host GPU scheduling과 사용자 CUDA extension compile용
`nvcc`는 지원 범위가 아니다.

Python 자체가 CUDA를 제공하는 것은 아니다. 이 플랫폼은 CPU single-user image와 별도인
CUDA image에 CUDA-enabled PyTorch wheel을 설치한다. Host에는 CUDA Toolkit 전체나 `nvcc`가
필수인 것이 아니라, GPU에 맞는 NVIDIA driver와 Docker에 장치를 주입하는 NVIDIA Container
Toolkit이 필요하다. 실제 driver가 CUDA 12.6 runtime 및 해당 GPU와 호환되는지는 버전 문자열만
믿지 않고 마지막 tensor probe로 확인한다.

먼저 장비와 OS가 GPU를 인식하는지 확인한다. `nvidia-smi: command not found`이면 플랫폼을
시작하기 전에 배포판/조직 표준 절차로 NVIDIA driver와 사용자 도구를 설치하고 reboot해야 한다.
서버 모델과 GPU 세대에 맞는 driver branch는 서버 담당자가 정하며 임의의 package 이름을 이
문서에서 고정하지 않는다.

```bash
uname -m
lspci -nn | grep -i nvidia
nvidia-smi --query-gpu=uuid,name,driver_version --format=csv,noheader,nounits
```

`uname -m`은 현재 image 계약에서 `x86_64`여야 한다. `nvidia-smi`가 성공한 뒤 NVIDIA 공식
저장소 절차로 Container Toolkit을 설치하고 Docker runtime을 구성한다. 다음 구성 명령은 Docker
daemon 설정을 바꾸고 restart는 실행 중 container에 영향을 줄 수 있으므로 최초 설치 또는 승인된
유지보수 창에서만 수행한다.

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
nvidia-ctk --version
docker info --format '{{json .Runtimes}}'
```

마지막 출력에는 `nvidia` runtime이 있어야 한다. Toolkit 설치 package/repository 명령은 OS별로
달라지므로 이 문서 끝의 NVIDIA 공식 설치 문서를 그대로 따른다.

플랫폼에 허용할 GPU UUID 1~64개와 실제 version을 Git 밖의 운영 파일에 고정한다. 아래 예제는
secret은 아니지만 scheduler 입력이므로 무결성이 중요하다. 저장소의 예제 파일을 root 소유
일반 파일로 복사하고 값을 직접 검토한다.

```bash
sudo install -d -m 0750 -o root -g "$(id -gn)" /etc/team-workspace
sudo install -m 0640 -o root -g "$(id -gn)" \
  infra/host/gpu-runtime.production.example.json \
  /etc/team-workspace/gpu-runtime.json
sudoedit /etc/team-workspace/gpu-runtime.json
```

형식은 다음과 같고 `gpu_uuids`에는 `nvidia-smi`가 출력한 `GPU-...` 물리 UUID를 중복 없이
넣는다. 입력 순서와 무관하게 플랫폼이 내부에서 사전순으로 canonicalize한다. index `0`, PCI
bus ID와 MIG UUID는 허용되지 않는다. 목록에 등록한 모든 GPU가 독점 할당 풀에 포함되므로
운영에 공개할 장치만 넣는다. version도 위 명령의 실제 출력과 정확히 같아야 한다.

```json
{
  "schema_version": 1,
  "nvidia_driver_version": "EXACT_DRIVER_VERSION",
  "nvidia_container_toolkit_version": "EXACT_TOOLKIT_VERSION",
  "gpu_uuids": [
    "GPU-11111111-1111-1111-1111-111111111111",
    "GPU-22222222-2222-2222-2222-222222222222"
  ]
}
```

형식과 canonical 순서를 확인할 때는 모든 UUID를 출력하는 옵션을 사용한다. 단일 GPU 호환 옵션인
`--print-device-id`는 목록이 여러 개면 의도적으로 실패한다.

```bash
python3 infra/host/check_gpu_runtime.py \
  --config /etc/team-workspace/gpu-runtime.json \
  --print-device-ids
```

`.env.production`에는 다음 절대경로만 넣는다. GPU UUID를 `.env.production`이나 Compose 파일에
직접 중복 기록하지 않는다.

```dotenv
PLATFORM_GPU_RUNTIME_CONFIG_FILE=/etc/team-workspace/gpu-runtime.json
```

이후 `make production-preflight`는 DB를 변경하기 전에 다음을 모두 확인한다.

- 설정 파일이 일반 non-symlink 파일이고 group/world writable이 아님
- 설정한 driver/toolkit version과 물리 GPU UUID 집합이 현재 host inventory와 정확히 일치
- Docker `nvidia` runtime 존재
- CPU image와 별도로 만든 CUDA image가 검토한 CPU image ID를 base로 사용
- 새 image뿐 아니라 재시작 가능하게 남은 모든 enabled GPU history image에서 각 allowlist
  GPU의 개별 probe와 전체 풀 동시 probe가 성공하고 PyTorch/CUDA/kernel 계약이 일치
- `torch.cuda.is_available()`, 정확한 device 수와 각 장치의 실제 CUDA tensor
  연산/synchronize 성공

CUDA image의 첫 build는 크고 `download.pytorch.org` 접근이 필요할 수 있다. 위 검증 중 하나라도
실패하면 GPU profile policy를 생성하거나 DB migration을 시작하지 않는다. 오류를 우회하려고
일반 Python profile에 `NVIDIA_VISIBLE_DEVICES`, `CUDA_VISIBLE_DEVICES` 같은 환경변수를 추가하면
안 되며 이 이름들은 사용자 환경변수에서 차단된다.

## 9. egress와 차단 사용자 정책 검토

사용자 container는 직접 인터넷에 연결되지 않고 Squid allowlist를 통한다. 다음 파일을
검토한다.

```text
infra/egress-proxy/approved-domains.production.txt
```

기본값은 PyPI, npm과 승인된 GitHub download 경로다. 사내망, metadata 주소, 임의 domain을
넣지 않는다. 변경은 code review 후 새 image build로 배포한다.

특정 내부 HTTP(S) 서비스가 필요한 경우 domain allowlist나 `.env`에 IP를 추가하지 않는다.
최초 `v0.1.6` 배포에서는 DB migration과 동적 정책용 volume/mount 생성 때문에 stack을
재생성해야 한다. 배포 후 관리자 포털의 **내부 서비스 통신** 메뉴에서 검토한 사설 IPv4
`/32`와 TCP port를
등록한다. 저장 직후 desired revision이 올라가고, 정상일 때 수 초 안에 applied revision이
같아져야 한다. 실패 상태에서는 이전 정상 정책이 유지되며 **적용 재시도**를 사용할 수 있다.

최초 `v0.1.7` 배포에서는 다중 GPU lease 및 CPU·메모리 관측 schema와 Hub 전용 endpoint가
함께 추가되므로 일부 서비스만 골라 재시작하지 말고 `production-up`으로 전체 stack을 같은
release로 재생성한다. 적용 후 관리자 현황의 실행 환경 수, 측정 성공·누락 합계와 환경별
CPU·메모리 사용량을 확인한다.

같은 host의 서비스를 대상으로 할 때 process가 `127.0.0.1`에만 listen하면 container의
loopback과 다른 주소이므로 접근할 수 없다. host LAN 주소 또는 보안 검토한 `0.0.0.0`에
listen시키고, 노트북 요청은 기존 `HTTP_PROXY/HTTPS_PROXY`를 그대로 사용한다. 대상 IP를
`NO_PROXY`에 넣으면 안 된다. 정책 삭제는 새 연결에 즉시 반영되지만 이미 열린 CONNECT/HTTP
연결 종료는 보장되지 않는다. 사고 대응에서 즉시 폐기가 필요하면 egress connection을 drain한
후 proxy를 재시작한다.

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

- 서버가 `<INTERNAL_SERVER_IP>`를 실제 보유함
- Docker Server Engine이 기능 호환 하한 27.1.2 이상임; 27.5.1 미만 경고를 검토함
- Engine 27이면 Docker Compose 2.24.4+와 compatibility overlay 렌더링이 유효함
- 인증서/개인키 일치, key mode/GID, 24시간 이상 유효, apex/wildcard 두 SAN
- 유효하고 비어 있지 않은 source CIDR allowlist
- 실행 중 single-user container와 local/domain-test stack 부재
- Platform/Hub DB volume이 둘 다 존재하거나 둘 다 존재하지 않음
- digest-pinned base image와 전체 Python/CPU/메모리 profile image 계약
- GPU 활성화 시 별도 CUDA/PyTorch image, 정확한 GPU UUID 풀·driver·toolkit과 개별/전체 풀 CUDA tensor 계약
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

1. Host·TLS·Engine/Compose·image/Compose 계약과 DB idle을 다시 사전 검사; GPU가 활성화된
   경우 exact UUID 집합을 주입한 CUDA tensor smoke도 반복
2. 기존 Gateway와 제어면 내부 service를 중지하고 DB idle을 다시 확인
3. 기존 DB가 있으면 Platform/Hub SQLite online backup 생성
4. Production profile policy를 exact single-user image ID에 결속하고 Alembic migration과
   profile import 실행
5. API, JupyterHub, reconciler, worker, frontend, egress proxy 등 내부 service를 먼저
   시작하고 health 대기
6. Live execution network의 exact option/label/IPAM/reserved endpoint, host bridge 주소 부재와
   non-root·no-capability·no-default-route probe를 검증
7. 6번이 통과한 뒤에만 Gateway를 publish/start하고 HTTPS health 확인

6번이 실패하면 Gateway는 시작되지 않아 잘못된 execution network 상태가 외부에
공개되지 않는다. Gateway 시작 후 HTTPS health가 실패하면 스크립트가 Gateway를
다시 중지한다.

새 설치면 DB volume 두 개를 새로 만든다. 기존 운영 DB 변경 뒤 실패하면 script가 출력한 backup
경로를 보존하고 무작정 `production-up`을 반복하지 않는다.

GPU profile import가 끝나면 관리자 **자원·커널 정책**에서 GPU를 활성화하고 전체 예산을
검증된 GPU 수 이하로 입력한다. 이어 사용자가 선택할 GPU 개수(예: 1, 2, 4)를 추가한다.
각 선택값은 전체 GPU 예산 이하여야 하며 CPU 전용 0개는 자동으로 유지된다. 실행 중 GPU
예약 합계보다 예산을 낮출 수 없다.

`production-up`은 Engine major version을 검사해 network 정의를 자동 선택한다. Engine 28+은
base Compose의 `internal=true`, IPv6 off, ICC on, IPv4/IPv6 isolated-gateway exact set을
사용한다. Engine 27은 compatibility overlay의 `!override`로 base `driver_opts` 전체를
ICC on과 `inhibit_ipv4=true` exact set으로 교체한다. Preflight와 live validator는 현재
Engine에 해당하는 exact set만 허용한다.

Base의 Engine 28 isolated 정의를 그대로 유지하므로 기존 28 운영 network은 config 차이
없이 계속 사용된다. 이는 Compose의 불필요한 immutable-network 재생성과 network ID
변경을 피한다. 실행 중 network의 option을 수동 수정하거나 network를 삭제하지 않는다.

27.1.2는 현재 지원·보안 권장 release가 아니라 기존 host의 기능 호환 하한이다. Preflight가
27.5.1 미만 경고를 출력해도 다른 계약이 모두 맞으면 진행할 수 있지만, 경고를 변경 기록에
남기고 가능한 즉시 27.5.1 이상 또는 조직이 승인한 현재 지원 release로 upgrade한다.

상태는 모든 장기 실행 service가 healthy여야 한다. worker의 health는 HTTP가 아니라 PID 1
operation-worker liveness를 뜻한다. `migrate`,
`bootstrap-profile`, `singleuser-image`는 exit code 0인 one-shot container다.

상세 로그는 다음으로 본다.

```bash
make production-logs
```

`production-logs`는 Engine 27 overlay를 자동 적용하고 최근 200줄을 follow한다. 종료할 때
`Ctrl-C`를 눌러도 service는 중지되지 않는다.

정적 Compose 렌더링과 container health만으로 실행망 격리를 판단하지 않는다. 실제 운영 host에서
live network와 bridge 주소를 확인한다.

```bash
docker network inspect platform-jupyter-compose-production \
  --format 'internal={{.Internal}} ipv6={{.EnableIPv6}} options={{json .Options}} labels={{json .Labels}} ipam={{json .IPAM.Config}}'
network_id="$(docker network inspect platform-jupyter-compose-production --format '{{.Id}}')"
bridge_name="br-${network_id:0:12}"
ip -4 -o address show dev "${bridge_name}"
ip -6 -o address show scope global dev "${bridge_name}"
```

두 `ip ... address` 명령은 아무 주소도 출력하지 않아야 한다. `Options`는 Engine 28+에서
`enable_icc=true + gateway_mode_ipv4/ipv6=isolated`, Engine 27에서
`enable_icc=true + inhibit_ipv4=true` exact set이어야 한다. 다른 option, IPv4
주소 또는 routable IPv6 주소가 보이면 공개하지 않는다.

## 12. 최초 관리자와 사용자 계정 생성

production에서는 웹 signup을 항상 닫는다. 계정 작업 전에 모든 workspace를 중지하고 운영
stack의 Gateway/API/Frontend/Egress proxy/Hub/Worker/Reconciler가 모두 실행 중이고 healthy인지
확인한다. 새 DB에서 관리자를 만들기 위해 공개 Gateway와 DB writer를 잠시 중지하고, 비밀번호를
argv·환경변수·로그에 남기지 않는 one-shot 도구를 사용한다.

```bash
make production-bootstrap-admin
```

프롬프트에서 `.env.production`의 `PLATFORM_ADMIN_USERNAME`용 비밀번호를 두 번 입력한다.
비밀번호는 12자 이상, common-password denylist 밖이어야 하며 UTF-8 72 byte를 넘지 않는다.
초기 관리자 bootstrap은 NativeAuthenticator 사용자 표가 비어 있을 때만 성공하고 기존 행을
덮어쓰지 않는다. 기존 계정에 다른 비밀번호를 넣어 재실행하면 비밀번호가 바뀌지 않았음을
명시하고 실패한다. 변경 전 Platform/Hub DB online backup을 만들며, 작업 후 control plane과
Gateway를 다시 healthy 상태로 시작한다.

기존 관리자 비밀번호를 모르면 bootstrap을 반복하지 않고 다음 전용 명령을 실행한다.

```bash
make production-reset-admin-password
```

명령은 `.env.production`의 정확한 관리자 ID만 대상으로 하며 새 비밀번호를 터미널에서 두 번
입력받는다. Gateway와 DB writer를 닫고 두 DB를 backup한 뒤, 승인 상태와 Hub의 admin bit가
모두 맞는 단일 계정의 password hash만 transaction으로 교체한다. 기존 장기 실행 container를
재생성하지 않고 같은 container ID로 복원하며, 완료 후 Hub가 재시작되므로 이전 로그인 실패
횟수에 따른 일시 잠금도 초기화된다. 작업 중에는 짧은 유지보수 중단이 발생한다.

재설정 완료 후 새 private browser 창에서 새 비밀번호로 로그인해 확인한다. 이 절차는 비밀번호
분실 복구용이며 기존 Hub cookie, OAuth/API token, 포털 session을 강제로 철회하지 않는다.
credential 유출이 의심되면 별도의 session/token 폐기 절차가 필요하다.

운영 변경기록에는 작업 시각, OS 운영자, 대상 username, Git commit, 출력된 backup 경로와
결과를 남기고 비밀번호나 password hash는 기록하지 않는다.

비밀번호 hash commit 뒤 runtime 복구만 실패했다는 메시지가 나오면 reset을 반복하지 않는다.
Gateway와 각 service의 현재 상태를 먼저 확인하고, health가 완전히 복구되지 않았다면 Gateway를
닫아 둔 채 원인을 확인한 뒤 `make production-preflight`와 `make production-up`으로 정상
배포·health를 복구한다.

일반 사용자는 운영자가 exact username을 지정해 사전 승인 계정으로 만든다.

```bash
make production-create-user USERNAME=alice
```

임시 비밀번호는 별도의 안전한 채널로 사용자에게 전달하고, 사용자는 첫 로그인 직후
포털의 **비밀번호 변경** 버튼으로 exact Hub
`https://cyberailabs.team/hub/change-password` self-service 화면을 열어 변경한다. 비밀번호
본문은 Platform API를 거치지 않는다. username은 소문자 영문으로
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
- 인증서의 apex/wildcard SAN과 portal/user hostname의 wildcard 적용 확인
- 등록하지 않은 nested hostname과 잘못된 Host/SNI 거부
- 회사/VPN 밖 source는 연결 거부
- Gateway 외 host published port 없음

### 13.2 인증과 관리자

- 관리자 로그인 후 관리자 전용 메뉴 표시
- 일반 사용자에게 관리자 메뉴/API가 보이지 않음
- 로그아웃 후 기존 계정으로 자동 재로그인되지 않고 ID/password를 다시 요구
- 로그인 완료 후 Hub의 exact `/hub/spawn` fallback이 포털 root로 `303` 응답하고 브라우저가
  `https://platform.cyberailabs.team/`에 도착
- 포털의 **비밀번호 변경** 버튼이 exact Hub `/hub/change-password` self-service 화면으로
  연결되고 비밀번호 본문은 Platform API 요청에 포함되지 않음
- 잘못된 비밀번호 반복 시 rate limit 확인
- 관리자 화면에서 생성 환경 수, 실제 실행 수, CPU/RAM 예약과 ceiling 확인
- GPU를 활성화한 경우 검증된 풀 크기 이하의 GPU 전체 예산, 사용자 선택 개수와 예약 합계 확인
- 관리자 자원·커널 정책에서 유휴 커널 자동 정리 on/off와 시간 저장

### 13.3 workspace

- 사용자 개인공간 준비 완료
- Python, CPU, 메모리와 허용된 accelerator 선택 후 중지 상태 workspace 생성
- 생성 후 일반/secret 환경변수 저장
- 환경변수 변경 시 restart 필요 안내
- 시작 후 JupyterLab, Notebook kernel, terminal과 WebSocket 동작
- GPU 환경에서 `python312-cuda`만 표시되고 `torch.__version__ == "2.7.1+cu126"`,
  `torch.version.cuda == "12.6"`, `torch.cuda.is_available() is True`, device 수가 선택한 GPU
  개수와 같으며 각 장치의 CUDA tensor 연산 성공; CPU 환경에서는 GPU가 보이지 않음
- 여러 GPU 환경을 동시에 시작해 UUID가 중복되지 않음을 확인하고, 남은 미예약 GPU보다 큰
  요청은 capacity 부족으로 거부되며 기존 환경을 정상 중지한 뒤에는 시작됨
- 테스트용 timeout 적용 후 busy kernel 유지, idle kernel 정리와 파일 보존 확인
- `/home/jovyan/work` private data 영속성
- `/home/jovyan/shared` 팀 공유 read/write와 재시작 후 보존
- 직접 사내망/인터넷 연결 차단 및 승인된 PyPI/Git만 proxy를 통해 성공
- host gateway·host 관리 주소·metadata·직접 DNS 연결 차단
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
make production-logs
df -h
df -i
docker system df
nvidia-smi
```

`nvidia-smi`는 GPU를 활성화한 host에서만 실행한다. GPU 사용률·memory는 관측 지표이며 현재
플랫폼이 사용자별 GPU memory quota를 강제한다는 의미는 아니다.

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
make production-recreate-gateway
curl --fail --silent --show-error https://platform.cyberailabs.team/healthz
```

장기적으로는 등록업체를 이전하지 않고도 `_acme-challenge`를 DNS API가 있는 validation
zone으로 CNAME/NS 위임해 갱신을 자동화할 수 있다. DNS API key는 전체 zone 권한이 아닌 최소
권한을 사용한다.

## 16. 애플리케이션 업그레이드

운영 서버에서 Git release를 받은 뒤 실행할 명령과 판정 기준은
[운영 서버 Git 업데이트 절차](production-update-after-git-ko.md)를 따른다.
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

GPU 기능이 포함된 release로 기존 CPU-only DB를 올릴 때 schema-v2 CPU profile history는 원래
digest와 image ID를 유지한 채 재시작 전용으로 남고, 새 schema-v3 CPU/GPU runtime이 추가된다.
GPU 설정 파일이 `disabled`이면 CPU profile만 생성되므로 GPU 설치는 upgrade의 필수 조건이
아니다. 반대로 GPU를 한 번 활성화해 enabled GPU history가 생긴 뒤에는 설정 파일을 단순히
`disabled`로 바꾸면 preflight가 실패하는 것이 정상이다. 기존 GPU workspace의 재시작 계약을
조용히 제거하지 않기 위한 보호이므로, GPU 폐기는 workspace와 profile history를 함께 다루는
검토된 별도 release/이전 절차로 진행하고 runtime policy 파일을 직접 편집하지 않는다.

DB migration과 schema-v3 profile promotion 뒤 구버전 code만 checkout하는 것은 안전한 rollback이
아니다. 되돌려야 한다면 upgrade 직전의 검증된 Platform/Hub DB backup과 같은 세대의 profile
policy를 함께 복원한다.

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
| `/etc/team-workspace/gpu-runtime.json` | 독점 풀의 물리 GPU UUID 집합과 검증할 driver/toolkit 계약(GPU 사용 시) |
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
| `this host does not own <INTERNAL_SERVER_IP>` | NIC 주소와 배포 대상 서버 확인 |
| TLS file이 symlink라 거부됨 | Certbot `live`를 직접 지정하지 말고 전용 regular file로 복사 |
| `PLATFORM_TLS_GID does not match` | key의 숫자 GID와 `.env.production` 비교 |
| 외부에서 444/timeout | source/SNAT CIDR, VIP ACL, ingress allowlist 확인 |
| 인증서 SAN 누락 | apex와 wildcard 두 SAN으로 재발급 |
| Docker/Compose version 거부 | Engine 27.1.2 미만은 차단; Engine 27은 Compose 2.24.4+ 확인; 27.5.1 미만 경고와 upgrade 계획 검토 |
| execution network option drift | Engine 28+ isolated / Engine 27 overlay 선택 확인; 수동 주석·삭제 금지, 모든 workspace를 정상 중지한 점검 창에서만 복구 판단 |
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
- [ ] Docker Server Engine 27.1.2+, Engine 27이면 Compose 2.24.4+, Docker firewall 관리 활성 확인
- [ ] Engine 27.5.1 미만 경고를 기록하고 최신 보안 patch로 올릴 일정 확인
- [ ] 서버가 `<INTERNAL_SERVER_IP>`를 보유하고 3030 충돌 없음
- [ ] DNS apex와 root wildcard를 통한 portal/random hostname이 VIP를 반환
- [ ] VIP 443→host 3030 L4 전달과 source IP/SNAT 계약 확인
- [ ] 인증서 apex/wildcard SAN 두 개, key mode/GID, 만료 경보 확인
- [ ] 회사/VPN CIDR allowlist와 상위 ACL 확인
- [ ] live execution network option, host bridge IPv4/routable IPv6 주소 부재와 Docker restart 후 연결 차단 확인
- [ ] CPU/RAM ceiling에 control-plane/OS 여유 포함
- [ ] GPU 사용 시 driver/toolkit/runtime, 외부 관리 GPU UUID 풀과 개별/전체 풀 tensor probe 확인
- [ ] GPU memory quota·MIG·공유 할당이 제공되지 않는 제한을 운영자와 사용자에게 공지
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
- [NVIDIA Container Toolkit 설치와 Docker runtime 구성](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [NVIDIA Container의 GPU 열거와 driver capability](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html)
- [NVIDIA CUDA minor-version compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
- [PyTorch CUDA 설치·검증](https://pytorch.org/get-started/locally/)
- [Let's Encrypt DNS-01 및 wildcard challenge](https://letsencrypt.org/ca/docs/challenge-types/)
- [Certbot manual DNS와 갱신 제한](https://eff-certbot.readthedocs.io/en/stable/using.html#manual)
- [HostingKR TXT 레코드 등록](https://help.hosting.kr/hc/ko/articles/5696985768217-TXT%EB%A0%88%EC%BD%94%EB%93%9C-%EB%93%B1%EB%A1%9D%ED%95%98%EA%B8%B0)
