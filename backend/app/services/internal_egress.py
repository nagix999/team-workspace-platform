from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..db import begin_immediate
from ..errors import AppError
from ..models import AuditEvent, InternalEgressPolicy, InternalEgressRule
from ..security import json_dumps_safe


MAX_INTERNAL_EGRESS_RULES = 32
BLOCKED_CONTROL_PORTS = frozenset({2375, 2376, 2377, 3128, 4243, 6443, 10250})
EMPTY_POLICY_DIGEST = "sha256:" + hashlib.sha256(b"").hexdigest()
DESIRED_HEADER = "PLATFORM_INTERNAL_EGRESS_V1"
ACK_HEADER = "PLATFORM_INTERNAL_EGRESS_ACK_V1"
SAFE_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RFC1918_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
IMMUTABLE_PLATFORM_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in (
        "172.24.0.0/16",
        "172.25.0.0/24",
        "172.26.0.0/24",
        "172.27.0.0/24",
        "172.28.0.0/24",
        "172.30.0.0/24",
        "172.29.0.0/24",
        "172.29.1.0/24",
        "172.29.2.0/24",
        "172.29.3.0/24",
        "172.29.4.0/24",
    )
)
INTERNAL_EGRESS_ERROR_SUMMARIES = {
    "ACK_INVALID": "The egress proxy acknowledgement is invalid.",
    "ACK_DIGEST_MISMATCH": "The egress proxy acknowledgement does not match the desired policy.",
    "ACK_REVISION_MISMATCH": "The egress proxy acknowledgement has an unexpected revision.",
    "POLICY_PUBLISH_FAILED": "The desired policy could not be published to the egress proxy.",
    "INVALID_POLICY": "The egress proxy rejected the desired policy.",
    "CANDIDATE_CONFIG_FAILED": "The egress proxy could not prepare the desired policy.",
    "SQUID_PARSE_FAILED": "The egress proxy rejected the generated configuration.",
    "ACTIVE_BACKUP_FAILED": "The egress proxy could not preserve its active policy.",
    "ACTIVE_REPLACE_FAILED": "The egress proxy could not stage the desired policy.",
    "SQUID_RECONFIGURE_FAILED": "The egress proxy could not activate the desired policy.",
}
RUNTIME_DIRECTORY_MODE = 0o2770
DESIRED_POLICY_MODE = 0o640
ACK_POLICY_MODE = 0o660
RUNTIME_READ_ATTEMPTS = 3


@dataclass(frozen=True)
class InternalEgressAck:
    revision: int
    digest: str
    status: str
    error_code: str | None


def normalize_destination_cidr(value: str) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("destination CIDR must be canonical ASCII IPv4")
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
        raise ValueError("destination CIDR must be canonical IPv4 /32") from exc
    canonical = str(network)
    if canonical != value or network.prefixlen != 32:
        raise ValueError("destination CIDR must be canonical IPv4 /32")
    if any(network.subnet_of(blocked) for blocked in IMMUTABLE_PLATFORM_NETWORKS):
        raise ValueError("destination belongs to an immutable platform network")
    if not any(network.subnet_of(private) for private in RFC1918_NETWORKS):
        raise ValueError("destination must be an RFC1918 private IPv4 /32")
    return canonical


def validate_internal_service_port(value: int) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError("port must be an unprivileged TCP port")
    if value in BLOCKED_CONTROL_PORTS:
        raise ValueError("port is reserved for control-plane access")
    return value


def _sort_key(rule: InternalEgressRule) -> tuple[int, int, str]:
    address = rule.destination_cidr.removesuffix("/32")
    return int(ipaddress.IPv4Address(address)), rule.port, rule.id


