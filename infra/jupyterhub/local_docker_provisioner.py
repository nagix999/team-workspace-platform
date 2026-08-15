"""Narrow Docker named-volume provisioning and deletion agent.

Only deterministic named volumes from :mod:`local_volume_policy` are accepted.
No image, command, mount path, network, or Docker option comes from a web request.
User provisioning stays behind the explicit unsafe local-test gate. The deletion
path may run in production only behind its separate policy flag and exact
``docker-volume-unlimited-v1`` contract. The Docker daemon socket remains a
host-root-equivalent capability in both cases.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Protocol

from local_volume_policy import (
    DEFAULT_PROJECT_ID_START,
    atomic_json,
    build_local_manifest,
    local_profile_runtime,
    private_volume_labels,
    reserve_project_id_block,
    shared_volume_labels,
    validate_username,
)


class DockerEngine(Protocol):
    def resolve_local_image(self, image_ref: str) -> str: ...

    def inspect_volume(self, name: str) -> dict[str, Any] | None: ...

    def create_volume(self, name: str, labels: dict[str, str]) -> None: ...

    def initialize_volume(
        self,
        *,
        name: str,
        image_id: str,
        uid: int,
        gid: int,
        mode: str,
    ) -> None: ...

    def remove_volume(self, name: str) -> None: ...

    def verify_volume_root(
        self, *, name: str, image_id: str, uid: int, gid: int, mode: str
    ) -> None: ...


class SDKDockerEngine:
    """Docker SDK adapter fixed to the host Unix socket and local images."""

    def __init__(self, client: Any, *, allow_image_pull: bool = False) -> None:
        self.client = client
        self.allow_image_pull = allow_image_pull

    @classmethod
    def from_host_socket(cls, *, allow_image_pull: bool = False) -> "SDKDockerEngine":
        import docker

        client = docker.DockerClient(
            base_url="unix:///var/run/docker.sock", version="auto", timeout=30
        )
        client.ping()
        return cls(client, allow_image_pull=allow_image_pull)

    def resolve_local_image(self, image_ref: str) -> str:
        import docker

        # Local tests may pull only the image selected by the read-only profile
        # allowlist. Production deletion requires the reviewed image to exist
        # locally already, then pins the helper run to its immutable image ID.
        try:
            image = self.client.images.get(image_ref)
        except docker.errors.ImageNotFound:
            if not self.allow_image_pull:
                raise RuntimeError(
                    "initializer image is not preloaded for production deletion"
                ) from None
            self.client.images.pull(image_ref)
            image = self.client.images.get(image_ref)
        image_id = getattr(image, "id", "")
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise RuntimeError("initializer image did not resolve to a local image ID")
        return image_id

    def inspect_volume(self, name: str) -> dict[str, Any] | None:
        import docker

        try:
            volume = self.client.volumes.get(name)
        except docker.errors.NotFound:
            return None
        volume.reload()
        return dict(volume.attrs)

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        self.client.volumes.create(
            name=name,
            driver="local",
            driver_opts={},
            labels=labels,
        )

    def initialize_volume(
        self,
        *,
        name: str,
        image_id: str,
        uid: int,
        gid: int,
        mode: str,
    ) -> None:
        from docker.types import Mount

        self.client.containers.run(
            image=image_id,
            command=[
                "-ceu",
                'chown "$1:$2" /volume; chmod "$3" /volume',
                "volume-init",
                str(uid),
                str(gid),
                mode,
            ],
            entrypoint="/bin/sh",
            user="0:0",
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            # FSETID is required to retain the setgid bit when root:shared-gid
            # is not one of the helper process's groups.
            cap_add=["CHOWN", "FOWNER", "FSETID", "DAC_OVERRIDE"],
            security_opt=["no-new-privileges:true"],
            privileged=False,
            pids_limit=64,
            mem_limit=256 * 1024 * 1024,
            nano_cpus=1_000_000_000,
            init=True,
            mounts=[
                Mount(target="/volume", source=name, type="volume", read_only=False)
            ],
            detach=False,
            remove=True,
            # docker-py always opens the logs endpoint for a foreground run.
            # Docker rejects that request when both streams are disabled, even
            # after this intentionally quiet helper exited successfully.
            stdout=True,
            stderr=False,
        )

    def remove_volume(self, name: str) -> None:
        """Remove one exact named volume without force.

        Docker refuses this operation while any running or stopped container
        still mounts the volume, which is the final daemon-side guard after the
        control plane has observed the named server as NOT_FOUND.
        """

        volume = self.client.volumes.get(name)
        volume.remove(force=False)

    def verify_volume_root(
        self, *, name: str, image_id: str, uid: int, gid: int, mode: str
    ) -> None:
        from docker.types import Mount

        output = self.client.containers.run(
            image=image_id,
            command=["-ceu", "stat -Lc '%F %a %u:%g' /volume"],
            entrypoint="/bin/sh",
            user="0:0",
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            privileged=False,
            pids_limit=32,
            mem_limit=128 * 1024 * 1024,
            nano_cpus=500_000_000,
            init=True,
            mounts=[
                Mount(target="/volume", source=name, type="volume", read_only=True)
            ],
            detach=False,
            remove=True,
            stdout=True,
            stderr=False,
        )
        expected = f"directory {int(mode, 8):o} {uid}:{gid}"
        try:
            actual = bytes(output).decode("ascii").strip()
        except (TypeError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "private volume root verification returned invalid data"
            ) from exc
        if actual != expected:
            raise RuntimeError("private volume root metadata failed exact verification")


def _verify_volume(
    engine: DockerEngine, *, name: str, expected_labels: dict[str, str]
) -> bool:
    value = engine.inspect_volume(name)
    if value is None:
        return False
    labels = value.get("Labels") or {}
    options = value.get("Options") or {}
    if (
        value.get("Name") != name
        or value.get("Driver") != "local"
        or options != {}
        or labels != expected_labels
    ):
        raise RuntimeError(f"existing Docker volume {name} conflicts with local policy")
    return True


def _ensure_volume(engine: DockerEngine, *, name: str, labels: dict[str, str]) -> None:
    if not _verify_volume(engine, name=name, expected_labels=labels):
        engine.create_volume(name, labels)
    if not _verify_volume(engine, name=name, expected_labels=labels):
        raise RuntimeError(f"Docker volume {name} failed post-create verification")


class LocalDockerProvisioner:
    def __init__(
        self,
        *,
        engine: DockerEngine,
        profile_policy: dict[str, Any],
        state_dir: Path,
        project_id_start: int = DEFAULT_PROJECT_ID_START,
    ) -> None:
        self.engine = engine
        self.profile_policy = profile_policy
        self.runtime = local_profile_runtime(profile_policy)
        self.state_dir = state_dir
        self.project_id_start = project_id_start

    def ensure_shared_volume(self) -> None:
        """Create/reconcile the one team volume before any user job is claimed."""

        image_id = self.engine.resolve_local_image(self.runtime["initializer_image"])
        shared = self.profile_policy["shared_volume"]
        _ensure_volume(
            self.engine,
            name=shared["name"],
            labels=shared_volume_labels(),
        )
        self.engine.initialize_volume(
            name=shared["name"],
            image_id=image_id,
            uid=0,
            gid=shared["gid"],
            mode="2770",
        )

    def provision(self, *, user_id: str, username: str) -> dict[str, Any]:
        output = self.state_dir / f"local-user-{user_id}.json"
        project_id_base = reserve_project_id_block(
            output=output,
            user_id=user_id,
            username=username,
            project_id_start=self.project_id_start,
        )
        manifest = build_local_manifest(
            user_id=user_id,
            username=username,
            uid=self.runtime["uid"],
            gid=self.runtime["gid"],
            hard_limit_bytes=self.runtime["hard_limit_bytes"],
            project_id_base=project_id_base,
        )

        # This check is intentionally first: a missing image must not leave a
        # partially-created user's volume inventory behind.
        image_id = self.engine.resolve_local_image(self.runtime["initializer_image"])
        shared = self.profile_policy["shared_volume"]
        _ensure_volume(self.engine, name=shared["name"], labels=shared_volume_labels())
        self.engine.initialize_volume(
            name=shared["name"],
            image_id=image_id,
            uid=0,
            gid=shared["gid"],
            mode="2770",
        )

        for slot in manifest["slots"]:
            labels = private_volume_labels(
                user_id=manifest["user_id"],
                username=manifest["username"],
                slot=slot,
            )
            _ensure_volume(
                self.engine,
                name=slot["volume_name"],
                labels=labels,
            )
            self.engine.initialize_volume(
                name=slot["volume_name"],
                image_id=image_id,
                uid=self.runtime["uid"],
                gid=self.runtime["gid"],
                mode="0700",
            )

        # The manifest becomes visible to the API/host CLI only after all five
        # volumes and their root metadata have completed successfully.
        atomic_json(output, manifest)
        return manifest

    def _deletion_checkpoint(self, workspace_id: str) -> Path:
        return self.state_dir / f"local-deletion-{workspace_id}.json"

    def _private_slot_from_existing_volume(
        self,
        *,
        owner_user_id: str,
        username: str,
        private_volume_slot_id: str,
        private_volume_slot_number: int,
        private_volume_name: str,
    ) -> dict[str, Any]:
        value = self.engine.inspect_volume(private_volume_name)
        if value is None:
            raise RuntimeError("private volume is missing before deletion")
        labels = value.get("Labels") or {}
        options = value.get("Options") or {}
        expected_label_keys = {
            "platform.managed",
            "platform.provisioned",
            "platform.owner.user_id",
            "platform.owner.username",
            "platform.volume.slot",
            "platform.volume.slot_id",
            "platform.quota.hard_bytes",
            "platform.quota.enforced",
            "platform.quota.project_id",
        }
        expected_identity = {
            "platform.managed": "true",
            "platform.provisioned": "true",
            "platform.owner.user_id": owner_user_id,
            "platform.owner.username": username,
            "platform.volume.slot": str(private_volume_slot_number),
            "platform.volume.slot_id": private_volume_slot_id,
            "platform.quota.enforced": "false",
        }
        if (
            value.get("Name") != private_volume_name
            or value.get("Driver") != "local"
            or options != {}
            or set(labels) != expected_label_keys
            or any(
                labels.get(key) != expected
                for key, expected in expected_identity.items()
            )
            or labels.get("platform.shared") is not None
        ):
            raise RuntimeError("private volume identity conflicts with deletion policy")
        try:
            hard_limit_bytes = int(labels["platform.quota.hard_bytes"], 10)
            project_id = int(labels["platform.quota.project_id"], 10)
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("private volume numeric labels are invalid") from None
        if hard_limit_bytes != self.runtime["hard_limit_bytes"] or project_id <= 0:
            raise RuntimeError("private volume numeric labels conflict with policy")
        return {
            "slot_id": private_volume_slot_id,
            "slot_number": private_volume_slot_number,
            "volume_name": private_volume_name,
            "hard_limit_bytes": hard_limit_bytes,
            "project_id": project_id,
        }

    def recreate_private_volume(
        self,
        *,
        workspace_id: str,
        deletion_id: str,
        owner_user_id: str,
        username: str,
        workspace_spec_version: int,
        private_volume_slot_id: str,
        private_volume_slot_number: int,
        private_volume_name: str,
    ) -> dict[str, Any]:
        """Irreversibly wipe and recreate one lease-bound private slot.

        A PREPARED checkpoint is durably written before Docker remove and is
        promoted to REMOVED afterward. Retries distinguish an old exact volume
        that still must be removed from an already-absent one, then initialize
        the new empty volume without deleting a possibly reused slot twice.
        """

        for label, value in (
            ("workspace", workspace_id),
            ("deletion", deletion_id),
            ("owner", owner_user_id),
            ("slot", private_volume_slot_id),
        ):
            try:
                parsed = uuid.UUID(value)
            except (AttributeError, TypeError, ValueError):
                raise RuntimeError(f"deletion {label} ID is invalid") from None
            if str(parsed) != value:
                raise RuntimeError(f"deletion {label} ID is invalid")

        if (
            not isinstance(workspace_spec_version, int)
            or isinstance(workspace_spec_version, bool)
            or workspace_spec_version <= 0
            or not isinstance(private_volume_slot_number, int)
            or isinstance(private_volume_slot_number, bool)
            or private_volume_slot_number not in range(1, 6)
        ):
            raise RuntimeError("deletion claim version or slot is invalid")
        validate_username(username, where="deletion")
        expected_slot_id = str(
            uuid.uuid5(
                uuid.UUID(owner_user_id),
                f"workspace-volume-slot-{private_volume_slot_number}",
            )
        )
        if (
            private_volume_slot_id != expected_slot_id
            or private_volume_name
            != f"jupyter-user-{username}-slot-{private_volume_slot_number}"
            or private_volume_name == self.profile_policy["shared_volume"]["name"]
        ):
            raise RuntimeError("deletion claim does not match private slot inventory")

        claim_binding = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            # The user-visible operation may be replaced when a failed job is
            # retried.  Bind destructive progress to the deletion job's stable
            # identity instead, so PREPARED/REMOVED checkpoints remain safely
            # resumable without accepting a different deletion generation.
            "deletion_id": deletion_id,
            "owner_user_id": owner_user_id,
            "username": username,
            "workspace_spec_version": workspace_spec_version,
            "private_volume_slot_id": private_volume_slot_id,
            "private_volume_slot_number": private_volume_slot_number,
            "private_volume_name": private_volume_name,
        }
        checkpoint = self._deletion_checkpoint(workspace_id)
        if checkpoint.exists():
            if checkpoint.is_symlink():
                raise RuntimeError("deletion checkpoint must not be a symlink")
            try:
                existing_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("deletion checkpoint is invalid") from exc
            if not isinstance(existing_checkpoint, dict):
                raise RuntimeError("deletion checkpoint binding mismatch")
            checkpoint_claim = {
                key: value
                for key, value in existing_checkpoint.items()
                if key not in {"phase", "hard_limit_bytes", "project_id"}
            }
            if (
                existing_checkpoint.get("phase") not in {"PREPARED", "REMOVED"}
                or set(existing_checkpoint)
                != {*claim_binding, "phase", "hard_limit_bytes", "project_id"}
                or checkpoint_claim != claim_binding
            ):
                raise RuntimeError("deletion checkpoint binding mismatch")
            hard_limit_bytes = existing_checkpoint["hard_limit_bytes"]
            project_id = existing_checkpoint["project_id"]
            if (
                isinstance(hard_limit_bytes, bool)
                or not isinstance(hard_limit_bytes, int)
                or hard_limit_bytes != self.runtime["hard_limit_bytes"]
                or isinstance(project_id, bool)
                or not isinstance(project_id, int)
                or project_id <= 0
            ):
                raise RuntimeError("deletion checkpoint volume policy is invalid")
            slot = {
                "slot_id": private_volume_slot_id,
                "slot_number": private_volume_slot_number,
                "volume_name": private_volume_name,
                "hard_limit_bytes": hard_limit_bytes,
                "project_id": project_id,
            }
        else:
            # Persist the exact destructive intent before touching Docker. If
            # the process dies immediately after remove(), PREPARED + a missing
            # volume is sufficient proof to resume at REMOVED safely.
            slot = self._private_slot_from_existing_volume(
                owner_user_id=owner_user_id,
                username=username,
                private_volume_slot_id=private_volume_slot_id,
                private_volume_slot_number=private_volume_slot_number,
                private_volume_name=private_volume_name,
            )
            existing_checkpoint = {
                **claim_binding,
                "hard_limit_bytes": slot["hard_limit_bytes"],
                "project_id": slot["project_id"],
                "phase": "PREPARED",
            }
            atomic_json(checkpoint, existing_checkpoint)

        expected_labels = private_volume_labels(
            user_id=owner_user_id, username=username, slot=slot
        )

        if existing_checkpoint["phase"] == "PREPARED":
            inspected = self.engine.inspect_volume(private_volume_name)
            if inspected is not None:
                # Re-run the exact label proof immediately before every retry's
                # destructive call. PREPARED must never skip remove merely
                # because the old volume still exists.
                if not _verify_volume(
                    self.engine,
                    name=private_volume_name,
                    expected_labels=expected_labels,
                ):
                    raise RuntimeError(
                        "private volume disappeared during deletion verification"
                    )
                self.engine.remove_volume(private_volume_name)
            if self.engine.inspect_volume(private_volume_name) is not None:
                raise RuntimeError("private volume still exists after deletion")
            existing_checkpoint = {**existing_checkpoint, "phase": "REMOVED"}
            atomic_json(checkpoint, existing_checkpoint)

        if existing_checkpoint["phase"] != "REMOVED":  # pragma: no cover - schema guard
            raise RuntimeError("deletion checkpoint phase is invalid")

        image_id = self.engine.resolve_local_image(self.runtime["initializer_image"])
        _ensure_volume(self.engine, name=private_volume_name, labels=expected_labels)
        self.engine.initialize_volume(
            name=private_volume_name,
            image_id=image_id,
            uid=self.runtime["uid"],
            gid=self.runtime["gid"],
            mode="0700",
        )
        if not _verify_volume(
            self.engine, name=private_volume_name, expected_labels=expected_labels
        ):
            raise RuntimeError("recreated private volume failed label verification")
        self.engine.verify_volume_root(
            name=private_volume_name,
            image_id=image_id,
            uid=self.runtime["uid"],
            gid=self.runtime["gid"],
            mode="0700",
        )
        return {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "owner_user_id": owner_user_id,
            "username": username,
            "workspace_spec_version": workspace_spec_version,
            "private_volume_slot_id": private_volume_slot_id,
            "private_volume_slot_number": private_volume_slot_number,
            "private_volume_name": private_volume_name,
            "hard_limit_bytes": slot["hard_limit_bytes"],
            "project_id": slot["project_id"],
            "uid": self.runtime["uid"],
            "gid": self.runtime["gid"],
            "mode": "0700",
            "volume_recreated": True,
        }

    def finalize_private_volume_recreation(self, workspace_id: str) -> None:
        """Remove only the exact per-workspace deletion checkpoint."""

        checkpoint = self._deletion_checkpoint(workspace_id)
        try:
            checkpoint.unlink()
        except FileNotFoundError:
            return
