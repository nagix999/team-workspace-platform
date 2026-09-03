# ADR-0006: 신뢰 팀용 Compose 네트워크와 무제한 영속 저장공간

- 상태: Accepted (2026-08-11)
- 날짜: 2026-08-11
- 결정 범위: 단일 호스트 사용자 실행망, private/shared volume, 디스크 제한
- 대체 범위: ADR-0004의 host `DOCKER-USER` 정책과 사용자별·공유 hard quota 결정

## 맥락

서비스는 약 10명의 상호 신뢰하는 부서 팀원이 사용한다. 사용자별 코드와 데이터는
별도 volume으로 분리하지만, 팀원이 명시적으로 자료를 공유할 공동 디렉터리도 필요하다.
운영자는 플랫폼 배포가 host `iptables`/`nftables` 규칙을 설치·교체하거나 주기적으로
재적용하는 방식을 원하지 않는다. 사용자별 또는 공유 디스크 hard quota도 이번 운영
범위에서는 요구하지 않는다.

CPU, memory, PID와 전체 동시 실행량 제한은 유지한다. 사용자 컨테이너의 직접 외부망 및
사내망 접근은 계속 금지하고, 승인된 package/Git 목적지만 egress proxy를 통해 허용한다.

## 결정

### 실행 네트워크

`jupyter` 실행망은 Compose가 소유하는 전용 bridge로 만든다.

- `internal=true`
- IPv6 endpoint 비활성화
- inter-container communication(ICC)은 활성화
- Docker Engine 28 이상: IPv4/IPv6 `gateway_mode=isolated`로 host-side gateway 주소 할당 금지
- Docker Engine 27: `com.docker.network.bridge.inhibit_ipv4=true`로 host-side IPv4 주소 할당 금지
- JupyterHub, single-user container와 dual-homed egress proxy만 연결
- 사용자 container에는 port publish, host network, `host-gateway`, Docker socket을 허용하지 않음

Base `compose.production.yaml`은 Engine 28 이상의 exact isolated-gateway option set을
유지한다. Engine 27에서는 운영 스크립트가
`compose.production.docker27.yaml`을 자동 추가하고 Compose `!override`로 base `driver_opts`
전체를 ICC on과 `inhibit_ipv4=true` exact set으로 교체한다. 따라서 Engine 27은
`!override`를 지원하는 Docker Compose 2.24.4 이상을 요구하며, base 파일만 직접
실행하는 절차를 허용하지 않는다.

Base의 Engine 28 정의를 변경하지 않으면 기존 28 운영 network에 config 차이가
생기지 않아 Compose의 불필요한 network 재생성과 network ID 교체를 피할 수 있다.
Docker network option은 immutable이므로 실행 중 endpoint가 있는 network를 수동
삭제하거나 다른 major version의 option set으로 교체하지 않는다. 두 버전 모두
`internal=true`, IPv6 off와 해당 Engine의 exact option set이 유지될 때만 승인한다.

27.1.2는 기존 운영 host를 위한 기능 호환 하한이지 현재 지원 또는 보안 권장 version이라는
뜻이 아니다. 27.5.1 미만에서는 명확히 경고하고, 가능하면 27.5.1 이상 또는 조직이 승인한 현재
지원 release를 사용한다.

플랫폼의 정상 설치·시작·domain-test 절차는 host firewall을 읽거나 변경하는
`apply_jupyter_firewall.py`, `firewall-apply` 또는 동등한 명령을 호출하지 않는다. 기존
host-firewall 도구는 이전 강화 모드의 참고·수동 선택지로만 보존한다.

Docker Engine은 bridge network를 구현하기 위해 host의 netfilter 규칙을 자체 관리할 수
있다. 이 결정은 Docker daemon의 정상 동작까지 금지한다는 뜻이 아니라, 플랫폼이
`DOCKER-USER` 등에 자체 규칙을 설치하거나 그 규칙에 가용성을 의존하지 않는다는 뜻이다.
Docker daemon의 firewall/netfilter rule 관리는 끄지 않으며, 정적 Compose 확인만으로
격리를 증명하지 않고 실제 host의 bridge 주소와 연결 실패를 출시 gate에서 확인한다.

ICC를 켜므로 같은 실행망의 사용자 container는 서로의 열린 TCP/UDP port에 접근할 수
있다. private volume은 다른 container에 mount되지 않아 파일시스템 namespace 격리는
유지되지만, 사용자 간 네트워크 강격리는 제공하지 않는다. 이는 상호 신뢰 팀이라는 현재
전제에서 명시적으로 수용한다. 비신뢰 사용자, 민감 workload 또는 사용자 간 네트워크
격리가 필요해지면 workspace별 network/sidecar를 새로 설계하기보다 기존 Kubernetes의
KubeSpawner와 NetworkPolicy로 전환한다.

### private와 shared 저장공간

