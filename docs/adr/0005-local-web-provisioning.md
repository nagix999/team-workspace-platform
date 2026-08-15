# ADR-0005: 로컬 Compose의 웹 사용자 저장공간 프로비저닝

- 상태: Accepted
- 날짜: 2026-08-10

> 2026-08-11 이후 host network 정책과 private/shared storage enforcement, 운영 XFS
> hard-quota 결정은 [ADR-0006](0006-trusted-network-shared-storage.md)이 대체한다.
> 아래 XFS·quota 관련 문장은 결정 당시의 역사적 기록이며 현재 운영 계약이 아니다.
> 로컬 웹 provisioning의 권한 경계, durable job과 manifest 검증 결정은 계속 유효하다.

## 배경

승인된 사용자는 첫 포털 OAuth 로그인 때 `PROVISIONING`으로 등록된다. 기존 구현은
관리자가 호스트에서 `make provision-user`를 실행해야 전용 볼륨 슬롯 5개가 검증되고
`ACTIVE`로 전환됐다. 워크스페이스 생성·시작·진행률 표시는 이미 포털에 있지만 이
수동 단계 때문에 사용자가 웹에서 전체 흐름을 끝낼 수 없었다.

FastAPI나 일반 operation worker에 Docker socket을 추가하면 웹 취약점 하나가 호스트
root급 Docker 권한으로 확대된다. 운영 XFS project quota 프로비저닝은 root-owned host
경계를 유지해야 하며, 로컬 개발용 Docker named volume 경로와 동일하게 취급할 수 없다.

## 결정

명시적 `local-dev` Compose에서만 self-service 프로비저닝을 허용한다.

1. 승인된 사용자가 포털에서 준비를 요청하면 FastAPI는 CSRF와 현재 사용자 상태를
   검사하고 SQLite의 1-user/1-job 작업을 `PENDING`으로 기록한다. 임의 username,
   volume 이름, project ID 또는 host path는 브라우저 입력으로 받지 않는다.
2. FastAPI와 일반 worker에는 Docker socket을 mount하지 않는다. 이미 DockerSpawner
   때문에 socket을 가진 JupyterHub 컨테이너 안에서 URL 없는 managed service를
   실행한다. 새 포트나 public Hub service route는 만들지 않는다.
3. managed service는 HMAC 서명·timestamp·nonce가 적용된 control-network 내부 API로
   작업을 lease한다. 응답의 platform user UUID와 Hub username만 사용해 고정 규칙으로
   shared volume과 private slot 5개를 만들고 label·소유권·초기 권한을 검증한다.
4. allocator registry와 manifest는 원자적 파일 교체와 파일 잠금을 사용한다. 재시도는
   같은 사용자에게 같은 5-ID block과 같은 volume 이름을 재사용하며, 기존 label이
   다르면 덮어쓰지 않고 실패한다.
5. 완료 manifest를 내부 API로 돌려보내면 FastAPI가 enabled profile의 UID/GID와 disk
   limit, 사용자 UUID·username, 결정적 slot UUID, 정확히 1..5인 slot, volume 이름을
   다시 검증한다. 같은 SQLite transaction에서 slot inventory를 반영하고
   `PROVISIONING -> ACTIVE`와 job 성공을 기록한다. 일부 생성이나 검증 실패는 사용자를
   활성화하지 않는다.
6. lease와 attempt 번호가 맞는 결과만 수락한다. 실패는 민감한 Docker 오류를 브라우저에
   전달하지 않고 고정된 오류 코드·요약으로 기록하며 제한된 자동 재시도 후 사용자가
   웹에서 다시 요청할 수 있다. 요청·성공·실패는 audit event로 남긴다.
7. production에서 web provisioning flag가 켜지면 API와 Hub 모두 시작을 거부한다.
   운영 XFS quota 경로는 기존 root-owned one-shot/host agent 결정을 유지한다.

## 대안

### FastAPI 또는 일반 worker에 Docker socket mount

구현은 작지만 인증·IDOR·request parsing 결함의 blast radius가 host 전체로 커지므로
기각한다.

### 별도 상시 provisioner Compose container

권한 분리는 명확하지만 raw socket을 가진 장기 실행 주체가 하나 더 생긴다. 10명 규모의
로컬 MVP에서는 이미 수용한 JupyterHub 권한 경계 안의 managed service보다 이점이 작아
기각한다.

### 첫 spawn 때 volume을 즉석 생성

포털 DB inventory 검증 전에 mount가 발생하고 workspace slot allocation 계약을
복잡하게 하므로 기각한다.

### 운영과 로컬 모두 같은 웹 프로비저닝 사용

운영 XFS project quota에는 root command, mount와 quota 상태 검증이 필요하다. Docker
named volume만 쓰는 로컬 예외를 그대로 확대할 수 없어 기각한다.

## 결과와 잔여 위험

- 사용자는 승인 후 포털에서 저장공간 준비를 요청하고, `ACTIVE` 전환 뒤 기존 환경 생성
  버튼으로 Jupyter 개발환경을 만들 수 있다.
- API/worker의 Docker 권한 금지와 기존 workspace 5개/전체 active 15개 제한은 유지된다.
- JupyterHub 침해는 원래부터 Docker socket 때문에 host root급이다. managed service는
  그 기존 경계 안의 기능을 늘리지만 별도 socket 보유 주체를 추가하지 않는다.
- local-dev 볼륨은 XFS hard quota를 강제하지 않는다. 이 예외는 운영 배포에 사용할 수
  없으며 production flag 검증으로 실패 시 닫는다.
- 프로세스가 Docker volume을 만든 뒤 완료 제출 전에 중단될 수 있다. 결정적 이름,
  label 검증과 idempotent manifest 재사용으로 재시도하되 자동 삭제·덮어쓰기는 하지 않는다.

## 확인 기준

- 승인 사용자: 첫 포털 로그인 → 웹 준비 요청 → `PENDING/RUNNING` → `ACTIVE` → 환경
  생성 → Jupyter URL 열기 흐름이 동작한다.
- 미인증·CSRF 누락·`DISABLED` 사용자의 준비 요청은 거부된다.
- 중복 요청은 중복 볼륨이나 중복 job을 만들지 않는다.
- 다른 owner label, 잘못된 manifest, stale lease와 attempt 결과는 실패하고 사용자는
  `PROVISIONING`에 남는다.
- production 설정은 managed local provisioner를 시작하지 않으며 flag 오설정을 거부한다.
