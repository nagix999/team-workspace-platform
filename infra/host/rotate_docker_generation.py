#!/usr/bin/env python3
"""Invalidate health and create a new generation after every Docker restart."""

from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path

from hostlib import HostConfigError, atomic_write, load_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--invalidate-only", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    output = Path(config["health"]["output_dir"])
    output.mkdir(parents=True, exist_ok=True, mode=0o755)
    for kind in ("network", "storage"):
        try:
            (output / f"{kind}_health.json").unlink()
        except FileNotFoundError:
            pass
    if not args.invalidate_only:
        atomic_write(output / "docker_generation", f"{uuid.uuid4()}\n".encode("ascii"))
    os.sync()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HostConfigError as exc:
        raise SystemExit(f"ERROR: {exc}")
