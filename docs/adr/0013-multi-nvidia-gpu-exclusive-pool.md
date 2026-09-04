# ADR-0013: 다중 NVIDIA GPU 풀과 환경별 독점 할당

- 상태: 승인
- 일자: 2026-09-04
- 기준 버전: 0.1.7
- 대체 결정: [ADR-0011](0011-single-nvidia-gpu-cuda-kernel.md)
- 관련 결정: [ADR-0004](0004-single-host-compose.md), [ADR-0008](0008-derived-resource-profiles-and-stopped-creation.md)

## 배경

단일 호스트에 물리 NVIDIA GPU가 여러 장 있을 때 한 장만 사용하는 제약을 제거하고,
사용자가 workload에 따라 한 환경에서 여러 장을 사용할 수 있어야 한다. 동시에 같은 GPU가
여러 환경에 중복 주입되거나 재시작 과정에서 다른 GPU로 바뀌는 상황은 막아야 한다.

## 결정

1. 운영자는 Git 밖의 보호된 정책 파일에 canonical physical NVIDIA GPU UUID를 1~64개
   중복 없이 등록한다. 입력 순서와 무관하게 내부 계약은 UUID를 사전순으로 canonicalize한다.
   ordinal, PCI bus ID와 MIG UUID는 허용하지 않는다.
2. 사전검사는 allowlist의 각 GPU를 개별 검사하고 전체 풀을 동시에 주입해 driver, NVIDIA
   Container Toolkit, PyTorch/CUDA metadata, 장치 수와 각 GPU의 tensor 연산을 확인한다.
   Profile image 검사는 모든 GPU 개수 계약을 실행하되, 8개 이하 풀은 각 물리 장치를
   순환 검증하고 더 큰 풀은 개수별 대표 exact assignment 하나로 제한한다. 물리 장치별
   검증은 앞선 host 사전검사가 담당하므로 64장에서도 profile probe가 제곱으로 늘지 않는다.
3. 관리자는 검증된 풀 크기 이하로 전체 GPU 예산을 정하고, 사용자에게 공개할 GPU 개수
   집합(예: 1, 2, 4)을 관리한다. CPU 전용 0개는 항상 선택 정책에 포함된다.
4. GPU 환경은 선택한 개수만큼의 물리 UUID를 하나의 durable exclusive lease 집합으로
   예약한다. 각 UUID는 동시에 한 workspace에만 속하며, 실행 중지 확인 후에만 풀로 반환한다.
   예약량은 실행 환경별 요청 GPU 개수의 합이다.
5. Spawn authorization은 요청 개수, 정확한 정렬 UUID 집합과 inventory digest를 함께 묶는다.
   JupyterHub는 이를 검증한 뒤 해당 UUID만 Docker `DeviceRequest`에 전달한다.
6. 각 GPU 개수 profile은 동일한 Python 3.12.13, `python312-cuda`, PyTorch
   `2.7.1+cu126`/CUDA 12.6 image 계약을 사용한다. CPU profile에는 GPU를 노출하지 않는다.

## 결과와 한계

- 여러 환경이 남은 물리 GPU를 나누어 독점 사용하거나 한 환경이 여러 GPU를 함께 사용할 수
  있다. 전체 예산 또는 미예약 장치 수가 부족하면 시작 admission이 거부된다.
- 중지·재시작, worker 재시도와 동시 시작에서도 lease DB를 기준으로 GPU 중복 배정을 막는다.
- GPU memory quota, MIG, MPS/time-slicing, GPU 공유, topology-aware 배치와 다중 호스트
  scheduling은 지원하지 않는다. 이런 요구가 생기면 Kubernetes와 전용 GPU scheduler를
  검토한다.
- 실제 GPU host에서 개별 장치와 전체 풀 tensor probe가 모두 성공해야 운영에 공개할 수 있다.
