#!/usr/bin/env python3
"""Hub-managed volume provisioning and destructive deletion poller.

Signup provisioning is local-test-only. Workspace deletion is an independent,
explicit capability that may run in production for the reviewed unlimited
Docker named-volume storage policy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from local_docker_provisioner import LocalDockerProvisioner, SDKDockerEngine
from local_volume_policy import (
    DEFAULT_PROJECT_ID_START,
    canonical_user_id,
    validate_web_provisioning_mode,
    validate_workspace_deletion_mode,
    validate_username,
)
from profile_policy import load_profile_policy
from public_url_policy import validate_runtime_mode


CLAIM_PATH = "/internal/v1/user-provisioning/claim"
COMPLETE_PATH = "/internal/v1/user-provisioning/complete"
FAIL_PATH = "/internal/v1/user-provisioning/fail"
DELETION_CLAIM_PATH = "/internal/v1/workspace-deletions/claim"
DELETION_COMPLETE_PATH = "/internal/v1/workspace-deletions/complete"
DELETION_FAIL_PATH = "/internal/v1/workspace-deletions/fail"
CLAIM_KEYS = {
    "schema_version",
    "user_id",
    "username",
    "attempt_no",
    "lease_expires_at",
}
DELETION_CLAIM_KEYS = {
    "schema_version",
    "workspace_id",
    "deletion_id",
    "operation_id",
    "owner_user_id",
    "username",
    "server_name",
    "workspace_spec_version",
    "private_volume_slot_id",
    "private_volume_slot_number",
    "private_volume_name",
    "attempt_no",
}
SERVER_NAME_RE = re.compile(r"^ws-[a-z0-9](?:[a-z0-9-]{6,61}[a-z0-9])$")
MAX_RESPONSE_BYTES = 64 * 1024
MANAGED_SERVICE_ENV_NAMES = (
    "PLATFORM_ENV",
    "ALLOW_UNSAFE_LOCAL_DEV",
    "PLATFORM_WEB_PROVISIONING_ENABLED",
    "PLATFORM_WORKSPACE_DELETION_ENABLED",
    "JUPYTER_STORAGE_POLICY_MODE",
    "JUPYTERHUB_PROFILE_ALLOWLIST_FILE",
    "SPAWN_VALIDATOR_HMAC_KEY_FILE",
    "PLATFORM_LOCAL_PROVISIONING_STATE_DIR",
    "PLATFORM_PROVISIONING_POLL_SECONDS",
    "PLATFORM_PROVISIONING_HTTP_TIMEOUT_SECONDS",
)
PROVISIONING_SERVICE_ENV_NAMES = (
    "PLATFORM_PROVISIONING_CLAIM_URL",
    "PLATFORM_PROVISIONING_COMPLETE_URL",
    "PLATFORM_PROVISIONING_FAIL_URL",
    "PLATFORM_LOCAL_PROJECT_ID_START",
)
DELETION_SERVICE_ENV_NAMES = (
    "PLATFORM_DELETION_CLAIM_URL",
    "PLATFORM_DELETION_COMPLETE_URL",
    "PLATFORM_DELETION_FAIL_URL",
)
MANAGED_SERVICE_OPTIONAL_ENV_NAMES = ("ALLOW_UNSAFE_DOMAIN_TEST",)


class AgentError(RuntimeError):
    pass


class AgentResponseError(AgentError):
    """A deterministic non-success response from the signed control plane."""

    def __init__(self, status: int) -> None:
        super().__init__("provisioning control-plane rejected the request")
        self.status = status


def managed_service_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Return only the configuration a Hub-managed provisioner must inherit.

    JupyterHub intentionally starts managed services with a fresh environment.
    Keep this an explicit allowlist: in particular, the HMAC key is referenced by
    its mounted file path and no OAuth, proxy, reconciler, or raw secret value is
    copied into the service configuration.
    """

    environment: dict[str, str] = {}
    for name in MANAGED_SERVICE_ENV_NAMES:
        value = source.get(name, "").strip()
        if not value:
            raise AgentError(f"required managed-service environment {name} is unset")
        environment[name] = value
    flags = {}
    for name in (
        "PLATFORM_WEB_PROVISIONING_ENABLED",
        "PLATFORM_WORKSPACE_DELETION_ENABLED",
    ):
        if environment[name] not in {"true", "false"}:
            raise AgentError(f"managed-service flag {name} must be true or false")
        flags[name] = environment[name] == "true"
    conditional_names: tuple[str, ...] = ()
    if flags["PLATFORM_WEB_PROVISIONING_ENABLED"]:
        conditional_names += PROVISIONING_SERVICE_ENV_NAMES
    if flags["PLATFORM_WORKSPACE_DELETION_ENABLED"]:
        conditional_names += DELETION_SERVICE_ENV_NAMES
    for name in conditional_names:
        value = source.get(name, "").strip()
        if not value:
            raise AgentError(f"required managed-service environment {name} is unset")
        environment[name] = value
    for name in MANAGED_SERVICE_OPTIONAL_ENV_NAMES:
        value = source.get(name, "").strip()
        if value:
            environment[name] = value
    return environment


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    except (TypeError, UnicodeEncodeError) as exc:
        raise AgentError("provisioning request is not canonical ASCII JSON") from exc