def canonical_policy_body(rules: list[InternalEgressRule]) -> bytes:
    values: list[tuple[int, int, str]] = []
    seen: set[tuple[str, int]] = set()
    for rule in rules:
        cidr = normalize_destination_cidr(rule.destination_cidr)
        port = validate_internal_service_port(rule.port)
        pair = (cidr, port)
        if pair in seen:
            raise ValueError("internal egress policy contains a duplicate tuple")
        seen.add(pair)
        values.append((int(ipaddress.IPv4Address(cidr[:-3])), port, cidr))
    if len(values) > MAX_INTERNAL_EGRESS_RULES:
        raise ValueError("internal egress policy exceeds the rule limit")
    values.sort()
    return "".join(f"{cidr} {port}\n" for _, port, cidr in values).encode("ascii")


def policy_digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def desired_policy_payload(revision: int, rules: list[InternalEgressRule]) -> bytes:
    if type(revision) is not int or revision <= 0:
        raise ValueError("internal egress policy revision is invalid")
    body = canonical_policy_body(rules)
    digest = policy_digest(body)
    return f"{DESIRED_HEADER} {revision} {digest}\n".encode("ascii") + body


def parse_ack_payload(payload: bytes) -> InternalEgressAck:
    if not payload or len(payload) > 512:
        raise ValueError("internal egress acknowledgement size is invalid")
    try:
        value = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("internal egress acknowledgement must be ASCII") from exc
    if not value.endswith("\n") or value.count("\n") != 1:
        raise ValueError("internal egress acknowledgement must contain one line")
    fields = value[:-1].split(" ")
    if len(fields) != 5 or fields[0] != ACK_HEADER or any(not item for item in fields):
        raise ValueError("internal egress acknowledgement schema is invalid")
    revision_text, digest, status_value, error_value = fields[1:]
    if not revision_text.isdigit() or revision_text.startswith("0"):
        raise ValueError("internal egress acknowledgement revision is invalid")
    revision = int(revision_text)
    if revision <= 0 or revision > 2**63 - 1 or not SHA256_RE.fullmatch(digest):
        raise ValueError("internal egress acknowledgement binding is invalid")
    if status_value == "APPLIED":
        if error_value != "NONE":
            raise ValueError("applied acknowledgement cannot contain an error")
        error_code = None
    elif status_value == "FAILED":
        if error_value == "NONE" or not SAFE_ERROR_CODE_RE.fullmatch(error_value):
            raise ValueError("failed acknowledgement error code is invalid")
        error_code = error_value
    else:
        raise ValueError("internal egress acknowledgement status is invalid")
    return InternalEgressAck(revision, digest, status_value, error_code)


def _rules(db: Session) -> list[InternalEgressRule]:
    values = list(db.scalars(select(InternalEgressRule)).all())
    values.sort(key=_sort_key)
    return values


def get_internal_egress_policy(
    db: Session, *, create: bool = False
) -> InternalEgressPolicy:
    policy = db.get(InternalEgressPolicy, 1)
    if policy is None:
        if not create:
            raise AppError(
                503,
                "INTERNAL_EGRESS_POLICY_UNAVAILABLE",
                "Internal egress policy is not initialized",
            )
        policy = InternalEgressPolicy(
            id=1,
            desired_revision=1,
            desired_digest=EMPTY_POLICY_DIGEST,
            applied_revision=None,
            applied_digest=None,
            apply_status="PENDING",
            last_error_code=None,
        )
        db.add(policy)
        db.flush()
    rules = _rules(db)
    try:
        actual_digest = policy_digest(canonical_policy_body(rules))
    except ValueError as exc:
        raise AppError(
            500,
            "INTERNAL_EGRESS_POLICY_INVALID",
            "Persisted internal egress policy is invalid",
        ) from exc
    if actual_digest != policy.desired_digest:
        raise AppError(
            500,
            "INTERNAL_EGRESS_POLICY_INVALID",
            "Internal egress policy digest does not match its rules",
        )
    if (
        policy.apply_status not in {"PENDING", "APPLYING", "APPLIED", "FAILED"}
        or policy.desired_revision <= 0
        or not SHA256_RE.fullmatch(policy.desired_digest)
        or (policy.applied_revision is None) != (policy.applied_digest is None)
        or (policy.applied_revision is None) != (policy.applied_at is None)
        or (
            policy.applied_revision is not None
            and (
                policy.applied_revision <= 0
                or policy.applied_revision > policy.desired_revision
                or policy.applied_digest is None
                or not SHA256_RE.fullmatch(policy.applied_digest)
            )
        )
        or (policy.apply_status == "FAILED") != (policy.last_error_code is not None)
        or (
            policy.last_error_code is not None
            and not SAFE_ERROR_CODE_RE.fullmatch(policy.last_error_code)
        )
        or (
            policy.apply_status == "APPLIED"
            and (
                policy.applied_revision != policy.desired_revision
                or policy.applied_digest != policy.desired_digest
            )
        )
    ):
        raise AppError(
            500,
            "INTERNAL_EGRESS_POLICY_INVALID",
            "Persisted internal egress policy state is invalid",
        )
    return policy


