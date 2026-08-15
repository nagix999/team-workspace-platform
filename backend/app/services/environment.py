from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import (
    AuditEvent,
    EnvironmentVariable,
    SpawnAuthorization,
    User,
    Workspace,
)
from ..security import TokenCipher, json_dumps_safe, keyed_hash
from ..serialization import iso


ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_VARIABLES_PER_SCOPE = 64
MAX_EFFECTIVE_VARIABLES = 128
MAX_VALUE_CHARS = 16_384
MAX_CANONICAL_BYTES = 65_536

_RESERVED_EXACT = {
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "IFS",
    "ENV",
    "BASH_ENV",
    "SHELLOPTS",
    "PS4",
    "PROMPT_COMMAND",
    "CDPATH",
    "GLOBIGNORE",
    "IPYTHONDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "RES_OPTIONS",
    "HOSTALIASES",
    "LOCALDOMAIN",
    "TEMP",
    "TEMPDIR",
}
_RESERVED_PREFIXES = (
    "PLATFORM_",
    "JUPYTER_",
    "JUPYTERHUB_",
    "JPY_",
    "DOCKER_",
    "LD_",
    "DYLD_",
    "PYTHON",
    "CONDA_",
    "MAMBA_",
    "XDG_",
    "NB_",
    "CHOWN_",
    "GRANT_SUDO",
    "TMP",
)


@dataclass(frozen=True)
class EffectiveEnvironment:
    values: dict[str, str]
    digest: str


def spawn_snapshot_purpose(
    authorization_id: str, workspace_id: str, environment_digest: str
) -> str:
    return (
        f"spawn-environment:v1:{authorization_id}:{workspace_id}:"
        f"{environment_digest}"
    )


