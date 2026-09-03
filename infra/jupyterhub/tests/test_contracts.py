from __future__ import annotations

import asyncio
import ast
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[1]
sys.path.insert(0, str(ROOT))

from docker import APIClient  # noqa: E402
import nativeauth_compat  # noqa: E402
from platform_spawner import PlatformDockerSpawnerMixin  # noqa: E402
from nativeauth_compat import apply_native_login_render_compatibility  # noqa: E402
from profile_policy import (  # noqa: E402
    ProfilePolicyError,
    kernel_runtime_environment,
    load_profile_policy,
    profile_digest,
)
from profile_image_check import (  # noqa: E402
    check_profile_images,
    docker_verification_command,
)
from generate_local_profile_matrix import generate_document  # noqa: E402
from generate_production_profile_policy import generate_policy  # noqa: E402
from rbac_policy import (  # noqa: E402
    PLATFORM_ADMIN_LIFECYCLE_SERVICE,
    PLATFORM_API_SERVICE,
    PLATFORM_RECONCILER_SERVICE,
    platform_load_roles,
    validate_builtin_admin_browser_access,
    validate_singleuser_browser_oauth_contract,
)
import spawn_guard  # noqa: E402


GPU_DEVICE_ID = "GPU-01234567-89ab-cdef-0123-456789abcdef"
CPU_ACCELERATOR = {
    "kind": "none",
    "count": 0,
    "sharing": "none",
    "cuda_version": None,
    "framework": None,
    "framework_version": None,
}
NVIDIA_ACCELERATOR = {
    "kind": "nvidia",
    "count": 1,
    "sharing": "exclusive",
    "cuda_version": "12.6",
    "framework": "pytorch",
    "framework_version": "2.7.1",
}


def schema_v3_profile(
    source: dict[str, object], *, nvidia: bool, profile_id: str | None = None
) -> dict[str, object]:
    profile = deepcopy(source)
    profile["accelerator"] = deepcopy(NVIDIA_ACCELERATOR if nvidia else CPU_ACCELERATOR)
    if profile_id is not None:
        profile["id"] = profile_id
    if nvidia:
        profile["python_version"] = "3.12.13"
        profile["kernels"] = [
            {
                "name": "python312-cuda",
                "display_name": "Python 3.12 (PyTorch CUDA 12.6)",
                "language": "python",
                "python_version": "3.12.13",
                "executable": "/opt/conda/envs/python312/bin/python",
            }
        ]
        profile["default_kernel"] = "python312-cuda"
    profile["config_digest"] = profile_digest(profile)
    return profile


