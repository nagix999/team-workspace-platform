# Gateway modes

`dev.conf`/`Dockerfile`은 기존 loopback HTTP 개발 모드다. 아래 두 HTTPS 모드는
`Dockerfile.production`을 사용하지만 설정과 배포 권한은 분리한다.

## Domain test

`compose.domain-test.yaml`은 운영 DNS 이름, TLS SNI, Secure cookie, OAuth callback과
Jupyter WebSocket을 배포 전에 한 호스트에서 연습하기 위한 **로컬 전용** overlay다.
운영 URL과 동일하게 만들기 위해 오직 `127.0.0.1:443 -> gateway:3030`만 허용한다.
443이 이미 사용 중이면 서비스를 우회 포트로 띄우지 말고 점유 프로세스를 확인한다.

중요: `platform.workspace.test`과 `*.hub.workspace.test`은 같은 registrable domain에
속한다. 따라서 이 모드는 기능 검증만 허용한다. 실제 `cyberailabs.team` 운영도 같은
registrable domain을 사용하지만, 그 범위와 보완 통제는
[ADR-0009](../docs/adr/0009-cyberailabs-production-domain.md)에서 명시적으로 승인했다.

인증서는 격리된 테스트 브라우저/호스트가 신뢰하는 로컬 CA가 서명해야 하며 leaf SAN은
정확히 다음 세 항목을 포함해야 한다.

```text
platform.workspace.test
hub.workspace.test
*.hub.workspace.test
```

`/etc/hosts`는 wildcard를 지원하지 않는다. 테스트할 승인 사용자 이름을 모두 exact
entry로 넣거나, 테스트 호스트에서만 `*.hub.workspace.test -> 127.0.0.1`을 응답하는
로컬 DNS resolver를 사용한다. 공용 DNS를 바꾸지 않는다. 브라우저의 Secure DNS/DoH가
OS resolver를 우회하지 않도록 격리된 테스트 profile에서 끄고, 시험 전 모든 exact host가
오직 loopback으로 해석되는지 사전검사한다. 예를 들어 `alice`, `bob`을 시험한다면 다음과
같이 실행한다.

```bash
python3 gateway/check_domain_test.py \
  --user alice --user bob \
  --certificate /absolute/path/to/fullchain.pem \
  --check-port-free
```

검사가 안내하는 exact host line을 관리자에게 검토받아 반영한다. 개인키는 world-readable이
아니어야 하며 `0400`, `0440`, `0600`, `0640` 중 하나로 둔다. Gateway는 인증서와 키를
항상 read-only bind로만 받는다.

```bash
export DOMAIN_TEST_TLS_CERT_FILE=/absolute/path/to/fullchain.pem
export DOMAIN_TEST_TLS_KEY_FILE=/absolute/path/to/privkey.pem
export DOMAIN_TEST_CA_CERT_FILE=/absolute/path/to/local-ca.pem
export DOMAIN_TEST_TLS_GID="$(stat -c '%g' "$DOMAIN_TEST_TLS_KEY_FILE")"

make domain-test-preflight USERS=alice,bob
make domain-test-up USERS=alice,bob
```

이는 기존 local project와 volume을 재사용하면서 gateway/API/Hub를 domain-test 계약으로
재생성한다. 고정 Gateway IP를 위해 local edge network를 `172.28.0.0/24`로 다시 만들므로
기존 stack을 먼저 `down`해야 한다. named DB/user volume은 보존되고 Compose 소유 Jupyter
network는 재생성된다. 실행 중인 workspace를 모두 중지하고 점검 창에서만 전환한다. 끝나면
전환기를 통해 domain-test network/container를 먼저 내리고 domain-test overlay 없이 기존
local stack을 명시적으로 재생성한다. `jupyter`는 이제 Compose 소유 internal/no-host-address
bridge이며 두 전환은 host firewall을 읽거나 변경하지 않고 sudo도 요청하지 않는다.
`down -v`는 사용하지 않으며 raw Compose 명령으로 idle gate를 우회하거나 두 모드를 동시에
실행하지 않는다.

```bash
make domain-test-down
```

최소 브라우저 시험은 portal login/OAuth callback, 사용자별 launch URL, Notebook
WebSocket/terminal, 큰 파일 streaming, 알 수 없는 Host의 421/연결 종료, 알 수 없는 SNI의
TLS handshake 거절이다.

## Production TLS gateway