def environment_map_digest(values: dict[str, str], hmac_key: str) -> str:
    if len(values) > MAX_EFFECTIVE_VARIABLES:
        raise AppError(
            409,
            "ENVIRONMENT_VARIABLE_LIMIT",
            "Effective environment contains too many variables",
        )
    canonical = json.dumps(
        values, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(canonical) > MAX_CANONICAL_BYTES:
        raise AppError(
            409,
            "ENVIRONMENT_PAYLOAD_LIMIT",
            "Effective environment payload is too large",
        )
    digest = hmac.new(
        hmac_key.encode("utf-8"),
        b"platform-spawn-environment-v1\0" + canonical,
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def validate_environment_name(name: str) -> str:
    if not ENVIRONMENT_NAME_RE.fullmatch(name):
        raise AppError(
            422,
            "ENVIRONMENT_NAME_INVALID",
            "Environment variable name must be a portable ASCII identifier",
        )
    upper = name.upper()
    if upper in _RESERVED_EXACT or any(
        upper.startswith(prefix) for prefix in _RESERVED_PREFIXES
    ):
        raise AppError(
            422,
            "ENVIRONMENT_NAME_RESERVED",
            "Environment variable name is reserved by the managed runtime",
        )
    return name


def validate_environment_value(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AppError(
            422,
            "ENVIRONMENT_VALUE_INVALID",
            "Environment variable value must be valid UTF-8",
        ) from exc
    if len(encoded) > MAX_VALUE_CHARS or "\x00" in value:
        raise AppError(
            422,
            "ENVIRONMENT_VALUE_INVALID",
            "Environment variable value is too large or contains NUL",
        )
    return value


def _purpose(item: EnvironmentVariable) -> str:
    workspace = item.workspace_id or "-"
    return (
        f"environment-variable:v1:{item.scope}:{item.owner_user_id}:"
        f"{workspace}:{item.name}:{item.id}"
    )


def _value_fingerprint(
    *,
    fingerprint_key: str,
    scope: str,
    owner_user_id: str,
    workspace_id: str | None,
    name: str,
    value: str,
    is_secret: bool,
) -> str:
    return keyed_hash(
        "platform-environment-value-v1\0"
        f"{scope}\0{owner_user_id}\0{workspace_id or '-'}\0{name}\0"
        f"{int(is_secret)}\0{value}",
        fingerprint_key,
    )


def environment_item_dict(item: EnvironmentVariable) -> dict[str, object]:
    return {
        "name": item.name,
        "scope": item.scope,
        "version": item.row_version,
        "is_secret": item.is_secret,
        "value": None if item.is_secret else item.plain_value,
        "is_set": True,
        "updated_at": iso(item.updated_at),
    }


def list_environment_variables(
    db: Session,
    *,
    owner_user_id: str,
    workspace_id: str | None,
) -> list[EnvironmentVariable]:
    scope = "USER" if workspace_id is None else "WORKSPACE"
    return list(
        db.scalars(
            select(EnvironmentVariable)
            .where(
                EnvironmentVariable.owner_user_id == owner_user_id,
                EnvironmentVariable.scope == scope,
                EnvironmentVariable.workspace_id == workspace_id,
                EnvironmentVariable.deleted_at.is_(None),
            )
            .order_by(EnvironmentVariable.name)
        ).all()
    )


def workspace_restart_required(workspace: Workspace, owner: User) -> bool:
    return bool(
        workspace.archived_at is None
        and workspace.observed_state == "RUNNING"
        and (
            workspace.applied_user_environment_generation
            != owner.environment_generation
            or workspace.applied_workspace_environment_generation
            != workspace.environment_generation
        )
    )


def _audit(
    db: Session,
    *,
    actor_user_id: str,
    workspace_id: str | None,
    action: str,
    request_id: str,
    scope: str,
    name: str,
    version: int,
) -> None:
    # Values, fingerprints and secret classification are deliberately absent.
    db.add(
        AuditEvent(
            id=str(uuid.uuid4()),
            actor_user_id=actor_user_id,
            workspace_id=workspace_id,
            action=action,
            result="ACCEPTED",
            request_id=request_id,
            safe_metadata_json=json_dumps_safe(
                {"scope": scope, "name": name, "version": version}
            ),
        )
    )


def _invalidate_environment_bindings(
    db: Session,
    *,
    owner: User,
    workspace: Workspace | None,
) -> None:
    now = datetime.utcnow()
    if workspace is None:
        owner.environment_generation += 1
        workspaces = db.scalars(
            select(Workspace).where(
                Workspace.owner_user_id == owner.id,
                Workspace.archived_at.is_(None),
                Workspace.deletion_started_at.is_(None),
                Workspace.desired_state != "DELETED",
            )
        ).all()
    else:
        workspace.environment_generation += 1
        workspaces = [workspace]
    workspace_ids: list[str] = []
    for item in workspaces:
        item.spec_version += 1
        item.row_version += 1
        item.updated_at = now
        workspace_ids.append(item.id)
    if workspace_ids:
        db.execute(
            update(SpawnAuthorization)
            .where(
                SpawnAuthorization.workspace_id.in_(workspace_ids),
                SpawnAuthorization.consumed_at.is_(None),
                SpawnAuthorization.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )


def put_environment_variable(
    db: Session,
    *,
    cipher: TokenCipher,
    fingerprint_key: str,
    owner: User,
    workspace: Workspace | None,
    name: str,
    value: str,
    is_secret: bool,
    expected_version: int | None,
    actor_user_id: str,
    request_id: str,
) -> tuple[EnvironmentVariable, bool]:
    name = validate_environment_name(name)
    value = validate_environment_value(value)
    workspace_id = workspace.id if workspace else None
    scope = "WORKSPACE" if workspace else "USER"
    item = db.scalar(
        select(EnvironmentVariable).where(
            EnvironmentVariable.owner_user_id == owner.id,
            EnvironmentVariable.scope == scope,
            EnvironmentVariable.workspace_id == workspace_id,
            EnvironmentVariable.name == name,
            EnvironmentVariable.deleted_at.is_(None),
        )
    )
    fingerprint = _value_fingerprint(
        fingerprint_key=fingerprint_key,
        scope=scope,
        owner_user_id=owner.id,
        workspace_id=workspace_id,
        name=name,
        value=value,
        is_secret=is_secret,
    )
    if item is not None:
        if expected_version is None:
            raise AppError(
                428,
                "ENVIRONMENT_VERSION_REQUIRED",
                "Updating an existing variable requires expected_version",
            )
        if item.row_version != expected_version:
            raise AppError(
                409,
                "ENVIRONMENT_VERSION_CONFLICT",
                "Environment variable was changed by another request",
            )
        if item.value_fingerprint == fingerprint and item.is_secret == is_secret:
            return item, False
        item.row_version += 1
        item.updated_at = datetime.utcnow()
    else:
        if expected_version is not None:
            raise AppError(
                409,
                "ENVIRONMENT_VERSION_CONFLICT",
                "Environment variable no longer exists",
            )
        used = len(
            list_environment_variables(
                db, owner_user_id=owner.id, workspace_id=workspace_id
            )
        )
        if used >= MAX_VARIABLES_PER_SCOPE:
            raise AppError(
                409,
                "ENVIRONMENT_VARIABLE_LIMIT",
                "Environment variable limit for this scope was reached",
            )
        now = datetime.utcnow()
        item = EnvironmentVariable(
            id=str(uuid.uuid4()),
            owner_user_id=owner.id,
            workspace_id=workspace_id,
            scope=scope,
            name=name,
            is_secret=is_secret,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
    item.is_secret = is_secret
    item.value_fingerprint = fingerprint
    if is_secret:
        item.value_cipher = cipher.encrypt(value, purpose=_purpose(item))
        item.plain_value = None
    else:
        item.value_cipher = None
        item.plain_value = value
    _invalidate_environment_bindings(db, owner=owner, workspace=workspace)
    _audit(
        db,
        actor_user_id=actor_user_id,
        workspace_id=workspace_id,
        action="ENVIRONMENT_VARIABLE_SET",
        request_id=request_id,
        scope=scope,
        name=name,
        version=item.row_version,
    )
    db.flush()
    _validate_staged_environment(
        db,
        cipher=cipher,
        hmac_key=fingerprint_key,
        owner=owner,
        workspace=workspace,
    )
    return item, True


def delete_environment_variable(
    db: Session,
    *,
    cipher: TokenCipher,
    hmac_key: str,
    owner: User,
    workspace: Workspace | None,
    name: str,
    expected_version: int,
    actor_user_id: str,
    request_id: str,
) -> EnvironmentVariable:
    name = validate_environment_name(name)
    workspace_id = workspace.id if workspace else None
    scope = "WORKSPACE" if workspace else "USER"
    item = db.scalar(
        select(EnvironmentVariable).where(
            EnvironmentVariable.owner_user_id == owner.id,
            EnvironmentVariable.scope == scope,
            EnvironmentVariable.workspace_id == workspace_id,
            EnvironmentVariable.name == name,
            EnvironmentVariable.deleted_at.is_(None),
        )
    )
    if item is None:
        raise AppError(404, "ENVIRONMENT_VARIABLE_NOT_FOUND", "Variable was not found")
    if item.row_version != expected_version:
        raise AppError(
            409,
            "ENVIRONMENT_VERSION_CONFLICT",
            "Environment variable was changed by another request",
        )
    now = datetime.utcnow()
    item.row_version += 1
    item.value_cipher = None
    item.plain_value = None
    item.value_fingerprint = None
    item.deleted_at = now
    item.updated_at = now
    _invalidate_environment_bindings(db, owner=owner, workspace=workspace)
    _audit(
        db,
        actor_user_id=actor_user_id,
        workspace_id=workspace_id,
        action="ENVIRONMENT_VARIABLE_DELETED",
        request_id=request_id,
        scope=scope,
        name=name,
        version=item.row_version,
    )
    db.flush()
    _validate_staged_environment(
        db,
        cipher=cipher,
        hmac_key=hmac_key,
        owner=owner,
        workspace=workspace,
    )
    return item


def create_initial_workspace_variables(
    db: Session,
    *,
    cipher: TokenCipher,
    fingerprint_key: str,
    owner: User,
    workspace: Workspace,
    variables: list[dict[str, object]],
    actor_user_id: str,
    request_id: str,
) -> None:
    if len(variables) > MAX_VARIABLES_PER_SCOPE:
        raise AppError(409, "ENVIRONMENT_VARIABLE_LIMIT", "Too many variables")
    for variable in variables:
        name = variable.get("name")
        value = variable.get("value")
        is_secret = variable.get("is_secret")
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or type(is_secret) is not bool
        ):
            raise AppError(
                422,
                "ENVIRONMENT_VALUE_INVALID",
                "Environment variable input has invalid types",
            )
        put_environment_variable(
            db,
            cipher=cipher,
            fingerprint_key=fingerprint_key,
            owner=owner,
            workspace=workspace,
            name=name,
            value=value,
            is_secret=is_secret,
            expected_version=None,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )


def _plain_value(item: EnvironmentVariable, cipher: TokenCipher) -> str:
    if item.is_secret:
        if item.value_cipher is None:  # pragma: no cover - guarded by DB check
            raise AppError(
                500, "ENVIRONMENT_STORAGE_INVALID", "Secret value is missing"
            )
        try:
            return cipher.decrypt(item.value_cipher, purpose=_purpose(item))
        except ValueError as exc:
            raise AppError(
                500,
                "ENVIRONMENT_DECRYPT_FAILED",
                "Environment variable could not be decrypted",
            ) from exc
    if item.plain_value is None:  # pragma: no cover - guarded by DB check
        raise AppError(500, "ENVIRONMENT_STORAGE_INVALID", "Variable value is missing")
    return item.plain_value


def effective_environment(
    db: Session,
    *,
    cipher: TokenCipher,
    hmac_key: str,
    owner_user_id: str,
    workspace_id: str,
) -> EffectiveEnvironment:
    values: dict[str, str] = {}
    for item in list_environment_variables(
        db, owner_user_id=owner_user_id, workspace_id=None
    ) + list_environment_variables(
        db, owner_user_id=owner_user_id, workspace_id=workspace_id
    ):
        validate_environment_name(item.name)
        values[item.name] = _plain_value(item, cipher)
    return EffectiveEnvironment(values, environment_map_digest(values, hmac_key))


def _validate_staged_environment(
    db: Session,
    *,
    cipher: TokenCipher,
    hmac_key: str,
    owner: User,
    workspace: Workspace | None,
) -> None:
    """Reject mutations that would leave any runnable workspace unspawnable."""

    if workspace is not None:
        effective_environment(
            db,
            cipher=cipher,
            hmac_key=hmac_key,
            owner_user_id=owner.id,
            workspace_id=workspace.id,
        )
        return

    # Validate the global scope even when the user has no workspace yet. This
    # prevents storing a configuration that could never be used by a later create.
    global_values = {
        item.name: _plain_value(item, cipher)
        for item in list_environment_variables(
            db, owner_user_id=owner.id, workspace_id=None
        )
    }
    environment_map_digest(global_values, hmac_key)
    workspace_ids = db.scalars(
        select(Workspace.id).where(
            Workspace.owner_user_id == owner.id,
            Workspace.archived_at.is_(None),
            Workspace.deletion_started_at.is_(None),
            Workspace.desired_state != "DELETED",
        )
    ).all()
    for workspace_id in workspace_ids:
        effective_environment(
            db,
            cipher=cipher,
            hmac_key=hmac_key,
            owner_user_id=owner.id,
            workspace_id=workspace_id,
        )
