from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


def _sqlite_version_is_safe(version: tuple[int, int, int]) -> bool:
    return (
        version >= (3, 51, 3)
        or (3, 50, 7) <= version < (3, 51, 0)
        or ((3, 44, 6) <= version < (3, 45, 0))
    )


def create_database_engine(settings: Settings) -> Engine:
    connect_args: dict[str, object] = {}
    if settings.database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 5}
        if settings.enforce_safe_sqlite and not _sqlite_version_is_safe(
            sqlite3.sqlite_version_info
        ):
            raise RuntimeError(
                "Unsafe SQLite runtime for WAL mode: "
                f"{sqlite3.sqlite_version}; require 3.51.3+, 3.50.7 backport, or 3.44.6 backport"
            )

    engine = create_engine(
        settings.database_url, connect_args=connect_args, future=True
    )

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _configure_sqlite(
            dbapi_connection: object, _connection_record: object
        ) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            if settings.database_url != "sqlite:///:memory:":
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )


def begin_immediate(session: Session) -> None:
    """Acquire SQLite's writer reservation before quota reads.

    The service is intentionally single-process/single-worker for the SQLite MVP.
    """
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def session_dependency(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
