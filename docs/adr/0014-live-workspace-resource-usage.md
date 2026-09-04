# ADR-0014: 실행 중 workspace CPU·메모리 관측

- 상태: Accepted
- 작성일: 2026-09-04
- 기준 버전: 0.1.7
- 관련 결정: [ADR-0001](0001-jupyterhub-control-plane.md), [ADR-0004](0004-single-host-compose.md), [ADR-0008](0008-derived-resource-profiles-and-stopped-creation.md)

## 배경

예약된 CPU·메모리는 admission과 용량 계획에는 필요하지만, 실제로 실행 중인 환경이 얼마를
사용하는지는 알려 주지 않는다. 사용자는 자신의 환경을 조정할 근거가 필요하고 관리자는 모든
실행 환경의 현재 사용량과 측정 누락 범위를 한 화면에서 확인해야 한다.

FastAPI나 React에 Docker socket을 추가하면 관측 기능 때문에 제어면 권한이 크게 넓어진다.
컨테이너별 브라우저 요청도 Docker daemon 부하와 응답 지연을 늘린다. 따라서 이미
DockerSpawner 때문에 socket을 보유한 JupyterHub에서만 원시 통계를 읽고, 기존 read-only
reconciler가 일괄 snapshot을 가져오는 방식을 사용한다.

## 결정

1. JupyterHub는 내부 전용 `GET /hub/api/platform/resource-usage` endpoint를 제공한다.
   endpoint는 token 인증만 허용하며 `read:servers` scope뿐 아니라 호출 주체가 정확히
   `platform-reconciler` ORM service인지 확인한다. 사용자 token과 cookie는 aggregate
   endpoint를 사용할 수 없다.
2. 측정 대상은 ready 상태인 DockerSpawner가 가리키는 실행 컨테이너뿐이다. Hub DB에
   저장된 full container ID와 inspect 결과 ID, `platform.managed`, kind, username,
   server name, workspace label을 모두 확인한다. 응답에는 container ID, label, image,
   host 정보나 오류 원문을 포함하지 않는다.
3. Docker stats 호출은 동시성을 제한한 batch로 수행한다. 한 환경의 실패는 해당 item만
   누락시키며 전체 Hub lifecycle snapshot을 실패시키지 않는다.
4. CPU는 Docker의 container/system 누적 counter 차이와 online CPU 수를 이용해
   millicore로 계산한다. 메모리는 cgroup v2 `inactive_file` 또는 v1
   `total_inactive_file` cache를 usage에서 빼고 byte로 표시한다.
5. reconciler는 endpoint schema, UTC 시각, 숫자 범위, 중복과 Hub server binding을
   검증한다. RUNNING 상태이고 freshness 범위 안이며 Docker memory limit이 immutable
   runtime profile의 limit과 정확히 일치할 때만 네 값을 하나의 cache 단위로 저장한다.
6. 측정값 네 컬럼은 전부 `NULL`이거나 전부 채워져야 한다. 중지·삭제·재시작 전환,
   NOT_FOUND/STOPPED 관측, 누락·오래된 값·limit 불일치에는 cache를 비운다. 통계 변화는
   workspace 업무용 `row_version`과 `updated_at`을 증가시키지 않는다.
7. 일반 사용자는 기존 소유권이 적용된 workspace API로 자신의 값만 본다. 관리자는 기존
   전체 workspace API의 환경별 값과 capacity API의 fresh 측정 합계를 본다. 합계에는
   측정 성공 수와 누락 수를 함께 제공한다.
8. 사용자 화면은 기존 15초 background refresh를 사용한다. 관리자 현황과 전체 환경 화면도
   안정 상태에서는 15초, lifecycle 작업 중에는 2초 주기로 갱신한다. 중지·측정 중·수집
   불가·오래된 값은 0 사용량과 구분한다. API는 측정값의 `expires_at`을 함께 반환하며,
   브라우저는 그 시각이 지나거나 refresh가 실패하면 보존된 값을 즉시 “이전 측정값”으로
   표시한다.

## 결과와 한계

- FastAPI, reconciler와 frontend에는 Docker socket을 추가하지 않는다.
- 이 값은 현재 운영 판단을 위한 짧은 TTL snapshot이며 청구, 장기 시계열, peak/평균,
  host 전체 사용량 또는 성능 SLA 자료가 아니다.
- GPU 개수 예약은 별도 lease로 관리하며 이번 endpoint는 GPU utilization과 GPU memory를
  측정하지 않는다.
- JupyterHub 5.5의 `extra_handlers`는 deprecated지만 현재 digest-pinned Hub에서 지원된다.
  Hub 업그레이드 시 동일 service identity 계약을 유지하는 별도 Hub-managed metrics service로
  이전할 수 있다.
