"""Fail-closed NativeAuthenticator account administration for production.

The production web signup endpoint is permanently disabled.  This one-shot
tool is run with JupyterHub and the public Gateway stopped, reads a password
from the controlling terminal, and creates or resets only an explicitly
validated NativeAuthenticator credential.  It never accepts a password through
argv or the environment.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import importlib.resources
import os
import re
import secrets
import sqlite3
import stat
from pathlib import Path

import bcrypt


USERNAME_PATTERN = re.compile(r"^(?!.*--)[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")
MINIMUM_PASSWORD_CHARACTERS = 12
MAXIMUM_PASSWORD_BYTES = 72


class NativeUserAdminError(RuntimeError):
    """Raised when account administration cannot preserve its invariants."""


def _common_passwords() -> set[str]:
    resource = importlib.resources.files("nativeauthenticator").joinpath(
        "common-credentials.txt"
    )
    return set(resource.read_text(encoding="utf-8").splitlines())


def validate_password(
    password: str, *, common_passwords: set[str] | None = None
) -> None:
    encoded = password.encode("utf-8")
    if len(password) < MINIMUM_PASSWORD_CHARACTERS:
        raise NativeUserAdminError(
            f"password must contain at least {MINIMUM_PASSWORD_CHARACTERS} characters"
        )
    if len(encoded) > MAXIMUM_PASSWORD_BYTES:
        raise NativeUserAdminError(
            f"UTF-8 password must not exceed {MAXIMUM_PASSWORD_BYTES} bytes"
        )
    if "\x00" in password:
        raise NativeUserAdminError("password must not contain NUL")
    denylist = common_passwords if common_passwords is not None else _common_passwords()
    if password in denylist:
        raise NativeUserAdminError("password is in the common-password denylist")


def _open_existing_database(path: Path) -> sqlite3.Connection:
    if not path.is_absolute():
        raise NativeUserAdminError("database path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise NativeUserAdminError(
            f"cannot inspect JupyterHub database: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise NativeUserAdminError(
            "JupyterHub database must be an existing regular non-symlink file"
        )
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=rw", uri=True, timeout=10, isolation_level=None
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    except sqlite3.Error as exc:
        raise NativeUserAdminError(f"cannot open JupyterHub database: {exc}") from exc


def create_authorized_user(
    database: Path,
    *,
    username: str,
    password: str,
    admin_username: str,
    require_empty: bool = False,
    common_passwords: set[str] | None = None,
) -> bool:
    """Create one authorized NativeAuthenticator user.

    Returns ``True`` when a row was created and ``False`` for a safe idempotent
    replay of an already-authorized account.  Existing rows are never modified.
    """

    if not USERNAME_PATTERN.fullmatch(username):
        raise NativeUserAdminError("username does not match the platform policy")
    if not USERNAME_PATTERN.fullmatch(admin_username):
        raise NativeUserAdminError("configured admin username is invalid")
    validate_password(password, common_passwords=common_passwords)

    connection = _open_existing_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"users", "users_info"}.issubset(tables):
            raise NativeUserAdminError(
                "JupyterHub schema is not initialized; run production-up first"
            )

        existing = connection.execute(
            "SELECT password, is_authorized FROM users_info WHERE username = ?",
            (username,),
        ).fetchall()
        if len(existing) > 1:
            raise NativeUserAdminError("duplicate NativeAuthenticator rows detected")
        if existing:
            password_hash, is_authorized = existing[0]
            if is_authorized != 1:
                raise NativeUserAdminError(
                    "existing NativeAuthenticator account is not authorized"
                )
            try:
                password_matches = bcrypt.checkpw(
                    password.encode("utf-8"), password_hash
                )
            except (TypeError, ValueError) as exc:
                raise NativeUserAdminError(
                    "existing NativeAuthenticator password hash is invalid"
                ) from exc
            if not password_matches:
                recovery_hint = (
                    "; use reset-admin-password for the configured administrator"
                    if username == admin_username
                    else ""
                )
                raise NativeUserAdminError(
                    "account already exists and the supplied password does not match; "
                    f"the password was not changed{recovery_hint}"
                )
            connection.execute("ROLLBACK")
            return False

        account_count = connection.execute(
            "SELECT COUNT(*) FROM users_info"
        ).fetchone()[0]
        if require_empty and account_count != 0:
            raise NativeUserAdminError(
                "initial admin bootstrap requires an empty NativeAuthenticator account table"
            )

        if username == admin_username:
            hub_admin = connection.execute(
                "SELECT admin FROM users WHERE name = ?", (username,)
            ).fetchall()
            if len(hub_admin) != 1 or hub_admin[0][0] != 1:
                raise NativeUserAdminError(
                    "configured JupyterHub admin row is missing or is not admin"
                )

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        otp_secret = secrets.token_bytes(10)
        connection.execute(
            """
            INSERT INTO users_info (
                username, password, is_authorized, login_email_sent,
                email, has_2fa, otp_secret
            ) VALUES (?, ?, 1, 0, NULL, 0, ?)
            """,
            (
                username,
                password_hash,
                base64.b32encode(otp_secret).decode("ascii"),
            ),
        )
        connection.execute("COMMIT")
        return True
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def reset_authorized_admin_password(
    database: Path,
    *,
    username: str,
    password: str,
    admin_username: str,
    common_passwords: set[str] | None = None,
) -> None:
    """Replace only the configured, authorized administrator's password hash."""

    if not USERNAME_PATTERN.fullmatch(username):
        raise NativeUserAdminError("username does not match the platform policy")
    if not USERNAME_PATTERN.fullmatch(admin_username):
        raise NativeUserAdminError("configured admin username is invalid")
    if username != admin_username:
        raise NativeUserAdminError(
            "password reset is restricted to the configured administrator"
        )
    validate_password(password, common_passwords=common_passwords)

    connection = _open_existing_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"users", "users_info"}.issubset(tables):
            raise NativeUserAdminError(
                "JupyterHub schema is not initialized; run production-up first"
            )

        existing = connection.execute(
            "SELECT id, password, is_authorized FROM users_info WHERE username = ?",
            (username,),
        ).fetchall()
        if len(existing) != 1:
            if not existing:
                raise NativeUserAdminError(
                    "configured NativeAuthenticator administrator does not exist"
                )
            raise NativeUserAdminError("duplicate NativeAuthenticator rows detected")
        user_id, current_hash, is_authorized = existing[0]
        if is_authorized != 1:
            raise NativeUserAdminError(
                "configured NativeAuthenticator administrator is not authorized"
            )

        hub_admin = connection.execute(
            "SELECT admin FROM users WHERE name = ?", (username,)
        ).fetchall()
        if len(hub_admin) != 1 or hub_admin[0][0] != 1:
            raise NativeUserAdminError(
                "configured JupyterHub admin row is missing or is not admin"
            )

        try:
            password_unchanged = bcrypt.checkpw(password.encode("utf-8"), current_hash)
        except (TypeError, ValueError) as exc:
            raise NativeUserAdminError(
                "existing NativeAuthenticator password hash is invalid"
            ) from exc
        if password_unchanged:
            raise NativeUserAdminError(
                "new administrator password must differ from the current password"
            )

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        if not bcrypt.checkpw(password.encode("utf-8"), password_hash):
            raise NativeUserAdminError(
                "new administrator password hash verification failed"
            )
        updated = connection.execute(
            "UPDATE users_info SET password = ? "
            "WHERE id = ? AND username = ? AND is_authorized = 1",
            (password_hash, user_id, username),
        )
        if updated.rowcount != 1:
            raise NativeUserAdminError(
                "administrator password update did not affect exactly one row"
            )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or reset one production NativeAuthenticator credential"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--admin-username", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--require-empty", action="store_true")
    mode.add_argument("--reset-admin-password", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not os.isatty(0):
        raise NativeUserAdminError("password input requires an interactive terminal")
    prompt = (
        "New administrator password"
        if args.reset_admin_password
        else "Temporary password"
    )
    password = getpass.getpass(f"{prompt} for {args.username}: ")
    confirmation = getpass.getpass(f"Confirm {prompt.lower()}: ")
    if password != confirmation:
        raise NativeUserAdminError("password confirmation does not match")
    if args.reset_admin_password:
        reset_authorized_admin_password(
            args.database,
            username=args.username,
            password=password,
            admin_username=args.admin_username,
        )
        print(f"NativeAuthenticator administrator password reset: {args.username}")
        return 0
    created = create_authorized_user(
        args.database,
        username=args.username,
        password=password,
        admin_username=args.admin_username,
        require_empty=args.require_empty,
    )
    print(
        f"NativeAuthenticator account {'created' if created else 'already configured'}: "
        f"{args.username}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NativeUserAdminError as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        raise SystemExit(1) from None
