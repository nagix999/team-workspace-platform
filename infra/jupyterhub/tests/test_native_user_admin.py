from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import bcrypt

try:
    from nativeauthenticator.orm import UserInfo as NativeAuthenticatorUserInfo
except ModuleNotFoundError:
    NativeAuthenticatorUserInfo = None


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from native_user_admin import (  # noqa: E402
    NativeUserAdminError,
    create_authorized_user,
    reset_authorized_admin_password,
)


STRONG_PASSWORD = "Correct-Horse-27!"


def initialized_database(root: Path, *, admin: str = "platform-admin") -> Path:
    path = root / "jupyterhub.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name VARCHAR(255),
            admin BOOLEAN
        );
        CREATE TABLE users_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(128) NOT NULL,
            password BLOB NOT NULL,
            is_authorized BOOLEAN,
            login_email_sent BOOLEAN,
            email VARCHAR(128),
            has_2fa BOOLEAN,
            otp_secret VARCHAR(16)
        );
        """
    )
    connection.execute(
        "INSERT INTO users (name, admin) VALUES (?, 1)",
        (admin,),
    )
    connection.commit()
    connection.close()
    return path


class NativeUserAdminTests(unittest.TestCase):
    def test_bootstraps_exact_admin_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = initialized_database(Path(temporary))
            created = create_authorized_user(
                database,
                username="platform-admin",
                password=STRONG_PASSWORD,
                admin_username="platform-admin",
                require_empty=True,
                common_passwords=set(),
            )
            self.assertTrue(created)
            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT password, is_authorized, has_2fa, length(otp_secret) "
                "FROM users_info WHERE username = 'platform-admin'"
            ).fetchone()
            connection.close()
            self.assertIsNotNone(row)
            self.assertTrue(bcrypt.checkpw(STRONG_PASSWORD.encode(), row[0]))
            self.assertEqual(row[1:], (1, 0, 16))

            replayed = create_authorized_user(
                database,
                username="platform-admin",
                password=STRONG_PASSWORD,
                admin_username="platform-admin",
                require_empty=True,
                common_passwords=set(),
            )
            self.assertFalse(replayed)

            with self.assertRaisesRegex(
                NativeUserAdminError, "password was not changed"
            ):
                create_authorized_user(
                    database,
                    username="platform-admin",
                    password="Different-Password-48!",
                    admin_username="platform-admin",
                    require_empty=True,
                    common_passwords=set(),
                )
            connection = sqlite3.connect(database)
            unchanged_hash = connection.execute(
                "SELECT password FROM users_info WHERE username = 'platform-admin'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(unchanged_hash, row[0])

    def test_resets_admin_password_without_changing_account_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = initialized_database(Path(temporary))
            create_authorized_user(
                database,
                username="platform-admin",
                password=STRONG_PASSWORD,
                admin_username="platform-admin",
                require_empty=True,
                common_passwords=set(),
            )
            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE users_info SET login_email_sent = 1, email = ?, "
                "has_2fa = 1, otp_secret = ? WHERE username = 'platform-admin'",
                ("admin@example.invalid", "ABCDEFGHIJKLMNOP"),
            )
            connection.commit()
            before = connection.execute(
                "SELECT is_authorized, login_email_sent, email, has_2fa, otp_secret "
                "FROM users_info WHERE username = 'platform-admin'"
            ).fetchone()
            connection.close()

            replacement = "Replacement-Admin-58!"
            reset_authorized_admin_password(
                database,
                username="platform-admin",
                password=replacement,
                admin_username="platform-admin",
                common_passwords=set(),
            )

            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT password, is_authorized, login_email_sent, email, "
                "has_2fa, otp_secret FROM users_info "
                "WHERE username = 'platform-admin'"
            ).fetchone()
            connection.close()
            self.assertFalse(bcrypt.checkpw(STRONG_PASSWORD.encode(), row[0]))
            self.assertTrue(bcrypt.checkpw(replacement.encode(), row[0]))
            self.assertEqual(row[1:], before)

    @unittest.skipIf(
        NativeAuthenticatorUserInfo is None,
        "NativeAuthenticator is available only in the JupyterHub image",
    )
    def test_reset_hash_is_accepted_by_installed_nativeauthenticator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = initialized_database(Path(temporary))
            create_authorized_user(
                database,
                username="platform-admin",
                password=STRONG_PASSWORD,
                admin_username="platform-admin",
                require_empty=True,
                common_passwords=set(),
            )
            replacement = "Replacement-Admin-58!"
            reset_authorized_admin_password(
                database,
                username="platform-admin",
                password=replacement,
                admin_username="platform-admin",
                common_passwords=set(),
            )
            connection = sqlite3.connect(database)
            password_hash = connection.execute(
                "SELECT password FROM users_info WHERE username = 'platform-admin'"
            ).fetchone()[0]
            connection.close()

            native_user = NativeAuthenticatorUserInfo(
                username="platform-admin",
                password=password_hash,
                is_authorized=True,
            )
            self.assertTrue(native_user.is_valid_password(replacement))
            self.assertFalse(native_user.is_valid_password(STRONG_PASSWORD))

    def test_admin_password_reset_rejects_same_or_weak_password(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = initialized_database(Path(temporary))
            create_authorized_user(
                database,
                username="platform-admin",
                password=STRONG_PASSWORD,
                admin_username="platform-admin",
                require_empty=True,
                common_passwords=set(),
            )
            for password, expected in (
                (STRONG_PASSWORD, "must differ"),
                ("too-short", "at least"),
                ("passwordpassword", "denylist"),
            ):
                with self.subTest(expected=expected):
                    with self.assertRaisesRegex(NativeUserAdminError, expected):
                        reset_authorized_admin_password(
                            database,
                            username="platform-admin",
                            password=password,
                            admin_username="platform-admin",
                            common_passwords={"passwordpassword"},
                        )
            connection = sqlite3.connect(database)
            password_hash = connection.execute(
                "SELECT password FROM users_info WHERE username = 'platform-admin'"
            ).fetchone()[0]
            connection.close()
            self.assertTrue(bcrypt.checkpw(STRONG_PASSWORD.encode(), password_hash))

    def test_admin_password_reset_rejects_missing_unauthorized_or_duplicate_row(
        self,
    ) -> None:
        for state, expected in (
            ("missing", "does not exist"),
            ("unauthorized", "not authorized"),
            ("duplicate", "duplicate"),
        ):
            with self.subTest(state=state):
                with tempfile.TemporaryDirectory() as temporary:
                    database = initialized_database(Path(temporary))
                    connection = sqlite3.connect(database)
                    if state != "missing":
                        password_hash = bcrypt.hashpw(
                            STRONG_PASSWORD.encode(), bcrypt.gensalt()
                        )
                        connection.execute(
                            "INSERT INTO users_info "
                            "(username, password, is_authorized) VALUES (?, ?, ?)",
                            ("platform-admin", password_hash, state == "duplicate"),
                        )
                        if state == "duplicate":
                            connection.execute(
                                "INSERT INTO users_info "
                                "(username, password, is_authorized) VALUES (?, ?, 1)",
                                ("platform-admin", password_hash),
                            )
                    connection.commit()
                    connection.close()
                    with self.assertRaisesRegex(NativeUserAdminError, expected):
                        reset_authorized_admin_password(
                            database,
                            username="platform-admin",
                            password="Replacement-Admin-58!",
                            admin_username="platform-admin",
                            common_passwords=set(),
                        )

    def test_admin_password_reset_rejects_admin_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = initialized_database(Path(temporary))
            create_authorized_user(
                database,
                username="platform-admin",
                password=STRONG_PASSWORD,
                admin_username="platform-admin",
                require_empty=True,
                common_passwords=set(),
            )
            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE users SET admin = 0 WHERE name = 'platform-admin'"
            )
            connection.commit()
            original_hash = connection.execute(
                "SELECT password FROM users_info WHERE username = 'platform-admin'"
            ).fetchone()[0]
            connection.close()
            with self.assertRaisesRegex(NativeUserAdminError, "admin row"):
                reset_authorized_admin_password(
                    database,
                    username="platform-admin",
                    password="Replacement-Admin-58!",
                    admin_username="platform-admin",
                    common_passwords=set(),
                )
            connection = sqlite3.connect(database)
            unchanged_hash = connection.execute(
                "SELECT password FROM users_info WHERE username = 'platform-admin'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(unchanged_hash, original_hash)

    def test_creates_pre_authorized_non_admin_after_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = initialized_database(Path(temporary))
            create_authorized_user(
                database,
                username="platform-admin",
                password=STRONG_PASSWORD,
                admin_username="platform-admin",
                require_empty=True,
                common_passwords=set(),
            )
            created = create_authorized_user(
                database,
                username="alice",
                password="Alice-Temporary-49!",
                admin_username="platform-admin",
                common_passwords=set(),
            )
            self.assertTrue(created)
            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT is_authorized FROM users_info WHERE username = 'alice'"
            ).fetchone()
            connection.close()
            self.assertEqual(row, (1,))

            with self.assertRaises(NativeUserAdminError) as raised:
                create_authorized_user(
                    database,
                    username="alice",
                    password="Different-Temporary-71!",
                    admin_username="platform-admin",
                    common_passwords=set(),
                )
            self.assertIn("password was not changed", str(raised.exception))
            self.assertNotIn("reset-admin-password", str(raised.exception))

    def test_initial_bootstrap_rejects_existing_different_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = initialized_database(Path(temporary))
            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT INTO users_info (username, password, is_authorized) "
                "VALUES ('alice', X'00', 1)"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(NativeUserAdminError, "empty"):
                create_authorized_user(
                    database,
                    username="platform-admin",
                    password=STRONG_PASSWORD,
                    admin_username="platform-admin",
                    require_empty=True,
                    common_passwords=set(),
                )

    def test_rejects_weak_username_password_and_admin_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = initialized_database(Path(temporary))
            for username, password, expected in (
                ("Alice", STRONG_PASSWORD, "username"),
                ("alice", "too-short", "at least"),
                ("alice", "passwordpassword", "denylist"),
            ):
                with self.subTest(username=username, expected=expected):
                    with self.assertRaisesRegex(NativeUserAdminError, expected):
                        create_authorized_user(
                            database,
                            username=username,
                            password=password,
                            admin_username="platform-admin",
                            common_passwords={"passwordpassword"},
                        )
            with self.assertRaisesRegex(NativeUserAdminError, "admin row"):
                create_authorized_user(
                    database,
                    username="other-admin",
                    password=STRONG_PASSWORD,
                    admin_username="other-admin",
                    require_empty=True,
                    common_passwords=set(),
                )

    def test_rejects_missing_database_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(NativeUserAdminError, "inspect"):
                create_authorized_user(
                    root / "missing.sqlite",
                    username="alice",
                    password=STRONG_PASSWORD,
                    admin_username="platform-admin",
                    common_passwords=set(),
                )
            empty = root / "empty.sqlite"
            sqlite3.connect(empty).close()
            with self.assertRaisesRegex(NativeUserAdminError, "schema"):
                create_authorized_user(
                    empty,
                    username="alice",
                    password=STRONG_PASSWORD,
                    admin_username="platform-admin",
                    common_passwords=set(),
                )


if __name__ == "__main__":
    unittest.main()