def internal_egress_policy_dict(
    db: Session, policy: InternalEgressPolicy
) -> dict[str, object]:
    rules = _rules(db)
    # Re-run the invariant check so callers never serialize corrupt rows.
    if policy_digest(canonical_policy_body(rules)) != policy.desired_digest:
        raise AppError(
            500,
            "INTERNAL_EGRESS_POLICY_INVALID",
            "Internal egress policy digest does not match its rules",
        )
    return {
        "policy": {
            "desired_revision": policy.desired_revision,
            "desired_digest": policy.desired_digest,
            "applied_revision": policy.applied_revision,
            "applied_digest": policy.applied_digest,
            "apply_status": policy.apply_status,
            "last_error_code": policy.last_error_code,
            "last_error_summary": (
                INTERNAL_EGRESS_ERROR_SUMMARIES.get(
                    policy.last_error_code,
                    "The egress proxy could not apply the desired policy.",
                )
                if policy.last_error_code
                else None
            ),
            "updated_at": policy.updated_at.isoformat() + "Z",
        },
        "rules": [
            {
                "id": rule.id,
                "destination_cidr": rule.destination_cidr,
                "port": rule.port,
                "row_version": rule.row_version,
                "created_at": rule.created_at.isoformat() + "Z",
                "updated_at": rule.updated_at.isoformat() + "Z",
            }
            for rule in rules
        ],
    }


def _require_revision(policy: InternalEgressPolicy, expected_revision: int) -> None:
    if policy.desired_revision != expected_revision:
        raise AppError(
            409,
            "INTERNAL_EGRESS_POLICY_VERSION_CONFLICT",
            "Internal egress policy was changed by another administrator",
        )


def _advance_policy(
    db: Session, policy: InternalEgressPolicy, *, actor_user_id: str
) -> None:
    db.flush()
    body = canonical_policy_body(_rules(db))
    policy.desired_revision += 1
    policy.desired_digest = policy_digest(body)
    policy.apply_status = "PENDING"
    policy.last_error_code = None
    policy.updated_by_user_id = actor_user_id
    policy.updated_at = datetime.utcnow()


def create_internal_egress_rule(
    db: Session,
    *,
    actor_user_id: str,
    expected_revision: int,
    destination_cidr: str,
    port: int,
) -> tuple[InternalEgressPolicy, InternalEgressRule]:
    policy = get_internal_egress_policy(db, create=True)
    _require_revision(policy, expected_revision)
    destination_cidr = normalize_destination_cidr(destination_cidr)
    port = validate_internal_service_port(port)
    if (
        int(db.scalar(select(func.count(InternalEgressRule.id))) or 0)
        >= MAX_INTERNAL_EGRESS_RULES
    ):
        raise AppError(
            409,
            "INTERNAL_EGRESS_RULE_LIMIT",
            "At most 32 internal egress rules are allowed",
        )
    duplicate = db.scalar(
        select(InternalEgressRule).where(
            InternalEgressRule.destination_cidr == destination_cidr,
            InternalEgressRule.port == port,
        )
    )
    if duplicate is not None:
        raise AppError(
            409,
            "INTERNAL_EGRESS_RULE_EXISTS",
            "The internal destination and port already exist",
        )
    now = datetime.utcnow()
    rule = InternalEgressRule(
        id=str(uuid.uuid4()),
        destination_cidr=destination_cidr,
        port=port,
        row_version=1,
        created_by_user_id=actor_user_id,
        updated_by_user_id=actor_user_id,
        created_at=now,
        updated_at=now,
    )
    db.add(rule)
    _advance_policy(db, policy, actor_user_id=actor_user_id)
    return policy, rule