class ProfilePolicyTests(unittest.TestCase):
    def test_production_policy_accepts_exact_local_image_id(self) -> None:
        document = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        policy = generate_policy(
            template=document,
            image_id="sha256:" + "a" * 64,
            shared_volume_name="jupyter-shared",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "production.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            loaded = load_profile_policy(path)

        self.assertEqual(len(loaded["profiles"]), 18)
        self.assertEqual(
            {profile["image"] for profile in loaded["profiles"].values()},
            {"sha256:" + "a" * 64},
        )

    def test_production_policy_generation_retains_old_runtime_versions(self) -> None:
        document = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        first = generate_policy(
            template=document,
            image_id="sha256:" + "a" * 64,
            shared_volume_name="jupyter-shared",
        )
        second = generate_policy(
            template=document,
            image_id="sha256:" + "b" * 64,
            shared_volume_name="jupyter-shared",
            previous=first,
        )

        self.assertEqual(len(second["profiles"]), 36)
        for profile_id in {row["id"] for row in first["profiles"]}:
            versions = [row for row in second["profiles"] if row["id"] == profile_id]
            self.assertEqual([row["version"] for row in versions], [1, 2])
            self.assertFalse(versions[0]["selectable"])
            self.assertTrue(versions[0]["enabled"])
            self.assertTrue(versions[1]["selectable"])

    def test_production_policy_generation_is_idempotent_for_same_image(self) -> None:
        document = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        first = generate_policy(
            template=document,
            image_id="sha256:" + "a" * 64,
            shared_volume_name="jupyter-shared",
        )
        repeated = generate_policy(
            template=document,
            image_id="sha256:" + "a" * 64,
            shared_volume_name="jupyter-shared",
            previous=first,
        )

        self.assertEqual(repeated, first)

    def test_production_policy_candidate_is_atomically_promoted(self) -> None:
        document = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        candidate = generate_policy(
            template=document,
            image_id="sha256:" + "a" * 64,
            shared_volume_name="jupyter-shared",
        )
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = Path(directory) / "candidate.json"
            output_path = Path(directory) / "profiles.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "generate_production_profile_policy.py"),
                    "--promote",
                    str(candidate_path),
                    "--output",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(output_path.read_text()), candidate)
            self.assertEqual(os.stat(output_path).st_mode & 0o777, 0o640)
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)

    def test_production_policy_write_preserves_shared_setgid_parent(self) -> None:
        document = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        candidate = generate_policy(
            template=document,
            image_id="sha256:" + "a" * 64,
            shared_volume_name="jupyter-shared",
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "shared-state"
            parent.mkdir(mode=0o700)
            parent.chmod(0o2770)
            candidate_path = Path(directory) / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "generate_production_profile_policy.py"),
                    "--promote",
                    str(candidate_path),
                    "--output",
                    str(parent / "profiles.json"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(os.stat(parent).st_mode & 0o7777, 0o2770)
            self.assertEqual(os.stat(parent / "profiles.json").st_mode & 0o777, 0o640)

    def test_singleuser_runtime_restores_profile_path_and_health_interpreter(
        self,
    ) -> None:
        singleuser = ROOT.parent / "singleuser"
        wrapper = (singleuser / "platform-singleuser").read_text(encoding="utf-8")
        dockerfile = (singleuser / "Dockerfile").read_text(encoding="utf-8")

        self.assertLess(
            wrapper.index('PATH="${platform_python_bin}:${PATH:-}"'),
            wrapper.index("verify_kernel_contract.py"),
        )
        self.assertLess(
            wrapper.index("verify_kernel_contract.py"),
            wrapper.index("verify_private_storage.py"),
        )
        self.assertLess(
            wrapper.index("verify_private_storage.py"),
            wrapper.index("umask 0002"),
        )
        self.assertLess(
            wrapper.index("umask 0002"),
            wrapper.index("verify_shared_storage.py"),
        )
        self.assertLess(
            wrapper.index("verify_shared_storage.py"),
            wrapper.index("exec /opt/conda/bin/jupyterhub-singleuser"),
        )
        self.assertIn("verify_private_storage.py", dockerfile)
        self.assertIn("verify_shared_storage.py", dockerfile)
        self.assertIn(
            "CMD /opt/conda/bin/python /etc/jupyter/docker_healthcheck.py || exit 1",
            dockerfile,
        )

    def test_explicit_local_policy_loads_only_with_unsafe_flag(self) -> None:
        path = ROOT / "profiles.local-dev.json"
        with self.assertRaises(ProfilePolicyError):
            load_profile_policy(path)
        policy = load_profile_policy(path, allow_unsafe_images=True)
        self.assertIn(("python-local", 1), policy["profiles"])
        self.assertEqual(policy["schema_version"], 2)

    def test_v2_policy_exposes_full_python_cpu_memory_cross_product(self) -> None:
        policy = load_profile_policy(
            ROOT / "profiles.local-dev.json", allow_unsafe_images=True
        )
        selectable = [
            profile
            for profile in policy["profiles"].values()
            if profile["selectable"] is True
        ]

        self.assertEqual(len(selectable), 18)
        self.assertEqual(
            {
                (
                    profile["python_version"],
                    float(profile["cpu_limit"]),
                    profile["memory_limit_bytes"],
                )
                for profile in selectable
            },
            {
                (python, cpu, memory)
                for python in {"3.12.13", "3.13.14"}
                for cpu in {1.0, 2.0, 4.0}
                for memory in {1073741824, 2147483648, 4294967296}
            },
        )
        self.assertEqual(
            {profile["private_disk_hard_limit_bytes"] for profile in selectable},
            {1073741824},
        )
        self.assertEqual(
            {profile["private_disk_quota_enforced"] for profile in selectable},
            {False},
        )

    def test_v3_policy_requires_explicit_accelerator_for_selectable_profiles(
        self,
    ) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        cpu = schema_v3_profile(
            source["profiles"][1],
            nvidia=False,
            profile_id="python312-v3-cpu1-mem1024",
        )
        gpu = schema_v3_profile(
            source["profiles"][2],
            nvidia=True,
            profile_id="python312-cuda-cpu1-mem2048",
        )
        document = {
            "schema_version": 3,
            "shared_volume": source["shared_volume"],
            "profiles": [cpu, gpu],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = load_profile_policy(path, allow_unsafe_images=True)

        self.assertEqual(loaded["schema_version"], 3)
        self.assertEqual(
            loaded["profiles"][(gpu["id"], gpu["version"])]["accelerator"],
            NVIDIA_ACCELERATOR,
        )
        changed = deepcopy(gpu)
        changed["accelerator"]["cuda_version"] = "12.7"
        self.assertNotEqual(profile_digest(changed), gpu["config_digest"])

        historical = deepcopy(source["profiles"][1])
        historical["selectable"] = False
        document["profiles"] = [historical, cpu]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            load_profile_policy(path, allow_unsafe_images=True)

        historical["selectable"] = True
        document["profiles"] = [historical, cpu]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                ProfilePolicyError, "requires accelerator metadata"
            ):
                load_profile_policy(path, allow_unsafe_images=True)

    def test_v3_accelerator_combinations_fail_closed(self) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        gpu = schema_v3_profile(
            source["profiles"][1], nvidia=True, profile_id="python312-cuda"
        )
        invalid_profiles = []
        for field, value in (
            ("count", True),
            ("count", 2),
            ("sharing", "shared"),
            ("cuda_version", "12"),
            ("framework", "tensorflow"),
            ("framework_version", "2.7"),
        ):
            invalid = deepcopy(gpu)
            invalid["accelerator"][field] = value
            invalid["config_digest"] = profile_digest(invalid)
            invalid_profiles.append(invalid)
        multiple_kernels = deepcopy(gpu)
        multiple_kernels["kernels"].append(deepcopy(multiple_kernels["kernels"][0]))
        multiple_kernels["kernels"][1]["name"] = "python312-cuda-copy"
        multiple_kernels["config_digest"] = profile_digest(multiple_kernels)
        invalid_profiles.append(multiple_kernels)
        cpu_with_cuda = schema_v3_profile(source["profiles"][1], nvidia=False)
        cpu_with_cuda["accelerator"]["cuda_version"] = "12.6"
        cpu_with_cuda["config_digest"] = profile_digest(cpu_with_cuda)
        invalid_profiles.append(cpu_with_cuda)
        missing_field = deepcopy(gpu)
        missing_field["accelerator"].pop("framework_version")
        missing_field["config_digest"] = profile_digest(missing_field)
        invalid_profiles.append(missing_field)
        extra_field = deepcopy(gpu)
        extra_field["accelerator"]["device_id"] = GPU_DEVICE_ID
        extra_field["config_digest"] = profile_digest(extra_field)
        invalid_profiles.append(extra_field)

        for invalid in invalid_profiles:
            document = {
                "schema_version": 3,
                "shared_volume": source["shared_volume"],
                "profiles": [invalid],
            }
            with (
                self.subTest(accelerator=invalid["accelerator"]),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "profiles.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(ProfilePolicyError):
                    load_profile_policy(path, allow_unsafe_images=True)

    def test_v3_gpu_runtime_environment_is_an_exact_trusted_contract(self) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        gpu = schema_v3_profile(
            source["profiles"][1], nvidia=True, profile_id="python312-cuda"
        )
        environment = kernel_runtime_environment(gpu)
        self.assertEqual(
            json.loads(environment["PLATFORM_ACCELERATOR_CONTRACT"]),
            {"schema_version": 1, **NVIDIA_ACCELERATOR},
        )
        self.assertEqual(environment["NVIDIA_DRIVER_CAPABILITIES"], "compute,utility")
        self.assertNotIn("PLATFORM_NVIDIA_GPU_DEVICE_IDS", environment)

        cpu = schema_v3_profile(source["profiles"][1], nvidia=False)
        cpu_environment = kernel_runtime_environment(cpu)
        self.assertNotIn("PLATFORM_ACCELERATOR_CONTRACT", cpu_environment)
        self.assertNotIn("NVIDIA_DRIVER_CAPABILITIES", cpu_environment)

        from profile_policy import derive_resource_profile

        derived = derive_resource_profile(gpu, cpu_millicores=3_500, memory_mb=3_072)
        self.assertEqual(derived["accelerator"], NVIDIA_ACCELERATOR)
        self.assertEqual(derived["cpu_limit"], 3.5)
        self.assertNotEqual(derived["config_digest"], gpu["config_digest"])

    def test_v3_production_generator_pins_cpu_and_gpu_images_separately(self) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        cpu = schema_v3_profile(source["profiles"][1], nvidia=False)
        gpu = schema_v3_profile(
            source["profiles"][2],
            nvidia=True,
            profile_id="python312-cuda-cpu1-mem2048",
        )
        template = {
            "schema_version": 3,
            "shared_volume": source["shared_volume"],
            "profiles": [cpu, gpu],
        }
        cpu_image_id = "sha256:" + "a" * 64
        gpu_image_id = "sha256:" + "b" * 64
        policy = generate_policy(
            template=template,
            image_id=cpu_image_id,
            gpu_image_id=gpu_image_id,
            shared_volume_name="jupyter-shared",
        )
        by_kind = {row["accelerator"]["kind"]: row for row in policy["profiles"]}
        self.assertEqual(by_kind["none"]["image"], cpu_image_id)
        self.assertEqual(by_kind["nvidia"]["image"], gpu_image_id)
        cpu_only = generate_policy(
            template=template,
            image_id=cpu_image_id,
            shared_volume_name="jupyter-shared",
        )
        self.assertEqual(
            [row["accelerator"]["kind"] for row in cpu_only["profiles"]],
            ["none"],
        )
        with self.assertRaisesRegex(RuntimeError, "GPU history requires"):
            generate_policy(
                template=template,
                image_id=cpu_image_id,
                previous=policy,
                shared_volume_name="jupyter-shared",
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            load_profile_policy(path)

    def test_checked_in_production_template_is_cpu_only_without_opt_in(self) -> None:
        template = json.loads(
            (ROOT / "profiles.production-template.json").read_text(encoding="utf-8")
        )
        cpu_image_id = "sha256:" + "a" * 64

        policy = generate_policy(
            template=template,
            image_id=cpu_image_id,
            shared_volume_name="jupyter-shared",
        )

        self.assertEqual(policy["schema_version"], 3)
        self.assertEqual(len(policy["profiles"]), 3)
        self.assertEqual(
            {row["accelerator"]["kind"] for row in policy["profiles"]}, {"none"}
        )
        self.assertEqual({row["image"] for row in policy["profiles"]}, {cpu_image_id})
        self.assertFalse(
            any(
                kernel["name"] == "python312-cuda"
                for row in policy["profiles"]
                for kernel in row["kernels"]
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            load_profile_policy(path)

    def test_checked_in_production_template_gpu_opt_in_is_exact_and_idempotent(
        self,
    ) -> None:
        template = json.loads(
            (ROOT / "profiles.production-template.json").read_text(encoding="utf-8")
        )
        cpu_image_id = "sha256:" + "a" * 64
        gpu_image_id = "sha256:" + "b" * 64

        policy = generate_policy(
            template=template,
            image_id=cpu_image_id,
            gpu_image_id=gpu_image_id,
            shared_volume_name="jupyter-shared",
        )
        repeated = generate_policy(
            template=template,
            image_id=cpu_image_id,
            gpu_image_id=gpu_image_id,
            shared_volume_name="jupyter-shared",
            previous=policy,
        )

        self.assertEqual(repeated, policy)
        gpu_profiles = [
            row for row in policy["profiles"] if row["accelerator"]["kind"] == "nvidia"
        ]
        self.assertEqual(len(gpu_profiles), 1)
        gpu = gpu_profiles[0]
        self.assertEqual(gpu["image"], gpu_image_id)
        self.assertEqual(gpu["accelerator"], NVIDIA_ACCELERATOR)
        self.assertEqual(gpu["default_kernel"], "python312-cuda")
        self.assertEqual([row["name"] for row in gpu["kernels"]], ["python312-cuda"])
        self.assertEqual(
            gpu["kernels"][0]["executable"], "/opt/conda/envs/python312/bin/python"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            load_profile_policy(path)

    def test_v3_generation_retains_v2_cpu_history_with_original_digest(self) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        previous = generate_policy(
            template=source,
            image_id="sha256:" + "a" * 64,
            shared_volume_name="jupyter-shared",
        )
        current = schema_v3_profile(source["profiles"][1], nvidia=False)
        template = {
            "schema_version": 3,
            "shared_volume": source["shared_volume"],
            "profiles": [current],
        }
        generated = generate_policy(
            template=template,
            image_id="sha256:" + "a" * 64,
            shared_volume_name="jupyter-shared",
            previous=previous,
        )
        versions = [row for row in generated["profiles"] if row["id"] == current["id"]]
        self.assertEqual([row["version"] for row in versions], [1, 2])
        self.assertNotIn("accelerator", versions[0])
        self.assertFalse(versions[0]["selectable"])
        previous_version = next(
            row for row in previous["profiles"] if row["id"] == current["id"]
        )
        self.assertEqual(
            versions[0]["config_digest"], previous_version["config_digest"]
        )
        self.assertEqual(versions[1]["accelerator"], CPU_ACCELERATOR)
        self.assertTrue(versions[1]["selectable"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(generated), encoding="utf-8")
            load_profile_policy(path)

    def test_checked_in_local_policy_is_the_deterministic_reviewed_matrix(
        self,
    ) -> None:
        document = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        self.assertEqual(generate_document(document), document)

    def test_legacy_v1_document_and_digest_remain_compatible(self) -> None:
        current = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        legacy = deepcopy(current["profiles"][0])
        legacy.pop("selectable")
        document = {
            "schema_version": 1,
            "shared_volume": current["shared_volume"],
            "profiles": [legacy],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            policy = load_profile_policy(path, allow_unsafe_images=True)

        loaded = policy["profiles"][("python-local", 1)]
        self.assertEqual(
            loaded["config_digest"],
            "sha256:b89d38d699271991f14976b728436141f0cef43a1001a3d3833d4ca0d0ed7021",
        )

    def test_v2_legacy_profile_cannot_be_newly_selectable(self) -> None:
        document = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        document["profiles"] = [deepcopy(document["profiles"][0])]
        document["profiles"][0]["selectable"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ProfilePolicyError, "cannot be selectable"):
                load_profile_policy(path, allow_unsafe_images=True)

    def test_private_mount_cannot_contain_the_shared_mount_or_be_contained(
        self,
    ) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        profile = deepcopy(source["profiles"][1])
        profile["private_mount_path"] = "/home/jovyan/shared/private"
        profile["config_digest"] = profile_digest(profile)
        source["profiles"] = [profile]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ProfilePolicyError, "mount paths overlap"):
                load_profile_policy(path, allow_unsafe_images=True)

    def test_workspace_mount_paths_are_exactly_allowlisted(self) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        source["shared_volume"]["mount_path"] = "/home/jovyan/team"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(
                ProfilePolicyError, "must be /home/jovyan/shared"
            ):
                load_profile_policy(path, allow_unsafe_images=True)

        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        profile = deepcopy(source["profiles"][1])
        profile["private_mount_path"] = "/home/jovyan/project"
        profile["config_digest"] = profile_digest(profile)
        source["profiles"] = [profile]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(
                ProfilePolicyError, "must be /home/jovyan/work"
            ):
                load_profile_policy(path, allow_unsafe_images=True)

    def test_kernel_or_python_mutation_changes_immutable_digest(self) -> None:
        document = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        profile = deepcopy(document["profiles"][1])
        expected = profile_digest(profile)
        profile["kernels"][0]["display_name"] = "Unreviewed kernel"
        self.assertNotEqual(profile_digest(profile), expected)
        profile = deepcopy(document["profiles"][1])
        profile["python_version"] = "3.12.12"
        self.assertNotEqual(profile_digest(profile), expected)
        profile = deepcopy(document["profiles"][1])
        profile["kernels"][1]["executable"] = "/opt/conda/bin/python"
        self.assertNotEqual(profile_digest(profile), expected)

    def test_unenforced_disk_profile_is_explicitly_accepted_by_production_loader(
        self,
    ) -> None:
        document = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        profile = deepcopy(document["profiles"][1])
        profile["image"] = "registry.example/singleuser@sha256:" + "a" * 64
        profile["config_digest"] = profile_digest(profile)
        document["profiles"] = [profile]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            policy = load_profile_policy(path)

        self.assertIs(
            policy["profiles"][(profile["id"], profile["version"])][
                "private_disk_quota_enforced"
            ],
            False,
        )

    def test_cpu_limit_must_be_finite_whole_millicores(self) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        valid = deepcopy(source)
        valid["profiles"][1]["cpu_limit"] = 1.001
        valid["profiles"][1]["config_digest"] = profile_digest(valid["profiles"][1])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "valid-profiles.json"
            path.write_text(json.dumps(valid), encoding="utf-8")
            load_profile_policy(path, allow_unsafe_images=True)

        for invalid in (1.0001, float("nan")):
            document = deepcopy(source)
            document["profiles"][1]["cpu_limit"] = invalid
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "profiles.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ProfilePolicyError, "millicores"):
                    load_profile_policy(path, allow_unsafe_images=True)

    def test_production_placeholder_fails_closed(self) -> None:
        document = json.loads(
            (ROOT / "profiles.production.example.json").read_text(encoding="utf-8")
        )
        profile = document["profiles"][0]
        self.assertIs(profile["private_disk_quota_enforced"], False)
        self.assertEqual(profile["config_digest"], profile_digest(profile))
        with self.assertRaises(ProfilePolicyError):
            load_profile_policy(ROOT / "profiles.production.example.json")

    def test_profile_image_check_covers_every_enabled_managed_profile(self) -> None:
        commands: list[list[str]] = []

        def record(command: list[str], *, check: bool, timeout: int):
            self.assertTrue(check)
            self.assertEqual(timeout, 120)
            commands.append(command)
            return SimpleNamespace(returncode=0)

        managed, skipped = check_profile_images(
            ROOT / "profiles.local-dev.json",
            allow_unsafe_policy=True,
            execute=True,
            runner=record,
        )

        self.assertEqual(managed, 18)
        self.assertEqual(skipped, 1)
        self.assertEqual(len(commands), 18)
        self.assertEqual(
            {command[-3] for command in commands},
            {"team-platform-singleuser:py312-py313-local"},
        )
        self.assertTrue(
            all(
                command[-2] == "/usr/local/bin/platform-singleuser"
                for command in commands
            )
        )
        self.assertTrue(all(command[-1] == "--version" for command in commands))
        self.assertTrue(all("--entrypoint" not in command for command in commands))
        contracts = {
            next(
                argument.removeprefix("PLATFORM_KERNEL_CONTRACT=")
                for argument in command
                if argument.startswith("PLATFORM_KERNEL_CONTRACT=")
            )
            for command in commands
        }
        self.assertEqual(
            {json.loads(contract)["default_kernel"] for contract in contracts},
            {"python312", "python3"},
        )

    def test_profile_image_check_uses_spawn_runtime_environment_exactly(self) -> None:
        policy = load_profile_policy(
            ROOT / "profiles.local-dev.json", allow_unsafe_images=True
        )
        profile = policy["profiles"][("python313-cpu1-mem1024", 1)]
        expected = kernel_runtime_environment(profile)
        command = docker_verification_command(profile)

        for name, value in expected.items():
            self.assertIn(f"{name}={value}", command)
        self.assertIn("no-new-privileges:true", command)
        self.assertIn("none", command)
        self.assertIn("/usr/local/bin/platform-singleuser", command)

    def test_gpu_profile_image_check_executes_exact_uuid_runtime_probe(
        self,
    ) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        profile = schema_v3_profile(
            source["profiles"][1], nvidia=True, profile_id="python312-cuda"
        )
        with self.assertRaisesRegex(ValueError, "exact GPU UUID"):
            docker_verification_command(profile)

        command = docker_verification_command(
            profile, nvidia_gpu_device_id=GPU_DEVICE_ID
        )
        expected = kernel_runtime_environment(profile)
        for name, value in expected.items():
            self.assertIn(f"{name}={value}", command)
        self.assertIn(f"PLATFORM_NVIDIA_GPU_DEVICE_IDS={GPU_DEVICE_ID}", command)
        self.assertIn("--gpus", command)
        self.assertIn(f"device={GPU_DEVICE_ID}", command)
        self.assertIn("--entrypoint", command)
        self.assertIn("/opt/conda/envs/python312/bin/python", command)
        self.assertEqual(
            command[-2:],
            ["/usr/local/libexec/verify_cuda_runtime.py", "--require-gpu"],
        )
        self.assertFalse(
            any(argument.startswith("PLATFORM_WORKSPACE_ID=") for argument in command)
        )

        cpu = schema_v3_profile(source["profiles"][1], nvidia=False)
        cpu_command = docker_verification_command(
            cpu, nvidia_gpu_device_id=GPU_DEVICE_ID
        )
        self.assertFalse(
            any(
                str(argument).startswith("PLATFORM_NVIDIA_GPU_DEVICE_IDS=")
                for argument in cpu_command
            )
        )
        self.assertNotIn("--gpus", cpu_command)

    def test_gpu_profile_image_check_deduplicates_identical_runtime_probes(
        self,
    ) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        first = schema_v3_profile(
            source["profiles"][1], nvidia=True, profile_id="python312-cuda-a"
        )
        second = deepcopy(first)
        second["id"] = "python312-cuda-b"
        second["version"] = 2
        second["config_digest"] = profile_digest(second)
        document = {
            "schema_version": 3,
            "shared_volume": source["shared_volume"],
            "profiles": [first, second],
        }
        commands: list[list[str]] = []

        def record(command: list[str], *, check: bool, timeout: int):
            self.assertTrue(check)
            self.assertEqual(timeout, 120)
            commands.append(command)
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "profiles.json"
            policy.write_text(json.dumps(document), encoding="utf-8")
            managed, skipped = check_profile_images(
                policy,
                allow_unsafe_policy=True,
                execute=True,
                nvidia_gpu_device_id=GPU_DEVICE_ID,
                runner=record,
            )

        self.assertEqual((managed, skipped), (2, 0))
        # One metadata probe exercises the real image entrypoint/wrapper and one
        # device-backed probe executes the CUDA tensor. Both are deduplicated
        # when historical profiles resolve to the same immutable image/runtime.
        self.assertEqual(len(commands), 2)
        wrapper_commands = [
            command for command in commands if "--entrypoint" not in command
        ]
        tensor_commands = [command for command in commands if "--entrypoint" in command]
        self.assertEqual(len(wrapper_commands), 1)
        self.assertEqual(len(tensor_commands), 1)
        self.assertEqual(
            wrapper_commands[0][-2:],
            ["/usr/local/bin/platform-singleuser", "--version"],
        )
        self.assertEqual(
            tensor_commands[0][-2:],
            ["/usr/local/libexec/verify_cuda_runtime.py", "--require-gpu"],
        )

    def test_gpu_profile_image_check_probes_current_and_retained_images(
        self,
    ) -> None:
        source = json.loads(
            (ROOT / "profiles.local-dev.json").read_text(encoding="utf-8")
        )
        retained = schema_v3_profile(
            source["profiles"][1], nvidia=True, profile_id="python312-cuda"
        )
        retained["selectable"] = False
        retained["image"] = "sha256:" + "a" * 64
        retained["config_digest"] = profile_digest(retained)
        current = deepcopy(retained)
        current["version"] = 2
        current["selectable"] = True
        current["image"] = "sha256:" + "b" * 64
        current["config_digest"] = profile_digest(current)
        document = {
            "schema_version": 3,
            "shared_volume": source["shared_volume"],
            "profiles": [retained, current],
        }
        commands: list[list[str]] = []

        def record(command: list[str], *, check: bool, timeout: int):
            self.assertTrue(check)
            self.assertEqual(timeout, 120)
            commands.append(command)
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "profiles.json"
            policy.write_text(json.dumps(document), encoding="utf-8")
            managed, skipped = check_profile_images(
                policy,
                allow_unsafe_policy=False,
                execute=True,
                nvidia_gpu_device_id=GPU_DEVICE_ID,
                runner=record,
            )

        self.assertEqual((managed, skipped), (2, 0))
        self.assertEqual(len(commands), 4)
        wrapper_commands = [
            command for command in commands if "--entrypoint" not in command
        ]
        tensor_commands = [command for command in commands if "--entrypoint" in command]
        self.assertEqual(len(wrapper_commands), 2)
        self.assertEqual(len(tensor_commands), 2)
        self.assertEqual(
            {command[-3] for command in tensor_commands},
            {retained["image"], current["image"]},
        )
        self.assertEqual(
            {command[-3] for command in wrapper_commands},
            {retained["image"], current["image"]},
        )
        for command in tensor_commands:
            self.assertIn(f"device={GPU_DEVICE_ID}", command)
            self.assertEqual(
                command[-2:],
                ["/usr/local/libexec/verify_cuda_runtime.py", "--require-gpu"],
            )
        for command in wrapper_commands:
            self.assertNotIn("--gpus", command)
            self.assertEqual(
                command[-2:], ["/usr/local/bin/platform-singleuser", "--version"]
            )


class HmacContractTests(unittest.TestCase):
    def test_v1_vector(self) -> None:
        vector = json.loads(
            (ROOT / "tests" / "contract_vectors.json").read_text(encoding="utf-8")
        )["hmac_v1_consume"]
        body = json.dumps(
            vector["body"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        self.assertEqual(body.decode("ascii"), vector["canonical_body"])
        digest = hashlib.sha256(body).hexdigest()
        self.assertEqual(digest, vector["content_sha256"])
        canonical = "\n".join(
            (
                "v1",
                vector["timestamp"],
                vector["nonce"],
                vector["method"],
                vector["path"],
                digest,
            )
        ).encode("ascii")
        signature = hmac.new(
            vector["key_ascii"].encode("ascii"), canonical, hashlib.sha256
        ).hexdigest()
        self.assertEqual(signature, vector["signature_hex"])


class RbacPolicyTests(unittest.TestCase):
    def test_standard_users_get_only_filtered_portal_service_access(self) -> None:
        roles = {role["name"]: role for role in platform_load_roles()}
        self.assertEqual(
            roles["user"]["scopes"],
            ["self", f"access:services!service={PLATFORM_API_SERVICE}"],
        )
        self.assertNotIn("access:services", roles["user"]["scopes"])
        self.assertNotIn("admin:services", roles["user"]["scopes"])

    def test_reconciler_role_remains_read_only_and_service_bound(self) -> None:
        roles = {role["name"]: role for role in platform_load_roles()}
        reconciler = roles["platform-reconciler-read-only"]
        self.assertEqual(reconciler["scopes"], ["list:users", "read:servers"])
        self.assertEqual(reconciler["services"], [PLATFORM_RECONCILER_SERVICE])

    def test_admin_lifecycle_role_can_manage_servers_but_not_access_content(
        self,
    ) -> None:
        roles = {role["name"]: role for role in platform_load_roles()}
        lifecycle = roles["platform-admin-lifecycle"]
        self.assertEqual(lifecycle["scopes"], ["admin:servers"])
        self.assertEqual(lifecycle["services"], [PLATFORM_ADMIN_LIFECYCLE_SERVICE])
        self.assertNotIn("access:servers", lifecycle["scopes"])
        self.assertNotIn("admin:users", lifecycle["scopes"])
        self.assertNotIn("tokens", lifecycle["scopes"])

    def test_admin_browser_access_depends_on_builtin_access_scope(self) -> None:
        validate_builtin_admin_browser_access(
            [{"name": "admin", "scopes": ["admin:servers", "access:servers"]}]
        )
        with self.assertRaisesRegex(RuntimeError, "browser server access"):
            validate_builtin_admin_browser_access(
                [{"name": "admin", "scopes": ["admin:servers"]}]
            )

    def test_singleuser_browser_oauth_is_narrowed_to_exact_target(self) -> None:
        class ExactSpawner:
            def __init__(self, *, user, orm_spawner):
                self.user = user
                self.orm_spawner = orm_spawner
                self.oauth_client_allowed_scopes = []

            async def _get_oauth_client_allowed_scopes(self):
                return [
                    "access:servers!server=" f"{self.user.name}/{self.orm_spawner.name}"
                ]

        class BroadSpawner(ExactSpawner):
            async def _get_oauth_client_allowed_scopes(self):
                return ["access:servers"]

        validate_singleuser_browser_oauth_contract(ExactSpawner)
        with self.assertRaisesRegex(RuntimeError, "target-server exact"):
            validate_singleuser_browser_oauth_contract(BroadSpawner)

    def test_singleuser_oauth_probe_works_inside_running_event_loop(self) -> None:
        class ExactSpawner:
            def __init__(self, *, user, orm_spawner):
                self.user = user
                self.orm_spawner = orm_spawner
                self.oauth_client_allowed_scopes = []

            async def _get_oauth_client_allowed_scopes(self):
                return [
                    f"access:servers!server={self.user.name}/"
                    f"{self.orm_spawner.name}"
                ]

        async def validate_during_hub_startup() -> None:
            validate_singleuser_browser_oauth_contract(ExactSpawner)

        asyncio.run(validate_during_hub_startup())


class SignedPostTests(unittest.IsolatedAsyncioTestCase):
    async def test_validator_response_is_fetched_before_size_check(self) -> None:
        client = SimpleNamespace(
            fetch=AsyncMock(return_value=SimpleNamespace(body=b'{"authorized":true}'))
        )
        config = SimpleNamespace(hmac_key=b"test-key", validator_timeout_seconds=1.0)

        with (
            patch.object(spawn_guard, "_get_config", return_value=config),
            patch.object(spawn_guard, "AsyncHTTPClient", return_value=client),
        ):
            result = await spawn_guard._signed_post(
                "http://api:8000/internal/workspaces/spawn/consume",
                {"workspace_id": "workspace-1"},
            )

        self.assertEqual(result, {"authorized": True})
        client.fetch.assert_awaited_once()


class DockerSpawnerProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_profile_policy(
            ROOT / "profiles.local-dev.json", allow_unsafe_images=True
        )
        self.config = SimpleNamespace(
            singleuser_command="/opt/conda/bin/jupyterhub-singleuser",
            shared_volume=self.policy["shared_volume"],
            hub_connect_host="172.30.0.10",
            egress_proxy_url="http://172.30.0.20:3128",
            network_name="platform-jupyter-compose-local",
            check_url="http://api:8000/internal/v1/spawn-authorizations/check",
            storage_policy_mode=spawn_guard.UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY,
            unsafe_local_dev=True,
            max_cpu_millicores=8_000,
            max_memory_mb=4_096,
        )
        self.authorization = {
            "workspace_id": "workspace-12345678",
            "username": "alice",
            "server_name": "ws-0123456789abcdef0123456789abcdef",
            "spawn_authorization_id": "authorization-12345678",
            "private_volume_name": "jupyter-user-alice-slot-1",
            "private_volume_slot_number": 1,
            "private_volume_slot_id": "slot-12345678",
            "kernel_idle_timeout_seconds": 3_600,
            "gpu_count": 0,
            "gpu_device_id": None,
            "gpu_inventory_digest": None,
        }

    def test_managed_profile_applies_only_allowlisted_runtime_values(self) -> None:
        profile = self.policy["profiles"][("python312-cpu2-mem2048", 1)]
        spawner = SimpleNamespace()
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            spawn_guard._apply_local_policy(spawner, self.authorization, profile)

        self.assertEqual(spawner.image, profile["image"])
        self.assertEqual(spawner.cpu_limit, 2.0)
        self.assertEqual(spawner.mem_limit, 2147483648)
        self.assertEqual(spawner.cmd, ["/usr/local/bin/platform-singleuser"])
        self.assertEqual(
            spawner.args,
            [
                "--MappingKernelManager.cull_idle_timeout=3600",
                "--MappingKernelManager.cull_interval=60",
                "--MappingKernelManager.cull_busy=False",
                "--MappingKernelManager.cull_connected=True",
            ],
        )
        self.assertEqual(spawner.notebook_dir, "/home/jovyan")
        self.assertEqual(spawner.default_url, "/lab/tree/work")
        self.assertEqual(
            spawner.environment["JUPYTER_RUNTIME_DIR"], "/tmp/jupyter-runtime"
        )
        self.assertEqual(
            spawner.volumes,
            {
                "jupyter-user-alice-slot-1": {
                    "bind": "/home/jovyan/work",
                    "mode": "rw",
                },
                "jupyter-shared-local": {
                    "bind": "/home/jovyan/shared",
                    "mode": "rw",
                },
            },
        )
        contract = json.loads(spawner.environment["PLATFORM_KERNEL_CONTRACT"])
        self.assertEqual(contract["default_kernel"], "python312")
        self.assertEqual(contract["python_version"], "3.12.13")
        self.assertEqual(spawner.environment["PLATFORM_DEFAULT_KERNEL"], "python312")
        self.assertEqual(
            spawner.environment["PLATFORM_PYTHON_EXECUTABLE"],
            "/opt/conda/envs/python312/bin/python",
        )
        self.assertEqual(
            spawner.environment["PATH"].split(":")[0],
            "/opt/conda/envs/python312/bin",
        )
        self.assertEqual(
            spawner.environment["PLATFORM_SHARED_MOUNT_PATH"],
            "/home/jovyan/shared",
        )
        self.assertEqual(spawner.environment["PLATFORM_SHARED_GID"], "100")
        self.assertEqual(
            spawner.environment["PLATFORM_PRIVATE_MOUNT_PATH"],
            "/home/jovyan/work",
        )
        self.assertEqual(spawner.environment["PLATFORM_PRIVATE_UID"], "1000")
        self.assertEqual(spawner.environment["PLATFORM_PRIVATE_GID"], "100")

    def test_legacy_profile_keeps_original_command_and_restart_contract(self) -> None:
        profile = self.policy["profiles"][("python-local", 1)]
        spawner = SimpleNamespace()
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            spawn_guard._apply_local_policy(spawner, self.authorization, profile)

        self.assertEqual(spawner.cmd, ["/opt/conda/bin/jupyterhub-singleuser"])
        self.assertEqual(
            spawner.args,
            [
                "--MappingKernelManager.cull_idle_timeout=3600",
                "--MappingKernelManager.cull_interval=60",
                "--MappingKernelManager.cull_busy=False",
                "--MappingKernelManager.cull_connected=True",
            ],
        )
        self.assertNotIn("PLATFORM_KERNEL_CONTRACT", spawner.environment)

    def test_docker_host_limits_and_labels_come_from_local_profile(self) -> None:
        profile = self.policy["profiles"][("python313-cpu1-mem1024", 1)]
        spawner = SimpleNamespace(
            _platform_profile=profile,
            _platform_spawn_authorization=self.authorization,
        )
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            host = spawn_guard.extra_host_config(spawner)
            create = spawn_guard.extra_create_kwargs(spawner)

        self.assertEqual(host["network_mode"], "platform-jupyter-compose-local")
        self.assertEqual(host["dns"], ["127.0.0.1"])
        self.assertEqual(host["pids_limit"], 256)
        self.assertEqual(host["memswap_limit"], profile["memory_limit_bytes"])
        self.assertEqual(host["shm_size"], 134217728)
        self.assertTrue(host["read_only"])
        self.assertNotIn("storage_opt", host)
        self.assertNotIn("device_requests", host)
        self.assertEqual(create["labels"]["platform.python.version"], "3.13.14")
        self.assertEqual(create["labels"]["platform.kernel.default"], "python3")
        self.assertEqual(
            create["labels"]["platform.profile_digest"], profile["config_digest"]
        )
        self.assertEqual(create["labels"]["platform.disk.quota_enforced"], "false")

    def test_gpu_profile_uses_exact_uuid_device_request_and_trusted_environment(
        self,
    ) -> None:
        profile = schema_v3_profile(
            self.policy["profiles"][("python312-cpu1-mem1024", 1)],
            nvidia=True,
            profile_id="python312-cuda",
        )
        self.config.nvidia_gpu_device_id = GPU_DEVICE_ID
        spawner = SimpleNamespace()
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            spawn_guard._apply_local_policy(spawner, self.authorization, profile)
            host = spawn_guard.extra_host_config(spawner)
            create = spawn_guard.extra_create_kwargs(spawner)

        self.assertEqual(
            spawner.environment["PLATFORM_NVIDIA_GPU_DEVICE_IDS"], GPU_DEVICE_ID
        )
        self.assertEqual(
            json.loads(spawner.environment["PLATFORM_ACCELERATOR_CONTRACT"]),
            {"schema_version": 1, **NVIDIA_ACCELERATOR},
        )
        self.assertEqual(
            spawner.environment["NVIDIA_DRIVER_CAPABILITIES"], "compute,utility"
        )
        self.assertEqual(
            host["device_requests"],
            [
                {
                    "Driver": "nvidia",
                    "Count": 0,
                    "DeviceIDs": [GPU_DEVICE_ID],
                    "Capabilities": [["gpu"]],
                    "Options": {},
                }
            ],
        )
        # Engine 27 negotiates API v1.46. Prove that the exact object emitted by
        # the Hub serializes to the DeviceRequests field accepted at that API
        # boundary without contacting a Docker daemon.
        api_v146 = APIClient(
            base_url="unix:///tmp/platform-device-request-contract.sock",
            version="1.46",
        )
        try:
            serialized = api_v146.create_host_config(
                device_requests=host["device_requests"]
            )
        finally:
            api_v146.close()
        self.assertEqual(serialized["DeviceRequests"], host["device_requests"])
        self.assertEqual(create["labels"]["platform.accelerator.kind"], "nvidia")
        self.assertEqual(create["labels"]["platform.accelerator.count"], "1")
        self.assertEqual(create["labels"]["platform.accelerator.sharing"], "exclusive")

    def test_gpu_profile_rejects_missing_or_noncanonical_trusted_device_id(
        self,
    ) -> None:
        profile = schema_v3_profile(
            self.policy["profiles"][("python312-cpu1-mem1024", 1)],
            nvidia=True,
            profile_id="python312-cuda",
        )
        for device_id in (None, "0", GPU_DEVICE_ID.upper()):
            self.config.nvidia_gpu_device_id = device_id
            spawner = SimpleNamespace(_platform_profile=profile)
            with (
                self.subTest(device_id=device_id),
                patch.object(spawn_guard, "_get_config", return_value=self.config),
                self.assertRaisesRegex(
                    spawn_guard.SpawnGuardError,
                    "NVIDIA GPU device policy is invalid",
                ),
            ):
                spawn_guard.extra_host_config(spawner)

    def test_user_supplied_image_or_resource_option_is_rejected(self) -> None:
        spawner = SimpleNamespace(
            name="ws-0123456789abcdef0123456789abcdef",
            user=SimpleNamespace(name="alice"),
        )
        options = {
            "profile_id": "python312-cpu1-mem1024",
            "profile_version": 1,
            "spawn_ticket": "A" * 32,
            "image": "attacker.invalid/root:latest",
        }
        with self.assertRaisesRegex(spawn_guard.SpawnGuardError, "schema mismatch"):
            spawn_guard._validate_user_options(spawner, options)

    def test_logical_offer_fields_never_cross_the_hub_execution_boundary(self) -> None:
        spawner = SimpleNamespace(
            name="ws-0123456789abcdef0123456789abcdef",
            user=SimpleNamespace(name="alice"),
        )
        for injected in (
            {"offer_id": "admin-created-offer"},
            {"cpu_limit": 99},
            {"memory_limit_mb": 999999},
        ):
            options = {
                "profile_id": "python312-cpu1-mem1024",
                "profile_version": 1,
                "spawn_ticket": "A" * 32,
                **injected,
            }
            with (
                self.subTest(injected=next(iter(injected))),
                self.assertRaisesRegex(spawn_guard.SpawnGuardError, "schema mismatch"),
            ):
                spawn_guard._validate_user_options(spawner, options)


class WorkspaceEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.policy = load_profile_policy(
            ROOT / "profiles.local-dev.json", allow_unsafe_images=True
        )
        self.profile = self.policy["profiles"][("python312-cpu1-mem1024", 1)]
        self.hmac_key = b"environment-test-key-that-is-at-least-32-bytes"
        self.config = SimpleNamespace(
            hmac_key=self.hmac_key,
            profiles=self.policy["profiles"],
            singleuser_command="/opt/conda/bin/jupyterhub-singleuser",
            shared_volume=self.policy["shared_volume"],
            hub_connect_host="172.30.0.10",
            egress_proxy_url="http://172.30.0.20:3128",
            network_name="platform-jupyter-compose-local",
            check_url="http://api:8000/internal/v1/spawn-authorizations/check",
            storage_policy_mode=spawn_guard.UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY,
            unsafe_local_dev=True,
            max_cpu_millicores=8_000,
            max_memory_mb=4_096,
        )

    def authorization(self, environment: dict[str, str]) -> dict[str, object]:
        return {
            "spawn_authorization_id": "authorization-12345678",
            "workspace_id": "workspace-12345678",
            "operation_id": "operation-12345678",
            "attempt_no": 1,
            "workspace_spec_version": 2,
            "username": "alice",
            "server_name": "ws-0123456789abcdef0123456789abcdef",
            "profile_id": self.profile["id"],
            "profile_version": self.profile["version"],
            "profile_config_digest": self.profile["config_digest"],
            "runtime_base_profile_id": self.profile["id"],
            "runtime_base_profile_version": self.profile["version"],
            "runtime_base_profile_config_digest": self.profile["config_digest"],
            "cpu_limit_millicores": 1_000,
            "memory_limit_bytes": self.profile["memory_limit_bytes"],
            "kernel_idle_timeout_seconds": 3_600,
            "gpu_count": 0,
            "gpu_device_id": None,
            "gpu_inventory_digest": None,
            "private_volume_slot_id": "slot-12345678",
            "private_volume_slot_number": 1,
            "private_volume_name": "jupyter-user-alice-slot-1",
            "private_disk_hard_limit_bytes": self.profile[
                "private_disk_hard_limit_bytes"
            ],
            "uid": self.profile["uid"],
            "gid": self.profile["gid"],
            "valid_until_unix": int(time.time()) + 60,
            "environment": environment,
            "environment_digest": spawn_guard.environment_digest(
                environment, self.hmac_key
            ),
            "user_environment_generation": 3,
            "workspace_environment_generation": 7,
        }

    def test_consume_separates_plaintext_and_platform_environment_wins(self) -> None:
        environment = {"TEAM_API_KEY": "super-secret-value", "LANG": "ko_KR.UTF-8"}
        authorization = self.authorization(environment)
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            public, profile, effective = spawn_guard._validate_authorization(
                authorization,
                username="alice",
                server_name=authorization["server_name"],
                profile_id=self.profile["id"],
                profile_version=self.profile["version"],
            )
            spawner = SimpleNamespace()
            spawn_guard._apply_local_policy(spawner, public, profile, effective)

        self.assertNotIn("environment", public)
        self.assertEqual(public["kernel_idle_timeout_seconds"], 3_600)
        self.assertNotIn("super-secret-value", json.dumps(public))
        self.assertEqual(spawner.environment["TEAM_API_KEY"], "super-secret-value")
        self.assertEqual(spawner.environment["HOME"], "/home/jovyan/work")
        self.assertNotIn(
            "super-secret-value", json.dumps(spawner._platform_spawn_authorization)
        )
        create = spawn_guard.extra_create_kwargs(spawner)
        self.assertNotIn("super-secret-value", json.dumps(create))
        self.assertFalse(any("environment" in key for key in create["labels"]))

    def test_admin_derived_resources_keep_the_allowlisted_runtime_contract(
        self,
    ) -> None:
        from profile_policy import derive_resource_profile

        derived = derive_resource_profile(
            self.profile, cpu_millicores=3_500, memory_mb=3_072
        )
        authorization = self.authorization({})
        authorization.update(
            {
                "profile_id": derived["id"],
                "profile_version": derived["version"],
                "profile_config_digest": derived["config_digest"],
                "cpu_limit_millicores": 3_500,
                "memory_limit_bytes": 3_072 * 1024 * 1024,
            }
        )
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            _public, profile, _environment = spawn_guard._validate_authorization(
                authorization,
                username="alice",
                server_name=authorization["server_name"],
                profile_id=derived["id"],
                profile_version=derived["version"],
            )
        self.assertEqual(profile["image"], self.profile["image"])
        self.assertEqual(profile["kernels"], self.profile["kernels"])
        self.assertEqual(profile["cpu_limit"], 3.5)
        self.assertEqual(profile["memory_limit_bytes"], 3_072 * 1024 * 1024)

        too_large = dict(authorization, cpu_limit_millicores=8_001)
        with (
            patch.object(spawn_guard, "_get_config", return_value=self.config),
            self.assertRaisesRegex(
                spawn_guard.SpawnGuardError, "exceed the execution ceiling"
            ),
        ):
            spawn_guard._validate_authorization(
                too_large,
                username="alice",
                server_name=authorization["server_name"],
                profile_id=derived["id"],
                profile_version=derived["version"],
            )

    def test_reserved_startup_loader_and_egress_keys_are_rejected_case_insensitively(
        self,
    ) -> None:
        for name in (
            "jupyterhub_api_token",
            "http_proxy",
            "LD_PRELOAD",
            "PYTHONPATH",
            "BASH_ENV",
            "GRANT_SUDO_EXTRA",
            "JUPYTER_PATH",
            "TMP_DATA",
            "PLATFORM_PRIVATE_UID",
            "nvidia_visible_devices",
            "Nb_User",
        ):
            environment = {name: "attacker-controlled"}
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    spawn_guard.SpawnGuardError, "workspace environment is invalid"
                ),
            ):
                spawn_guard.validate_user_environment(
                    environment,
                    expected_digest=spawn_guard.environment_digest(
                        environment, self.hmac_key
                    ),
                    hmac_key=self.hmac_key,
                )

        with self.assertRaisesRegex(
            spawn_guard.SpawnGuardError, "workspace environment is invalid"
        ):
            spawn_guard.validate_user_environment(
                {"SAFE_KEY": "\ud800"},
                expected_digest="hmac-sha256:" + "0" * 64,
                hmac_key=self.hmac_key,
            )

    def test_signed_kernel_idle_timeout_is_strictly_validated(self) -> None:
        for value in (0, 300, 3_600, 604_800):
            authorization = self.authorization({})
            authorization["kernel_idle_timeout_seconds"] = value
            with patch.object(spawn_guard, "_get_config", return_value=self.config):
                public, _profile, _environment = spawn_guard._validate_authorization(
                    authorization,
                    username="alice",
                    server_name=authorization["server_name"],
                    profile_id=self.profile["id"],
                    profile_version=self.profile["version"],
                )
                spawner = SimpleNamespace()
                spawn_guard._apply_local_policy(
                    spawner, public, self.profile, _environment
                )
            self.assertEqual(public["kernel_idle_timeout_seconds"], value)
            self.assertEqual(
                spawner.args[0],
                f"--MappingKernelManager.cull_idle_timeout={value}",
            )

        for value in (True, -1, 1, 299, 301, 604_860, "3600"):
            authorization = self.authorization({})
            authorization["kernel_idle_timeout_seconds"] = value
            with (
                self.subTest(value=value),
                patch.object(spawn_guard, "_get_config", return_value=self.config),
                self.assertRaisesRegex(
                    spawn_guard.SpawnGuardError,
                    "kernel_idle_timeout_seconds is invalid",
                ),
            ):
                spawn_guard._validate_authorization(
                    authorization,
                    username="alice",
                    server_name=authorization["server_name"],
                    profile_id=self.profile["id"],
                    profile_version=self.profile["version"],
                )

    def test_signed_gpu_binding_must_match_profile_and_local_inventory(self) -> None:
        gpu_profile = schema_v3_profile(
            self.profile, nvidia=True, profile_id="python312-cuda"
        )
        config = SimpleNamespace(**vars(self.config))
        config.profiles = {
            **self.config.profiles,
            (gpu_profile["id"], gpu_profile["version"]): gpu_profile,
        }
        config.nvidia_gpu_device_id = GPU_DEVICE_ID
        self.assertEqual(
            spawn_guard.nvidia_gpu_inventory_digest(GPU_DEVICE_ID),
            "sha256:df1a007a9153b95d91a0881a0eb37c09590d10572c2c51afeae956271349a0b8",
        )
        authorization = self.authorization({})
        authorization.update(
            {
                "profile_id": gpu_profile["id"],
                "profile_version": gpu_profile["version"],
                "profile_config_digest": gpu_profile["config_digest"],
                "runtime_base_profile_id": gpu_profile["id"],
                "runtime_base_profile_version": gpu_profile["version"],
                "runtime_base_profile_config_digest": gpu_profile["config_digest"],
                "gpu_count": 1,
                "gpu_device_id": GPU_DEVICE_ID,
                "gpu_inventory_digest": spawn_guard.nvidia_gpu_inventory_digest(
                    GPU_DEVICE_ID
                ),
            }
        )
        with patch.object(spawn_guard, "_get_config", return_value=config):
            _public, validated, _environment = spawn_guard._validate_authorization(
                authorization,
                username="alice",
                server_name=authorization["server_name"],
                profile_id=gpu_profile["id"],
                profile_version=gpu_profile["version"],
            )
        self.assertEqual(validated["accelerator"], NVIDIA_ACCELERATOR)

        for field, invalid in (
            ("gpu_count", 0),
            ("gpu_device_id", "GPU-ffffffff-ffff-ffff-ffff-ffffffffffff"),
            ("gpu_inventory_digest", "sha256:" + "0" * 64),
        ):
            changed = deepcopy(authorization)
            changed[field] = invalid
            with (
                self.subTest(field=field),
                patch.object(spawn_guard, "_get_config", return_value=config),
                self.assertRaisesRegex(
                    spawn_guard.SpawnGuardError,
                    "GPU authorization does not match local inventory",
                ),
            ):
                spawn_guard._validate_authorization(
                    changed,
                    username="alice",
                    server_name=changed["server_name"],
                    profile_id=gpu_profile["id"],
                    profile_version=gpu_profile["version"],
                )

    def test_cpu_profile_rejects_any_gpu_authorization_binding(self) -> None:
        authorization = self.authorization({})
        authorization["gpu_count"] = 1
        authorization["gpu_device_id"] = GPU_DEVICE_ID
        authorization["gpu_inventory_digest"] = spawn_guard.nvidia_gpu_inventory_digest(
            GPU_DEVICE_ID
        )
        with (
            patch.object(spawn_guard, "_get_config", return_value=self.config),
            self.assertRaisesRegex(
                spawn_guard.SpawnGuardError,
                "CPU profile has an unexpected GPU binding",
            ),
        ):
            spawn_guard._validate_authorization(
                authorization,
                username="alice",
                server_name=authorization["server_name"],
                profile_id=self.profile["id"],
                profile_version=self.profile["version"],
            )

    def test_backend_and_hub_environment_boundaries_have_semantic_parity(self) -> None:
        source = (PROJECT_ROOT / "backend/app/services/environment.py").read_text(
            encoding="utf-8"
        )
        assignments: dict[str, object] = {}
        for node in ast.parse(source).body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                try:
                    assignments[node.targets[0].id] = ast.literal_eval(node.value)
                except (TypeError, ValueError):
                    continue

        backend_exact = assignments["_RESERVED_EXACT"]
        backend_prefixes = assignments["_RESERVED_PREFIXES"]
        self.assertIsInstance(backend_exact, set)
        self.assertIsInstance(backend_prefixes, tuple)

        def backend_reserved(name: str) -> bool:
            normalized = name.upper()
            return normalized in backend_exact or normalized.startswith(
                backend_prefixes
            )

        candidates = {
            "SAFE_TEAM_VALUE",
            *spawn_guard.RESERVED_ENVIRONMENT_KEYS,
            *backend_exact,
            *(prefix + "PARITY_CANARY" for prefix in backend_prefixes),
            *(
                prefix + "PARITY_CANARY"
                for prefix in spawn_guard.RESERVED_ENVIRONMENT_PREFIXES
            ),
        }
        for name in candidates:
            with self.subTest(name=name):
                self.assertEqual(
                    spawn_guard._is_reserved_environment_key(name),
                    backend_reserved(name),
                )
                self.assertEqual(
                    spawn_guard._is_reserved_environment_key(name.lower()),
                    backend_reserved(name.lower()),
                )

        self.assertEqual(
            assignments["MAX_EFFECTIVE_VARIABLES"],
            spawn_guard.MAX_ENVIRONMENT_VARIABLES,
        )
        self.assertEqual(
            assignments["MAX_VALUE_CHARS"],
            spawn_guard.MAX_ENVIRONMENT_VALUE_BYTES,
        )
        self.assertEqual(
            assignments["MAX_CANONICAL_BYTES"],
            spawn_guard.MAX_ENVIRONMENT_CANONICAL_BYTES,
        )

    def test_environment_digest_generation_and_size_fail_closed(self) -> None:
        with self.assertRaisesRegex(spawn_guard.SpawnGuardError, "binding"):
            spawn_guard.validate_user_environment(
                {"SAFE_KEY": "value"},
                expected_digest="hmac-sha256:" + "0" * 64,
                hmac_key=self.hmac_key,
            )
        oversized = {"SAFE_KEY": "x" * (spawn_guard.MAX_ENVIRONMENT_VALUE_BYTES + 1)}
        with self.assertRaisesRegex(spawn_guard.SpawnGuardError, "invalid"):
            spawn_guard.validate_user_environment(
                oversized,
                expected_digest=spawn_guard.environment_digest(
                    oversized, self.hmac_key
                ),
                hmac_key=self.hmac_key,
            )
        authorization = self.authorization({})
        authorization["user_environment_generation"] = 0
        with (
            patch.object(spawn_guard, "_get_config", return_value=self.config),
            self.assertRaisesRegex(spawn_guard.SpawnGuardError, "generation"),
        ):
            spawn_guard._validate_authorization(
                authorization,
                username="alice",
                server_name=authorization["server_name"],
                profile_id=self.profile["id"],
                profile_version=self.profile["version"],
            )

    async def test_pre_spawn_check_never_replays_environment_plaintext(self) -> None:
        secret = "never-send-this-value-on-check"
        authorization = self.authorization({"TEAM_TOKEN": secret})
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            public, profile, effective = spawn_guard._validate_authorization(
                authorization,
                username="alice",
                server_name=authorization["server_name"],
                profile_id=self.profile["id"],
                profile_version=self.profile["version"],
            )
        spawner = SimpleNamespace(
            name=authorization["server_name"],
            _platform_spawn_authorization=public,
            _platform_profile=profile,
            _platform_user_environment=effective,
            log=SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        captured: list[dict[str, object]] = []

        async def signed_post(_url, payload):
            captured.append(deepcopy(payload))
            return {
                "schema_version": 1,
                "authorized": True,
                "spawn_authorization_id": public["spawn_authorization_id"],
            }

        with (
            patch.object(spawn_guard, "_get_config", return_value=self.config),
            patch.object(spawn_guard, "assert_network_policy", AsyncMock()),
            patch.object(spawn_guard, "assert_host_health"),
            patch.object(spawn_guard, "_verify_docker_volumes", AsyncMock()),
            patch.object(spawn_guard, "_signed_post", side_effect=signed_post),
        ):
            await spawn_guard.pre_spawn_hook(spawner)

        self.assertEqual(len(captured), 1)
        self.assertNotIn("environment", captured[0])
        self.assertNotIn(secret, json.dumps(captured[0]))
        self.assertEqual(
            captured[0]["environment_digest"], public["environment_digest"]
        )

    async def test_lifecycle_cleanup_drops_plaintext_and_fresh_apply_reinjects(
        self,
    ) -> None:
        first_secret = "first-container-only"
        second_secret = "fresh-restart-value"
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            first_auth = self.authorization({"TEAM_TOKEN": first_secret})
            public, profile, effective = spawn_guard._validate_authorization(
                first_auth,
                username="alice",
                server_name=first_auth["server_name"],
                profile_id=self.profile["id"],
                profile_version=self.profile["version"],
            )
            spawner = SimpleNamespace()
            spawn_guard._apply_local_policy(spawner, public, profile, effective)
            self.assertEqual(spawner.environment["TEAM_TOKEN"], first_secret)

            spawn_guard.clear_user_environment(spawner)
            self.assertIsNone(spawner._platform_user_environment)
            self.assertNotIn("TEAM_TOKEN", spawner.environment)
            self.assertNotIn(first_secret, json.dumps(spawner.environment))

            second_auth = self.authorization({"TEAM_TOKEN": second_secret})
            second_public, second_profile, second_effective = (
                spawn_guard._validate_authorization(
                    second_auth,
                    username="alice",
                    server_name=second_auth["server_name"],
                    profile_id=self.profile["id"],
                    profile_version=self.profile["version"],
                )
            )
            spawn_guard._apply_local_policy(
                spawner, second_public, second_profile, second_effective
            )

        self.assertEqual(spawner.environment["TEAM_TOKEN"], second_secret)
        self.assertEqual(
            spawner._platform_user_environment, {"TEAM_TOKEN": second_secret}
        )

    async def test_pre_spawn_failure_clears_plaintext_immediately(self) -> None:
        secret = "must-not-survive-volume-check-failure"
        authorization = self.authorization({"TEAM_TOKEN": secret})
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            public, profile, effective = spawn_guard._validate_authorization(
                authorization,
                username="alice",
                server_name=authorization["server_name"],
                profile_id=self.profile["id"],
                profile_version=self.profile["version"],
            )
            spawner = SimpleNamespace(
                name=authorization["server_name"],
                log=SimpleNamespace(warning=lambda *args, **kwargs: None),
            )
            spawn_guard._apply_local_policy(spawner, public, profile, effective)

        with (
            patch.object(spawn_guard, "_get_config", return_value=self.config),
            patch.object(spawn_guard, "assert_network_policy", AsyncMock()),
            patch.object(spawn_guard, "assert_host_health"),
            patch.object(
                spawn_guard,
                "_verify_docker_volumes",
                AsyncMock(
                    side_effect=spawn_guard.SpawnGuardError(
                        "required Docker volume is missing"
                    )
                ),
            ),
            self.assertRaises(spawn_guard.HTTPError),
        ):
            await spawn_guard.pre_spawn_hook(spawner)

        self.assertIsNone(spawner._platform_user_environment)
        self.assertNotIn("TEAM_TOKEN", spawner.environment)
        self.assertNotIn(secret, json.dumps(spawner.environment))

    async def test_spawner_wrapper_clears_success_and_all_start_failures(self) -> None:
        class SyntheticDockerSpawner:
            def __init__(self, failure: str | None) -> None:
                self.failure = failure
                self.captured_environment: dict[str, str] | None = None
                self._platform_user_environment = {"TEAM_TOKEN": "wrapper-secret"}
                self._platform_runtime_environment = {"HOME": "/home/jovyan/work"}
                self.environment = {
                    "TEAM_TOKEN": "wrapper-secret",
                    "HOME": "/home/jovyan/work",
                }

            async def create_object(self):
                self.captured_environment = self.environment.copy()
                if self.failure == "create":
                    raise RuntimeError("synthetic create failure")
                return {"Id": "container-id"}

            async def start(self):
                if self.failure == "before-create":
                    raise RuntimeError("synthetic pull failure")
                result = await self.create_object()
                if self.failure == "after-create":
                    raise RuntimeError("synthetic start failure")
                return result

        class WrappedSpawner(PlatformDockerSpawnerMixin, SyntheticDockerSpawner):
            pass

        for failure in (None, "before-create", "create", "after-create"):
            with self.subTest(failure=failure):
                spawner = WrappedSpawner(failure)
                if failure is None:
                    await spawner.start()
                    self.assertEqual(
                        spawner.captured_environment["TEAM_TOKEN"], "wrapper-secret"
                    )
                else:
                    with self.assertRaises(RuntimeError):
                        await spawner.start()
                self.assertIsNone(spawner._platform_user_environment)
                self.assertEqual(spawner.environment, {"HOME": "/home/jovyan/work"})

        config_source = (ROOT / "jupyterhub_config.py").read_text(encoding="utf-8")
        self.assertIn(
            "c.JupyterHub.spawner_class = PlatformDockerSpawner", config_source
        )
        self.assertIn('c.JupyterHub.default_url = f"{portal_origin}/"', config_source)
        self.assertIn(
            "c.Spawner.post_stop_hook = spawn_guard.post_stop_hook", config_source
        )
        self.assertNotIn("c.Spawner.post_spawn_hook", config_source)


class DockerNetworkPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.inspected = {
            "Name": "platform-jupyter-compose-local",
            "Driver": "bridge",
            "Scope": "local",
            "Internal": True,
            "EnableIPv6": False,
            "Attachable": False,
            "Ingress": False,
            "Options": {
                "com.docker.network.bridge.enable_icc": "true",
                "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
                "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
            },
            "Labels": {
                "com.docker.compose.network": "jupyter",
                "platform.managed": "true",
                "platform.kind": "jupyter-execution",
                "platform.network.policy": "compose-internal-trusted-v1",
            },
            "IPAM": {
                "Config": [
                    {
                        "Subnet": "172.30.0.0/24",
                        "IPRange": "172.30.0.128/25",
                    }
                ]
            },
            "Containers": {
                "proxy-container-id": {
                    "Name": "team-platform-local-egress-proxy-1",
                    "IPv4Address": "172.30.0.20/24",
                },
                "hub-container-id": {
                    "Name": "team-platform-local-jupyterhub-1",
                    "IPv4Address": "172.30.0.10/24",
                },
            },
        }
        self.config = SimpleNamespace(
            network_policy_mode=spawn_guard.COMPOSE_INTERNAL_NETWORK_POLICY,
            network_name="platform-jupyter-compose-local",
            network_subnet="172.30.0.0/24",
            network_dynamic_ip_range="172.30.0.128/25",
            hub_connect_host="172.30.0.10",
            egress_proxy_url="http://172.30.0.20:3128",
        )

    def spawner_for(
        self,
        inspected: dict | None = None,
        *,
        version: str = "28.0.0",
        os_name: str = "linux",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            docker=AsyncMock(
                side_effect=[
                    {"Version": version, "Os": os_name},
                    self.inspected if inspected is None else inspected,
                ]
            )
        )

    async def test_compose_internal_network_is_inspected_without_host_manifest(
        self,
    ) -> None:
        spawner = self.spawner_for()
        with (
            patch.object(spawn_guard, "_get_config", return_value=self.config),
            patch.object(spawn_guard, "_health_manifest") as health_manifest,
        ):
            await spawn_guard.assert_network_policy(spawner)

        self.assertEqual(
            spawner.docker.await_args_list,
            [
                call("version"),
                call("inspect_network", "platform-jupyter-compose-local"),
            ],
        )
        health_manifest.assert_not_called()

    async def test_inhibit_ipv4_contract_is_accepted_on_engine_27(self) -> None:
        self.inspected["Options"] = {
            "com.docker.network.bridge.enable_icc": "true",
            "com.docker.network.bridge.inhibit_ipv4": "true",
        }
        for version in ("27.1.2", "27.1.2-1~ubuntu.24.04~noble", "27.5.0"):
            spawner = self.spawner_for(version=version)
            with (
                self.subTest(version=version),
                patch.object(spawn_guard, "_get_config", return_value=self.config),
            ):
                await spawn_guard.assert_network_policy(spawner)

    async def test_network_contract_is_bound_to_minimum_engine_version(self) -> None:
        inhibit = deepcopy(self.inspected)
        inhibit["Options"] = {
            "com.docker.network.bridge.enable_icc": "true",
            "com.docker.network.bridge.inhibit_ipv4": "true",
        }
        for version, inspected in (
            ("26.1.4", inhibit),
            ("27.0.0", inhibit),
            ("27.1.1", inhibit),
            ("27.5.1", self.inspected),
        ):
            spawner = self.spawner_for(inspected, version=version)
            with (
                self.subTest(version=version, options=inspected["Options"]),
                patch.object(spawn_guard, "_get_config", return_value=self.config),
                self.assertRaises(spawn_guard.SpawnGuardError),
            ):
                await spawn_guard.assert_network_policy(spawner)

    async def test_unreviewed_gateway_option_combinations_are_rejected(self) -> None:
        for options in (
            {
                "com.docker.network.bridge.enable_icc": "true",
                "com.docker.network.bridge.inhibit_ipv4": "false",
            },
            {
                "com.docker.network.bridge.enable_icc": "true",
                "com.docker.network.bridge.inhibit_ipv4": "true",
                "com.docker.network.bridge.gateway_mode_ipv4": "nat",
            },
            {
                "com.docker.network.bridge.enable_icc": "true",
                "com.docker.network.bridge.gateway_mode_ipv4": "routed",
                "com.docker.network.bridge.gateway_mode_ipv6": "routed",
            },
        ):
            inspected = deepcopy(self.inspected)
            inspected["Options"] = options
            spawner = self.spawner_for(inspected, version="28.0.0")
            with (
                self.subTest(options=options),
                patch.object(spawn_guard, "_get_config", return_value=self.config),
                self.assertRaises(spawn_guard.SpawnGuardError),
            ):
                await spawn_guard.assert_network_policy(spawner)

    async def test_docker_server_identity_is_fail_closed(self) -> None:
        for version, os_name in (
            ("27.5", "linux"),
            ("27.5.1-rc.1", "linux"),
            ("27.5.1", "windows"),
        ):
            spawner = self.spawner_for(version=version, os_name=os_name)
            with (
                self.subTest(version=version, os_name=os_name),
                patch.object(spawn_guard, "_get_config", return_value=self.config),
                self.assertRaises(spawn_guard.SpawnGuardError),
            ):
                await spawn_guard.assert_network_policy(spawner)
            spawner.docker.assert_awaited_once_with("version")

    async def test_compose_network_rejects_unsafe_identity_or_missing_proxy(
        self,
    ) -> None:
        for mutation in ("external", "icc", "gateway", "proxy", "hub"):
            inspected = deepcopy(self.inspected)
            if mutation == "external":
                inspected["Internal"] = False
            elif mutation == "icc":
                inspected["Options"]["com.docker.network.bridge.enable_icc"] = "false"
            elif mutation == "gateway":
                inspected["IPAM"]["Config"][0]["Gateway"] = "172.30.0.1"
            elif mutation == "proxy":
                inspected["Containers"].pop("proxy-container-id")
            else:
                inspected["Containers"].pop("hub-container-id")
            spawner = self.spawner_for(inspected)
            with (
                self.subTest(mutation=mutation),
                patch.object(spawn_guard, "_get_config", return_value=self.config),
                self.assertRaises(spawn_guard.SpawnGuardError),
            ):
                await spawn_guard.assert_network_policy(spawner)

    async def test_network_drift_is_rejected_before_spawn_ticket_consumption(
        self,
    ) -> None:
        inspected = deepcopy(self.inspected)
        inspected["Internal"] = False
        spawner = SimpleNamespace(
            name="ws-0123456789abcdef0123456789abcdef",
            user=SimpleNamespace(name="alice"),
            log=SimpleNamespace(warning=lambda *args, **kwargs: None),
            docker=self.spawner_for(inspected).docker,
        )
        signed_post = AsyncMock()
        with (
            patch.object(spawn_guard, "_get_config", return_value=self.config),
            patch.object(spawn_guard, "_signed_post", signed_post),
            self.assertRaises(spawn_guard.HTTPError) as raised,
        ):
            await spawn_guard.apply_user_options(
                spawner,
                {
                    "profile_id": "python312-cpu1-mem1024",
                    "profile_version": 1,
                    "spawn_ticket": "A" * 32,
                },
            )

        self.assertEqual(getattr(raised.exception, "status_code", None), 403)
        signed_post.assert_not_awaited()

    async def test_legacy_mode_remains_explicitly_manifest_backed(self) -> None:
        config = SimpleNamespace(
            network_policy_mode=spawn_guard.LEGACY_HOST_FIREWALL_NETWORK_POLICY,
            expected_network_policy_sha256="sha256:" + "a" * 64,
        )
        spawner = SimpleNamespace(docker=AsyncMock())
        with (
            patch.object(spawn_guard, "_get_config", return_value=config),
            patch.object(spawn_guard, "_health_manifest") as health_manifest,
        ):
            await spawn_guard.assert_network_policy(spawner)

        health_manifest.assert_called_once_with(
            "network", config.expected_network_policy_sha256
        )
        spawner.docker.assert_not_awaited()


class StoragePolicyModeTests(unittest.TestCase):
    def test_unlimited_mode_is_explicit_and_has_no_quota_health_digest(self) -> None:
        spawn_guard.validate_storage_policy_configuration(
            spawn_guard.UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY,
            "",
            production=True,
        )
        for mode, digest in (
            ("", ""),
            ("unreviewed", ""),
            (
                spawn_guard.UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY,
                "sha256:" + "a" * 64,
            ),
        ):
            with (
                self.subTest(mode=mode, digest=bool(digest)),
                self.assertRaises(spawn_guard.SpawnGuardError),
            ):
                spawn_guard.validate_storage_policy_configuration(
                    mode, digest, production=True
                )

    def test_unlimited_skips_only_host_quota_manifest(self) -> None:
        config = SimpleNamespace(
            storage_policy_mode=spawn_guard.UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY,
            expected_storage_policy_sha256="",
            unsafe_local_dev=False,
        )
        with (
            patch.object(spawn_guard, "_get_config", return_value=config),
            patch.object(spawn_guard, "_health_manifest") as health_manifest,
        ):
            spawn_guard.assert_host_health()
        health_manifest.assert_not_called()

    def test_legacy_xfs_mode_remains_manifest_backed(self) -> None:
        config = SimpleNamespace(
            storage_policy_mode=spawn_guard.LEGACY_XFS_QUOTA_STORAGE_POLICY,
            expected_storage_policy_sha256="sha256:" + "a" * 64,
            unsafe_local_dev=False,
        )
        with (
            patch.object(spawn_guard, "_get_config", return_value=config),
            patch.object(spawn_guard, "_health_manifest") as health_manifest,
        ):
            spawn_guard.assert_host_health()
        health_manifest.assert_called_once_with(
            "storage", config.expected_storage_policy_sha256
        )


class DockerVolumeVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.policy = load_profile_policy(
            ROOT / "profiles.local-dev.json", allow_unsafe_images=True
        )
        self.profile = self.policy["profiles"][("python312-cpu1-mem1024", 1)]
        self.owner_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
        self.slot_id = uuid.uuid5(self.owner_id, "workspace-volume-slot-1")
        self.authorization = {
            "username": "alice",
            "private_volume_name": "jupyter-user-alice-slot-1",
            "private_volume_slot_number": 1,
            "private_volume_slot_id": str(self.slot_id),
        }
        self.mounts = {
            "jupyter-user-alice-slot-1": {
                "bind": "/home/jovyan/work",
                "mode": "rw",
            },
            "jupyter-shared-local": {
                "bind": "/home/jovyan/shared",
                "mode": "rw",
            },
        }
        self.private = {
            "Name": "jupyter-user-alice-slot-1",
            "Driver": "local",
            "Scope": "local",
            "Options": None,
            "Labels": {
                "platform.managed": "true",
                "platform.provisioned": "true",
                "platform.owner.user_id": str(self.owner_id),
                "platform.owner.username": "alice",
                "platform.volume.slot": "1",
                "platform.volume.slot_id": str(self.slot_id),
                "platform.quota.hard_bytes": "1073741824",
                "platform.quota.enforced": "false",
                "platform.quota.project_id": "10000",
            },
        }
        self.shared = {
            "Name": "jupyter-shared-local",
            "Driver": "local",
            "Scope": "local",
            "Options": None,
            "Labels": {
                "platform.managed": "true",
                "platform.shared": "true",
                "platform.provisioned": "true",
                "platform.quota.enforced": "false",
            },
        }
        self.config = SimpleNamespace(
            unsafe_local_dev=False,
            storage_policy_mode=spawn_guard.UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY,
            shared_volume=self.policy["shared_volume"],
        )

    async def test_unlimited_volume_contract_is_accepted_in_production(self) -> None:
        spawner = SimpleNamespace(
            volumes=deepcopy(self.mounts),
            docker=AsyncMock(side_effect=[self.private, self.shared]),
        )
        with patch.object(spawn_guard, "_get_config", return_value=self.config):
            await spawn_guard._verify_docker_volumes(
                spawner, self.authorization, self.profile
            )

    async def test_label_or_mount_drift_is_rejected(self) -> None:
        private = deepcopy(self.private)
        private["Labels"]["unreviewed.extra"] = "true"
        spawner = SimpleNamespace(
            volumes=deepcopy(self.mounts),
            docker=AsyncMock(side_effect=[private, self.shared]),
        )
        with (
            patch.object(spawn_guard, "_get_config", return_value=self.config),
            self.assertRaisesRegex(spawn_guard.SpawnGuardError, "labels"),
        ):
            await spawn_guard._verify_docker_volumes(
                spawner, self.authorization, self.profile
            )

        mounts = deepcopy(self.mounts)
        mounts["jupyter-shared-local"]["mode"] = "ro"
        spawner = SimpleNamespace(volumes=mounts, docker=AsyncMock())
        with (
            patch.object(spawn_guard, "_get_config", return_value=self.config),
            self.assertRaisesRegex(spawn_guard.SpawnGuardError, "mapping"),
        ):
            await spawn_guard._verify_docker_volumes(
                spawner, self.authorization, self.profile
            )
        spawner.docker.assert_not_awaited()


class NativeAuthenticatorCompatibilityTests(unittest.TestCase):
    def test_legacy_login_render_forwards_synchronous_error_keywords(self) -> None:
        class LegacyLoginHandler:
            def _render(self, login_error=None, username=None):
                raise AssertionError("legacy implementation must be replaced")

        self.assertTrue(apply_native_login_render_compatibility(LegacyLoginHandler))
        handler = LegacyLoginHandler()
        with patch.object(
            nativeauth_compat,
            "_render_synchronous_error",
            return_value="rendered",
        ) as render_error:
            result = handler._render(
                login_error="expired",
                username="alice",
                sync=True,
                status_code=403,
            )

        self.assertEqual(result, "rendered")
        render_error.assert_called_once_with(
            handler,
            login_error="expired",
            username="alice",
            status_code=403,
        )

    def test_legacy_login_render_keeps_native_async_template_for_normal_flow(
        self,
    ) -> None:
        class LegacyLoginHandler:
            def _render(self, login_error=None, username=None):
                raise AssertionError("legacy implementation must be replaced")

        self.assertTrue(apply_native_login_render_compatibility(LegacyLoginHandler))
        captured: dict[str, object] = {}
        handler = LegacyLoginHandler()
        handler.settings = {"login_url": "/hub/login"}
        handler.hub = SimpleNamespace(base_url="/hub/")
        handler.authenticator = SimpleNamespace(
            custom_html="",
            enable_signup=True,
            allow_2fa=False,
            login_url=lambda base_url: f"{base_url}login",
        )
        handler.get_argument = lambda name, *args, **kwargs: (
            "target" if name == "next" else ""
        )

        def render_template(name, **kwargs):
            captured["name"] = name
            captured.update(kwargs)
            return "native-rendered"

        handler.render_template = render_template
        result = handler._render(login_error="invalid", username="alice")

        self.assertEqual(result, "native-rendered")
        self.assertEqual(captured["name"], "native-login.html")
        self.assertEqual(captured["login_error"], "invalid")
        self.assertNotIn("sync", captured)

    def test_modern_login_render_is_not_replaced(self) -> None:
        class ModernLoginHandler:
            def _render(self, login_error=None, username=None, **kwargs):
                return kwargs

        original = ModernLoginHandler._render
        self.assertFalse(apply_native_login_render_compatibility(ModernLoginHandler))
        self.assertIs(ModernLoginHandler._render, original)

    def test_compatibility_patch_is_idempotent(self) -> None:
        class LegacyLoginHandler:
            def _render(self, login_error=None, username=None):
                raise AssertionError("legacy implementation must be replaced")

        self.assertTrue(apply_native_login_render_compatibility(LegacyLoginHandler))
        compatible_render = LegacyLoginHandler._render

        self.assertFalse(apply_native_login_render_compatibility(LegacyLoginHandler))
        self.assertIs(LegacyLoginHandler._render, compatible_render)


if __name__ == "__main__":
    unittest.main()
