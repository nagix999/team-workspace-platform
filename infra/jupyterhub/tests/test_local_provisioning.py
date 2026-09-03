from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_docker_provisioner import (  # noqa: E402
    LocalDockerProvisioner,
    SDKDockerEngine,
)
from local_provisioner_agent import (  # noqa: E402
    AgentError,
    COMPLETE_PATH,
    DELETION_CLAIM_PATH,
    DELETION_COMPLETE_PATH,
    DELETION_FAIL_PATH,
    FAIL_PATH,
    HTTPResult,
    LocalProvisionerAgent,
    MANAGED_SERVICE_ENV_NAMES,
    DELETION_SERVICE_ENV_NAMES,
    PROVISIONING_SERVICE_ENV_NAMES,
    SignedJSONClient,
    managed_service_environment,
    signed_headers,
)
from local_volume_policy import (  # noqa: E402
    private_volume_labels,
    validate_web_provisioning_mode,
    validate_workspace_deletion_mode,
)
from profile_policy import load_profile_policy  # noqa: E402


class FakeDockerEngine:
    def __init__(self) -> None:
        self.volumes: dict[str, dict] = {}
        self.initializations: list[dict] = []
        self.resolved_images: list[str] = []
        self.removed_volumes: list[str] = []
        self.root_verifications: list[dict] = []

    def resolve_local_image(self, image_ref: str) -> str:
        self.resolved_images.append(image_ref)
        return "sha256:" + "1" * 64

    def inspect_volume(self, name: str):
        value = self.volumes.get(name)
        return copy.deepcopy(value) if value is not None else None

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        if name in self.volumes:
            raise AssertionError("duplicate volume creation")
        self.volumes[name] = {
            "Name": name,
            "Driver": "local",
            "Options": None,
            "Labels": labels.copy(),
            "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
        }

    def initialize_volume(self, **values) -> None:
        self.initializations.append(values.copy())

    def remove_volume(self, name: str) -> None:
        if name not in self.volumes:
            raise RuntimeError("missing fake volume")
        self.removed_volumes.append(name)
        del self.volumes[name]

    def verify_volume_root(self, **values) -> None:
        self.root_verifications.append(values.copy())


class LocalDockerProvisionerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_profile_policy(
            ROOT / "profiles.local-dev.json", allow_unsafe_images=True
        )

    def test_shared_volume_reconcile_is_idempotent_without_a_user_job(self) -> None:
        engine = FakeDockerEngine()
        with tempfile.TemporaryDirectory() as directory:
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=Path(directory),
            )
            provisioner.ensure_shared_volume()
            provisioner.ensure_shared_volume()

        self.assertEqual(set(engine.volumes), {"jupyter-shared-local"})
        self.assertEqual(len(engine.initializations), 2)
        for initialization in engine.initializations:
            self.assertEqual(
                {
                    "name": initialization["name"],
                    "uid": initialization["uid"],
                    "gid": initialization["gid"],
                    "mode": initialization["mode"],
                },
                {
                    "name": "jupyter-shared-local",
                    "uid": 0,
                    "gid": 100,
                    "mode": "2770",
                },
            )

    def test_exact_five_private_volumes_and_shared_volume_are_idempotent(self) -> None:
        engine = FakeDockerEngine()
        user_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=Path(directory),
            )
            first = provisioner.provision(user_id=user_id, username="alice")
            second = provisioner.provision(user_id=user_id, username="alice")

        self.assertEqual(first, second)
        self.assertEqual(len(first["slots"]), 5)
        self.assertEqual(len(engine.volumes), 6)
        self.assertEqual(len(engine.initializations), 12)
        self.assertEqual(
            engine.resolved_images,
            [
                "quay.io/jupyterhub/singleuser:5.5",
                "quay.io/jupyterhub/singleuser:5.5",
            ],
        )
        self.assertEqual(
            [slot["project_id"] for slot in first["slots"]],
            [10000, 10001, 10002, 10003, 10004],
        )
        for shared_initialization in (
            engine.initializations[0],
            engine.initializations[6],
        ):
            self.assertEqual(shared_initialization["name"], "jupyter-shared-local")
            self.assertEqual(shared_initialization["uid"], 0)
            self.assertEqual(shared_initialization["gid"], 100)
            self.assertEqual(shared_initialization["mode"], "2770")
        for slot in first["slots"]:
            self.assertEqual(
                engine.volumes[slot["volume_name"]]["Labels"],
                private_volume_labels(
                    user_id=user_id,
                    username="alice",
                    slot=slot,
                ),
            )

    def test_production_manifest_binds_exact_docker_mount_inventory(self) -> None:
        engine = FakeDockerEngine()
        user_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=Path(directory),
                production_manifest=True,
            )
            manifest = provisioner.provision(user_id=user_id, username="alice")

        self.assertEqual(
            set(manifest),
            {
                "schema_version",
                "inventory_sha256",
                "user_id",
                "username",
                "uid",
                "gid",
                "slots",
            },
        )
        self.assertNotIn("unsafe_local_dev", manifest)
        self.assertRegex(manifest["inventory_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(len({slot["path"] for slot in manifest["slots"]}), 5)
        for slot in manifest["slots"]:
            self.assertEqual(
                slot["path"],
                f"/var/lib/docker/volumes/{slot['volume_name']}/_data",
            )

    def test_second_production_user_imports_first_production_manifest(self) -> None:
        engine = FakeDockerEngine()
        first_user_id = str(uuid.uuid4())
        second_user_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=state_dir,
                production_manifest=True,
            )
            first = provisioner.provision(user_id=first_user_id, username="alice")
            second = provisioner.provision(user_id=second_user_id, username="bob")
            first_retry = provisioner.provision(user_id=first_user_id, username="alice")

        self.assertEqual(first_retry, first)
        self.assertEqual(
            [slot["project_id"] for slot in first["slots"]],
            [10000, 10001, 10002, 10003, 10004],
        )
        self.assertEqual(
            [slot["project_id"] for slot in second["slots"]],
            [10005, 10006, 10007, 10008, 10009],
        )

    def test_tampered_persisted_production_manifest_fails_closed(self) -> None:
        engine = FakeDockerEngine()
        first_user_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=state_dir,
                production_manifest=True,
            )
            provisioner.provision(user_id=first_user_id, username="alice")
            output = state_dir / f"local-user-{first_user_id}.json"
            value = json.loads(output.read_text())
            value["slots"][0]["path"] = "/var/lib/docker/volumes/replaced/_data"
            output.write_text(json.dumps(value))

            with self.assertRaisesRegex(RuntimeError, "inventory digest is invalid"):
                provisioner.provision(user_id=str(uuid.uuid4()), username="bob")

    def test_existing_volume_with_extra_or_conflicting_policy_fails_closed(
        self,
    ) -> None:
        engine = FakeDockerEngine()
        engine.volumes["jupyter-shared-local"] = {
            "Name": "jupyter-shared-local",
            "Driver": "local",
            "Options": None,
            "Labels": {
                "platform.managed": "true",
                "platform.provisioned": "true",
                "platform.shared": "true",
                "platform.quota.enforced": "false",
                "unreviewed.extra": "true",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=Path(directory),
            )
            with self.assertRaisesRegex(RuntimeError, "conflicts with local policy"):
                provisioner.provision(user_id=str(uuid.uuid4()), username="alice")

    def test_partial_docker_failure_reuses_reserved_block_on_retry(self) -> None:
        class FailOnceEngine(FakeDockerEngine):
            def __init__(self) -> None:
                super().__init__()
                self.fail_after = 3

            def initialize_volume(self, **values) -> None:
                if len(self.initializations) == self.fail_after:
                    self.fail_after = -1
                    raise RuntimeError("synthetic initializer failure")
                super().initialize_volume(**values)

        engine = FailOnceEngine()
        user_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=state_dir,
            )
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                provisioner.provision(user_id=user_id, username="alice")
            manifest = provisioner.provision(user_id=user_id, username="alice")
            registry = json.loads(
                (state_dir / ".local-project-id-allocations.json").read_text()
            )

        self.assertEqual(
            [slot["project_id"] for slot in manifest["slots"]],
            [10000, 10001, 10002, 10003, 10004],
        )
        self.assertEqual(len(registry["allocations"]), 1)
        self.assertEqual(len(engine.volumes), 6)

    def test_missing_allowlisted_local_image_is_pulled_then_resolved_to_id(
        self,
    ) -> None:
        import docker

        class Images:
            def __init__(self) -> None:
                self.get_calls = 0
                self.pull_calls: list[str] = []

            def get(self, image_ref: str):
                self.get_calls += 1
                if self.get_calls == 1:
                    raise docker.errors.ImageNotFound("missing")
                return type("Image", (), {"id": "sha256:" + "2" * 64})()

            def pull(self, image_ref: str) -> None:
                self.pull_calls.append(image_ref)

        images = Images()
        engine = SDKDockerEngine(
            type("Client", (), {"images": images})(), allow_image_pull=True
        )
        resolved = engine.resolve_local_image("quay.io/jupyterhub/singleuser:5.5")

        self.assertEqual(resolved, "sha256:" + "2" * 64)
        self.assertEqual(images.pull_calls, ["quay.io/jupyterhub/singleuser:5.5"])

    def test_production_deletion_never_pulls_a_missing_helper_image(self) -> None:
        import docker

        class Images:
            def __init__(self) -> None:
                self.pull_calls: list[str] = []

            def get(self, _image_ref: str):
                raise docker.errors.ImageNotFound("missing")

            def pull(self, image_ref: str) -> None:
                self.pull_calls.append(image_ref)

        images = Images()
        engine = SDKDockerEngine(type("Client", (), {"images": images})())

        with self.assertRaisesRegex(RuntimeError, "preloaded"):
            engine.resolve_local_image(
                "registry.example/platform-singleuser@sha256:" + "2" * 64
            )

        self.assertEqual(images.pull_calls, [])

    def test_sdk_initializer_keeps_one_docker_log_stream_enabled(self) -> None:
        class Containers:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def run(self, **kwargs) -> bytes:
                self.calls.append(kwargs.copy())
                if not kwargs.get("stdout", True) and not kwargs.get("stderr", False):
                    raise RuntimeError("Docker rejects an empty logs stream selection")
                return b""

        containers = Containers()
        engine = SDKDockerEngine(type("Client", (), {"containers": containers})())

        engine.initialize_volume(
            name="jupyter-user-test-1",
            image_id="sha256:" + "3" * 64,
            uid=1000,
            gid=100,
            mode="0700",
        )

        self.assertEqual(len(containers.calls), 1)
        call = containers.calls[0]
        self.assertIs(call["stdout"], True)
        self.assertIs(call["stderr"], False)
        self.assertIs(call["detach"], False)
        self.assertIs(call["remove"], True)
        self.assertEqual(call["cap_add"], ["CHOWN", "FOWNER", "FSETID", "DAC_OVERRIDE"])

    def test_deletion_recreates_only_exact_private_slot_and_is_retry_safe(self) -> None:
        engine = FakeDockerEngine()
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=Path(directory),
            )
            user_manifest = provisioner.provision(user_id=user_id, username="alice")
            slot = user_manifest["slots"][1]
            expected_labels = copy.deepcopy(
                engine.volumes[slot["volume_name"]]["Labels"]
            )
            result = provisioner.recreate_private_volume(
                workspace_id=workspace_id,
                deletion_id=str(uuid.uuid4()),
                owner_user_id=user_id,
                username="alice",
                workspace_spec_version=3,
                private_volume_slot_id=slot["slot_id"],
                private_volume_slot_number=slot["slot_number"],
                private_volume_name=slot["volume_name"],
            )
            # A retry after remove/recreate must not issue a second destructive
            # remove. It only reconciles and verifies the already-empty volume.
            checkpoint_value = json.loads(
                (Path(directory) / f"local-deletion-{workspace_id}.json").read_text()
            )
            deletion_id = checkpoint_value["deletion_id"]
            self.assertNotIn("operation_id", checkpoint_value)
            with self.assertRaisesRegex(RuntimeError, "binding mismatch"):
                provisioner.recreate_private_volume(
                    workspace_id=workspace_id,
                    deletion_id=str(uuid.uuid4()),
                    owner_user_id=user_id,
                    username="alice",
                    workspace_spec_version=3,
                    private_volume_slot_id=slot["slot_id"],
                    private_volume_slot_number=slot["slot_number"],
                    private_volume_name=slot["volume_name"],
                )
            # A user-visible retry operation may change, but the deletion job's
            # immutable identity remains bound to destructive progress.
            retried = provisioner.recreate_private_volume(
                workspace_id=workspace_id,
                deletion_id=deletion_id,
                owner_user_id=user_id,
                username="alice",
                workspace_spec_version=3,
                private_volume_slot_id=slot["slot_id"],
                private_volume_slot_number=slot["slot_number"],
                private_volume_name=slot["volume_name"],
            )
            provisioner.finalize_private_volume_recreation(workspace_id)
            checkpoint = Path(directory) / f"local-deletion-{workspace_id}.json"

        self.assertEqual(engine.removed_volumes, [slot["volume_name"]])
        self.assertEqual(engine.volumes[slot["volume_name"]]["Labels"], expected_labels)
        self.assertEqual(result, retried)
        self.assertEqual(result["mode"], "0700")
        self.assertIs(result["volume_recreated"], True)
        self.assertEqual(len(engine.root_verifications), 2)
        self.assertFalse(checkpoint.exists())

    def test_deletion_rejects_shared_or_label_drift_before_remove(self) -> None:
        engine = FakeDockerEngine()
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=Path(directory),
            )
            user_manifest = provisioner.provision(user_id=user_id, username="alice")
            slot = user_manifest["slots"][0]
            engine.volumes[slot["volume_name"]]["Labels"]["platform.shared"] = "true"
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                provisioner.recreate_private_volume(
                    workspace_id=workspace_id,
                    deletion_id=str(uuid.uuid4()),
                    owner_user_id=user_id,
                    username="alice",
                    workspace_spec_version=2,
                    private_volume_slot_id=slot["slot_id"],
                    private_volume_slot_number=slot["slot_number"],
                    private_volume_name=slot["volume_name"],
                )
        self.assertEqual(engine.removed_volumes, [])

    def test_deletion_recovers_from_crash_after_remove_before_removed_checkpoint(
        self,
    ) -> None:
        engine = FakeDockerEngine()
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        deletion_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            provisioner = LocalDockerProvisioner(
                engine=engine, profile_policy=self.policy, state_dir=state_dir
            )
            user_manifest = provisioner.provision(user_id=user_id, username="alice")
            slot = user_manifest["slots"][0]
            real_atomic = __import__("local_docker_provisioner").atomic_json
            checkpoint_writes = 0

            def fail_removed_checkpoint(path, value):
                nonlocal checkpoint_writes
                if path.name == f"local-deletion-{workspace_id}.json":
                    checkpoint_writes += 1
                    if checkpoint_writes == 2:
                        raise RuntimeError("synthetic crash after Docker remove")
                return real_atomic(path, value)

            arguments = {
                "workspace_id": workspace_id,
                "deletion_id": deletion_id,
                "owner_user_id": user_id,
                "username": "alice",
                "workspace_spec_version": 2,
                "private_volume_slot_id": slot["slot_id"],
                "private_volume_slot_number": slot["slot_number"],
                "private_volume_name": slot["volume_name"],
            }
            with (
                patch(
                    "local_docker_provisioner.atomic_json",
                    side_effect=fail_removed_checkpoint,
                ),
                self.assertRaisesRegex(RuntimeError, "synthetic crash"),
            ):
                provisioner.recreate_private_volume(**arguments)

            self.assertNotIn(slot["volume_name"], engine.volumes)
            prepared = json.loads(
                (state_dir / f"local-deletion-{workspace_id}.json").read_text()
            )
            self.assertEqual(prepared["phase"], "PREPARED")

            recovered = provisioner.recreate_private_volume(**arguments)

        self.assertEqual(engine.removed_volumes, [slot["volume_name"]])
        self.assertIs(recovered["volume_recreated"], True)
        self.assertIn(slot["volume_name"], engine.volumes)

    def test_prepared_retry_rejects_replaced_volume_with_wrong_labels(self) -> None:
        engine = FakeDockerEngine()
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            provisioner = LocalDockerProvisioner(
                engine=engine,
                profile_policy=self.policy,
                state_dir=Path(directory),
            )
            user_manifest = provisioner.provision(user_id=user_id, username="alice")
            slot = user_manifest["slots"][0]
            arguments = {
                "workspace_id": workspace_id,
                "deletion_id": str(uuid.uuid4()),
                "owner_user_id": user_id,
                "username": "alice",
                "workspace_spec_version": 2,
                "private_volume_slot_id": slot["slot_id"],
                "private_volume_slot_number": slot["slot_number"],
                "private_volume_name": slot["volume_name"],
            }
            with (
                patch.object(
                    engine,
                    "remove_volume",
                    side_effect=RuntimeError("synthetic crash before remove"),
                ),
                self.assertRaisesRegex(RuntimeError, "synthetic crash"),
            ):
                provisioner.recreate_private_volume(**arguments)
            checkpoint = json.loads(
                (Path(directory) / f"local-deletion-{workspace_id}.json").read_text()
            )
            self.assertEqual(checkpoint["phase"], "PREPARED")

            engine.volumes[slot["volume_name"]]["Labels"] = {
                "platform.managed": "true",
                "platform.shared": "true",
            }
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                provisioner.recreate_private_volume(**arguments)

        self.assertEqual(engine.removed_volumes, [])