def update_internal_egress_rule(
    db: Session,
    *,
    rule_id: str,
    actor_user_id: str,
    expected_revision: int,
    expected_version: int,
    destination_cidr: str,
    port: int,
) -> tuple[InternalEgressPolicy, InternalEgressRule]:
    policy = get_internal_egress_policy(db, create=True)
    _require_revision(policy, expected_revision)
    rule = db.get(InternalEgressRule, rule_id)
    if rule is None:
        raise AppError(404, "INTERNAL_EGRESS_RULE_NOT_FOUND", "Rule was not found")
    if rule.row_version != expected_version:
        raise AppError(
            409,
            "INTERNAL_EGRESS_RULE_VERSION_CONFLICT",
            "Internal egress rule was changed by another administrator",
        )
    destination_cidr = normalize_destination_cidr(destination_cidr)
    port = validate_internal_service_port(port)
    duplicate = db.scalar(
        select(InternalEgressRule).where(
            InternalEgressRule.id != rule.id,
            InternalEgressRule.destination_cidr == destination_cidr,
            InternalEgressRule.port == port,
        )
    )
    if duplicate is not None:
        raise AppError(
            409,
            "INTERNAL_EGRESS_RULE_EXISTS",
            "The internal destination and port already exist",
        )
    rule.destination_cidr = destination_cidr
    rule.port = port
    rule.row_version += 1
    rule.updated_by_user_id = actor_user_id
    rule.updated_at = datetime.utcnow()
    _advance_policy(db, policy, actor_user_id=actor_user_id)
    return policy, rule


def delete_internal_egress_rule(
    db: Session,
    *,
    rule_id: str,
    actor_user_id: str,
    expected_revision: int,
    expected_version: int,
) -> tuple[InternalEgressPolicy, dict[str, object]]:
    policy = get_internal_egress_policy(db, create=True)
    _require_revision(policy, expected_revision)
    rule = db.get(InternalEgressRule, rule_id)
    if rule is None:
        raise AppError(404, "INTERNAL_EGRESS_RULE_NOT_FOUND", "Rule was not found")
    if rule.row_version != expected_version:
        raise AppError(
            409,
            "INTERNAL_EGRESS_RULE_VERSION_CONFLICT",
            "Internal egress rule was changed by another administrator",
        )
    deleted = {
        "id": rule.id,
        "destination_cidr": rule.destination_cidr,
        "port": rule.port,
        "version": rule.row_version,
    }
    db.delete(rule)
    _advance_policy(db, policy, actor_user_id=actor_user_id)
    return policy, deleted


def retry_internal_egress_policy(
    db: Session,
    *,
    actor_user_id: str,
    expected_revision: int,
) -> InternalEgressPolicy:
    policy = get_internal_egress_policy(db, create=True)
    _require_revision(policy, expected_revision)
    if policy.apply_status != "FAILED":
        raise AppError(
            409,
            "INTERNAL_EGRESS_POLICY_NOT_FAILED",
            "Only a failed internal egress policy can be retried",
        )
    _advance_policy(db, policy, actor_user_id=actor_user_id)
    return policy


def _require_runtime_directory(path: Path, *, exact_mode: int | None) -> None:
    try:
        value = path.lstat()
    except FileNotFoundError as exc:
        raise OSError("runtime policy directory is not initialized") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise OSError("runtime policy path is not a directory")
    if exact_mode is not None and stat.S_IMODE(value.st_mode) != exact_mode:
        raise OSError("runtime policy directory mode is invalid")


