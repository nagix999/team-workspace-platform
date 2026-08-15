from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> None:
    parser = argparse.ArgumentParser(description="Run platform Alembic migrations")
    parser.add_argument("command", nargs="?", default="upgrade", choices=["upgrade"])
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args()
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    command.upgrade(config, args.revision)


if __name__ == "__main__":
    main()
