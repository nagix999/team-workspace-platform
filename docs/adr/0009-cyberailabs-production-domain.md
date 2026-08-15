# ADR-0009: cyberailabs.team 단일 호스트 운영 도메인

- 상태: Accepted (2026-08-15)
- 결정 범위: 공개 DNS, TLS 종료, 브라우저 origin, 운영 Compose

## 맥락

조직은 HostingKR에서 `cyberailabs.team`을 구매했고, 운영 VIP
`123.214.65.254:443`을 단일 production host `10.155.1.24:3030`으로 Static NAT한다.
HostingKR UI는 중첩 wildcard `*.hub` 이름을 직접 받지 않으며, DNS 사업자 이전과 사용자별
record 등록은 원하지 않는다. 팀원은 상호 신뢰하지만 Notebook과 설치 패키지는 비신뢰 코드로
취급한다. 플랫폼은 운영 host 방화벽을 변경하지 않는다.

## 결정

Hub를 도메인 apex에 배치해 HostingKR의 한 단계 wildcard만 사용한다.

- `platform.cyberailabs.team`: Portal/API
- `cyberailabs.team`: JupyterHub
- `<username>.cyberailabs.team`: single-user Jupyter server

권한 DNS의 apex, `platform`, `*` A record는 모두 VIP를 가리킨다. 인증서는 apex, portal,
wildcard 세 SAN을 포함하며 Gateway Nginx가 production host에서 TLS를 종료한다. VIP는 HTTP를
해석하지 않는 TCP pass-through/DNAT-only 구성이고 외부 443을 내부 3030으로 전달한다.
Gateway 이외 service는 host port를 publish하지 않는다.

운영 Gateway는 세 hostname, SNI와 Host 일치, TLS key/SAN/유효기간, 회사/VPN source CIDR을
fail-closed로 확인한다. API는 고정 Gateway container IP 하나의 forwarded header만 신뢰한다.
사용자 서버는 per-user subdomain과 `__Host-` cookie를 사용하며 portal mutation은 exact Origin,
CSRF header와 session을 모두 요구한다.

Portal과 user content가 같은 registrable domain이라는 위험을 명시적으로 수용한다.
`PLATFORM_ALLOW_SAME_SITE_USER_CONTENT=true`는 이 결정에 한해서만 설정한다. 이는 서로 다른
registrable domain보다 방어 심도가 낮으며, 임의 Notebook HTML/JavaScript나 dependency
supply-chain 공격을 신뢰한다는 뜻이 아니다. 향후 비신뢰 사용자, 외부 고객 또는 민감 데이터가
들어오면 portal을 별도 registrable domain으로 이전하는 것이 release gate다.

프로필 image는 registry digest 또는 production host의 exact Docker image ID만 허용한다.
후자는 한 호스트에서 build한 image를 content-addressed ID로 실행하고 policy generation 시
모든 profile digest에 결속한다. 새 image version을 배포해도 이전 enabled/non-selectable
profile과 image ID를 보존해 기존 workspace restart를 유지한다.

`docker-volume-unlimited-v1` production mode에서는 별도의 명시적 capability flag가 있을 때만
웹 volume provisioning agent를 허용한다. Agent는 JupyterHub 내부 managed service로만 실행하고
API/worker/user container에는 Docker socket을 주지 않는다. HMAC으로 결속된 user UUID/username,
고정 5-slot 이름, label, mount inventory만 생성하며 임의 image/path/option을 받지 않는다.

## 결과와 제약

- HostingKR를 이전하거나 사용자별 DNS record를 만들지 않아도 된다.
- 루트 도메인은 Hub 전용이므로 같은 apex에 별도 홍보 사이트를 둘 수 없다.
- source CIDR 통제는 NAT가 원본 source IP를 보존할 때 가장 정확하다. SNAT이면 네트워크
  담당자가 제공한 exact SNAT CIDR을 사용한다.
- wildcard 인증서 DNS-01 갱신 자동화가 없으면 수동 갱신이 운영 위험이 된다.
- 단일 host, SQLite, Docker socket blast radius와 신뢰 팀 east-west network 위험은 유지된다.

## 출시 gate

- 권한 DNS의 apex/portal/random wildcard A가 VIP와 일치한다.
- 인증서 SAN, key, 유효기간과 Gateway Host/SNI 거부 시험이 통과한다.
- VIP 443→host 3030 WebSocket, OAuth callback, 2 GiB upload 경로가 통과한다.
- Gateway 외 published port가 없고 회사/VPN 밖 source가 거부된다.
- backup→migration/profile import→health와 실제 restore 훈련이 통과한다.
- 사용자 두 명의 origin/cookie 분리, private/shared volume 및 cross-user 접근 경계를 재검증한다.