내부 서버에 코드를 배치한 이후의 DNS/VIP, 공인 인증서, secret, 최초 계정, 검증·갱신과
복구 순서는 [운영 서버 전체 배포 가이드](../docs/operations/production-deployment-ko.md)를
따른다.

`production.conf`는 환경변수로 받은 exact host 세 개를 시작 시 렌더링한다. 이 저장소의
production stack은 Engine 28+ base Compose와 Engine 27 자동 compatibility overlay 모두에서
다음 Gateway 계약으로 고정돼 있다.

- `platform.cyberailabs.team`: Portal/API
- `cyberailabs.team`: JupyterHub
- `*.cyberailabs.team`: 사용자 서버
- VIP `123.214.65.254:443` → production host `10.155.1.24:3030` TCP 전달

upstream에는 외부 Host, `https`, port `443`, 검증된 client IP만 전달한다. Hub upload 한도는
2 GiB이며 body를 disk에 모두 buffering하지 않고 WebSocket/장기 연결 timeout은 1시간이다.
운영 구성은 `compose.yaml` 또는 `compose.domain-test.yaml`과 합치지 않는 독립 stack이다.

```bash
make production-init
# .env.production의 TLS/CIDR 경로와 GID를 검토
make production-preflight
make production-up
```

`production-up`은 내부 service를 먼저 시작한 뒤 live execution-network/host bridge와
hardened non-root probe를 검증한다. 이 검증이 통과한 뒤에만 Gateway를 publish/start하고
서버 내부 HTTPS health를 확인하며, 실패한 Gateway는 다시 중지한다.

API의 `FORWARDED_ALLOW_IPS`에는 production edge network의 Gateway 고정 IP
`172.38.0.10` 하나만 들어간다. VIP는 TLS passthrough/DNAT-only로 전달하고 가능하면
SNAT하지 않아야 `$remote_addr` 기반 회사/VPN allowlist와 감사 IP가 일치한다.

운영 시작 시에는 별도 read-only 파일
`/run/platform-ingress/company-vpn-cidrs.txt`도 필수다. 주석/빈 줄 외에는 검토한 IPv4
CIDR만 한 줄에 하나씩 허용한다. 시작 스크립트가 이를 `allow` 목록과 마지막
기본 거부인 Nginx source-IP map으로 변환하므로 rewrite 단계에서 즉시 반환하는 경로도
우회할 수 없다. 파일 누락, 빈 목록, 잘못된 값과 `0.0.0.0/0`은 Gateway 시작 실패가 된다.
이 애플리케이션 방어와 별개로 VIP/상위 네트워크 ACL에도 같은 allowlist를 적용한다.
플랫폼 배포 절차가 운영 host firewall 규칙을 설치하거나 변경하지는 않는다.
Production 스크립트는 Engine 28 이상에서 base Compose의 ICC on과 IPv4/IPv6
isolated-gateway exact set을 사용하고, Engine 27에서 `compose.production.docker27.yaml`을
자동으로 덧대어 ICC on과 `inhibit_ipv4=true` exact set으로 교체한다. 두 경우 모두
`internal=true`와 IPv6 off가 필수이며 Docker daemon이 자체 bridge firewall/netfilter rule을
정상 관리해야 하며,
플랫폼이 별도 `DOCKER-USER` rule을 설치하지 않는다는 것과 Docker firewall을 끄는 것은
다른 의미다. Production 기능 호환 하한은 Engine 27.1.2지만 이는 현재 지원되는 보안 release를
뜻하지 않는다. Engine 27 overlay는 Docker Compose 2.24.4 이상을 요구하며, Engine은
27.5.1 이상 또는 조직이 승인한 현재 지원 release를 권장한다.

운영 Gateway image build에서는 `GATEWAY_BASE_IMAGE`를 반드시
`repository@sha256:<64 lowercase hex>`로 전달한다. 시작 시 production leaf의 apex/wildcard
SAN 두 개, 유효기간,
개인키 mode, certificate/key 일치와 `nginx -t`를 검증하며 하나라도 틀리면 시작하지 않는다.
빌드한 image에는 선택된 server config mode가 별도 marker로 고정된다. 런타임
`PLATFORM_GATEWAY_MODE`가 이 값과 정확히 다르면 시작을 거부하며, mutable base image 예외는
`domain-test.conf`를 선택한 로컬 시험 build에서만 허용된다.