workspace마다 기존과 같이 정확히 하나의 owner-bound private named volume만
`/home/jovyan/work`에 read-write mount한다. 다른 사용자의 private volume은 container
mount namespace에 포함하지 않는다.

팀 shared named volume은 모든 workspace에 `/home/jovyan/shared`로 read-write mount한다.
root와 고정 shared GID가 소유하고 setgid/group-write 권한을 유지하며, single-user process는
해당 supplemental group과 협업용 `umask 0002`로 실행한다. 시작 전 mount root의 type,
ownership, mode와 실제 create/delete 가능 여부를 검증한다. JupyterLab에서는 private와
shared가 모두 보이는 공통 안전 root를 사용한다.

shared는 모든 팀원이 읽고 수정하고 삭제할 수 있는 공동 신뢰 영역이다. secret, 개인
데이터, 자동 실행되는 startup/config 파일을 두지 않는다. 중요 자료는 별도 version control
또는 backup으로 보호한다.

### 디스크 제한

private와 shared volume에 사용자별 hard quota를 적용하지 않는다. API는 quota 미적용
profile의 디스크 제한을 `null`로 반환하고 UI는 `개별 하드 제한 없음 (호스트 가용량 공유)`로
표시한다. 사용자가 선택하는 저장공간 facet은 제공하지 않는다.

기존 DB row, volume slot, manifest와 profile digest의 호환성을 위해 내부
`private_disk_hard_limit_bytes`/`hard_limit_mb` 값은 당분간 유지할 수 있다. 단,
`private_disk_quota_enforced=false`일 때 그 값은 사용자에게 제한으로 노출하거나 실제
quota라고 판단하지 않는다. 새로운 profile tuple은 이 enforcement fact를 digest에 결속한다.

## 결과와 위험

장점:

- 정상 배포와 Docker 재시작에 host firewall용 sudo 작업이 필요하지 않는다.
- JupyterHub↔single-user와 single-user↔egress proxy 연결이 Docker network 계약만으로
  동작한다.
- private 자료와 명시적 공동 자료의 저장 위치가 분명하다.
- 사용자가 임의의 profile 디스크 크기를 선택하지 않아 volume inventory가 단순해진다.

수용한 위험:

- 악성·취약 package를 포함한 사용자 코드가 같은 execution bridge의 다른 사용자 서비스와
  Hub/egress proxy의 열린 port를 탐색하거나 공격할 수 있다.
- 한 사용자 또는 작업이 host filesystem을 가득 채워 다른 사용자와 control plane에 영향을
  줄 수 있다.
- shared 자료는 어떤 팀원도 변경·삭제할 수 있고 사용자별 파일 소유권을 강하게 구분하지
  않는다.

따라서 host filesystem 여유 공간·inode 모니터링, 임계치 경보, control-plane용 별도
filesystem 또는 예약 공간, shared/private backup과 복구 시험은 운영 필수다. hard quota가
없다는 선택은 disk 관측과 backup이 불필요하다는 의미가 아니다.

## 출시 gate

- 정상 `up`, domain-test 전환과 Docker 재시작 절차가 sudo/firewall 명령을 호출하지 않는다.
- 실행망 inspect 결과가 internal, IPv6 off, 예상 label/subnet과 Engine 28+ base의
  `enable_icc+IPv4/IPv6 isolated` exact set 또는 Engine 27 overlay의
  `enable_icc+inhibit_ipv4` exact set에 일치하지 않으면 spawn을 거부한다.
- 실제 host bridge에 IPv4나 routable IPv6 주소가 없고, Docker restart 뒤에도 이 조건과
  single-user의 host·사내망·direct Internet 차단이 유지된다.
- single-user에서 host·사내망·direct Internet 연결은 실패하고 승인된 proxy 목적지는
  성공한다.
- 두 사용자 container가 서로의 network port에는 접근할 수 있다는 잔여 위험을 운영자가
  인지하고 승인한다.
- 각 workspace에는 owner-bound private volume 하나와 shared volume 하나만 mount된다.
- JupyterLab file browser와 terminal에서 `/home/jovyan/shared`를 읽고 쓸 수 있고, 새 파일은
  shared group write 권한을 유지한다.
- API/UI가 quota 미적용 숫자를 제한처럼 노출하지 않으며 저장공간 크기를 선택받지 않는다.
- host disk/inode 임계치 경보와 backup/restore가 준비되기 전에는 운영 공개하지 않는다.

## 재검토 조건

- 팀 밖 사용자 또는 상호 불신 사용자가 들어옴
- 사용자 간 service/network 접근을 차단해야 함
- 코드·데이터 반출 통제가 package allowlist보다 강해야 함
- disk 고갈 사고가 발생하거나 사용자별 비용·용량 회계가 필요함
- 여러 host, GPU, HA 또는 더 세밀한 resource/network policy가 필요함
