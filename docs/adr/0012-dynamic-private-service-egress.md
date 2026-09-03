# ADR-0012: 관리자 제어형 내부 서비스 egress 예외

- 상태: 승인
- 기준 버전: 0.1.6

## 배경

single-user container는 host와 사내망으로 직접 route하지 못하고 승인된 외부 목적지만 Squid를
통해 사용한다. 일부 notebook은 같은 운영 host 또는 사내의 특정 HTTP(S) 응용 서비스와
통신해야 하지만, 사설망 전체를 열거나 code repository의 domain allowlist에 운영 IP를 넣는
것은 기존 격리 목적과 공개 저장소의 정보 비공개 원칙을 깨뜨린다.

## 결정

관리자만 포털의 **내부 서비스 통신** 메뉴에서 `(canonical RFC1918 IPv4 /32, TCP port)`를
최대 32개까지 추가·수정·삭제한다. `/24` 같은 넓은 CIDR, 비정규 IP/port, privileged port와
Docker/Kubernetes/Squid control port는 API와 proxy에서 각각 다시 거부한다. metadata,
loopback, execution, ingress, control, edge, proxy network는 동적 정책보다 먼저 적용되는
immutable deny이므로 관리자 규칙으로도 열 수 없다.

API transaction은 규칙과 `desired_revision/digest`를 함께 저장하고 감사 이벤트를 남긴다.
worker는 Docker socket이나 proxy PID 권한 없이 canonical policy를 desired 전용 volume에
`fsync + rename`으로 게시한다. Squid container는 desired volume을 read-only로만 읽는다.
Squid container 내부 watcher만 candidate를 다시 검증하고 전체 config를 `squid -k parse`한 뒤
active include를 교체하여 `squid -k reconfigure`한다. 성공 ack는 별도의 ack 전용 volume에
atomic write하며 worker는 그 volume을 read-only로만 읽어 DB의
`applied_revision/digest`에 반영한다. 따라서 proxy는 desired를 위조할 수 없고 worker는 ack를
위조할 수 없다. 실패하면 active include를 되돌리고 기존 applied revision을 유지하며 관리자
화면에 실패 코드와 재시도 동작을 제공한다.

부팅 정책은 image에 포함된 빈 deny-all 파일이다. 운영 IP는 Git, image, domain allowlist,
`.env`에 저장하지 않는다. DB 정책이 아직 게시되지 않았거나 dynamic volume이 유효하지 않으면
내부 목적지는 열리지 않는다. 이 기능을 처음 포함하는 `v0.1.6` 배포에는 schema migration과
새 volume/mount를 위한 stack 재생성이 필요하지만, 그 뒤의 규칙 CRUD·재시도에는 container
재시작이 필요하지 않다.

지원되는 DB restore 명령은 stack이 중지된 상태에서 derived desired/ack volume을 함께
폐기한다. proxy는 image의 빈 bootstrap으로 먼저 시작하고 restored DB의 정책을 worker가 다시
게시한다. 이 초기화가 실패하면 restore가 실패하므로 더 높은 과거 revision의 정책이 잠시라도
재활성화되는 것을 허용하지 않는다.

같은 host의 서비스는 `127.0.0.1`에만 bind하면 proxy container에서 보이지 않는다. host의
LAN IPv4 또는 검토된 `0.0.0.0` bind를 사용하고, 기존 host INPUT 정책이 있다면 proxy bridge
source에 대한 별도 검토가 필요하다. 노트북은 `NO_PROXY`에 대상 IP를 추가하지 않고 기존
`HTTP_PROXY/HTTPS_PROXY`를 통해 요청해야 한다.

## 결과와 잔여 위험

- execution network의 host/private direct 차단은 유지된다.
- desired/applied revision이 다를 때 관리자 목록은 요청 정책이다. UI는 이를 실제 적용 목록으로
  오인하지 않도록 이전 applied revision(없으면 기본 차단)이 유지 중임을 명시한다. 정확한 변경
  이력은 관리자 감사 이벤트로 추적한다.
- 한 규칙의 CIDR과 port를 별도 공용 ACL로 분리하지 않아 여러 규칙 사이의 조합 허용이 없다.
- Squid reconfigure는 새 요청과 새 연결의 정책을 갱신하지만 이미 열린 HTTP 또는 CONNECT
  tunnel을 강제로 종료한다고 보장하지 않는다. 즉시 폐기가 필요한 사고 대응에서는 별도
  egress connection drain 또는 proxy 재시작 절차가 필요하다.
- 승인한 내부 서비스 자체의 SSRF, open redirect, upload/data-exfiltration 기능은 이 ACL이
  제거하지 않는다. 서비스별 인증·인가, method 제한, 감사와 최소 권한 검토가 계속 필요하다.
- single-host trusted-team 전제보다 강한 사용자별 network policy가 필요하면 Kubernetes
  egress policy 또는 전용 보안 gateway로 재설계한다.
