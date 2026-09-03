# ADR-0010: 유휴 Jupyter 커널 자동 정리

- 상태: Accepted (2026-09-03)
- 날짜: 2026-09-03
- 결정 범위: kernel 메모리 수명주기, 관리자 정책, spawn authorization
- 관련 결정: [ADR-0001](0001-jupyterhub-control-plane.md), [ADR-0007](0007-admin-runtime-and-environment-management.md)

## 맥락

사용자가 Notebook을 오래 열어 두면 실행이 끝난 Python kernel도 변수, 모델과 dataset을
메모리에 계속 보유한다. 한 호스트를 여러 사용자가 공유하는 환경에서는 실제 계산이 없는
kernel 메모리를 회수할 수 있어야 한다. 반면 workspace container나 Jupyter single-user
server를 외부에서 바로 종료하면 Platform DB의 `desired=RUNNING`과 Hub 실제 상태가 어긋나고,
기존 operation/reconciler 계약을 우회할 수 있다.

브라우저 연결 여부만 기준으로 삼으면 JupyterLab 탭을 열어 둔 사용자의 유휴 kernel이 계속
남는다. 반대로 busy 상태까지 정리하면 출력이 드문 장시간 학습·분석 작업을 중단할 위험이
있다. 정책 값도 일반 사용자 환경변수로 전달하면 사용자가 운영 기준을 완화할 수 있다.

## 결정

1. Jupyter Server의 `MappingKernelManager` 내장 culler로 kernel process만 정리한다.
2. 기본 timeout은 1시간이다. 관리자는 관리자 전용 자원·커널 정책 화면에서 자동 정리를
   끄거나 `5분~7일` 범위의 60초 단위 값으로 바꿀 수 있다. `0`은 비활성이다.
3. 검사 주기는 60초로 고정한다. `cull_busy=False`로 Jupyter가 `busy`로 인식하는 셀 실행 중
   kernel을 보호하고, `cull_connected=True`로 브라우저 연결이 남아 있어도 idle kernel을
   정리한다. 셀이 반환된 뒤 별도 background process로 실행한 작업은 busy 보호 대상이 아니다.
4. worker는 시작 시점의 정책 값을 `spawn_authorizations`에 snapshot으로 저장한다. API가
   서명한 consume/check exact contract에 값을 포함하고, Hub가 형식·범위·step을 다시
   검사한 뒤 managed/legacy profile 모두에 동일한 server argument를 강제한다.
5. 사용자 환경변수의 `PLATFORM_*`/`JUPYTER_*` 예약 prefix와 arbitrary Hub option은 계속
   거부한다. 사용자는 timeout이나 busy/connected 기준을 덮어쓸 수 없다.
6. 정책 변경은 이미 실행 중인 process를 mutation하지 않는다. 변경 뒤 새로 시작하거나
   재시작한 workspace부터 적용한다.
7. `ServerApp.shutdown_no_activity_timeout`과 Hub 전체 idle-culler는 사용하지 않는다. 전체
   workspace 자동 중지는 Platform operation과 상태 수렴을 포함하는 별도 결정으로 남긴다.

## 결과와 위험

- idle kernel의 메모리는 회수되지만 Notebook 및 private/shared volume의 파일은 유지된다.
- 메모리의 변수·모델·실행 상태는 사라지며 사용자는 kernel을 다시 시작해야 한다.
- kernel activity와 busy 상태는 Jupyter가 관측한 message/state를 기준으로 한다. 실제 종료는
  timeout 뒤 다음 60초 검사까지 지연될 수 있다.
- single-user server/container는 계속 실행되므로 포털의 active workspace 수와 aggregate
  CPU/RAM 예약은 줄지 않는다. terminal과 kernel 밖 background process도 정리 대상이 아니다.
- 너무 짧은 timeout은 반복적인 kernel 재시작과 사용자 상태 손실을 유발할 수 있어 최솟값을
  5분으로 제한하고, 일반 사용자 화면에 정책과 상태 소실 안내를 표시한다.

## 검증 조건

- DB, Pydantic, API와 Hub가 `0` 또는 exact 범위/step만 허용한다.
- 정책 update의 version conflict와 idempotency가 유지되고 변경 전후 값이 감사 metadata에
  남는다.
- 발급 뒤 정책이 바뀌어도 이미 발급한 authorization은 원래 snapshot을 사용하며 변조된
  timeout은 Hub와 API check에서 거부된다.
- managed/legacy profile 모두 동일한 네 argument를 받고 `busy=False`, `connected=True`가
  바뀌지 않는다.
- 일반 사용자에게 활성 timeout과 파일 보존·메모리 상태 소실을 알리고, 관리자에게 현재
  실행 중 환경은 재시작 후 적용된다고 안내한다.