class FakeControlClient:
    def __init__(self, responses: list[HTTPResult]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []

    def post(self, path: str, payload: dict) -> HTTPResult:
        self.requests.append((path, copy.deepcopy(payload)))
        return self.responses.pop(0)


class FakeProvisioner:
    def __init__(self, manifest: dict | None = None, error: Exception | None = None):
        self.manifest = manifest
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.deletion_calls: list[dict] = []
        self.finalized: list[str] = []

    def provision(self, *, user_id: str, username: str):
        self.calls.append((user_id, username))
        if self.error:
            raise self.error
        return copy.deepcopy(self.manifest)

    def recreate_private_volume(self, **values):
        self.deletion_calls.append(copy.deepcopy(values))
        if self.error:
            raise self.error
        return copy.deepcopy(self.manifest)

    def finalize_private_volume_recreation(self, workspace_id: str) -> None:
        self.finalized.append(workspace_id)


def claim_body(user_id: str) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "user_id": user_id,
            "username": "alice",
            "attempt_no": 2,
            "lease_expires_at": "2099-08-10T12:00:00Z",
        }
    ).encode()


def deletion_claim_body(
    *,
    user_id: str,
    workspace_id: str,
    deletion_id: str,
    operation_id: str,
    slot_id: str,
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "deletion_id": deletion_id,
            "operation_id": operation_id,
            "owner_user_id": user_id,
            "username": "alice",
            "server_name": "ws-0123456789abcdef0123456789abcdef",
            "workspace_spec_version": 4,
            "private_volume_slot_id": slot_id,
            "private_volume_slot_number": 1,
            "private_volume_name": "jupyter-user-alice-slot-1",
            "attempt_no": 2,
        }
    ).encode()


