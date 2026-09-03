#!/usr/bin/env python3
"""Read-only production preflight for an exclusive NVIDIA/PyTorch runtime."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


ACCELERATOR_CONTRACT = {
    "schema_version": 1,
    "kind": "nvidia",
    "count": 1,
    "sharing": "exclusive",
    "cuda_version": "12.6",
    "framework": "pytorch",
    "framework_version": "2.7.1",
}
EXPECTED_RUNTIME_REPORT = {
    "framework": "pytorch",
    "framework_version": "2.7.1",
    "framework_build_version": "2.7.1+cu126",
    "cuda_version": "12.6",
    "python_version": "3.12.13",
    "python_executable": "/opt/conda/envs/python312/bin/python",
    "kernel": "python312-cuda",
}
CONFIG_KEYS = {
    "schema_version",
    "nvidia_driver_version",
    "nvidia_container_toolkit_version",
    "gpu_uuids",
}
GPU_UUID_RE = re.compile(r"^GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
DRIVER_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
TOOLKIT_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
VERSION_TOKEN_RE = re.compile(
    r"(?<![0-9.])([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)(?![0-9.])"
)
Runner = Callable[..., subprocess.CompletedProcess[str]]


class GpuPreflightError(RuntimeError):
    """A required production GPU invariant was not proven."""


def _exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    if set(value) != expected:
        raise GpuPreflightError(
            f"{where} keys mismatch: missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}"
        )


def load_gpu_config(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink():
            raise GpuPreflightError("GPU config must not be a symlink")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise GpuPreflightError("GPU config must be a regular file")
        if metadata.st_mode & 0o022:
            raise GpuPreflightError("GPU config must not be group/world writable")
        config = json.loads(path.read_text(encoding="utf-8"))
    except GpuPreflightError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise GpuPreflightError(f"cannot read GPU config: {exc}") from exc
    if not isinstance(config, dict):
        raise GpuPreflightError("GPU config must be an object")
    _exact_keys(config, CONFIG_KEYS, "GPU config")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise GpuPreflightError("GPU config schema_version must be 1")
    driver = config["nvidia_driver_version"]
    if not isinstance(driver, str) or not DRIVER_VERSION_RE.fullmatch(driver):
        raise GpuPreflightError("NVIDIA driver version must be exact")
    toolkit = config["nvidia_container_toolkit_version"]
    if not isinstance(toolkit, str) or not TOOLKIT_VERSION_RE.fullmatch(toolkit):
        raise GpuPreflightError("NVIDIA Container Toolkit version must be exact")
    gpu_uuids = config["gpu_uuids"]
    if (
        not isinstance(gpu_uuids, list)
        or len(gpu_uuids) != 1
        or any(not isinstance(value, str) for value in gpu_uuids)
        or any(not GPU_UUID_RE.fullmatch(value) for value in gpu_uuids)
        or gpu_uuids != sorted(set(gpu_uuids))
    ):
        raise GpuPreflightError(
            "gpu_uuids must contain exactly one canonical physical NVIDIA GPU UUID"
        )
    return config


def _run_checked(
    runner: Runner,
    args: list[str],
    *,
    description: str,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GpuPreflightError(f"{description} could not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        suffix = f": {detail}" if detail else ""
        raise GpuPreflightError(f"{description} failed{suffix}")
    return result


def _toolkit_version(output: str) -> str:
    versions = VERSION_TOKEN_RE.findall(output)
    if len(versions) != 1:
        raise GpuPreflightError("could not read one NVIDIA Container Toolkit version")
    return versions[0]


def _host_gpu_inventory(output: str) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2:
            raise GpuPreflightError("nvidia-smi inventory output is malformed")
        uuid, driver = fields
        if (
            not GPU_UUID_RE.fullmatch(uuid)
            or not DRIVER_VERSION_RE.fullmatch(driver)
            or uuid in inventory
        ):
            raise GpuPreflightError("nvidia-smi inventory value is invalid")
        inventory[uuid] = driver
    if not inventory:
        raise GpuPreflightError("nvidia-smi found no physical GPU")
    return inventory


def validate_image_reference(image: str) -> str:
    if not isinstance(image, str) or not (
        IMAGE_ID_RE.fullmatch(image) or REPOSITORY_DIGEST_RE.fullmatch(image)
    ):
        raise GpuPreflightError(
            "GPU image must be an immutable image ID or repository digest"
        )
    return image


def _verify_image_identity(image: str, runner: Runner) -> tuple[str, list[str]]:
    image_id = _run_checked(
        runner,
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        description="CUDA image ID inspection",
    ).stdout.strip()
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise GpuPreflightError("Docker returned an invalid CUDA image ID")
    raw_digests = _run_checked(
        runner,
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image],
        description="CUDA repository digest inspection",
    ).stdout.strip()
    try:
        repository_digests = json.loads(raw_digests)
    except json.JSONDecodeError:
        raise GpuPreflightError("Docker returned invalid repository digests") from None
    if repository_digests is None:
        repository_digests = []
    if not isinstance(repository_digests, list) or any(
        not isinstance(value, str) or not REPOSITORY_DIGEST_RE.fullmatch(value)
        for value in repository_digests
    ):
        raise GpuPreflightError("Docker returned invalid repository digests")
    if IMAGE_ID_RE.fullmatch(image):
        if image_id != image:
            raise GpuPreflightError("CUDA image ID does not match --image")
    elif image not in repository_digests:
        raise GpuPreflightError("CUDA repository digest does not match --image")
    return image_id, repository_digests


def _runtime_probe_command(image: str, gpu_uuid: str) -> list[str]:
    accelerator_contract = json.dumps(
        ACCELERATOR_CONTRACT, sort_keys=True, separators=(",", ":")
    )
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--gpus",
        f"device={gpu_uuid}",
        "--network=none",
        "--read-only",
        "--user=1000:100",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=256",
        "--memory=2g",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m",
        "--env",
        f"PLATFORM_ACCELERATOR_CONTRACT={accelerator_contract}",
        "--env",
        f"PLATFORM_NVIDIA_GPU_DEVICE_IDS={gpu_uuid}",
        "--env",
        "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
        "--entrypoint=/opt/conda/envs/python312/bin/python",
        image,
        "/usr/local/libexec/verify_cuda_runtime.py",
        "--require-gpu",
    ]


def _validate_runtime_report(output: str, gpu_uuid: str) -> dict[str, Any]:
    try:
        report = json.loads(output.strip())
    except json.JSONDecodeError:
        raise GpuPreflightError("CUDA runtime probe returned invalid JSON") from None
    if not isinstance(report, dict):
        raise GpuPreflightError("CUDA runtime probe report must be an object")
    expected_keys = set(EXPECTED_RUNTIME_REPORT) | {
        "schema_version",
        "mode",
        "device_count",
        "devices",
        "status",
    }
    _exact_keys(report, expected_keys, "CUDA runtime report")
    for key, value in EXPECTED_RUNTIME_REPORT.items():
        if report[key] != value:
            raise GpuPreflightError(f"CUDA runtime report {key} does not match")
    if (
        type(report["schema_version"]) is not int
        or report["schema_version"] != 1
        or report["mode"] != "runtime"
        or report["status"] != "passed"
        or type(report["device_count"]) is not int
        or report["device_count"] != 1
    ):
        raise GpuPreflightError("CUDA runtime probe did not pass exactly")
    devices = report["devices"]
    if not isinstance(devices, list) or len(devices) != 1:
        raise GpuPreflightError("CUDA runtime report must contain one device")
    device = devices[0]
    if not isinstance(device, dict):
        raise GpuPreflightError("CUDA runtime device report is malformed")
    _exact_keys(device, {"index", "uuid", "name", "compute_capability"}, "GPU")
    if (
        type(device["index"]) is not int
        or device["index"] != 0
        or device["uuid"] != gpu_uuid
        or not isinstance(device["name"], str)
        or not device["name"].strip()
        or not isinstance(device["compute_capability"], str)
        or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", device["compute_capability"])
    ):
        raise GpuPreflightError("CUDA runtime device report does not match")
    return report


def run_preflight(
    config: dict[str, Any],
    *,
    image: str,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    image = validate_image_reference(image)
    toolkit_output = _run_checked(
        runner,
        ["nvidia-ctk", "--version"],
        description="NVIDIA Container Toolkit version check",
    )
    toolkit_version = _toolkit_version(
        "\n".join((toolkit_output.stdout, toolkit_output.stderr))
    )
    if toolkit_version != config["nvidia_container_toolkit_version"]:
        raise GpuPreflightError("NVIDIA Container Toolkit version does not match")

    runtimes_raw = _run_checked(
        runner,
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        description="Docker NVIDIA runtime inspection",
    ).stdout.strip()
    try:
        runtimes = json.loads(runtimes_raw)
    except json.JSONDecodeError:
        raise GpuPreflightError("Docker runtime inventory is invalid") from None
    if not isinstance(runtimes, dict) or "nvidia" not in runtimes:
        raise GpuPreflightError("Docker NVIDIA runtime is not configured")

    inventory_raw = _run_checked(
        runner,
        [
            "nvidia-smi",
            "--query-gpu=uuid,driver_version",
            "--format=csv,noheader,nounits",
        ],
        description="host NVIDIA GPU inventory",
    ).stdout
    inventory = _host_gpu_inventory(inventory_raw)
    for gpu_uuid in config["gpu_uuids"]:
        if inventory.get(gpu_uuid) != config["nvidia_driver_version"]:
            raise GpuPreflightError(
                "configured GPU is missing or its driver version does not match"
            )

    image_id, repository_digests = _verify_image_identity(image, runner)
    probes: list[dict[str, Any]] = []
    for gpu_uuid in config["gpu_uuids"]:
        result = _run_checked(
            runner,
            _runtime_probe_command(image, gpu_uuid),
            description=f"CUDA runtime probe for {gpu_uuid}",
            timeout=120,
        )
        probes.append(_validate_runtime_report(result.stdout, gpu_uuid))
    return {
        "schema_version": 1,
        "status": "passed",
        "image": image,
        "image_id": image_id,
        "repository_digests": repository_digests,
        "nvidia_driver_version": config["nvidia_driver_version"],
        "nvidia_container_toolkit_version": toolkit_version,
        "gpu_uuids": config["gpu_uuids"],
        "runtime_contract": ACCELERATOR_CONTRACT,
        "probes": probes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image")
    mode.add_argument("--print-device-id", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_gpu_config(args.config)
        if args.print_device_id:
            print(config["gpu_uuids"][0])
            return 0
        missing = [
            command
            for command in ("docker", "nvidia-ctk", "nvidia-smi")
            if shutil.which(command) is None
        ]
        if missing:
            raise GpuPreflightError(f"missing required commands: {', '.join(missing)}")
        report = run_preflight(config, image=args.image)
    except GpuPreflightError as exc:
        print(f"GPU production preflight failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
