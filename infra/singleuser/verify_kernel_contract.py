#!/opt/conda/bin/python
"""Verify the digest-bound profile against the running image before Hub startup."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from jupyter_client.kernelspec import KernelSpecManager


KERNEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PYTHON_VERSION_RE = re.compile(
    r"^(?:[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
PYTHON_EXECUTABLE_RE = re.compile(
    r"^/opt/conda(?:/envs/[a-z][a-z0-9_-]{0,63})?/bin/python$"
)


class KernelContractError(RuntimeError):
    """The image does not implement the selected immutable profile."""


def _load_contract() -> dict[str, object]:
    raw = os.environ.get("PLATFORM_KERNEL_CONTRACT", "")
    if not raw or len(raw) > 16 * 1024:
        raise KernelContractError("kernel contract is missing or too large")
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError:
        raise KernelContractError("kernel contract is not valid JSON") from None
    if not isinstance(contract, dict) or set(contract) != {
        "schema_version",
        "python_version",
        "kernels",
        "default_kernel",
    }:
        raise KernelContractError("kernel contract schema mismatch")
    if contract["schema_version"] != 1:
        raise KernelContractError("kernel contract version mismatch")
    version = contract["python_version"]
    if not isinstance(version, str) or not PYTHON_VERSION_RE.fullmatch(version):
        raise KernelContractError("kernel Python version is invalid")
    kernels = contract["kernels"]
    if not isinstance(kernels, list) or not kernels:
        raise KernelContractError("kernel contract contains no kernels")
    names: list[str] = []
    for kernel in kernels:
        if not isinstance(kernel, dict) or set(kernel) != {
            "name",
            "display_name",
            "language",
            "python_version",
            "executable",
        }:
            raise KernelContractError("kernel item schema mismatch")
        name = kernel["name"]
        display_name = kernel["display_name"]
        if not isinstance(name, str) or not KERNEL_NAME_RE.fullmatch(name):
            raise KernelContractError("kernel name is invalid")
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 128:
            raise KernelContractError("kernel display name is invalid")
        if kernel["language"] != "python":
            raise KernelContractError("non-Python kernel is not supported")
        kernel_version = kernel["python_version"]
        if not isinstance(kernel_version, str) or not PYTHON_VERSION_RE.fullmatch(
            kernel_version
        ):
            raise KernelContractError("kernel Python version is invalid")
        executable = kernel["executable"]
        if not isinstance(executable, str) or not PYTHON_EXECUTABLE_RE.fullmatch(
            executable
        ):
            raise KernelContractError("kernel executable is not allowlisted")
        names.append(name)
    if names != sorted(set(names)):
        raise KernelContractError("kernel names are not unique and sorted")
    if contract["default_kernel"] not in names:
        raise KernelContractError("default kernel is not allowlisted")
    default = next(
        kernel for kernel in kernels if kernel["name"] == contract["default_kernel"]
    )
    if default["python_version"] != version:
        raise KernelContractError(
            "profile Python version does not match the default kernel"
        )
    return contract


def _actual_python_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [
                executable,
                "-I",
                "-c",
                "import sys; print('.'.join(map(str, sys.version_info[:3])))",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise KernelContractError(
            "a kernel interpreter could not be verified"
        ) from None
    return result.stdout.strip()


def verify_contract(contract: dict[str, object]) -> None:
    specifications = KernelSpecManager().get_all_specs()
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
    for expected in contract["kernels"]:  # type: ignore[union-attr]
        name = expected["name"]
        actual = specifications.get(name)
        if not isinstance(actual, dict):
            raise KernelContractError("an allowlisted kernelspec is missing")
        resource_dir = actual.get("resource_dir")
        expected_dir = Path("/opt/conda/share/jupyter/kernels") / name
        try:
            if Path(str(resource_dir)).resolve(strict=True) != expected_dir.resolve(
                strict=True
            ):
                raise KernelContractError("a kernelspec came from an untrusted path")
        except OSError:
            raise KernelContractError("a kernelspec path is unavailable") from None
        spec = actual.get("spec")
        if not isinstance(spec, dict):
            raise KernelContractError("a kernelspec is malformed")
        if (
            spec.get("display_name") != expected["display_name"]
            or spec.get("language") != expected["language"]
        ):
            raise KernelContractError("kernelspec metadata does not match the profile")
        argv = spec.get("argv")
        if not isinstance(argv, list) or tuple(argv[1:]) not in reviewed_arguments:
            raise KernelContractError(
                "a kernel command is not the reviewed ipykernel form"
            )
        executable = argv[0]
        if executable != expected["executable"]:
            raise KernelContractError(
                "a kernel interpreter path does not match the profile"
            )
        if _actual_python_version(executable) != expected["python_version"]:
            raise KernelContractError(
                "a kernel Python version does not match the profile"
            )


def main() -> int:
    try:
        contract = _load_contract()
        if os.environ.get("PLATFORM_DEFAULT_KERNEL") != contract["default_kernel"]:
            raise KernelContractError(
                "default kernel environment does not match the contract"
            )
        default = next(
            kernel
            for kernel in contract["kernels"]
            if kernel["name"] == contract["default_kernel"]
        )
        executable = default["executable"]
        if os.environ.get("PLATFORM_PYTHON_EXECUTABLE") != executable:
            raise KernelContractError(
                "default Python executable environment does not match the contract"
            )
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if not path_entries or path_entries[0] != str(Path(executable).parent):
            raise KernelContractError("default Python bin is not first on PATH")
        verify_contract(contract)
    except KernelContractError as exc:
        print(f"platform kernel verification failed: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
