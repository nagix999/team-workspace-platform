#!/usr/bin/env python3
"""Generate the reviewed local Python/CPU/memory profile cross product."""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from profile_policy import profile_digest


CPU_LIMITS = (1, 2, 4)
MEMORY_LIMITS_MB = (1024, 2048, 4096)
FAMILIES = (
    ("python312", "python312-cpu1-mem1024", "python312"),
    ("python313", "python313-cpu1-mem1024", "python3"),
)
MATRIX_ID = re.compile(r"^(python312|python313)-cpu(1|2|4)-mem(1024|2048|4096)$")
VARIABLE_FIELDS = {
    "id",
    "config_digest",
    "cpu_limit",
    "memory_limit_bytes",
}


def _execution_contract(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in profile.items() if key not in VARIABLE_FIELDS}


def generate_document(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != 2:
        raise ValueError("local profile matrix requires schema_version 2")
    profiles = document.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("profiles must be a list")

    by_id = {
        profile.get("id"): profile
        for profile in profiles
        if isinstance(profile, dict) and isinstance(profile.get("id"), str)
    }
    templates: dict[str, dict[str, Any]] = {}
    for family, source_id, default_kernel in FAMILIES:
        source = by_id.get(source_id)
        if source is None:
            raise ValueError(f"missing reviewed template {source_id}")
        if source.get("version") != 1 or source.get("selectable") is not True:
            raise ValueError(f"invalid reviewed template {source_id}")
        if source.get("default_kernel") != default_kernel:
            raise ValueError(f"template {source_id} has the wrong default kernel")
        templates[family] = source

    for profile in profiles:
        if not isinstance(profile, dict) or profile.get("selectable") is not True:
            continue
        profile_id = profile.get("id")
        match = MATRIX_ID.fullmatch(profile_id) if isinstance(profile_id, str) else None
        if match is None:
            raise ValueError(f"unexpected selectable local profile {profile_id!r}")
        family = match.group(1)
        if _execution_contract(profile) != _execution_contract(templates[family]):
            raise ValueError(f"runtime contract drift in {profile_id}")

    generated = [
        deepcopy(profile)
        for profile in profiles
        if not isinstance(profile, dict) or profile.get("selectable") is not True
    ]
    for family, _source_id, _default_kernel in FAMILIES:
        template = templates[family]
        for cpu_limit in CPU_LIMITS:
            for memory_mb in MEMORY_LIMITS_MB:
                profile = deepcopy(template)
                profile["id"] = f"{family}-cpu{cpu_limit}-mem{memory_mb}"
                profile["cpu_limit"] = cpu_limit
                profile["memory_limit_bytes"] = memory_mb * 1024 * 1024
                profile["config_digest"] = profile_digest(profile)
                generated.append(profile)

    result = deepcopy(document)
    result["profiles"] = generated
    return result


def _render(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).with_name("profiles.local-dev.json"),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="replace the policy with the deterministic generated matrix",
    )
    args = parser.parse_args()

    current_text = args.policy.read_text(encoding="utf-8")
    generated_text = _render(generate_document(json.loads(current_text)))
    if args.write:
        args.policy.write_text(generated_text, encoding="utf-8")
        print(
            f"wrote {len(CPU_LIMITS) * len(MEMORY_LIMITS_MB) * len(FAMILIES)} "
            f"selectable profiles to {args.policy}"
        )
        return 0
    if current_text != generated_text:
        print(
            "local profile matrix is stale; run "
            "infra/jupyterhub/generate_local_profile_matrix.py --write",
            file=sys.stderr,
        )
        return 1
    print("local profile matrix is current: selectable=18")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
