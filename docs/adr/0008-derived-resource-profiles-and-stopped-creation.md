# ADR-0008: 파생 자원 프로필과 중지 상태 workspace 생성

- 상태: Accepted (2026-08-15)
- 날짜: 2026-08-15
- 결정 범위: 관리자 CPU/RAM 선택값, runtime profile 파생, workspace 생성·환경변수 순서
- 관련 결정: [ADR-0007](0007-admin-runtime-and-environment-management.md)

## 맥락

정적 profile JSON에 CPU와 메모리의 모든 조합을 미리 적으면 다른 사양의 호스트에 배포할
때마다 소스와 Hub allowlist를 수정해야 한다. 반대로 브라우저나 일반 관리자 입력을 그대로
Docker limit으로 전달하면 기존의 immutable image/kernel/command 검증 경계를 우회한다.

생성 요청에 환경변수를 함께 받으면 workspace가 즉시 시작되는 동안 값을 수정할 시간이
없고, 비밀 값이 큰 생성 payload와 idempotency 경로에 불필요하게 결합된다.

## 결정

1. 배포 설정의 CPU millicore와 메모리 MiB는 API와 JupyterHub가 공유하는 hard ceiling이다.
2. 관리자는 hard ceiling과 현재 전체 예산 이하의 positive whole millicore/MiB 값을 자원
   선택 정책에 직접 추가하거나 제거한다.
3. backend는 배포 profile에서 검증된 각 Python runtime family를 기준으로 선택된
   CPU × memory 전체 조합을 materialize한다. image, kernel, command, mount, UID/GID,
   pids와 containment 값은 원본에서 복사하고 CPU/memory만 바꾼 새 digest를 계산한다.
4. spawn authorization은 원본 profile ID/version/digest, 파생 profile ID/version/digest와
   CPU/memory를 함께 결속한다. Hub는 로컬 allowlist의 원본에서 같은 파생 profile을 다시
   만들고 ID/digest, whole-MiB/millicore와 자신의 hard ceiling을 검사한다. 따라서 API DB의
   임의 row나 브라우저 resource field만으로 image·command·상한을 바꿀 수 없다.
5. 새 Python/image/kernel family는 여전히 배포 profile policy와 image contract 검증을
   거쳐야 한다. 웹 관리자는 새로운 실행 이미지를 등록할 수 없다.
6. workspace 생성 API는 profile tuple과 선택적 이름만 받는다. free private-volume slot을
   배정하고 `desired=STOPPED`, `observed=NOT_FOUND`인 workspace와 terminal CREATE operation을
   만든다. 실행 CPU/RAM은 예약하지 않는다.
7. 사용자는 생성된 workspace의 환경변수 API에서 일반/비밀 값을 설정한 다음 START를
   요청한다. aggregate CPU/RAM admission과 execution-host health 검사는 START transaction에
   적용한다.

## 결과와 위험

- 더 큰 호스트는 hard ceiling 설정을 높인 뒤 웹에서 8 core, 16 GiB 같은 값을 추가할 수 있다.
- 선택값의 cross product만큼 DB profile/offer row가 늘어난다. 각 축은 최대 32개로 제한하고,
  실제 runtime family × CPU × memory 결과가 1,024개를 넘는 정책은 row를 만들기 전에
  거부한다. 관리 화면은 명시적으로 선택된 값만 노출한다.
- CPU/RAM은 Docker hard limit이지만 전체 budget을 host 실측 없이 높이면 control plane이
  압박될 수 있다. 운영자는 부하 시험 후 hard ceiling을 설정해야 한다.
- 중지 상태 생성도 private slot 하나를 사용하므로 사용자당 5개 보존 환경 한도는 유지된다.

## 검증 조건

- 임의 CPU/memory 값은 정렬·중복·positive·hard ceiling·전체 예산을 검증한다.
- 모든 Python runtime × 선택 resource 조합이 생성되고 public catalog에 정확히 한 번 나타난다.
- Hub가 원본 digest, 파생 ID/digest, CPU/memory 또는 hard ceiling 변조를 거부한다.
- 생성 payload의 `environment`와 raw execution field는 `422`로 거부한다.
- 생성 직후 상태는 STOPPED/NOT_FOUND이고 예약 CPU/RAM은 0이다.
- 환경변수를 생성 후 저장하고 START하면 그 시점의 encrypted snapshot만 container에 적용된다.
