# Gateway modes

`dev.conf`/`Dockerfile`은 기존 loopback HTTP 개발 모드다. 아래 두 HTTPS 모드는
`Dockerfile.production`을 사용하지만 설정과 배포 권한은 분리한다.

## Domain test

`compose.domain-test.yaml`은 운영 DNS 이름, TLS SNI, Secure cookie, OAuth callback과
Jupyter WebSocket을 배포 전에 한 호스트에서 연습하기 위한 **로컬 전용** overlay다.
운영 URL과 동일하게 만들기 위해 오직 `127.0.0.1:443 -> gateway:3030`만 허용한다.
443이 이미 사용 중이면 서비스를 우회 포트로 띄우지 말고 점유 프로세스를 확인한다.

중요: `platform.workspace.test`과 `*.hub.workspace.test`은 같은 registrable domain에
속한다. 따라서 이 모드는 기능 검증만 허용하며
[ADR-0004](../docs/adr/0004-single-host-compose.md)의 portal/user-content 분리 출시 gate를
충족하지 않는다. 실제 운영은 별도 user-content 도메인을 확보하거나 해당 ADR을 변경하는
명시적 위험 승인 전까지 **BLOCK**이다.

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
local stack을 명시적으로 재생성한다. `jupyter`는 이제 Compose 소유 internal/isolated
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

`production.conf`는 문서용 예시 backend `192.0.2.10:3030`에서 TLS를 종료하고 예시 VIP
`198.51.100.10:443`이 DNAT-only로 전달한다는 계약이다. 두 주소는 실제 배포에 사용할
수 없는 RFC 문서용 예약 주소이므로 반드시 조직의 값으로 교체한다. upstream에는 외부 Host,
`https`, port `443`, 그리고 새로 만든 client IP header만 전달한다. Hub upload 한도는
2 GiB이며 body를 disk에 모두 buffering하지 않고 WebSocket/장기 연결 timeout은 1시간이다.

실제 운영 Compose는 아직 제공하지 않는다. 현재 루트 `compose.yaml`은 mutable image,
local profile과 unsafe health 예외를 포함하므로 여기에 ingress overlay만 얹어 외부로
공개해서는 안 된다. 완전한 production base, Nginx 회사/VPN CIDR allowlist와 VIP ACL, 운영
profile/quota/health/secrets, 인증서 자동 갱신 및 rollback gate가 먼저 필요하다.

완전한 production base가 만들어질 때 Gateway service가 만족해야 할 핵심 형태는
다음과 같다. 아래 조각만 현재 local Compose에 합쳐 실행하면 안 된다.

```yaml
build:
  context: ./gateway
  dockerfile: Dockerfile.production
  args:
    GATEWAY_BASE_IMAGE: nginx@sha256:<reviewed-64-hex-digest>
ports:
  - 192.0.2.10:3030:3030
environment:
  PLATFORM_GATEWAY_MODE: production
volumes:
  - type: bind
    source: <absolute-fullchain-path>
    target: /run/platform-tls/tls.crt
    read_only: true
    bind: {create_host_path: false}
  - type: bind
    source: <absolute-private-key-path>
    target: /run/platform-tls/tls.key
    read_only: true
    bind: {create_host_path: false}
  - type: bind
    source: <absolute-reviewed-cidr-file>
    target: /run/platform-ingress/company-vpn-cidrs.txt
    read_only: true
    bind: {create_host_path: false}
```

API의 `FORWARDED_ALLOW_IPS`에는 production edge network에서 Gateway에 배정한 단 하나의
고정 IP를 넣는다. 예시 VIP 흐름은 `198.51.100.10:443 -> 192.0.2.10:3030`이며 TLS
passthrough/DNAT-only로 전달하고 SNAT하지 않아야 `$remote_addr` 기반 회사/VPN
allowlist와 감사 IP가 일치한다.

운영 시작 시에는 별도 read-only 파일
`/run/platform-ingress/company-vpn-cidrs.txt`도 필수다. 주석/빈 줄 외에는 검토한 IPv4
CIDR만 한 줄에 하나씩 허용한다. 시작 스크립트가 이를 `allow` 목록과 마지막
기본 거부인 Nginx source-IP map으로 변환하므로 rewrite 단계에서 즉시 반환하는 경로도
우회할 수 없다. 파일 누락, 빈 목록, 잘못된 값과 `0.0.0.0/0`은 Gateway 시작 실패가 된다.
이 애플리케이션 방어와 별개로 VIP/상위 네트워크 ACL에도 같은 allowlist를 적용한다.
플랫폼 배포 절차가 운영 host firewall 규칙을 설치하거나 변경하지는 않는다.

운영 Gateway image build에서는 `GATEWAY_BASE_IMAGE`를 반드시
`repository@sha256:<64 lowercase hex>`로 전달한다. 시작 시 leaf SAN 세 개, 유효기간,
개인키 mode, certificate/key 일치와 `nginx -t`를 검증하며 하나라도 틀리면 시작하지 않는다.
빌드한 image에는 선택된 server config mode가 별도 marker로 고정된다. 런타임
`PLATFORM_GATEWAY_MODE`가 이 값과 정확히 다르면 시작을 거부하며, mutable base image 예외는
`domain-test.conf`를 선택한 로컬 시험 build에서만 허용된다.
