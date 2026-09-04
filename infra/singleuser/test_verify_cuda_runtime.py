from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_cuda_runtime as cuda_runtime  # noqa: E402
from verify_cuda_runtime import (  # noqa: E402
    CudaRuntimeContractError,
    EXPECTED_ACCELERATOR_CONTRACT,
    EXPECTED_KERNEL_CONTRACT,
    EXPECTED_PYTHON_EXECUTABLE,
    assigned_gpu_uuid,
    assigned_gpu_uuids,
    load_accelerator_contract,
    validate_image_environment,
    validate_profile_kernel_contract,
    verify_gpu,
    verify_metadata,
)


GPU_UUID = "GPU-12345678-1234-5678-9abc-123456789abc"
GPU_UUID_2 = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class _Tensor:
    def transpose(self, first: int, second: int) -> "_Tensor":
        if (first, second) != (0, 1):
            raise AssertionError("unexpected transpose")
        return self

    def item(self) -> float:
        return 5.0


class _Cuda:
    def __init__(self, *, available: bool = True, count: int = 1) -> None:
        self.available = available
        self.count = count
        self.synchronized: list[int] = []

    def is_available(self) -> bool:
        return self.available

    def device_count(self) -> int:
        return self.count

    def get_device_properties(self, index: int) -> SimpleNamespace:
        if index < 0 or index >= self.count:
            raise AssertionError("unexpected GPU index")
        return SimpleNamespace(name="Test GPU", major=8, minor=0)

    def synchronize(self, index: int) -> None:
        self.synchronized.append(index)


class _Torch:
    __version__ = "2.7.1+cu126"
    version = SimpleNamespace(cuda="12.6", hip=None)
    backends = SimpleNamespace(cuda=SimpleNamespace(is_built=lambda: True))

    def __init__(self, *, available: bool = True, count: int = 1) -> None:
        self.cuda = _Cuda(available=available, count=count)
        self.tensor_devices: list[str] = []

    def tensor(self, value: object, *, device: str) -> _Tensor:
        if value != [[1.0, 2.0]] or not re.fullmatch(r"cuda:[0-9]+", device):
            raise AssertionError("unexpected tensor")
        self.tensor_devices.append(device)
        return _Tensor()

    @staticmethod
    def matmul(left: _Tensor, right: _Tensor) -> _Tensor:
        if not isinstance(left, _Tensor) or not isinstance(right, _Tensor):
            raise AssertionError("unexpected matrix")
        return _Tensor()


class CudaRuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.dumps(EXPECTED_ACCELERATOR_CONTRACT, separators=(",", ":"))

    def test_accelerator_contract_is_exact(self) -> None:
        loaded = load_accelerator_contract(
            {"PLATFORM_ACCELERATOR_CONTRACT": self.contract}
        )
        self.assertEqual(loaded, EXPECTED_ACCELERATOR_CONTRACT)

        multi_contract = {
            **EXPECTED_ACCELERATOR_CONTRACT,
            "count": 2,
        }
        self.assertEqual(
            load_accelerator_contract(
                {"PLATFORM_ACCELERATOR_CONTRACT": json.dumps(multi_contract)}
            ),
            multi_contract,
        )

        for mutation in (
            "",
            "[]",
            self.contract.replace('"count":1', '"count":0'),
            self.contract.replace('"count":1', '"count":65'),
            self.contract.replace('"count":1', '"count":true'),
            self.contract[:-1] + ',"extra":true}',
        ):
            with self.subTest(mutation=mutation), self.assertRaises(
                CudaRuntimeContractError
            ):
                load_accelerator_contract({"PLATFORM_ACCELERATOR_CONTRACT": mutation})

    def test_assignment_requires_one_canonical_physical_uuid(self) -> None:
        self.assertEqual(
            assigned_gpu_uuid({"PLATFORM_NVIDIA_GPU_DEVICE_IDS": GPU_UUID}),
            GPU_UUID,
        )
        for value in (
            "",
            GPU_UUID.upper(),
            f"{GPU_UUID},{GPU_UUID}",
            "MIG-12345678-1234-5678-9abc-123456789abc",
            "0",
        ):
            with self.subTest(value=value), self.assertRaises(CudaRuntimeContractError):
                assigned_gpu_uuid({"PLATFORM_NVIDIA_GPU_DEVICE_IDS": value})

    def test_assignment_requires_the_exact_sorted_gpu_set(self) -> None:
        self.assertEqual(
            assigned_gpu_uuids(
                {
                    "PLATFORM_NVIDIA_GPU_DEVICE_IDS": (
                        f"{GPU_UUID},{GPU_UUID_2}"
                    )
                },
                expected_count=2,
            ),
            [GPU_UUID, GPU_UUID_2],
        )
        for value, count in (
            (GPU_UUID, 2),
            (f"{GPU_UUID_2},{GPU_UUID}", 2),
            (f"{GPU_UUID},{GPU_UUID}", 2),
            (f"{GPU_UUID},{GPU_UUID_2}", 1),
        ):
            with self.subTest(value=value, count=count), self.assertRaises(
                CudaRuntimeContractError
            ):
                assigned_gpu_uuids(
                    {"PLATFORM_NVIDIA_GPU_DEVICE_IDS": value},
                    expected_count=count,
                )

    def test_image_environment_pins_driver_capabilities_and_kernel_search_path(
        self,
    ) -> None:
        expected = {
            "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
            "JUPYTER_PATH": "/opt/conda/share/jupyter",
        }
        validate_image_environment(expected)
        for key, value in (
            ("NVIDIA_DRIVER_CAPABILITIES", "all"),
            ("JUPYTER_PATH", "/home/jovyan/.local/share/jupyter"),
        ):
            changed = expected.copy()
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(CudaRuntimeContractError):
                validate_image_environment(changed)

    def test_supplied_profile_kernel_contract_must_match_cuda_image(self) -> None:
        environment = {
            "PLATFORM_KERNEL_CONTRACT": json.dumps(EXPECTED_KERNEL_CONTRACT),
            "PLATFORM_DEFAULT_KERNEL": "python312-cuda",
            "PLATFORM_PYTHON_EXECUTABLE": EXPECTED_PYTHON_EXECUTABLE,
            "PATH": f"{Path(EXPECTED_PYTHON_EXECUTABLE).parent}:/usr/bin",
        }
        validate_profile_kernel_contract(environment)

        for field, value in (
            ("PLATFORM_DEFAULT_KERNEL", "python312"),
            ("PLATFORM_PYTHON_EXECUTABLE", "/opt/conda/bin/python"),
            ("PATH", "/opt/conda/bin:/usr/bin"),
        ):
            changed = environment.copy()
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(CudaRuntimeContractError):
                validate_profile_kernel_contract(changed)

        changed_contract = json.loads(environment["PLATFORM_KERNEL_CONTRACT"])
        changed_contract["kernels"][0]["name"] = "python312"
        environment["PLATFORM_KERNEL_CONTRACT"] = json.dumps(changed_contract)
        with self.assertRaisesRegex(CudaRuntimeContractError, "does not match"):
            validate_profile_kernel_contract(environment)

    def test_host_probe_may_omit_profile_kernel_contract(self) -> None:
        validate_profile_kernel_contract({})

    def test_metadata_pins_python_torch_cuda_and_kernel(self) -> None:
        with (
            patch("verify_cuda_runtime.sys.executable", EXPECTED_PYTHON_EXECUTABLE),
            patch(
                "verify_cuda_runtime.platform.python_version", return_value="3.12.13"
            ),
            patch("verify_cuda_runtime._verify_kernel"),
        ):
            report = verify_metadata(_Torch())
        self.assertEqual(report["framework_build_version"], "2.7.1+cu126")
        self.assertEqual(report["cuda_version"], "12.6")
        self.assertEqual(report["kernel"], "python312-cuda")

        drifted = _Torch()
        drifted.__version__ = "2.7.1+cpu"
        with (
            patch("verify_cuda_runtime.sys.executable", EXPECTED_PYTHON_EXECUTABLE),
            patch(
                "verify_cuda_runtime.platform.python_version", return_value="3.12.13"
            ),
            self.assertRaisesRegex(CudaRuntimeContractError, "wheel build"),
        ):
            verify_metadata(drifted)

    def test_cuda_kernel_inventory_rejects_an_inherited_cpu_kernel(self) -> None:
        manager = SimpleNamespace(
            get_all_specs=lambda: {
                "python312-cuda": {},
                "python3": {},
            }
        )
        with (
            patch("verify_cuda_runtime.KernelSpecManager", return_value=manager),
            self.assertRaisesRegex(CudaRuntimeContractError, "only the reviewed"),
        ):
            cuda_runtime._verify_kernel()

    def test_runtime_proves_one_assigned_gpu_and_executes_tensor(self) -> None:
        torch = _Torch()

        def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=GPU_UUID + "\n",
                stderr="",
            )

        devices = verify_gpu(torch, GPU_UUID, runner=runner)
        self.assertEqual(
            devices,
            [
                {
                    "index": 0,
                    "uuid": GPU_UUID,
                    "name": "Test GPU",
                    "compute_capability": "8.0",
                }
            ],
        )
        self.assertEqual(torch.cuda.synchronized, [0])
        self.assertEqual(torch.tensor_devices, ["cuda:0"])

    def test_runtime_executes_a_tensor_probe_on_every_assigned_gpu(self) -> None:
        torch = _Torch(count=2)

        def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=f"{GPU_UUID}\n{GPU_UUID_2}\n",
                stderr="",
            )

        devices = verify_gpu(torch, [GPU_UUID, GPU_UUID_2], runner=runner)

        self.assertEqual([device["index"] for device in devices], [0, 1])
        self.assertEqual([device["uuid"] for device in devices], [GPU_UUID, GPU_UUID_2])
        self.assertEqual(torch.cuda.synchronized, [0, 1])
        self.assertEqual(torch.tensor_devices, ["cuda:0", "cuda:1"])

    def test_runtime_rejects_device_count_or_uuid_drift(self) -> None:
        def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n",
                stderr="",
            )

        with self.assertRaisesRegex(CudaRuntimeContractError, "assigned GPU count"):
            verify_gpu(_Torch(count=2), GPU_UUID, runner=runner)
        with self.assertRaisesRegex(CudaRuntimeContractError, "assignment"):
            verify_gpu(_Torch(), GPU_UUID, runner=runner)

    def test_cuda_dockerfile_is_separate_hashed_and_gpu_optional_at_build(self) -> None:
        root = Path(__file__).resolve().parent
        dockerfile = (root / "Dockerfile.cuda").read_text(encoding="utf-8")
        requirements = (root / "requirements.cuda.txt").read_text(encoding="utf-8")
        wrapper = (root / "platform-singleuser").read_text(encoding="utf-8")

        self.assertIn("cuda-base-REPLACE_WITH_CPU_IMAGE_ID", dockerfile)
        self.assertIn("ARG CUDA_SINGLEUSER_BASE_IMAGE_ID=sha256:", dockerfile)
        self.assertIn("base tag is not bound to its image ID", dockerfile)
        self.assertIn("io.team-workspace.cpu-base.image-id", dockerfile)
        self.assertIn("torch-2.7.1%2Bcu126-cp312-cp312", requirements)
        self.assertIn(
            "sha256=63bce0590bc540fc16139e2be0177847585182b8c5e68d7f9213789d1d96c978",
            requirements,
        )
        self.assertIn("python312-cuda", dockerfile)
        self.assertIn("JUPYTER_PATH=/opt/conda/share/jupyter", dockerfile)
        self.assertIn("--metadata-only", dockerfile)
        self.assertNotIn("--require-gpu", dockerfile)
        self.assertIn('if [ -n "${workspace_id}" ]', wrapper)
        self.assertLess(
            wrapper.index("--require-gpu"), wrapper.index("--metadata-only")
        )
        self.assertIn('[ -n "${accelerator_contract}" ]', wrapper)
        self.assertIn('[ -n "${nvidia_gpu_device_ids}" ]', wrapper)
        self.assertIn('[ -n "${nvidia_driver_capabilities}" ]', wrapper)
        self.assertIn("accelerator verifier is unavailable", wrapper)
        self.assertIn("--KernelSpecManager.ensure_native_kernel=False", wrapper)
        self.assertIn(
            'set -- "--KernelSpecManager.allowed_kernelspecs=${PLATFORM_DEFAULT_KERNEL}" "$@"',
            wrapper,
        )
        self.assertLess(
            dockerfile.index("verify_kernel_contract.py"),
            dockerfile.index("verify_cuda_runtime.py --metadata-only"),
        )


if __name__ == "__main__":
    unittest.main()