def signed_headers(
    *,
    key: bytes,
    path: str,
    body: bytes,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    timestamp_value = str(int(time.time()) if timestamp is None else timestamp)
    nonce_value = nonce or secrets.token_urlsafe(24)
    digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        ("v1", timestamp_value, nonce_value, "POST", path, digest)
    ).encode("ascii")
    signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Platform-HMAC-Version": "v1",
        "X-Platform-Timestamp": timestamp_value,
        "X-Platform-Nonce": nonce_value,
        "X-Platform-Content-SHA256": digest,
        "X-Platform-Signature": f"v1={signature}",
    }


def _validate_endpoint(url: str, expected_path: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "api"
        or parsed.port != 8000
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
    ):
        raise AgentError(f"provisioning endpoint for {expected_path} is invalid")
    return url


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: bytes


class SignedJSONClient:
    def __init__(
        self,
        *,
        key: bytes,
        claim_url: str | None = None,
        complete_url: str | None = None,
        fail_url: str | None = None,
        deletion_claim_url: str | None = None,
        deletion_complete_url: str | None = None,
        deletion_fail_url: str | None = None,
        timeout_seconds: float = 5.0,
        open_request: Callable[..., Any] | None = None,
    ) -> None:
        if len(key) < 32:
            raise AgentError("provisioning HMAC key is too short")
        self.key = key
        self.urls: dict[str, str] = {}
        endpoint_groups = (
            (
                (claim_url, complete_url, fail_url),
                (CLAIM_PATH, COMPLETE_PATH, FAIL_PATH),
            ),
            (
                (deletion_claim_url, deletion_complete_url, deletion_fail_url),
                (DELETION_CLAIM_PATH, DELETION_COMPLETE_PATH, DELETION_FAIL_PATH),
            ),
        )
        for values, paths in endpoint_groups:
            if all(value is None for value in values):
                continue
            if any(value is None for value in values):
                raise AgentError("control-plane endpoint group is incomplete")
            for value, path in zip(values, paths, strict=True):
                assert value is not None
                self.urls[path] = _validate_endpoint(value, path)
        if not self.urls:
            raise AgentError("no volume-agent endpoint is configured")
        if not 0 < timeout_seconds <= 30:
            raise AgentError("provisioning HTTP timeout is outside the permitted range")
        self.timeout_seconds = timeout_seconds
        self.open_request = open_request or build_opener(_NoRedirect).open

    def post(self, path: str, payload: dict[str, Any]) -> HTTPResult:
        if path not in self.urls:
            raise AgentError("unknown provisioning endpoint path")
        body = _canonical_json(payload)
        request = Request(
            self.urls[path],
            data=body,
            headers=signed_headers(key=self.key, path=path, body=body),
            method="POST",
        )
        try:
            with self.open_request(request, timeout=self.timeout_seconds) as response:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(response.status)
        except HTTPError as exc:
            try:
                response_body = exc.read(MAX_RESPONSE_BYTES + 1)
                status = int(exc.code)
            finally:
                exc.close()
        except (URLError, TimeoutError, OSError) as exc:
            raise AgentError("provisioning control-plane request failed") from exc
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise AgentError("provisioning control-plane response is too large")
        return HTTPResult(status=status, body=response_body)


@dataclass(frozen=True)
class ProvisioningClaim:
    user_id: str
    username: str
    attempt_no: int


@dataclass(frozen=True)
class DeletionClaim:
    workspace_id: str
    deletion_id: str
    operation_id: str
    owner_user_id: str
    username: str
    server_name: str
    workspace_spec_version: int
    private_volume_slot_id: str
    private_volume_slot_number: int
    private_volume_name: str
    attempt_no: int


