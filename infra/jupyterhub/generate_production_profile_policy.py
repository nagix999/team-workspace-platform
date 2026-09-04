#!/usr/bin/env python3
"""Generate a production profile history pinned to reviewed local image IDs.

The single-host deployment deliberately pins CPU and optional NVIDIA runtimes
to separate exact local image IDs. Old enabled versions remain in the document
so stopped workspaces can restart; only the newest version of each template
stays selectable for new workspaces.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from profile_policy import ProfilePolicyError, load_profile_policy, profile_digest


IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
MAX_NVIDIA_GPU_COUNT = 64


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read profile policy {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"profile policy {path} is not an object")
    return value


def generate_policy(
    *,
    template: dict[str, Any],
    image_id: str,
    gpu_image_id: str | None = None,
    gpu_count: int | None = None,
    shared_volume_name: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise RuntimeError(
            "production single-user image must be an exact local image ID"
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", shared_volume_name):
        raise RuntimeError("production shared volume name is invalid")
    template_schema = template.get("schema_version")
    if template_schema not in {2, 3}:
        raise RuntimeError("production profile template must use schema version 2 or 3")
    if gpu_image_id is not None and not IMAGE_ID_RE.fullmatch(gpu_image_id):
        raise RuntimeError(
            "production GPU single-user image must be an exact local image ID"
        )
    if gpu_count is None:
        gpu_count = 1 if gpu_image_id is not None else 0
    if type(gpu_count) is not int or not 0 <= gpu_count <= MAX_NVIDIA_GPU_COUNT:
        raise RuntimeError("production GPU count must be between 0 and 64")
    if (gpu_image_id is None) != (gpu_count == 0):
        raise RuntimeError(
            "production GPU image and positive GPU count are required together"
        )

    template_profiles = []
    for raw in template.get("profiles", []):
        if not isinstance(raw, dict):
            raise RuntimeError("production profile template contains an invalid row")
        if (
            raw.get("enabled") is True
            and "python_version" in raw
            and (template_schema == 2 or "accelerator" in raw)
        ):
            template_profiles.append(raw)
    if not template_profiles or not any(
        row.get("selectable") is True for row in template_profiles
    ):
        raise RuntimeError("production profile template has no selectable runtime")

    retained: list[dict[str, Any]] = []
    previous_by_id: dict[str, list[dict[str, Any]]] = {}
    if previous is not None:
        previous_schema = previous.get("schema_version")
        if previous_schema not in ({2} if template_schema == 2 else {2, 3}):
            raise RuntimeError(
                "previous production policy schema is incompatible with template"
            )
        for raw in previous.get("profiles", []):
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                raise RuntimeError("previous production policy contains an invalid row")
            row = dict(raw)
            row["selectable"] = False
            retained.append(row)
            previous_by_id.setdefault(row["id"], []).append(row)
        historical_gpu_counts = [
            row["accelerator"].get("count")
            for row in retained
            if isinstance(row.get("accelerator"), dict)
            and row["accelerator"].get("kind") == "nvidia"
            and row.get("enabled") is True
        ]
        if historical_gpu_counts and gpu_image_id is None:
            raise RuntimeError("enabled production GPU history requires --gpu-image-id")
        if historical_gpu_counts and any(
            type(count) is not int or count < 1 or count > gpu_count
            for count in historical_gpu_counts
        ):
            raise RuntimeError(
                "enabled production GPU history exceeds the configured GPU count"
            )

    expanded_templates: list[dict[str, Any]] = []
    for source in template_profiles:
        accelerator = source.get("accelerator") if template_schema == 3 else None
        if isinstance(accelerator, dict) and accelerator.get("kind") == "nvidia":
            if gpu_image_id is None:
                continue
            if accelerator.get("count") != 1:
                raise RuntimeError(
                    "production NVIDIA template must retain the GPU1 base contract"
                )
            for requested_count in range(1, gpu_count + 1):
                variant = dict(source)
                variant["accelerator"] = {
                    **accelerator,
                    "count": requested_count,
                }
                if requested_count > 1:
                    variant["id"] = f"{source['id']}-gpu{requested_count}"
                    if not PROFILE_ID_RE.fullmatch(variant["id"]):
                        raise RuntimeError("generated multi-GPU profile ID is invalid")
                expanded_templates.append(variant)
        else:
            expanded_templates.append(source)

    expanded_ids = [row.get("id") for row in expanded_templates]
    if any(not isinstance(profile_id, str) for profile_id in expanded_ids) or len(
        set(expanded_ids)
    ) != len(expanded_ids):
        raise RuntimeError("expanded production profile IDs must be unique")

    generated: list[dict[str, Any]] = []
    for source in expanded_templates:
        profile_id = source["id"]
        prior = previous_by_id.get(profile_id, [])
        latest = max(prior, key=lambda row: row["version"]) if prior else None
        candidate = dict(source)
        if template_schema == 3:
            accelerator = candidate.get("accelerator")
            if not isinstance(accelerator, dict):
                raise RuntimeError("schema-v3 runtime is missing accelerator metadata")
            accelerator_kind = accelerator.get("kind")
            if accelerator_kind == "nvidia":
                candidate["image"] = gpu_image_id
            elif accelerator_kind == "none":
                candidate["image"] = image_id
            else:
                raise RuntimeError("schema-v3 accelerator kind is unsupported")
        else:
            candidate["image"] = image_id
        candidate["enabled"] = True
        candidate["selectable"] = bool(source["selectable"])

        # Reuse the latest immutable tuple when the rebuilt image and every
        # execution field are identical.  This makes repeated preflight runs
        # idempotent instead of consuming profile versions.
        if latest is not None:
            probe = dict(candidate)
            probe["version"] = latest["version"]
            probe["config_digest"] = profile_digest(probe)
            if all(
                probe.get(key) == latest.get(key)
                for key in probe
                if key not in {"enabled", "selectable"}
            ):
                latest["enabled"] = True
                latest["selectable"] = bool(source["selectable"])
                latest["config_digest"] = probe["config_digest"]
                continue

        candidate["version"] = max((row["version"] for row in prior), default=0) + 1
        candidate["config_digest"] = profile_digest(candidate)
        generated.append(candidate)

    profiles = retained + generated
    profiles.sort(key=lambda row: (row["id"], row["version"]))
    policy = {
        "schema_version": template_schema,
        "shared_volume": {
            "name": shared_volume_name,
            "mount_path": "/home/jovyan/shared",
            "gid": 100,
        },
        "profiles": profiles,
    }
    if not any(row["selectable"] is True for row in profiles):
        raise RuntimeError("generated production policy has no selectable runtime")
    return policy


def _atomic_write(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> None:
    if mode not in {0o600, 0o640}:
        raise RuntimeError("production profile file mode is invalid")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_metadata = path.parent.lstat()
    parent_mode = stat.S_IMODE(parent_metadata.st_mode)
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or parent_mode not in {0o700, 0o2770}
    ):
        raise RuntimeError(
            "production profile parent must be operator-owned mode 0700 or 2770"
        )
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, mode)
        payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path)
    parser.add_argument("--image-id")
    parser.add_argument("--gpu-image-id")
    parser.add_argument("--gpu-count", type=int)
    parser.add_argument("--shared-volume", default="jupyter-shared")
    parser.add_argument("--previous", type=Path)
    parser.add_argument(
        "--promote",
        type=Path,
        help="atomically validate and promote an already-generated candidate",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.promote is not None:
        if (
            args.template is not None
            or args.image_id is not None
            or args.gpu_image_id is not None
            or args.gpu_count is not None
            or args.previous
        ):
            parser.error("--promote cannot be combined with generation arguments")
        try:
            loaded = load_profile_policy(args.promote, allow_unsafe_images=False)
        except ProfilePolicyError as exc:
            raise RuntimeError(
                f"production profile candidate is invalid: {exc}"
            ) from exc
        policy = _load_object(args.promote)
        # The promoted policy is mounted read-only into non-root API and Hub
        # containers. Its setgid parent supplies the reviewed service group;
        # group-read is required while group/world write remains forbidden.
        _atomic_write(args.output, policy, mode=0o640)
        print(
            "production profile policy promoted: "
            f"profiles={len(loaded['profiles'])} output={args.output}"
        )
        return 0

    if args.template is None or args.image_id is None:
        parser.error("--template and --image-id are required for generation")

    template = _load_object(args.template)
    previous = (
        _load_object(args.previous)
        if args.previous is not None and args.previous.exists()
        else None
    )
    policy = generate_policy(
        template=template,
        image_id=args.image_id,
        gpu_image_id=args.gpu_image_id,
        gpu_count=args.gpu_count,
        shared_volume_name=args.shared_volume,
        previous=previous,
    )
    # Candidates are operator-only until the maintenance transition stops
    # writers, snapshots the databases, and explicitly promotes the file.
    _atomic_write(args.output, policy, mode=0o600)
    try:
        loaded = load_profile_policy(args.output, allow_unsafe_images=False)
    except ProfilePolicyError as exc:
        raise RuntimeError(
            f"generated production profile policy is invalid: {exc}"
        ) from exc
    print(
        "production profile policy ready: "
        f"profiles={len(loaded['profiles'])} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
