from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


HOST_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = HOST_ROOT.parents[1]
sys.path.insert(0, str(HOST_ROOT))

from check_gpu_runtime import (  # noqa: E402
    ACCELERATOR_CONTRACT,
    EXPECTED_RUNTIME_REPORT,
    GpuPreflightError,
    load_gpu_config,
    main,
    run_preflight,
    validate_image_reference,
)


GPU_UUID = "GPU-12345678-1234-5678-9abc-123456789abc"
GPU_UUID_2 = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
IMAGE_ID = "sha256:" + "a" * 64


def runtime_report(gpu_uuids: list[str] | None = None) -> dict[str, object]:
    gpu_uuids = gpu_uuids or [GPU_UUID]
    return {
        **EXPECTED_RUNTIME_REPORT,
        "schema_version": 1,
        "mode": "runtime",
        "device_count": len(gpu_uuids),
        "devices": [
            {
                "index": index,
                "uuid": gpu_uuid,
                "name": "Test GPU",
                "compute_capability": "8.0",
            }
            for index, gpu_uuid in enumerate(gpu_uuids)
        ],
        "status": "passed",
    }


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.runtimes: object = {"io.containerd.runc.v2": {}, "nvidia": {}}
        self.gpu_uuids = [GPU_UUID]
        self.probe_report: dict[str, object] | None = None

    def __call__(
        self, args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if args == ["nvidia-ctk", "--version"]:
            stdout = "NVIDIA Container Toolkit CLI version 1.17.4\n"
        elif args == ["docker", "info", "--format", "{{json .Runtimes}}"]:
            stdout = json.dumps(self.runtimes)
        elif args[0] == "nvidia-smi":
            stdout = "".join(
                f"{gpu_uuid}, 570.124.06\n" for gpu_uuid in self.gpu_uuids
            )
        elif args[:5] == [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
        ]:
            stdout = IMAGE_ID + "\n"
        elif args[:5] == [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{json .RepoDigests}}",
        ]:
            stdout = "[]\n"
        elif args[:2] == ["docker", "run"]:
            device_argument = next(
                argument
                for argument in args
                if argument.strip('"').startswith("device=")
            )
            requested = (
                device_argument.strip('"').removeprefix("device=").split(",")
            )
            stdout = json.dumps(self.probe_report or runtime_report(requested)) + "\n"
        else:
            raise AssertionError(f"unexpected command: {args}")
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=stdout, stderr=""
        )


class GpuConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "schema_version": 1,
            "nvidia_driver_version": "570.124.06",
            "nvidia_container_toolkit_version": "1.17.4",
            "gpu_uuids": [GPU_UUID],
        }

    def _load(self, config: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "gpu.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            os.chmod(path, 0o600)
            return load_gpu_config(path)

    def test_config_requires_canonical_uuid_and_exact_host_versions(self) -> None:
        self.assertEqual(self._load(self.config), self.config)
        invalid_values = (
            ("schema_version", True),
            ("gpu_uuids", []),
            ("gpu_uuids", [GPU_UUID.upper()]),
            ("gpu_uuids", [GPU_UUID, GPU_UUID]),
            ("nvidia_driver_version", "latest"),
            ("nvidia_container_toolkit_version", "latest"),
        )
        for key, value in invalid_values:
            with self.subTest(key=key, value=value):
                candidate = self.config.copy()
                candidate[key] = value
                with self.assertRaises(GpuPreflightError):
                    self._load(candidate)

        multiple = {**self.config, "gpu_uuids": [GPU_UUID, GPU_UUID_2]}
        self.assertEqual(self._load(multiple), multiple)
        unsorted = {**self.config, "gpu_uuids": [GPU_UUID_2, GPU_UUID]}
        self.assertEqual(self._load(unsorted), multiple)

    def test_runtime_image_argument_must_be_immutable(self) -> None:
        self.assertEqual(validate_image_reference(IMAGE_ID), IMAGE_ID)
        repository_digest = "registry.example/runtime@sha256:" + "b" * 64
        self.assertEqual(validate_image_reference(repository_digest), repository_digest)
        for image in (
            "registry.example/runtime:latest",
            "REPLACE_WITH_IMMUTABLE_CUDA_IMAGE_ID_OR_DIGEST",
            "sha256:short",
        ):
            with self.subTest(image=image), self.assertRaises(GpuPreflightError):
                validate_image_reference(image)

    def test_group_writable_or_symlink_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            root = Path(directory)
            path = root / "gpu.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            os.chmod(path, 0o620)
            with self.assertRaisesRegex(GpuPreflightError, "writable"):
                load_gpu_config(path)
            os.chmod(path, 0o600)
            link = root / "link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(GpuPreflightError, "symlink"):
                load_gpu_config(link)

    def test_print_device_id_validates_config_without_host_commands(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "gpu.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            os.chmod(path, 0o600)
            output = StringIO()
            with (
                patch(
                    "check_gpu_runtime.shutil.which",
                    side_effect=AssertionError("host command lookup must not run"),
                ),
                redirect_stdout(output),
            ):
                status = main(["--config", str(path), "--print-device-id"])
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue(), GPU_UUID + "\n")

    def test_print_device_ids_supports_a_validated_pool(self) -> None:
        config = {**self.config, "gpu_uuids": [GPU_UUID, GPU_UUID_2]}
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "gpu.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            os.chmod(path, 0o600)
            output = StringIO()
            with redirect_stdout(output):
                status = main(["--config", str(path), "--print-device-ids"])
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue(), f"{GPU_UUID},{GPU_UUID_2}\n")

            error = StringIO()
            with redirect_stderr(error):
                status = main(["--config", str(path), "--print-device-id"])
            self.assertEqual(status, 1)
            self.assertIn("use --print-device-ids", error.getvalue())

    def test_cli_requires_exactly_one_output_or_probe_mode(self) -> None:
        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as missing:
            main(["--config", "/does/not/matter"])
        self.assertEqual(missing.exception.code, 2)
        with redirect_stderr(error), self.assertRaises(SystemExit) as conflicting:
            main(
                [
                    "--config",
                    "/does/not/matter",
                    "--print-device-id",
                    "--image",
                    IMAGE_ID,
                ]
            )
        self.assertEqual(conflicting.exception.code, 2)


class GpuPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "schema_version": 1,
            "nvidia_driver_version": "570.124.06",
            "nvidia_container_toolkit_version": "1.17.4",
            "gpu_uuids": [GPU_UUID],
        }

    def test_preflight_checks_toolkit_runtime_identity_and_real_tensor_probe(
        self,
    ) -> None:
        runner = FakeRunner()
        report = run_preflight(self.config, image=IMAGE_ID, runner=runner)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["runtime_contract"], ACCELERATOR_CONTRACT)
        probe = next(
            command for command in runner.commands if command[:2] == ["docker", "run"]
        )
        for required in (
            "--pull=never",
            "--network=none",
            "--read-only",
            "--user=1000:100",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            f"device={GPU_UUID}",
            f"PLATFORM_NVIDIA_GPU_DEVICE_IDS={GPU_UUID}",
            "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
            "--entrypoint=/opt/conda/envs/python312/bin/python",
            "--require-gpu",
        ):
            self.assertIn(required, probe)
        self.assertNotIn("--runtime=nvidia", probe)

    def test_preflight_probes_each_gpu_and_the_complete_pool(self) -> None:
        runner = FakeRunner()
        runner.gpu_uuids = [GPU_UUID, GPU_UUID_2]
        config = {**self.config, "gpu_uuids": runner.gpu_uuids}

        report = run_preflight(config, image=IMAGE_ID, runner=runner)

        probe_commands = [
            command
            for command in runner.commands
            if command[:2] == ["docker", "run"]
        ]
        self.assertEqual(len(probe_commands), 3)
        self.assertEqual(
            {
                next(
                    value
                    for value in command
                    if value.strip('"').startswith("device=")
                )
                for command in probe_commands
            },
            {
                f"device={GPU_UUID}",
                f"device={GPU_UUID_2}",
                f'"device={GPU_UUID},{GPU_UUID_2}"',
            },
        )
        self.assertEqual(len(report["probes"]), 2)
        self.assertEqual(report["pool_probe"]["device_count"], 2)
        self.assertEqual(report["runtime_contract"]["count"], 2)

    def test_missing_docker_nvidia_runtime_fails_closed(self) -> None:
        runner = FakeRunner()
        runner.runtimes = {"io.containerd.runc.v2": {}}
        with self.assertRaisesRegex(GpuPreflightError, "not configured"):
            run_preflight(self.config, image=IMAGE_ID, runner=runner)
        self.assertFalse(
            any(command[:2] == ["docker", "run"] for command in runner.commands)
        )

    def test_runtime_uuid_or_framework_drift_fails_closed(self) -> None:
        for key, value in (
            (
                "devices",
                [
                    {
                        **runtime_report()["devices"][0],
                        "uuid": GPU_UUID_2,
                    }
                ],
            ),
            ("framework_build_version", "2.7.1+cpu"),
            ("device_count", 0),
            ("device_count", True),
        ):
            with self.subTest(key=key):
                runner = FakeRunner()
                runner.probe_report = runtime_report()
                runner.probe_report[key] = value
                with self.assertRaises(GpuPreflightError):
                    run_preflight(self.config, image=IMAGE_ID, runner=runner)

    def test_toolkit_and_driver_versions_are_exact(self) -> None:
        wrong_toolkit = self.config.copy()
        wrong_toolkit["nvidia_container_toolkit_version"] = "1.17.3"
        with self.assertRaisesRegex(GpuPreflightError, "Toolkit version"):
            run_preflight(wrong_toolkit, image=IMAGE_ID, runner=FakeRunner())

        wrong_driver = self.config.copy()
        wrong_driver["nvidia_driver_version"] = "570.124.05"
        with self.assertRaisesRegex(GpuPreflightError, "driver version"):
            run_preflight(wrong_driver, image=IMAGE_ID, runner=FakeRunner())


if __name__ == "__main__":
    unittest.main()
