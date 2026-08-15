#!/usr/bin/env python3
"""Print canonical profile digests while preparing a reviewed allowlist."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from profile_policy import profile_digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("policy", type=Path)
    args = parser.parse_args()
    data = json.loads(args.policy.read_text(encoding="utf-8"))
    for profile in data.get("profiles", []):
        print(f"{profile.get('id')}@{profile.get('version')} {profile_digest(profile)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