class LocalProvisionerAgent:
    def __init__(
        self,
        *,
        client: SignedJSONClient,
        provisioner: LocalDockerProvisioner,
        worker_id: str,
        provisioning_enabled: bool = True,
        deletion_enabled: bool = True,
    ) -> None:
        if not worker_id or len(worker_id) > 128 or not worker_id.isascii():
            raise AgentError("provisioning worker ID is invalid")
        self.client = client
        self.provisioner = provisioner
        self.worker_id = worker_id
        self.provisioning_enabled = provisioning_enabled
        self.deletion_enabled = deletion_enabled
        if not provisioning_enabled and not deletion_enabled:
            raise AgentError("volume agent has no enabled capability")

    def claim(self) -> ProvisioningClaim | None:
        result = self.client.post(
            CLAIM_PATH,
            {"schema_version": 1, "worker_id": self.worker_id},
        )
        if result.status == 204:
            if result.body:
                raise AgentError("empty provisioning claim response contained a body")
            return None
        if result.status != 200:
            raise AgentError("provisioning claim returned an unexpected status")
        try:
            value = json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError("provisioning claim returned invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != CLAIM_KEYS:
            raise AgentError("provisioning claim response schema mismatch")
        if value["schema_version"] != 1:
            raise AgentError("provisioning claim schema version mismatch")
        user_id = canonical_user_id(value["user_id"], where="claimed")
        username = validate_username(value["username"], where="claimed")
        attempt_no = value["attempt_no"]
        if (
            isinstance(attempt_no, bool)
            or not isinstance(attempt_no, int)
            or attempt_no <= 0
        ):
            raise AgentError("provisioning claim attempt number is invalid")
        lease_expires_at = value["lease_expires_at"]
        if (
            not isinstance(lease_expires_at, str)
            or not 1 <= len(lease_expires_at) <= 64
            or not lease_expires_at.endswith("Z")
        ):
            raise AgentError("provisioning claim lease expiry is invalid")
        try:
            lease_expiry = datetime.fromisoformat(lease_expires_at[:-1] + "+00:00")
        except ValueError as exc:
            raise AgentError("provisioning claim lease expiry is invalid") from exc
        if lease_expiry <= datetime.now(timezone.utc):
            raise AgentError("provisioning claim lease is already expired")
        return ProvisioningClaim(user_id, username, attempt_no)

    def _expect_empty_success(self, path: str, payload: dict[str, Any]) -> None:
        result = self.client.post(path, payload)
        if result.status != 204 or result.body:
            raise AgentResponseError(result.status)

    @staticmethod
    def _canonical_uuid(value: Any, where: str) -> str:
        if not isinstance(value, str):
            raise AgentError(f"deletion {where} is invalid")
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            raise AgentError(f"deletion {where} is invalid") from None
        if str(parsed) != value:
            raise AgentError(f"deletion {where} is invalid")
        return value

    def claim_deletion(self) -> DeletionClaim | None:
        result = self.client.post(
            DELETION_CLAIM_PATH,
            {"schema_version": 1, "worker_id": self.worker_id},
        )
        if result.status == 204:
            if result.body:
                raise AgentError("empty deletion claim response contained a body")
            return None
        if result.status != 200:
            raise AgentError("deletion claim returned an unexpected status")
        try:
            value = json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError("deletion claim returned invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != DELETION_CLAIM_KEYS:
            raise AgentError("deletion claim response schema mismatch")
        if value["schema_version"] != 1:
            raise AgentError("deletion claim schema version mismatch")
        workspace_id = self._canonical_uuid(value["workspace_id"], "workspace ID")
        deletion_id = self._canonical_uuid(value["deletion_id"], "deletion ID")
        operation_id = self._canonical_uuid(value["operation_id"], "operation ID")
        owner_user_id = canonical_user_id(value["owner_user_id"], where="deletion")
        slot_id = self._canonical_uuid(value["private_volume_slot_id"], "slot ID")
        username = validate_username(value["username"], where="deletion")
        server_name = value["server_name"]
        spec_version = value["workspace_spec_version"]
        slot_number = value["private_volume_slot_number"]
        attempt_no = value["attempt_no"]
        if not isinstance(server_name, str) or not SERVER_NAME_RE.fullmatch(
            server_name
        ):
            raise AgentError("deletion server name is invalid")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in (spec_version, attempt_no)
        ):
            raise AgentError("deletion version or attempt is invalid")
        if (
            isinstance(slot_number, bool)
            or not isinstance(slot_number, int)
            or slot_number not in range(1, 6)
        ):
            raise AgentError("deletion slot number is invalid")
        volume_name = value["private_volume_name"]
        if volume_name != f"jupyter-user-{username}-slot-{slot_number}":
            raise AgentError("deletion private volume name is invalid")
        return DeletionClaim(
            workspace_id=workspace_id,
            deletion_id=deletion_id,
            operation_id=operation_id,
            owner_user_id=owner_user_id,
            username=username,
            server_name=server_name,
            workspace_spec_version=spec_version,
            private_volume_slot_id=slot_id,
            private_volume_slot_number=slot_number,
            private_volume_name=volume_name,
            attempt_no=attempt_no,
        )

    def _run_deletion_once(self) -> bool:
        claim = self.claim_deletion()
        if claim is None:
            return False
        try:
            manifest = self.provisioner.recreate_private_volume(
                workspace_id=claim.workspace_id,
                deletion_id=claim.deletion_id,
                owner_user_id=claim.owner_user_id,
                username=claim.username,
                workspace_spec_version=claim.workspace_spec_version,
                private_volume_slot_id=claim.private_volume_slot_id,
                private_volume_slot_number=claim.private_volume_slot_number,
                private_volume_name=claim.private_volume_name,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "local private volume deletion failed for a claimed job"
            )
            self._expect_empty_success(
                DELETION_FAIL_PATH,
                {
                    "schema_version": 1,
                    "worker_id": self.worker_id,
                    "workspace_id": claim.workspace_id,
                    "attempt_no": claim.attempt_no,
                },
            )
            return True
        try:
            self._expect_empty_success(
                DELETION_COMPLETE_PATH,
                {
                    "schema_version": 1,
                    "worker_id": self.worker_id,
                    "workspace_id": claim.workspace_id,
                    "attempt_no": claim.attempt_no,
                    "manifest": manifest,
                },
            )
        except AgentResponseError as exc:
            if not 400 <= exc.status < 500:
                raise
            # A signed 4xx is a deterministic rejection, not an ambiguous
            # network outcome. Release the lease immediately instead of making
            # the user wait for its full expiry before the bounded retry.
            logging.getLogger(__name__).exception(
                "workspace deletion completion was rejected by the control plane"
            )
            self._expect_empty_success(
                DELETION_FAIL_PATH,
                {
                    "schema_version": 1,
                    "worker_id": self.worker_id,
                    "workspace_id": claim.workspace_id,
                    "attempt_no": claim.attempt_no,
                },
            )
            return True
        self.provisioner.finalize_private_volume_recreation(claim.workspace_id)
        return True

    def run_once(self) -> bool:
        claim = self.claim() if self.provisioning_enabled else None
        if claim is None:
            return self._run_deletion_once() if self.deletion_enabled else False
        try:
            manifest = self.provisioner.provision(
                user_id=claim.user_id,
                username=claim.username,
            )
        except Exception:
            # Docker/host details stay in the privileged service log and are not
            # reflected into the API request or user-visible database fields.
            logging.getLogger(__name__).exception(
                "local user volume provisioning failed for a claimed job"
            )
            self._expect_empty_success(
                FAIL_PATH,
                {
                    "schema_version": 1,
                    "worker_id": self.worker_id,
                    "user_id": claim.user_id,
                    "attempt_no": claim.attempt_no,
                },
            )
            return True
        self._expect_empty_success(
            COMPLETE_PATH,
            {
                "schema_version": 1,
                "worker_id": self.worker_id,
                "user_id": claim.user_id,
                "attempt_no": claim.attempt_no,
                "manifest": manifest,
            },
        )
        return True


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AgentError(f"required environment variable {name} is unset")
    return value


def _read_hmac_key() -> bytes:
    path = Path(_required_env("SPAWN_VALIDATOR_HMAC_KEY_FILE"))
    try:
        stat = path.stat()
        if stat.st_mode & 0o022:
            raise AgentError("provisioning HMAC key file is writable by group/world")
        value = path.read_bytes().strip()
    except OSError as exc:
        raise AgentError("cannot read provisioning HMAC key file") from exc
    if len(value) < 32:
        raise AgentError("provisioning HMAC key is too short")
    return value


def build_agent_from_env() -> tuple[LocalProvisionerAgent, float]:
    provisioning_enabled = os.environ.get("PLATFORM_WEB_PROVISIONING_ENABLED") == "true"
    deletion_enabled = os.environ.get("PLATFORM_WORKSPACE_DELETION_ENABLED") == "true"
    try:
        mode = validate_runtime_mode(
            platform_env=os.environ.get("PLATFORM_ENV", "production"),
            unsafe_local_dev=os.environ.get("ALLOW_UNSAFE_LOCAL_DEV") == "true",
            unsafe_domain_test=(os.environ.get("ALLOW_UNSAFE_DOMAIN_TEST") == "true"),
        )
        validate_web_provisioning_mode(
            platform_env=mode.name,
            unsafe_local_dev=os.environ.get("ALLOW_UNSAFE_LOCAL_DEV") == "true",
            unsafe_domain_test=(os.environ.get("ALLOW_UNSAFE_DOMAIN_TEST") == "true"),
            enabled=provisioning_enabled,
        )
        validate_workspace_deletion_mode(
            enabled=deletion_enabled,
            storage_policy_mode=os.environ.get("JUPYTER_STORAGE_POLICY_MODE", ""),
        )
    except RuntimeError as exc:
        raise AgentError(str(exc)) from exc
    if not provisioning_enabled and not deletion_enabled:
        raise AgentError("volume agent has no enabled capability")

    state_dir = Path(_required_env("PLATFORM_LOCAL_PROVISIONING_STATE_DIR"))
    if state_dir != Path("/srv/jupyterhub/local-provisioning"):
        raise AgentError(
            "local provisioning state path is outside the fixed bind mount"
        )
    try:
        poll_seconds = float(os.environ.get("PLATFORM_PROVISIONING_POLL_SECONDS", "2"))
        timeout_seconds = float(
            os.environ.get("PLATFORM_PROVISIONING_HTTP_TIMEOUT_SECONDS", "5")
        )
        project_id_start = int(
            os.environ.get(
                "PLATFORM_LOCAL_PROJECT_ID_START", str(DEFAULT_PROJECT_ID_START)
            )
        )
    except ValueError as exc:
        raise AgentError("local provisioner numeric configuration is invalid") from exc
    if not 0.25 <= poll_seconds <= 60:
        raise AgentError("local provisioner polling interval is invalid")

    profile_path = _required_env("JUPYTERHUB_PROFILE_ALLOWLIST_FILE")
    profile_policy = load_profile_policy(
        profile_path, allow_unsafe_images=mode.unsafe_local_runtime
    )
    engine = SDKDockerEngine.from_host_socket(
        allow_image_pull=mode.unsafe_local_runtime
    )
    provisioner = LocalDockerProvisioner(
        engine=engine,
        profile_policy=profile_policy,
        state_dir=state_dir,
        project_id_start=project_id_start,
    )
    # Reconcile the shared root on every managed-agent start, including upgrades
    # where all users are already ACTIVE and no new provisioning job will run.
    if provisioning_enabled:
        provisioner.ensure_shared_volume()
    client = SignedJSONClient(
        key=_read_hmac_key(),
        claim_url=(
            _required_env("PLATFORM_PROVISIONING_CLAIM_URL")
            if provisioning_enabled
            else None
        ),
        complete_url=(
            _required_env("PLATFORM_PROVISIONING_COMPLETE_URL")
            if provisioning_enabled
            else None
        ),
        fail_url=(
            _required_env("PLATFORM_PROVISIONING_FAIL_URL")
            if provisioning_enabled
            else None
        ),
        deletion_claim_url=(
            _required_env("PLATFORM_DELETION_CLAIM_URL") if deletion_enabled else None
        ),
        deletion_complete_url=(
            _required_env("PLATFORM_DELETION_COMPLETE_URL")
            if deletion_enabled
            else None
        ),
        deletion_fail_url=(
            _required_env("PLATFORM_DELETION_FAIL_URL") if deletion_enabled else None
        ),
        timeout_seconds=timeout_seconds,
    )
    worker_id = f"jhub-local-{uuid.uuid4().hex[:16]}"
    return (
        LocalProvisionerAgent(
            client=client,
            provisioner=provisioner,
            worker_id=worker_id,
            provisioning_enabled=provisioning_enabled,
            deletion_enabled=deletion_enabled,
        ),
        poll_seconds,
    )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("JUPYTERHUB_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s volume-agent %(message)s",
    )
    agent, poll_seconds = build_agent_from_env()
    stopping = threading.Event()

    def stop(_signum, _frame) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping.is_set():
        try:
            processed = agent.run_once()
        except AgentError:
            logging.getLogger(__name__).exception(
                "local provisioning control loop request failed"
            )
            processed = False
        stopping.wait(0.1 if processed else poll_seconds)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AgentError as exc:
        raise SystemExit(f"ERROR: {exc}")