def _validate_runtime_layout(path: Path) -> None:
    # Docker creates this common parent around two separately mounted volumes;
    # it can legitimately be 0755. Only the writable mount roots have a strict
    # setgid/group-only contract.
    _require_runtime_directory(path, exact_mode=None)
    _require_runtime_directory(path / "desired", exact_mode=RUNTIME_DIRECTORY_MODE)
    _require_runtime_directory(path / "ack", exact_mode=RUNTIME_DIRECTORY_MODE)


def _read_regular_file(
    path: Path, *, maximum: int, mode: int
) -> tuple[bytes, int] | None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for attempt in range(RUNTIME_READ_ATTEMPTS):
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            if attempt + 1 < RUNTIME_READ_ATTEMPTS:
                time.sleep(0.001)
                continue
            return None
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise OSError("runtime policy file type is invalid")
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            if attempt + 1 < RUNTIME_READ_ATTEMPTS:
                time.sleep(0.001)
                continue
            return None
        with os.fdopen(descriptor, "rb") as input_file:
            opened_stat = os.fstat(input_file.fileno())
            payload = input_file.read(maximum + 1)
            final_stat = os.fstat(input_file.fileno())
        try:
            final_path_stat = path.lstat()
        except FileNotFoundError:
            if attempt + 1 < RUNTIME_READ_ATTEMPTS:
                time.sleep(0.001)
                continue
            return None
        identities = {
            (value.st_dev, value.st_ino)
            for value in (path_stat, opened_stat, final_stat, final_path_stat)
        }
        sizes = {
            value.st_size
            for value in (path_stat, opened_stat, final_stat, final_path_stat)
        }
        modes = {
            stat.S_IMODE(value.st_mode)
            for value in (path_stat, opened_stat, final_stat, final_path_stat)
        }
        if len(identities) != 1 or len(sizes) != 1 or len(modes) != 1:
            if attempt + 1 < RUNTIME_READ_ATTEMPTS:
                time.sleep(0.001)
                continue
            return None
        if not all(
            stat.S_ISREG(value.st_mode)
            for value in (opened_stat, final_stat, final_path_stat)
        ):
            raise OSError("runtime policy file type is invalid")
        if modes != {mode}:
            raise OSError("runtime policy file mode is invalid")
        if (
            not payload
            or len(payload) > maximum
            or not 0 < final_stat.st_size <= maximum
        ):
            raise OSError("runtime policy file size is invalid")
        return payload, final_stat.st_mtime_ns
    return None


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, DESIRED_POLICY_MODE)
        os.fchmod(descriptor, DESIRED_POLICY_MODE)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_ack(path: Path, *, not_before_mtime_ns: int) -> InternalEgressAck | None:
    snapshot = _read_regular_file(path, maximum=512, mode=ACK_POLICY_MODE)
    if snapshot is None:
        return None
    payload, modified_at_ns = snapshot
    if modified_at_ns < not_before_mtime_ns:
        return None
    return parse_ack_payload(payload)


def _worker_audit(
    db: Session,
    policy: InternalEgressPolicy,
    *,
    result: str,
    error_code: str | None = None,
) -> None:
    metadata: dict[str, object] = {
        "revision": policy.desired_revision,
        "digest": policy.desired_digest,
    }
    if error_code:
        metadata["error_code"] = error_code
    db.add(
        AuditEvent(
            id=str(uuid.uuid4()),
            actor_user_id=policy.updated_by_user_id,
            workspace_id=None,
            action="INTERNAL_EGRESS_POLICY_APPLY",
            result=result,
            request_id=f"worker:internal-egress:{policy.desired_revision}",
            safe_metadata_json=json_dumps_safe(metadata),
        )
    )