class LocalProvisionerAgentTests(unittest.TestCase):
    def test_managed_service_environment_is_an_exact_non_secret_allowlist(
        self,
    ) -> None:
        source = {
            "PLATFORM_ENV": "local-dev",
            "ALLOW_UNSAFE_LOCAL_DEV": "true",
            "PLATFORM_WEB_PROVISIONING_ENABLED": "true",
            "PLATFORM_WORKSPACE_DELETION_ENABLED": "true",
            "JUPYTER_STORAGE_POLICY_MODE": "docker-volume-unlimited-v1",
            "JUPYTERHUB_PROFILE_ALLOWLIST_FILE": "/etc/platform/profiles.json",
            "SPAWN_VALIDATOR_HMAC_KEY_FILE": "/run/secrets/spawn-hmac-key",
            "PLATFORM_PROVISIONING_CLAIM_URL": (
                "http://api:8000/internal/v1/user-provisioning/claim"
            ),
            "PLATFORM_PROVISIONING_COMPLETE_URL": (
                "http://api:8000/internal/v1/user-provisioning/complete"
            ),
            "PLATFORM_PROVISIONING_FAIL_URL": (
                "http://api:8000/internal/v1/user-provisioning/fail"
            ),
            "PLATFORM_DELETION_CLAIM_URL": (
                "http://api:8000/internal/v1/workspace-deletions/claim"
            ),
            "PLATFORM_DELETION_COMPLETE_URL": (
                "http://api:8000/internal/v1/workspace-deletions/complete"
            ),
            "PLATFORM_DELETION_FAIL_URL": (
                "http://api:8000/internal/v1/workspace-deletions/fail"
            ),
            "PLATFORM_LOCAL_PROVISIONING_STATE_DIR": (
                "/srv/jupyterhub/local-provisioning"
            ),
            "PLATFORM_PROVISIONING_POLL_SECONDS": "2",
            "PLATFORM_PROVISIONING_HTTP_TIMEOUT_SECONDS": "5",
            "PLATFORM_LOCAL_PROJECT_ID_START": "10000",
            # These values exist in the parent Hub but must never be copied.
            "CONFIGPROXY_AUTH_TOKEN": "raw-proxy-secret",
            "PLATFORM_OAUTH_CLIENT_SECRET": "raw-oauth-secret",
            "PLATFORM_RECONCILER_TOKEN": "raw-reconciler-secret",
            "SPAWN_VALIDATOR_HMAC_KEY": "raw-hmac-secret",
        }

        environment = managed_service_environment(source)

        self.assertEqual(
            set(environment),
            set(MANAGED_SERVICE_ENV_NAMES)
            | set(PROVISIONING_SERVICE_ENV_NAMES)
            | set(DELETION_SERVICE_ENV_NAMES),
        )
        self.assertEqual(
            environment["SPAWN_VALIDATOR_HMAC_KEY_FILE"],
            "/run/secrets/spawn-hmac-key",
        )
        serialized = json.dumps(environment)
        self.assertNotIn("raw-", serialized)
        self.assertFalse(
            {
                "CONFIGPROXY_AUTH_TOKEN",
                "PLATFORM_OAUTH_CLIENT_SECRET",
                "PLATFORM_RECONCILER_TOKEN",
                "SPAWN_VALIDATOR_HMAC_KEY",
            }
            & set(environment)
        )

    def test_domain_test_flag_is_forwarded_without_relaxing_the_allowlist(self) -> None:
        source = {
            name: "configured"
            for name in (
                *MANAGED_SERVICE_ENV_NAMES,
                *PROVISIONING_SERVICE_ENV_NAMES,
                *DELETION_SERVICE_ENV_NAMES,
            )
        }
        source.update(
            {
                "PLATFORM_ENV": "domain-test",
                "ALLOW_UNSAFE_LOCAL_DEV": "false",
                "ALLOW_UNSAFE_DOMAIN_TEST": "true",
                "PLATFORM_WEB_PROVISIONING_ENABLED": "true",
                "PLATFORM_WORKSPACE_DELETION_ENABLED": "true",
                "UNRELATED_UNSAFE_FLAG": "true",
            }
        )

        environment = managed_service_environment(source)

        self.assertEqual(environment["ALLOW_UNSAFE_DOMAIN_TEST"], "true")
        self.assertNotIn("UNRELATED_UNSAFE_FLAG", environment)

    def test_production_volume_capability_is_forwarded_without_secret_values(
        self,
    ) -> None:
        source = {
            name: "configured"
            for name in (
                *MANAGED_SERVICE_ENV_NAMES,
                *PROVISIONING_SERVICE_ENV_NAMES,
                *DELETION_SERVICE_ENV_NAMES,
            )
        }
        source.update(
            {
                "PLATFORM_ENV": "production",
                "ALLOW_UNSAFE_LOCAL_DEV": "false",
                "PLATFORM_WEB_PROVISIONING_ENABLED": "true",
                "PLATFORM_WORKSPACE_DELETION_ENABLED": "true",
                "PLATFORM_PRODUCTION_DOCKER_VOLUME_PROVISIONING_ENABLED": "true",
                "SPAWN_VALIDATOR_HMAC_KEY": "must-not-be-forwarded",
            }
        )

        environment = managed_service_environment(source)

        self.assertEqual(
            environment["PLATFORM_PRODUCTION_DOCKER_VOLUME_PROVISIONING_ENABLED"],
            "true",
        )
        self.assertNotIn("SPAWN_VALIDATOR_HMAC_KEY", environment)

    def test_managed_service_environment_fails_closed_when_a_key_is_missing(
        self,
    ) -> None:
        source = {
            name: "configured"
            for name in (
                *MANAGED_SERVICE_ENV_NAMES,
                *PROVISIONING_SERVICE_ENV_NAMES,
                *DELETION_SERVICE_ENV_NAMES,
            )
        }
        source["PLATFORM_WEB_PROVISIONING_ENABLED"] = "true"
        source["PLATFORM_WORKSPACE_DELETION_ENABLED"] = "true"
        del source["PLATFORM_PROVISIONING_FAIL_URL"]

        with self.assertRaisesRegex(AgentError, "FAIL_URL"):
            managed_service_environment(source)

    def test_production_configuration_requires_explicit_volume_capability(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError, "reviewed production Docker-volume mode"
        ):
            validate_web_provisioning_mode(
                platform_env="production",
                unsafe_local_dev=False,
                enabled=True,
            )
        validate_web_provisioning_mode(
            platform_env="production",
            unsafe_local_dev=False,
            enabled=True,
            production_docker_volume=True,
            storage_policy_mode="docker-volume-unlimited-v1",
        )

    def test_domain_test_web_provisioning_requires_its_own_flag(self) -> None:
        validate_web_provisioning_mode(
            platform_env="domain-test",
            unsafe_local_dev=False,
            unsafe_domain_test=True,
            enabled=True,
        )
        with self.assertRaisesRegex(RuntimeError, "explicit local test mode"):
            validate_web_provisioning_mode(
                platform_env="domain-test",
                unsafe_local_dev=True,
                unsafe_domain_test=False,
                enabled=True,
            )

    def test_production_deletion_is_separate_and_requires_unlimited_volume_mode(
        self,
    ) -> None:
        validate_workspace_deletion_mode(
            enabled=True,
            storage_policy_mode="docker-volume-unlimited-v1",
        )
        with self.assertRaisesRegex(RuntimeError, "docker-volume-unlimited"):
            validate_workspace_deletion_mode(
                enabled=True,
                storage_policy_mode="legacy-xfs-project-quota-v1",
            )

    def test_deletion_only_managed_environment_needs_no_signup_endpoints(self) -> None:
        source = {name: "configured" for name in MANAGED_SERVICE_ENV_NAMES}
        source.update(
            {
                "PLATFORM_WEB_PROVISIONING_ENABLED": "false",
                "PLATFORM_WORKSPACE_DELETION_ENABLED": "true",
                "JUPYTER_STORAGE_POLICY_MODE": "docker-volume-unlimited-v1",
                "PLATFORM_DELETION_CLAIM_URL": (
                    "http://api:8000/internal/v1/workspace-deletions/claim"
                ),
                "PLATFORM_DELETION_COMPLETE_URL": (
                    "http://api:8000/internal/v1/workspace-deletions/complete"
                ),
                "PLATFORM_DELETION_FAIL_URL": (
                    "http://api:8000/internal/v1/workspace-deletions/fail"
                ),
            }
        )

        environment = managed_service_environment(source)

        self.assertNotIn("PLATFORM_PROVISIONING_CLAIM_URL", environment)
        self.assertEqual(
            environment["PLATFORM_DELETION_CLAIM_URL"],
            source["PLATFORM_DELETION_CLAIM_URL"],
        )

    def test_control_endpoint_is_fixed_to_compose_api_service(self) -> None:
        with self.assertRaisesRegex(AgentError, "endpoint"):
            SignedJSONClient(
                key=b"x" * 32,
                claim_url="http://attacker.invalid:8000/internal/v1/user-provisioning/claim",
                complete_url="http://api:8000/internal/v1/user-provisioning/complete",
                fail_url="http://api:8000/internal/v1/user-provisioning/fail",
                deletion_claim_url="http://api:8000/internal/v1/workspace-deletions/claim",
                deletion_complete_url="http://api:8000/internal/v1/workspace-deletions/complete",
                deletion_fail_url="http://api:8000/internal/v1/workspace-deletions/fail",
            )

    def test_signing_matches_hmac_v1_canonical_contract(self) -> None:
        body = b'{"schema_version":1,"worker_id":"worker-1"}'
        key = b"test-key-that-is-at-least-thirty-two-bytes"
        headers = signed_headers(
            key=key,
            path="/internal/v1/user-provisioning/claim",
            body=body,
            timestamp=1_800_000_000,
            nonce="fixed-nonce",
        )
        digest = hashlib.sha256(body).hexdigest()
        canonical = (
            "v1\n1800000000\nfixed-nonce\nPOST\n"
            "/internal/v1/user-provisioning/claim\n"
            f"{digest}"
        ).encode("ascii")
        expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Platform-Content-SHA256"], digest)
        self.assertEqual(headers["X-Platform-Signature"], f"v1={expected}")

    def test_claim_provision_complete_contract(self) -> None:
        user_id = str(uuid.uuid4())
        manifest = {"schema_version": 1, "marker": "safe-test"}
        client = FakeControlClient(
            [HTTPResult(200, claim_body(user_id)), HTTPResult(204, b"")]
        )
        provisioner = FakeProvisioner(manifest=manifest)
        agent = LocalProvisionerAgent(
            client=client,
            provisioner=provisioner,
            worker_id="worker-1",
        )

        self.assertTrue(agent.run_once())
        self.assertEqual(provisioner.calls, [(user_id, "alice")])
        path, payload = client.requests[-1]
        self.assertEqual(path, COMPLETE_PATH)
        self.assertEqual(
            set(payload),
            {"schema_version", "worker_id", "user_id", "attempt_no", "manifest"},
        )
        self.assertEqual(payload["manifest"], manifest)

    def test_provision_failure_sends_no_exception_text(self) -> None:
        user_id = str(uuid.uuid4())
        client = FakeControlClient(
            [HTTPResult(200, claim_body(user_id)), HTTPResult(204, b"")]
        )
        provisioner = FakeProvisioner(error=RuntimeError("sensitive docker detail"))
        agent = LocalProvisionerAgent(
            client=client,
            provisioner=provisioner,
            worker_id="worker-1",
        )

        with patch("local_provisioner_agent.logging.getLogger"):
            self.assertTrue(agent.run_once())
        path, payload = client.requests[-1]
        self.assertEqual(path, FAIL_PATH)
        self.assertEqual(
            payload,
            {
                "schema_version": 1,
                "worker_id": "worker-1",
                "user_id": user_id,
                "attempt_no": 2,
            },
        )
        self.assertNotIn("sensitive", json.dumps(payload))

    def test_claim_with_extra_key_is_rejected(self) -> None:
        user_id = str(uuid.uuid4())
        value = json.loads(claim_body(user_id))
        value["unexpected"] = True
        client = FakeControlClient([HTTPResult(200, json.dumps(value).encode())])
        agent = LocalProvisionerAgent(
            client=client,
            provisioner=FakeProvisioner(manifest={}),
            worker_id="worker-1",
        )
        with self.assertRaisesRegex(AgentError, "schema mismatch"):
            agent.claim()

    def test_no_job_is_exact_empty_204(self) -> None:
        client = FakeControlClient([HTTPResult(204, b""), HTTPResult(204, b"")])
        agent = LocalProvisionerAgent(
            client=client,
            provisioner=FakeProvisioner(manifest={}),
            worker_id="worker-1",
        )
        self.assertFalse(agent.run_once())

    def test_deletion_claim_recreates_volume_and_completes_exact_manifest(self) -> None:
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        deletion_id = str(uuid.uuid4())
        operation_id = str(uuid.uuid4())
        slot_id = str(uuid.uuid5(uuid.UUID(user_id), "workspace-volume-slot-1"))
        manifest = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "volume_recreated": True,
        }
        client = FakeControlClient(
            [
                HTTPResult(
                    200,
                    deletion_claim_body(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        deletion_id=deletion_id,
                        operation_id=operation_id,
                        slot_id=slot_id,
                    ),
                ),
                HTTPResult(204, b""),
            ]
        )
        provisioner = FakeProvisioner(manifest=manifest)
        agent = LocalProvisionerAgent(
            client=client,
            provisioner=provisioner,
            worker_id="worker-1",
            provisioning_enabled=False,
            deletion_enabled=True,
        )

        self.assertTrue(agent.run_once())
        self.assertEqual(client.requests[0][0], DELETION_CLAIM_PATH)
        self.assertEqual(len(provisioner.deletion_calls), 1)
        self.assertEqual(provisioner.deletion_calls[0]["deletion_id"], deletion_id)
        self.assertNotIn("operation_id", provisioner.deletion_calls[0])
        self.assertEqual(provisioner.finalized, [workspace_id])
        path, payload = client.requests[-1]
        self.assertEqual(path, DELETION_COMPLETE_PATH)
        self.assertEqual(
            set(payload),
            {"schema_version", "worker_id", "workspace_id", "attempt_no", "manifest"},
        )
        self.assertEqual(payload["manifest"], manifest)

    def test_deletion_completion_rejection_releases_lease_without_waiting(self) -> None:
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        deletion_id = str(uuid.uuid4())
        operation_id = str(uuid.uuid4())
        slot_id = str(uuid.uuid5(uuid.UUID(user_id), "workspace-volume-slot-1"))
        client = FakeControlClient(
            [
                HTTPResult(
                    200,
                    deletion_claim_body(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        deletion_id=deletion_id,
                        operation_id=operation_id,
                        slot_id=slot_id,
                    ),
                ),
                HTTPResult(409, b'{"error":{"code":"MANIFEST_MISMATCH"}}'),
                HTTPResult(204, b""),
            ]
        )
        provisioner = FakeProvisioner(manifest={"schema_version": 1})
        agent = LocalProvisionerAgent(
            client=client,
            provisioner=provisioner,
            worker_id="worker-1",
            provisioning_enabled=False,
            deletion_enabled=True,
        )

        with patch("local_provisioner_agent.logging.getLogger"):
            self.assertTrue(agent.run_once())

        self.assertEqual(
            [path for path, _payload in client.requests],
            [DELETION_CLAIM_PATH, DELETION_COMPLETE_PATH, DELETION_FAIL_PATH],
        )
        self.assertEqual(provisioner.finalized, [])
        self.assertEqual(
            client.requests[-1][1],
            {
                "schema_version": 1,
                "worker_id": "worker-1",
                "workspace_id": workspace_id,
                "attempt_no": 2,
            },
        )

    def test_deletion_failure_reports_no_privileged_error_detail(self) -> None:
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        deletion_id = str(uuid.uuid4())
        operation_id = str(uuid.uuid4())
        slot_id = str(uuid.uuid5(uuid.UUID(user_id), "workspace-volume-slot-1"))
        client = FakeControlClient(
            [
                HTTPResult(
                    200,
                    deletion_claim_body(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        deletion_id=deletion_id,
                        operation_id=operation_id,
                        slot_id=slot_id,
                    ),
                ),
                HTTPResult(204, b""),
            ]
        )
        agent = LocalProvisionerAgent(
            client=client,
            provisioner=FakeProvisioner(
                error=RuntimeError("sensitive host volume path")
            ),
            worker_id="worker-1",
            provisioning_enabled=False,
            deletion_enabled=True,
        )

        with patch("local_provisioner_agent.logging.getLogger"):
            self.assertTrue(agent.run_once())
        path, payload = client.requests[-1]
        self.assertEqual(path, DELETION_FAIL_PATH)
        self.assertEqual(
            payload,
            {
                "schema_version": 1,
                "worker_id": "worker-1",
                "workspace_id": workspace_id,
                "attempt_no": 2,
            },
        )
        self.assertNotIn("sensitive", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
