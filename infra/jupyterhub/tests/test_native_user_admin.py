from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import bcrypt


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from native_user_admin import NativeUserAdminError, create_authorized_user  # noqa: E402


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
