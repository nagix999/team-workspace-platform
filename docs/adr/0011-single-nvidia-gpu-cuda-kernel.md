# ADR-0011: 단일 NVIDIA GPU와 CUDA Python kernel

- 상태: [ADR-0013](0013-multi-nvidia-gpu-exclusive-pool.md)으로 대체됨
- 일자: 2026-09-03
- 대체 일자: 2026-09-04
- 관련 결정: [ADR-0004](0004-single-host-compose.md), [ADR-0008](0008-derived-resource-profiles-and-stopped-creation.md), [ADR-0010](0010-idle-kernel-culling.md)

## 배경

사용자가 웹에서 GPU 환경을 선택하고 Python notebook에서 CUDA 연산을 실행할 수 있어야
한다. Python interpreter 자체가 CUDA를 제공하지는 않으므로 host driver, container GPU
주입, CUDA-enabled framework와 kernelspec을 하나의 검증 가능한 계약으로 묶어야 한다.
현재 배포는 단일 호스트이며 GPU 공유 스케줄러를 두지 않는다.

## 결정

1. CPU single-user image와 CUDA single-user image를 분리한다. GPU profile은 Python
   3.12.13, `python312-cuda` kernelspec 하나와 PyTorch `2.7.1+cu126`/CUDA 12.6만
   제공한다. CPU-only kernelspec을 GPU image에서 제거하고 실행 중에도 허용 kernelspec을
   하나로 제한한다.
2. 운영자가 Git 밖의 보호된 정책 파일에 canonical physical NVIDIA GPU UUID 하나를
   allowlist한다. ordinal, MIG UUID, 여러 GPU, MPS와 time-slicing은 받지 않는다.
3. GPU 1개는 workspace 단위의 durable exclusive lease다. 중지 확인 전에는 다른
   workspace에 재할당하지 않는다. 유휴 kernel process가 정리돼도 workspace lease는
   유지되며 명시적 중지 후 해제된다.
4. JupyterHub는 signed spawn authorization의 GPU binding을 검증하고 Docker Engine에
   exact UUID `DeviceRequest`를 전달한다. 사용자는 NVIDIA 관련 환경변수를 덮어쓸 수 없다.
5. 배포 전에는 현재 image뿐 아니라 재시작 가능한 enabled GPU history image도 같은 UUID로
   시작해 `nvidia-smi`, PyTorch CUDA 초기화와 실제 tensor 연산을 확인한다. 실제 workspace도
   Jupyter 시작 전에 같은 검사를 반복한다.
6. GPU 정책은 기본적으로 비활성화한다. Host driver와 NVIDIA Container Toolkit, 보호된
   UUID 정책 및 실제 tensor 검사가 모두 준비되지 않으면 GPU profile을 생성하거나 노출하지
   않는다.

## 결과와 한계

- 사용자는 일반 Python 환경과 CUDA 환경을 명확히 구분하고, 관리자는 예약된 GPU 하나의
  소유를 포털에서 확인할 수 있다.
- PyTorch wheel에 포함된 CUDA runtime을 사용하므로 host에 전체 CUDA Toolkit이나 `nvcc`는
  필수가 아니다. 사용자 CUDA extension compile 환경은 현재 범위가 아니다.
- GPU memory quota, 동시 공유, MIG, 여러 장치와 여러 host 스케줄링이 필요하면 이 계약을
  확장하지 않고 Kubernetes와 전용 GPU scheduler 전환을 검토한다.
- 실제 GPU가 없는 개발 호스트에서는 metadata와 정책만 검사할 수 있다. 운영 승인은 대상
  GPU host에서 실제 tensor probe가 성공해야 한다.
