from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


USERNAME_RE = re.compile(r"^(?!.*--)[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def random_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def sha256_hex(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def keyed_hash(value: str, key: str) -> str:
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def validate_hub_username(username: str) -> str:
    normalized = username.strip().lower()
    if normalized != username or not USERNAME_RE.fullmatch(normalized):
        raise ValueError(
            "Hub returned a username outside the configured DNS-safe policy"
        )
    return normalized


class TokenCipher:
    """Small AEAD envelope. The key id makes rotation/migration explicit."""

    def __init__(self, key_id: str, encoded_key: str) -> None:
        try:
            padded = encoded_key + "=" * (-len(encoded_key) % 4)
            key = base64.urlsafe_b64decode(padded.encode())
        except Exception as exc:  # pragma: no cover - defensive error path
            raise RuntimeError(
                "PLATFORM_TOKEN_ENCRYPTION_KEY must be URL-safe base64"
            ) from exc
        if len(key) != 32:
            raise RuntimeError(
                "PLATFORM_TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes"
            )
        self._key_id = key_id
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, *, purpose: str) -> str:
        nonce = secrets.token_bytes(12)
        encrypted = self._aead.encrypt(nonce, plaintext.encode(), purpose.encode())
        payload = base64.urlsafe_b64encode(nonce + encrypted).decode()
        return f"{self._key_id}:{payload}"

    def decrypt(self, envelope: str, *, purpose: str) -> str:
        key_id, separator, payload = envelope.partition(":")
        if separator != ":" or key_id != self._key_id:
            raise ValueError("unknown encryption key id")
        try:
            raw = base64.b64decode(
                payload.encode("ascii"), altchars=b"-_", validate=True
            )
            if len(raw) < 13:
                raise ValueError("invalid encrypted envelope")
            plaintext = self._aead.decrypt(raw[:12], raw[12:], purpose.encode())
            return plaintext.decode("utf-8")
        except (binascii.Error, InvalidTag, UnicodeError, ValueError) as exc:
            # Normalize malformed/tampered envelopes so callers can fail closed
            # without leaking cryptography-specific exceptions or killing a worker.
            raise ValueError("invalid encrypted envelope") from exc


def csrf_token(session_cookie: str, key: str) -> str:
    return keyed_hash(f"csrf:{session_cookie}", key)


def safe_redirect_path(path: str | None) -> str:
    if not path:
        return "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    if any(ord(char) < 32 for char in path):
        return "/"
    # OAuth transaction redirects are deliberately limited to the SPA itself.
    if path != "/" and not path.startswith("/workspaces"):
        return "/"
    return path


@dataclass(frozen=True)
class SignedInternalRequest:
    version: str
    timestamp: int
    nonce: str
    content_sha256: str
    signature: str


def internal_signature(
    key: str, *, timestamp: int, nonce: str, method: str, path: str, body: bytes
) -> str:
    body_digest = hashlib.sha256(body).hexdigest()
    canonical = f"v1\n{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{body_digest}"
    return hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def verify_internal_signature(
    key: str,
    signed: SignedInternalRequest,
    *,
    method: str,
    path: str,
    body: bytes,
    now: datetime,
    max_skew_seconds: int = 30,
) -> bool:
    if not signed.nonce or len(signed.nonce) > 128:
        return False
    body_digest = hashlib.sha256(body).hexdigest()
    if signed.version != "v1" or signed.content_sha256 != body_digest:
        return False
    now_epoch = (
        int(now.replace(tzinfo=timezone.utc).timestamp())
        if now.tzinfo is None
        else int(now.timestamp())
    )
    if abs(now_epoch - signed.timestamp) > max_skew_seconds:
        return False
    expected = internal_signature(
        key,
        timestamp=signed.timestamp,
        nonce=signed.nonce,
        method=method,
        path=path,
        body=body,
    )
    if not signed.signature.startswith("v1="):
        return False
    return constant_time_equal(expected, signed.signature[3:])


def json_dumps_safe(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
