#!/opt/conda/envs/python312/bin/python
"""Fail-closed contract checks for the dedicated PyTorch CUDA runtime."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from jupyter_client.kernelspec import KernelSpecManager


EXPECTED_ACCELERATOR_CONTRACT = {
    "schema_version": 1,
    "kind": "nvidia",
    "count": 1,
    "sharing": "exclusive",
    "cuda_version": "12.6",
    "framework": "pytorch",
    "framework_version": "2.7.1",
}
EXPECTED_TORCH_BUILD_VERSION = "2.7.1+cu126"
EXPECTED_PYTHON_VERSION = "3.12.13"
EXPECTED_PYTHON_EXECUTABLE = "/opt/conda/envs/python312/bin/python"
EXPECTED_KERNEL_NAME = "python312-cuda"
EXPECTED_KERNEL_DISPLAY_NAME = "Python 3.12 (PyTorch CUDA 12.6)"
EXPECTED_DRIVER_CAPABILITIES = "compute,utility"
EXPECTED_JUPYTER_PATH = "/opt/conda/share/jupyter"
EXPECTED_KERNEL_CONTRACT = {
    "schema_version": 1,
    "python_version": EXPECTED_PYTHON_VERSION,
    "kernels": [
        {
            "name": EXPECTED_KERNEL_NAME,
            "display_name": EXPECTED_KERNEL_DISPLAY_NAME,
            "language": "python",
            "python_version": EXPECTED_PYTHON_VERSION,
            "executable": EXPECTED_PYTHON_EXECUTABLE,
        }
    ],
    "default_kernel": EXPECTED_KERNEL_NAME,
}
GPU_UUID_RE = re.compile(r"^GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


class CudaRuntimeContractError(RuntimeError):
    """The image or assigned accelerator does not match the reviewed runtime."""


def load_accelerator_contract(environment: dict[str, str]) -> dict[str, object]:
    raw = environment.get("PLATFORM_ACCELERATOR_CONTRACT", "")
    if not raw or len(raw) > 4096:
        raise CudaRuntimeContractError("accelerator contract is missing or too large")
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError:
        raise CudaRuntimeContractError(
            "accelerator contract is not valid JSON"
        ) from None
    if not isinstance(contract, dict) or set(contract) != set(
        EXPECTED_ACCELERATOR_CONTRACT
    ):
        raise CudaRuntimeContractError("accelerator contract does not match the image")
    for key, expected in EXPECTED_ACCELERATOR_CONTRACT.items():
        value = contract[key]
        if type(value) is not type(expected) or value != expected:
            raise CudaRuntimeContractError(
                "accelerator contract does not match the image"
            )
    return contract


def assigned_gpu_uuid(environment: dict[str, str]) -> str:
    value = environment.get("PLATFORM_NVIDIA_GPU_DEVICE_IDS", "")
    if not GPU_UUID_RE.fullmatch(value):
        raise CudaRuntimeContractError(
            "exactly one physical NVIDIA GPU UUID must be assigned"
        )
    return value


def validate_image_environment(environment: dict[str, str]) -> None:
    """Require the immutable CUDA image's protected runtime environment."""

    if environment.get("NVIDIA_DRIVER_CAPABILITIES") != EXPECTED_DRIVER_CAPABILITIES:
        raise CudaRuntimeContractError(
            "NVIDIA driver capabilities must be compute,utility"
        )
    if environment.get("JUPYTER_PATH") != EXPECTED_JUPYTER_PATH:
        raise CudaRuntimeContractError("CUDA kernelspec search path does not match")


