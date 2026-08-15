#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from hostlib import HostConfigError, load_config, policy_digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    print(f"EXPECTED_NETWORK_POLICY_SHA256={policy_digest(config, 'network')}")
    print(f"EXPECTED_STORAGE_POLICY_SHA256={policy_digest(config, 'storage')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HostConfigError as exc:
        raise SystemExit(f"ERROR: {exc}")
