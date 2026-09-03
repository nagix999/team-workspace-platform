from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from .db import begin_immediate, create_database_engine, create_session_factory
from .domain import DesiredState, HubServerState, OperationStatus
from .hub import HubServer
from .models import AuditEvent, Operation, User, Workspace
from .security import json_dumps_safe


PAGINATION_MEDIA_TYPE = "application/jupyterhub-pagination+json"
RECONCILER_SERVICE_NAME = "platform-reconciler"
REQUIRED_EFFECTIVE_SCOPES = {
    "list:users",
    "read:servers",
    "read:users:name",
}
ACTIVE_OPERATION_STATUSES = {
    OperationStatus.PENDING.value,
    OperationStatus.RUNNING.value,
    OperationStatus.WAITING_EXTERNAL.value,
}
MAX_PAGE_BYTES = 1024 * 1024
MAX_USERS = 1000
MAX_SERVERS = 1000
PAGE_LIMIT = 100
SAFE_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]*$")
HEARTBEAT_PATH = Path("/tmp/platform-reconciler-health.json")
HEARTBEAT_MODE = 0o600
FORBIDDEN_SECRET_ENVIRONMENT = {
    "CONFIGPROXY_AUTH_TOKEN",
    "CONFIGPROXY_AUTH_TOKEN_FILE",
    "JUPYTERHUB_ADMIN_LIFECYCLE_TOKEN_FILE",
    "JUPYTERHUB_COOKIE_SECRET",
    "JUPYTERHUB_COOKIE_SECRET_FILE",
    "JUPYTERHUB_OAUTH_CLIENT_SECRET",
    "JUPYTERHUB_OAUTH_CLIENT_SECRET_FILE",
    "JUPYTERHUB_RECONCILER_TOKEN",
    "PLATFORM_ADMIN_LIFECYCLE_TOKEN",
    "PLATFORM_ADMIN_LIFECYCLE_TOKEN_FILE",
    "PLATFORM_INTERNAL_HMAC_KEY",
    "PLATFORM_INTERNAL_HMAC_KEY_FILE",
    "PLATFORM_OAUTH_CLIENT_SECRET",
    "PLATFORM_OAUTH_CLIENT_SECRET_FILE",
    "PLATFORM_RECONCILER_TOKEN",
    "PLATFORM_RECONCILER_TOKEN_FILE",
    "PLATFORM_SESSION_HASH_KEY",
    "PLATFORM_SESSION_HASH_KEY_FILE",
    "PLATFORM_TOKEN_ENCRYPTION_KEY",
    "PLATFORM_TOKEN_ENCRYPTION_KEY_FILE",
    "SPAWN_VALIDATOR_HMAC_KEY",
    "SPAWN_VALIDATOR_HMAC_KEY_FILE",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReconciliationError(RuntimeError):
    """A deliberately non-sensitive Hub snapshot failure."""


@dataclass(frozen=True)
class ReconcilerConfig:
    database_url: str
    hub_internal_url: str
    token_file: Path
    interval_seconds: float
    freshness_seconds: int
    enforce_safe_sqlite: bool = True

    @classmethod
    def from_env(cls) -> "ReconcilerConfig":
        if FORBIDDEN_SECRET_ENVIRONMENT.intersection(os.environ):
            raise RuntimeError("reconciler received a forbidden credential")
        database_url = os.environ.get(
            "PLATFORM_DATABASE_URL", "sqlite:////var/lib/platform/platform.db"
        ).strip()
        if database_url != "sqlite:////var/lib/platform/platform.db":
            raise RuntimeError("reconciler database URL must use the platform volume")

        hub_internal_url = os.environ.get(
            "JUPYTERHUB_INTERNAL_URL", "http://jupyterhub:8081"
        ).strip()
        parsed = urlsplit(hub_internal_url)
        if (
            hub_internal_url != "http://jupyterhub:8081"
            or parsed.scheme != "http"
            or parsed.hostname != "jupyterhub"
            or parsed.port != 8081
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise RuntimeError("reconciler Hub URL must use the control service")

        token_file = Path(
            os.environ.get(
                "JUPYTERHUB_RECONCILER_TOKEN_FILE",
                "/run/platform-secrets/reconciler_token",
            ).strip()
        )
        if token_file != Path("/run/platform-secrets/reconciler_token"):
            raise RuntimeError("reconciler token path is outside its secret mount")
        try:
            interval_seconds = float(
                os.environ.get("PLATFORM_RECONCILIATION_INTERVAL_SECONDS", "5")
            )
            freshness_seconds = int(
                os.environ.get("PLATFORM_RECONCILIATION_FRESHNESS_SECONDS", "30")
            )
        except ValueError as exc:
            raise RuntimeError("reconciliation timing is invalid") from exc
        if (
            not 1 <= interval_seconds <= 60
            or not 5 <= freshness_seconds <= 300
            or interval_seconds * 2 > freshness_seconds
        ):
            raise RuntimeError("reconciliation timing is outside the safe range")
        enforce_safe_sqlite = (
            os.environ.get("PLATFORM_ENFORCE_SAFE_SQLITE", "true").strip().lower()
        )
        if enforce_safe_sqlite not in {"true", "false"}:
            raise RuntimeError("safe SQLite setting must be true or false")
        return cls(
            database_url=database_url,
            hub_internal_url=hub_internal_url,
            token_file=token_file,
            interval_seconds=interval_seconds,
            freshness_seconds=freshness_seconds,
            enforce_safe_sqlite=enforce_safe_sqlite == "true",
        )


@dataclass(frozen=True)
class HubSnapshot:
    servers: Mapping[tuple[str, str], HubServer]
    # A snapshot must never overwrite a lifecycle result committed after the
    # Hub read began. The next five-second pass will observe the new server.
    captured_at: datetime = field(default_factory=_utcnow)


class SnapshotSource(Protocol):
    async def fetch(self) -> HubSnapshot: ...


def read_reconciler_token(path: Path) -> str:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ReconciliationError("reconciler token no-follow support is unavailable")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        file_stat = os.fstat(descriptor)
        permission_mode = stat.S_IMODE(file_stat.st_mode)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            # 0440 is the Compose contract: the unprivileged service reads via
            # its supplemental secret group. 0600/0640 are also safe for
            # non-Compose deployments. Never accept execute, special, group
            # write, or any permission for "other" users.
            or permission_mode not in {0o400, 0o440, 0o600, 0o640}
            or not 32 <= file_stat.st_size <= 4098
        ):
            raise ReconciliationError("reconciler token file permissions are unsafe")
        with os.fdopen(descriptor, encoding="ascii") as input_file:
            descriptor = None
            value = input_file.read(4099).rstrip("\r\n")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ReconciliationError(
                "reconciler token file permissions are unsafe"
            ) from exc
        raise ReconciliationError("reconciler token file is unavailable") from exc
    except UnicodeError as exc:
        raise ReconciliationError("reconciler token file is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not 32 <= len(value) <= 4096 or any(character.isspace() for character in value):
        raise ReconciliationError("reconciler token is invalid")
    return value


def _json_without_duplicate_keys(payload: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        return json.loads(payload, object_pairs_hook=object_pairs)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ReconciliationError("Hub snapshot returned invalid JSON") from exc


def _safe_text(value: Any, where: str, *, minimum: int = 0, maximum: int = 255) -> str:
    if (
        not isinstance(value, str)
        or len(value) < minimum
        or len(value) > maximum
        or not SAFE_TEXT_RE.fullmatch(value)
    ):
        raise ReconciliationError(f"Hub snapshot {where} is invalid")
    return value


def _hub_datetime(value: Any, where: str) -> datetime | None:
    if value is None:
        return None
    text = _safe_text(value, where, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise ReconciliationError(f"Hub snapshot {where} is invalid") from exc
    if parsed.tzinfo is None:
        raise ReconciliationError(f"Hub snapshot {where} lacks a timezone")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _server_model(username: str, server_name: str, value: Any) -> HubServer:
    expected_keys = {
        "name",
        "full_name",
        "last_activity",
        "started",
        "pending",
        "ready",
        "stopped",
        "url",
        "user_options",
        "progress_url",
        "full_url",
        "full_progress_url",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ReconciliationError("Hub server snapshot schema is invalid")
    if value["name"] != server_name or value["full_name"] != (
        f"{username}/{server_name}"
    ):
        raise ReconciliationError("Hub server snapshot identity is invalid")
    if type(value["ready"]) is not bool or type(value["stopped"]) is not bool:
        raise ReconciliationError("Hub server snapshot state is invalid")
    pending = value["pending"]
    # Test the type before membership. A malformed JSON array/object is
    # unhashable and must become a fail-closed snapshot error, not escape as a
    # TypeError that leaves the persisted workspace freshness unchanged.
    if pending is not None and (
        not isinstance(pending, str) or pending not in {"spawn", "stop"}
    ):
        raise ReconciliationError("Hub server snapshot pending state is invalid")
    for field_name in ("url", "progress_url"):
        _safe_text(value[field_name], field_name, maximum=2048)
    for field_name in ("full_url", "full_progress_url"):
        if value[field_name] is not None:
            _safe_text(value[field_name], field_name, maximum=2048)
    # JupyterHub 5.5 emits null for records which never had spawn options and a
    # JSON object for records created through the platform. The content may
    # include an already-consumed spawn ticket, so it is deliberately neither
    # inspected nor copied into the normalized snapshot.
    if value["user_options"] is not None and not isinstance(
        value["user_options"], dict
    ):
        raise ReconciliationError("Hub server snapshot options are invalid")

    ready = value["ready"]
    stopped = value["stopped"]
    if ready:
        if stopped or pending is not None:
            raise ReconciliationError("Hub server ready state is inconsistent")
        state = HubServerState.RUNNING
    elif pending == "spawn":
        if stopped:
            raise ReconciliationError("Hub server spawn state is inconsistent")
        state = HubServerState.STARTING
    elif pending == "stop":
        state = HubServerState.STOPPING
    elif stopped:
        state = HubServerState.STOPPED
    else:
        state = HubServerState.FAILED
    return HubServer(
        state=state,
        ready=ready,
        progress_percent=(
            100 if state in {HubServerState.RUNNING, HubServerState.STOPPED} else None
        ),
        # URLs are deliberately not trusted or persisted by reconciliation.
        full_url=None,
        started_at=_hub_datetime(value["started"], "started"),
        last_activity_at=_hub_datetime(value["last_activity"], "last_activity"),
    )


class HubSnapshotClient:
    def __init__(
        self,
        *,
        hub_internal_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.hub_internal_url = hub_internal_url.rstrip("/")
        self.token = token
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def _get_json(
        self, path: str, *, params: dict[str, object] | None = None
    ) -> Any:
        request = self.client.build_request(
            "GET",
            f"{self.hub_internal_url}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": PAGINATION_MEDIA_TYPE,
            },
        )
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ReconciliationError("Hub snapshot endpoint is unavailable") from exc
        try:
            try:
                if response.status_code in {401, 403}:
                    raise ReconciliationError("Hub rejected the reconciler credential")
                if response.status_code != 200:
                    raise ReconciliationError("Hub snapshot endpoint returned an error")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if content_type.lower() != "application/json":
                    raise ReconciliationError("Hub snapshot content type is invalid")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_PAGE_BYTES:
                        raise ReconciliationError("Hub snapshot page is too large")
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            # A 200 response can still fail halfway through the body. Treat
            # connect, stream and close failures identically so the database is
            # marked stale immediately instead of waiting for the freshness TTL.
            raise ReconciliationError("Hub snapshot endpoint is unavailable") from exc
        return _json_without_duplicate_keys(bytes(body))

    async def _validate_principal(self) -> None:
        value = await self._get_json("/hub/api/user")
        if not isinstance(value, dict) or set(value) != {
            "kind",
            "admin",
            "name",
            "token_id",
            "session_id",
            "scopes",
        }:
            raise ReconciliationError("Hub reconciler principal schema is invalid")
        scopes = value["scopes"]
        if (
            value["kind"] != "service"
            or value["name"] != RECONCILER_SERVICE_NAME
            or value["admin"] is not False
            or value["session_id"] is not None
            or not isinstance(value["token_id"], str)
            or not value["token_id"]
            or not isinstance(scopes, list)
            or any(not isinstance(scope, str) for scope in scopes)
            or len(scopes) != len(set(scopes))
            or set(scopes) != REQUIRED_EFFECTIVE_SCOPES
        ):
            raise ReconciliationError("Hub reconciler principal is not least privilege")

    async def fetch(self) -> HubSnapshot:
        captured_at = _utcnow()
        await self._validate_principal()
        offset = 0
        total: int | None = None
        users_seen: set[str] = set()
        servers: dict[tuple[str, str], HubServer] = {}
        while True:
            value = await self._get_json(
                "/hub/api/users",
                params={
                    "include_stopped_servers": 1,
                    "offset": offset,
                    "limit": PAGE_LIMIT,
                    "sort": "id",
                },
            )
            if not isinstance(value, dict) or set(value) != {"items", "_pagination"}:
                raise ReconciliationError("Hub users snapshot schema is invalid")
            items = value["items"]
            pagination = value["_pagination"]
            if (
                not isinstance(items, list)
                or len(items) > PAGE_LIMIT
                or not isinstance(pagination, dict)
                or set(pagination) != {"offset", "limit", "total", "next"}
                or type(pagination["offset"]) is not int
                or type(pagination["limit"]) is not int
                or type(pagination["total"]) is not int
                or pagination["offset"] != offset
                or pagination["limit"] != PAGE_LIMIT
                or not 0 <= pagination["total"] <= MAX_USERS
                or offset + len(items) > pagination["total"]
            ):
                raise ReconciliationError("Hub users pagination is invalid")
            if total is None:
                total = pagination["total"]
            elif pagination["total"] != total:
                raise ReconciliationError("Hub users changed during snapshot")
            for user_value in items:
                if not isinstance(user_value, dict) or set(user_value) != {
                    "kind",
                    "name",
                    "admin",
                    "servers",
                }:
                    raise ReconciliationError("Hub user snapshot schema is invalid")
                username = _safe_text(
                    user_value["name"], "username", minimum=1, maximum=128
                )
                if (
                    user_value["kind"] != "user"
                    or type(user_value["admin"]) is not bool
                    or username in users_seen
                    or not isinstance(user_value["servers"], dict)
                ):
                    raise ReconciliationError("Hub user snapshot is invalid")
                users_seen.add(username)
                for raw_name, server_value in user_value["servers"].items():
                    server_name = _safe_text(raw_name, "server name", maximum=128)
                    key = (username, server_name)
                    if key in servers:
                        raise ReconciliationError("Hub server snapshot is duplicated")
                    servers[key] = _server_model(username, server_name, server_value)
                    if len(servers) > MAX_SERVERS:
                        raise ReconciliationError("Hub server snapshot is too large")

            next_page = pagination["next"]
            if next_page is None:
                if len(users_seen) != total:
                    raise ReconciliationError("Hub users snapshot is incomplete")
                break
            if (
                not isinstance(next_page, dict)
                or set(next_page) != {"offset", "limit", "url"}
                or type(next_page["offset"]) is not int
                or type(next_page["limit"]) is not int
                or next_page["offset"] != offset + PAGE_LIMIT
                or next_page["limit"] != PAGE_LIMIT
                or not 0 <= next_page["offset"] < pagination["total"]
                or next_page["offset"] > MAX_USERS
            ):
                raise ReconciliationError("Hub users next page is invalid")
            _safe_text(next_page["url"], "next URL", maximum=2048)
            offset = next_page["offset"]
        return HubSnapshot(servers=servers, captured_at=captured_at)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class WorkspaceReconciler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        snapshot_source: SnapshotSource,
        *,
        maximum_snapshot_age_seconds: int = 30,
    ) -> None:
        self.session_factory = session_factory
        self.snapshot_source = snapshot_source
        self.maximum_snapshot_age_seconds = maximum_snapshot_age_seconds
        self.last_successful_snapshot_at: datetime | None = None

    @staticmethod
    def _observed(server: HubServer) -> str:
        return {
            HubServerState.NOT_FOUND: "NOT_FOUND",
            HubServerState.STARTING: "STARTING",
            HubServerState.RUNNING: "RUNNING",
            HubServerState.STOPPING: "STOPPING",
            HubServerState.STOPPED: "STOPPED",
            HubServerState.FAILED: "FAILED",
        }[server.state]

    def mark_unavailable(self) -> None:
        now = _utcnow()
        with self.session_factory() as db:
            begin_immediate(db)
            workspaces = db.scalars(
                select(Workspace).where(
                    Workspace.archived_at.is_(None), Workspace.stale.is_(False)
                )
            ).all()
            for workspace in workspaces:
                workspace.stale = True
                workspace.row_version += 1
                workspace.updated_at = now
            db.commit()

    def apply_snapshot(self, snapshot: HubSnapshot) -> None:
        now = _utcnow()
        active_operation = exists(
            select(Operation.id).where(
                Operation.workspace_id == Workspace.id,
                Operation.status.in_(ACTIVE_OPERATION_STATUSES),
            )
        )
        operation_newer_than_snapshot = exists(
            select(Operation.id).where(
                Operation.workspace_id == Workspace.id,
                (
                    (Operation.requested_at > snapshot.captured_at)
                    | (Operation.completed_at > snapshot.captured_at)
                ),
            )
        )
        with self.session_factory() as db:
            begin_immediate(db)
            rows = db.execute(
                select(Workspace, User)
                .join(User, User.id == Workspace.owner_user_id)
                .where(
                    Workspace.archived_at.is_(None),
                    ~active_operation,
                    ~operation_newer_than_snapshot,
                )
            ).all()
            for workspace, owner in rows:
                if (
                    workspace.last_reconciled_at is not None
                    and workspace.last_reconciled_at > snapshot.captured_at
                ):
                    continue
                server = snapshot.servers.get(
                    (owner.hub_username, workspace.hub_server_name),
                    HubServer(state=HubServerState.NOT_FOUND),
                )
                observed = self._observed(server)
                previous = workspace.observed_state
                state_changed = previous != observed
                release_gpu = bool(
                    workspace.assigned_gpu_device_id is not None
                    and server.state
                    in {HubServerState.NOT_FOUND, HubServerState.STOPPED}
                    and workspace.desired_state != DesiredState.RUNNING.value
                )
                material_changed = state_changed or any(
                    (
                        workspace.progress_percent != server.progress_percent,
                        workspace.stale is not False,
                        workspace.hub_started_at != server.started_at,
                        workspace.hub_last_activity_at != server.last_activity_at,
                        workspace.hub_server_url is not None,
                        release_gpu,
                    )
                )
                workspace.observed_state = observed
                workspace.progress_percent = server.progress_percent
                workspace.stale = False
                workspace.hub_started_at = server.started_at
                workspace.hub_last_activity_at = server.last_activity_at
                workspace.hub_server_url = None
                if release_gpu:
                    workspace.assigned_gpu_device_id = None
                # Freshness is the age of the observation, not the time this
                # database transaction happened to finish.
                workspace.last_reconciled_at = snapshot.captured_at
                if material_changed:
                    workspace.row_version += 1
                    workspace.updated_at = now
                if state_changed:
                    db.add(
                        AuditEvent(
                            id=str(uuid.uuid4()),
                            actor_user_id=None,
                            workspace_id=workspace.id,
                            action="EXTERNAL_CHANGE",
                            result="OBSERVED",
                            request_id=f"reconciler:{uuid.uuid4().hex[:24]}",
                            safe_metadata_json=json_dumps_safe(
                                {
                                    "previous_state": previous,
                                    "observed_state": observed,
                                    "source": "JUPYTERHUB_SNAPSHOT",
                                }
                            ),
                        )
                    )
            db.commit()

    async def run_once(self) -> bool:
        self.last_successful_snapshot_at = None
        try:
            snapshot = await self.snapshot_source.fetch()
        except ReconciliationError:
            self.mark_unavailable()
            logging.getLogger(__name__).warning(
                "JupyterHub reconciliation snapshot is unavailable"
            )
            return False
        snapshot_age = (_utcnow() - snapshot.captured_at).total_seconds()
        if not 0 <= snapshot_age < self.maximum_snapshot_age_seconds:
            self.mark_unavailable()
            logging.getLogger(__name__).warning(
                "JupyterHub reconciliation snapshot exceeded the freshness bound"
            )
            return False
        self.apply_snapshot(snapshot)
        self.last_successful_snapshot_at = snapshot.captured_at
        return True


def clear_heartbeat(path: Path = HEARTBEAT_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def write_heartbeat(snapshot_at: datetime, path: Path = HEARTBEAT_PATH) -> None:
    payload = json.dumps(
        {
            "schema_version": 1,
            "snapshot_started_at_unix": int(
                snapshot_at.replace(tzinfo=timezone.utc).timestamp()
            ),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = os.open(temporary, flags, HEARTBEAT_MODE)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def heartbeat_is_fresh(*, freshness_seconds: int, path: Path = HEARTBEAT_PATH) -> bool:
    try:
        file_stat = path.lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or path.is_symlink()
            or file_stat.st_uid != os.getuid()
            or stat.S_IMODE(file_stat.st_mode) != HEARTBEAT_MODE
            or not 1 <= file_stat.st_size <= 256
        ):
            return False
        value = _json_without_duplicate_keys(path.read_bytes())
    except (OSError, ReconciliationError):
        return False
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "snapshot_started_at_unix"}
        or value["schema_version"] != 1
        or type(value["snapshot_started_at_unix"]) is not int
    ):
        return False
    age = time.time() - value["snapshot_started_at_unix"]
    return 0 <= age < freshness_seconds


async def run_forever(
    reconciler: WorkspaceReconciler,
    *,
    interval_seconds: float,
    heartbeat_path: Path = HEARTBEAT_PATH,
) -> None:
    clear_heartbeat(heartbeat_path)
    while True:
        try:
            successful = await reconciler.run_once()
            if successful and reconciler.last_successful_snapshot_at is not None:
                write_heartbeat(reconciler.last_successful_snapshot_at, heartbeat_path)
            else:
                clear_heartbeat(heartbeat_path)
        except Exception:
            clear_heartbeat(heartbeat_path)
            # Database/runtime details belong in the private service log. The
            # freshness cutoff makes the admin view fail closed if this loop is
            # unable to persist heartbeats.
            logging.getLogger(__name__).exception("workspace reconciliation failed")
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the read-only Hub reconciler")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("PLATFORM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s reconciler %(message)s",
    )
    config = ReconcilerConfig.from_env()
    if args.healthcheck:
        raise SystemExit(
            0 if heartbeat_is_fresh(freshness_seconds=config.freshness_seconds) else 1
        )
    token = read_reconciler_token(config.token_file)
    engine = create_database_engine(config)  # type: ignore[arg-type]
    factory = create_session_factory(engine)
    client = HubSnapshotClient(
        hub_internal_url=config.hub_internal_url,
        token=token,
    )
    reconciler = WorkspaceReconciler(
        factory,
        client,
        maximum_snapshot_age_seconds=config.freshness_seconds,
    )

    async def run() -> bool:
        try:
            if args.once:
                return await reconciler.run_once()
            else:
                await run_forever(reconciler, interval_seconds=config.interval_seconds)
                return True
        finally:
            await client.aclose()
            engine.dispose()

    if not asyncio.run(run()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