def validate_profile_kernel_contract(environment: dict[str, str]) -> None:
    """Cross-check a supplied profile contract against this CUDA image.

    The host-level image probe intentionally has no profile and may omit these
    values. Deployment profile checks and real workspaces always supply them;
    when present they must describe this exact interpreter and kernelspec.
    """

    raw = environment.get("PLATFORM_KERNEL_CONTRACT", "")
    if not raw:
        return
    if len(raw) > 16 * 1024:
        raise CudaRuntimeContractError("CUDA profile kernel contract is too large")
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError:
        raise CudaRuntimeContractError(
            "CUDA profile kernel contract is not valid JSON"
        ) from None
    canonical = json.dumps(
        contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    expected = json.dumps(
        EXPECTED_KERNEL_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if canonical != expected:
        raise CudaRuntimeContractError(
            "CUDA profile kernel contract does not match the image"
        )
    if (
        environment.get("PLATFORM_DEFAULT_KERNEL") != EXPECTED_KERNEL_NAME
        or environment.get("PLATFORM_PYTHON_EXECUTABLE") != EXPECTED_PYTHON_EXECUTABLE
    ):
        raise CudaRuntimeContractError(
            "CUDA profile kernel environment does not match the image"
        )
    path_entries = environment.get("PATH", "").split(os.pathsep)
    if not path_entries or path_entries[0] != str(
        Path(EXPECTED_PYTHON_EXECUTABLE).parent
    ):
        raise CudaRuntimeContractError("CUDA profile interpreter is not first on PATH")


def _verify_kernel() -> None:
    try:
        manager = KernelSpecManager(ensure_native_kernel=False)
        specifications = manager.get_all_specs()
    except Exception:
        raise CudaRuntimeContractError("CUDA kernelspec inventory failed") from None
    if set(specifications) != {EXPECTED_KERNEL_NAME}:
        raise CudaRuntimeContractError(
            "CUDA image must expose only the reviewed CUDA kernelspec"
        )
    item = specifications[EXPECTED_KERNEL_NAME]
    if not isinstance(item, dict):
        raise CudaRuntimeContractError("CUDA kernelspec is malformed")
    resource_dir = item.get("resource_dir")
    expected_dir = Path("/opt/conda/share/jupyter/kernels") / EXPECTED_KERNEL_NAME
    try:
        if Path(str(resource_dir)).resolve(strict=True) != expected_dir.resolve(
            strict=True
        ):
            raise CudaRuntimeContractError(
                "CUDA kernelspec came from an untrusted path"
            )
    except OSError:
        raise CudaRuntimeContractError("CUDA kernelspec path is unavailable") from None
    spec = item.get("spec")
    if not isinstance(spec, dict):
        raise CudaRuntimeContractError("CUDA kernelspec is malformed")
    if (
        spec.get("display_name") != EXPECTED_KERNEL_DISPLAY_NAME
        or spec.get("language") != "python"
    ):
        raise CudaRuntimeContractError("CUDA kernelspec metadata does not match")
    argv = spec.get("argv")
    reviewed_arguments = {
        ("-m", "ipykernel_launcher", "-f", "{connection_file}"),
        (
            "-Xfrozen_modules=off",
            "-m",
            "ipykernel_launcher",
            "-f",
            "{connection_file}",
        ),
    }
    if (
        not isinstance(argv, list)
        or not argv
        or argv[0] != EXPECTED_PYTHON_EXECUTABLE
        or tuple(argv[1:]) not in reviewed_arguments
    ):
        raise CudaRuntimeContractError("CUDA kernelspec command does not match")


def verify_metadata(torch_module: Any) -> dict[str, object]:
    if sys.executable != EXPECTED_PYTHON_EXECUTABLE:
        raise CudaRuntimeContractError("CUDA verifier used the wrong Python executable")
    if platform.python_version() != EXPECTED_PYTHON_VERSION:
        raise CudaRuntimeContractError("CUDA Python patch version does not match")
    if str(getattr(torch_module, "__version__", "")) != EXPECTED_TORCH_BUILD_VERSION:
        raise CudaRuntimeContractError("PyTorch CUDA wheel build does not match")
    torch_version = getattr(torch_module, "version", None)
    if (
        torch_version is None
        or getattr(torch_version, "cuda", None)
        != EXPECTED_ACCELERATOR_CONTRACT["cuda_version"]
        or getattr(torch_version, "hip", None) is not None
    ):
        raise CudaRuntimeContractError("PyTorch CUDA metadata does not match")
    try:
        backends = getattr(torch_module, "backends", None)
        cuda_backend = getattr(backends, "cuda", None)
        is_built = cuda_backend is not None and cuda_backend.is_built() is True
    except Exception:
        is_built = False
    if not is_built:
        raise CudaRuntimeContractError("PyTorch was not built with CUDA support")
    _verify_kernel()
    return {
        "framework": "pytorch",
        "framework_version": EXPECTED_ACCELERATOR_CONTRACT["framework_version"],
        "framework_build_version": EXPECTED_TORCH_BUILD_VERSION,
        "cuda_version": EXPECTED_ACCELERATOR_CONTRACT["cuda_version"],
        "python_version": EXPECTED_PYTHON_VERSION,
        "python_executable": EXPECTED_PYTHON_EXECUTABLE,
        "kernel": EXPECTED_KERNEL_NAME,
    }


def _nvidia_smi_gpu_uuids(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[str]:
    try:
        result = runner(
            [
                "nvidia-smi",
                "--query-gpu=uuid",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise CudaRuntimeContractError("nvidia-smi runtime probe failed") from None
    values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not values or any(not GPU_UUID_RE.fullmatch(value) for value in values):
        raise CudaRuntimeContractError("nvidia-smi returned invalid GPU UUIDs")
    return values


def verify_gpu(
    torch_module: Any,
    expected_uuid: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, object]]:
    cuda = getattr(torch_module, "cuda", None)
    try:
        available = cuda is not None and cuda.is_available() is True
        device_count = cuda.device_count() if available else 0
    except Exception:
        raise CudaRuntimeContractError("PyTorch cannot initialize CUDA") from None
    if not available:
        raise CudaRuntimeContractError("PyTorch cannot initialize CUDA")
    if type(device_count) is not int or device_count != 1:
        raise CudaRuntimeContractError("CUDA runtime must expose exactly one GPU")
    visible_uuids = _nvidia_smi_gpu_uuids(runner)
    if visible_uuids != [expected_uuid]:
        raise CudaRuntimeContractError("visible GPU UUID does not match the assignment")

    try:
        properties = cuda.get_device_properties(0)
        name = str(properties.name).strip()
        major = int(properties.major)
        minor = int(properties.minor)
        source = torch_module.tensor([[1.0, 2.0]], device="cuda:0")
        product = torch_module.matmul(source, source.transpose(0, 1))
        cuda.synchronize(0)
        result = float(product.item())
    except Exception:
        raise CudaRuntimeContractError("CUDA tensor execution probe failed") from None
    if not name or major < 1 or minor < 0 or result != 5.0:
        raise CudaRuntimeContractError("CUDA tensor execution result is invalid")
    return [
        {
            "index": 0,
            "uuid": expected_uuid,
            "name": name,
            "compute_capability": f"{major}.{minor}",
        }
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--metadata-only", action="store_true")
    mode.add_argument("--require-gpu", action="store_true")
    args = parser.parse_args(argv)
    try:
        environment = dict(os.environ)
        load_accelerator_contract(environment)
        validate_profile_kernel_contract(environment)
        validate_image_environment(environment)
        try:
            import torch
        except Exception:
            raise CudaRuntimeContractError("PyTorch cannot be imported") from None
        report = verify_metadata(torch)
        devices: list[dict[str, object]] = []
        if args.require_gpu:
            devices = verify_gpu(torch, assigned_gpu_uuid(environment))
        report.update(
            {
                "schema_version": 1,
                "mode": "runtime" if args.require_gpu else "metadata",
                "device_count": len(devices),
                "devices": devices,
                "status": "passed",
            }
        )
    except CudaRuntimeContractError as exc:
        print(f"CUDA runtime verification failed: {exc}", file=sys.stderr)
        return 78
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