def _record_runtime_failure(
    db: Session,
    policy: InternalEgressPolicy,
    *,
    error_code: str,
) -> bool:
    if not SAFE_ERROR_CODE_RE.fullmatch(error_code):
        raise ValueError("internal egress error code is unsafe")
    if policy.apply_status == "FAILED" and policy.last_error_code == error_code:
        db.rollback()
        return False
    policy.apply_status = "FAILED"
    policy.last_error_code = error_code
    policy.updated_at = datetime.utcnow()
    _worker_audit(db, policy, result="FAILED", error_code=error_code)
    db.commit()
    return True


def sync_internal_egress_runtime(
    session_factory: sessionmaker[Session], runtime_directory: Path
) -> bool:
    """Publish desired policy and import a helper acknowledgement.

    The last acknowledged revision is never cleared on a publish or helper
    failure. The egress helper can therefore keep serving its previous in-memory
    policy while the UI exposes the failed desired revision.
    """

    with session_factory() as db:
        begin_immediate(db)
        policy = get_internal_egress_policy(db, create=True)
        rules = _rules(db)
        payload = desired_policy_payload(policy.desired_revision, rules)
        if policy_digest(canonical_policy_body(rules)) != policy.desired_digest:
            db.rollback()
            raise AppError(
                500,
                "INTERNAL_EGRESS_POLICY_INVALID",
                "Internal egress policy digest does not match its rules",
            )

        desired_path = runtime_directory / "desired" / "policy.txt"
        try:
            _validate_runtime_layout(runtime_directory)
            desired_snapshot = _read_regular_file(
                desired_path,
                maximum=65_536,
                mode=DESIRED_POLICY_MODE,
            )
        except OSError:
            return _record_runtime_failure(
                db, policy, error_code="POLICY_PUBLISH_FAILED"
            )

        current_payload = desired_snapshot[0] if desired_snapshot is not None else None
        if current_payload != payload:
            # Failed desired state changes require an explicit, audited retry.
            if policy.apply_status == "FAILED":
                db.rollback()
                return False
            try:
                _atomic_write(desired_path, payload)
            except OSError:
                return _record_runtime_failure(
                    db, policy, error_code="POLICY_PUBLISH_FAILED"
                )
            policy.apply_status = "APPLYING"
            policy.last_error_code = None
            policy.updated_at = datetime.utcnow()
            db.commit()
            return True

        assert desired_snapshot is not None
        desired_modified_at_ns = desired_snapshot[1]
        ack_path = runtime_directory / "ack" / "status.txt"
        try:
            ack = _read_ack(
                ack_path,
                not_before_mtime_ns=desired_modified_at_ns,
            )
        except (OSError, ValueError):
            return _record_runtime_failure(db, policy, error_code="ACK_INVALID")

        if ack is not None and ack.revision < policy.desired_revision:
            # A split read-only ACK mount can retain the previous revision while
            # Squid is still consuming a newly published desired file.
            ack = None
        if ack is not None and ack.revision > policy.desired_revision:
            return _record_runtime_failure(
                db, policy, error_code="ACK_REVISION_MISMATCH"
            )
        if ack is not None and ack.digest != policy.desired_digest:
            return _record_runtime_failure(db, policy, error_code="ACK_DIGEST_MISMATCH")
        if ack is not None and ack.status == "APPLIED":
            if (
                policy.apply_status == "APPLIED"
                and policy.applied_revision == ack.revision
                and policy.applied_digest == ack.digest
            ):
                db.rollback()
                return False
            policy.applied_revision = ack.revision
            policy.applied_digest = ack.digest
            policy.apply_status = "APPLIED"
            policy.last_error_code = None
            policy.applied_at = datetime.utcnow()
            policy.updated_at = datetime.utcnow()
            _worker_audit(db, policy, result="SUCCEEDED")
            db.commit()
            return True
        if ack is not None:
            assert ack.error_code is not None
            return _record_runtime_failure(db, policy, error_code=ack.error_code)

        if policy.apply_status == "PENDING":
            policy.apply_status = "APPLYING"
            policy.last_error_code = None
            policy.updated_at = datetime.utcnow()
            db.commit()
            return True
        db.rollback()
        return False
