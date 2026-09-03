from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from app.admin import (
    PROFILE_EXECUTION_FIELDS_V1,
    PROFILE_EXECUTION_FIELDS_V3,
    _profile_digest,
    import_profiles,
)
from app.db import create_database_engine, create_session_factory
from app.errors import AppError
from app.models import WorkspaceProfile, WorkspaceProfileOffer
from app.services.resource_policy import get_resource_policy, update_resource_policy
from app.services.resource_profiles import DYNAMIC_BASE_KEY


def _legacy_profile(*, selectable: bool | None = None) -> dict[str, object]:
    profile: dict[str, object] = {
        "id": "python-local",
        "version": 1,
        "enabled": True,
        "image": "registry.example/singleuser@sha256:" + "a" * 64,
        "cpu_limit": 1.0,
        "memory_limit_bytes": 1024 * 1024 * 1024,
        "pids_limit": 256,
        "private_disk_hard_limit_bytes": 1024 * 1024 * 1024,
        "writable_layer_size_bytes": 512 * 1024 * 1024,
        "tmpfs_size_bytes": 128 * 1024 * 1024,
        "shm_size_bytes": 128 * 1024 * 1024,
        "log_max_size_bytes": 5 * 1024 * 1024,
        "log_max_files": 2,
        "private_mount_path": "/home/jovyan/work",
        "uid": 1000,
        "gid": 100,
    }
    profile["config_digest"] = _profile_digest(profile, PROFILE_EXECUTION_FIELDS_V1)
    if selectable is not None:
        profile["selectable"] = selectable
    return profile


def _extended_profile(
    *,
    profile_id: str = "python-312-balanced",
    version: int = 1,
    cpu_limit: float = 2.0,
    quota_enforced: bool = True,
) -> dict[str, object]:
    profile: dict[str, object] = {
        "id": profile_id,
        "version": version,
        "enabled": True,
        "selectable": True,
        "python_version": "3.12.11",
        "kernels": [
            {
                "name": "python3",
                "display_name": "Python 3.12",
                "language": "python",
                "python_version": "3.12.11",
                "executable": "/opt/conda/bin/python",
            }
        ],
        "default_kernel": "python3",
        "private_disk_quota_enforced": quota_enforced,
        "image": "registry.example/singleuser@sha256:" + "b" * 64,
        "cpu_limit": cpu_limit,
        "memory_limit_bytes": 2 * 1024 * 1024 * 1024,
        "pids_limit": 256,
        "private_disk_hard_limit_bytes": 1024 * 1024 * 1024,
        "writable_layer_size_bytes": 512 * 1024 * 1024,
        "tmpfs_size_bytes": 128 * 1024 * 1024,
        "shm_size_bytes": 128 * 1024 * 1024,
        "log_max_size_bytes": 5 * 1024 * 1024,
        "log_max_files": 2,
        "private_mount_path": "/home/jovyan/work",
        "uid": 1000,
        "gid": 100,
    }
    profile["config_digest"] = _profile_digest(profile)
    return profile


def _schema_v3_profile(
    *,
    profile_id: str,
    cuda_version: str | None = None,
    framework_version: str | None = None,
    selectable: bool = True,
) -> dict[str, object]:
    profile = _extended_profile(profile_id=profile_id)
    profile["selectable"] = selectable
    profile["accelerator"] = (
        {
            "kind": "none",
            "count": 0,
            "sharing": "none",
            "cuda_version": None,
            "framework": None,
            "framework_version": None,
        }
        if cuda_version is None
        else {
            "kind": "nvidia",
            "count": 1,
            "sharing": "exclusive",
            "cuda_version": cuda_version,
            "framework": "pytorch",
            "framework_version": framework_version,
        }
    )
    profile["config_digest"] = _profile_digest(profile, PROFILE_EXECUTION_FIELDS_V3)
    return profile


def _write_policy(
    path: Path,
    *,
    schema_version: int,
    profiles: list[dict],
    shared_mount_path: str = "/home/jovyan/shared",
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "shared_volume": {
                    "name": "jupyter-shared",
                    "mount_path": shared_mount_path,
                    "gid": 100,
                },
                "profiles": profiles,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    return Config(str(backend_root / "alembic.ini"))


def _set_migration_environment(monkeypatch, database_url: str) -> None:
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url)
    monkeypatch.setenv("PLATFORM_ALLOW_INSECURE_DEV_SECRETS", "1")
    monkeypatch.setenv("PLATFORM_INSECURE_LOCAL_DEV", "true")


@pytest.mark.parametrize(
    "field",
    ["workspace_cpu_budget_millicores", "workspace_memory_budget_mb"],
)
def test_workspace_resource_budgets_must_be_positive(settings, field: str):
    with pytest.raises(RuntimeError, match="must be greater than 0"):
        replace(settings, **{field: 0}).validate()


def test_fresh_migration_and_v2_profile_import(settings, tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")

    local_settings = replace(settings, database_url=database_url)
    policy_path = tmp_path / "profiles-v2.json"
    _write_policy(
        policy_path,
        schema_version=2,
        profiles=[_extended_profile()],
    )
    import_profiles(local_settings, str(policy_path))

    engine = create_engine(database_url)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT kernel_name, kernel_display_name, python_version, "
                "enabled, selectable, private_disk_quota_enforced "
                "FROM workspace_profiles"
            )
        ).one()
        kernel_idle_timeout = connection.execute(
            text(
                "SELECT kernel_idle_timeout_seconds FROM resource_policies WHERE id = 1"
            )
        ).scalar_one()
    engine.dispose()
    assert tuple(row) == ("python3", "Python 3.12", "3.12.11", 1, 1, 1)
    assert kernel_idle_timeout == 3_600


def test_admin_resource_values_materialize_verified_runtime_cross_product(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'dynamic-resources.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    local_settings = replace(
        settings,
        database_url=database_url,
        workspace_cpu_budget_millicores=8_000,
        workspace_memory_budget_mb=8_192,
    )
    policy_path = tmp_path / "profiles-dynamic.json"
    _write_policy(
        policy_path,
        schema_version=2,
        profiles=[_extended_profile(cpu_limit=2.0)],
    )
    import_profiles(local_settings, str(policy_path))

    engine = create_database_engine(local_settings)
    factory = create_session_factory(engine)
    with factory() as db:
        policy = get_resource_policy(db, local_settings)
        update_resource_policy(
            db,
            settings=local_settings,
            actor_user_id=None,  # type: ignore[arg-type] - service-level import test
            expected_version=policy.version,
            cpu_budget_millicores=8_000,
            memory_budget_mb=8_192,
            selectable_cpu_millicores=[2_000, 3_500],
            selectable_memory_mb=[2_048, 3_072],
            gpu_budget_count=0,
            selectable_gpu_counts=[0],
            kernel_idle_timeout_seconds=3_600,
            reserved_cpu_millicores=0,
            reserved_memory_mb=0,
            reserved_gpu_count=0,
        )
        db.commit()
        profiles = db.scalars(
            select(WorkspaceProfile).where(WorkspaceProfile.selectable.is_(True))
        ).all()
        offers = db.scalars(select(WorkspaceProfileOffer)).all()
        resources = {
            (profile.cpu_limit, profile.memory_limit_mb) for profile in profiles
        }
        assert resources == {
            ("2.0", 2_048),
            ("2", 3_072),
            ("3.5", 2_048),
            ("3.5", 3_072),
        }
        assert len(offers) == 4
        derived = next(profile for profile in profiles if profile.cpu_limit == "3.5")
        provider = json.loads(derived.provider_options_json)
        assert provider[DYNAMIC_BASE_KEY] == {
            "id": "python-312-balanced",
            "version": 1,
            "config_digest": _extended_profile()["config_digest"],
        }
    engine.dispose()


def test_resource_matrix_keeps_distinct_cuda_runtime_families(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'gpu-runtime-matrix.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    gpu_id = "GPU-01234567-89ab-cdef-0123-456789abcdef"
    local_settings = replace(
        settings,
        database_url=database_url,
        workspace_cpu_budget_millicores=8_000,
        workspace_memory_budget_mb=8_192,
        nvidia_gpu_device_ids=(gpu_id,),
    )
    policy_path = tmp_path / "profiles-gpu-families.json"
    first = _schema_v3_profile(
        profile_id="python312-cuda126",
        cuda_version="12.6",
        framework_version="2.7.1",
    )
    second = _schema_v3_profile(
        profile_id="python312-cuda127",
        cuda_version="12.7",
        framework_version="2.8.0",
    )
    _write_policy(policy_path, schema_version=3, profiles=[first, second])
    import_profiles(local_settings, str(policy_path))

    engine = create_database_engine(local_settings)
    factory = create_session_factory(engine)
    with factory() as db:
        policy = get_resource_policy(db, local_settings)
        update_resource_policy(
            db,
            settings=local_settings,
            actor_user_id=None,  # type: ignore[arg-type] - service-level import test
            expected_version=policy.version,
            cpu_budget_millicores=8_000,
            memory_budget_mb=8_192,
            selectable_cpu_millicores=[2_000, 3_500],
            selectable_memory_mb=[2_048, 3_072],
            gpu_budget_count=1,
            selectable_gpu_counts=[1],
            kernel_idle_timeout_seconds=3_600,
            reserved_cpu_millicores=0,
            reserved_memory_mb=0,
            reserved_gpu_count=0,
        )
        db.commit()
        derived = db.scalars(
            select(WorkspaceProfile).where(
                WorkspaceProfile.selectable.is_(True),
                WorkspaceProfile.cpu_limit == "3.5",
                WorkspaceProfile.memory_limit_mb == 3_072,
            )
        ).all()
        assert {
            (
                profile.accelerator_kind,
                profile.cuda_version,
                profile.gpu_framework,
                profile.gpu_framework_version,
            )
            for profile in derived
        } == {
            ("nvidia", "12.6", "pytorch", "2.7.1"),
            ("nvidia", "12.7", "pytorch", "2.8.0"),
        }
        assert {
            json.loads(profile.provider_options_json)[DYNAMIC_BASE_KEY]["id"]
            for profile in derived
        } == {"python312-cuda126", "python312-cuda127"}
    engine.dispose()


def test_resource_policy_rejects_phantom_selected_gpu_value(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'phantom-gpu-selection.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    local_settings = replace(
        settings,
        database_url=database_url,
        nvidia_gpu_device_ids=("GPU-01234567-89ab-cdef-0123-456789abcdef",),
    )
    policy_path = tmp_path / "profiles-cpu-only-v3.json"
    _write_policy(
        policy_path,
        schema_version=3,
        profiles=[_schema_v3_profile(profile_id="python312-cpu")],
    )
    import_profiles(local_settings, str(policy_path))

    engine = create_database_engine(local_settings)
    factory = create_session_factory(engine)
    with factory() as db:
        policy = get_resource_policy(db, local_settings)
        original_version = policy.version
        with pytest.raises(AppError) as caught:
            update_resource_policy(
                db,
                settings=local_settings,
                actor_user_id=None,  # type: ignore[arg-type] - service-level test
                expected_version=policy.version,
                cpu_budget_millicores=policy.cpu_budget_millicores,
                memory_budget_mb=policy.memory_budget_mb,
                selectable_cpu_millicores=[2_000],
                selectable_memory_mb=[2_048],
                gpu_budget_count=1,
                selectable_gpu_counts=[0, 1],
                kernel_idle_timeout_seconds=3_600,
                reserved_cpu_millicores=0,
                reserved_memory_mb=0,
                reserved_gpu_count=0,
            )
        assert caught.value.status_code == 422
        assert caught.value.code == "RESOURCE_SELECTION_EMPTY"
        db.rollback()
        assert get_resource_policy(db, local_settings).version == original_version
    engine.dispose()


def test_schema_v3_rejects_selectable_v2_row_but_keeps_history_compatible(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'schema-v3-v2-row.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    local_settings = replace(settings, database_url=database_url)
    policy_path = tmp_path / "profiles-schema-v3.json"
    historical = _extended_profile(profile_id="python312-history")

    _write_policy(policy_path, schema_version=3, profiles=[historical])
    with pytest.raises(
        ValueError,
        match="selectable schema-v3 profile requires accelerator metadata",
    ):
        import_profiles(local_settings, str(policy_path))

    historical["selectable"] = False
    current = _schema_v3_profile(profile_id="python312-current")
    _write_policy(
        policy_path,
        schema_version=3,
        profiles=[historical, current],
    )
    import_profiles(local_settings, str(policy_path))
    engine = create_engine(database_url)
    with engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT enabled, selectable, accelerator_kind, gpu_count "
                "FROM workspace_profiles WHERE id = 'python312-history'"
            )
        ).one()
    engine.dispose()
    assert tuple(stored) == (1, 0, "none", 0)


def test_upgrade_preserves_legacy_row_and_v2_bootstrap_is_idempotent(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    _set_migration_environment(monkeypatch, database_url)
    config = _alembic_config()
    command.upgrade(config, "0002")

    legacy_v1 = _legacy_profile()
    legacy_provider_json = json.dumps(legacy_v1, sort_keys=True, separators=(",", ":"))
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspace_profiles "
                "(id, version, name, image_ref, cpu_limit, memory_limit_mb, "
                "pids_limit, private_disk_limit_mb, idle_timeout_seconds, "
                "provider_options_json, config_digest, enabled) VALUES "
                "(:id, 1, :name, :image, '1.0', 1024, 256, 1024, NULL, "
                ":provider, :digest, 1)"
            ),
            {
                "id": legacy_v1["id"],
                "name": legacy_v1["id"],
                "image": legacy_v1["image"],
                "provider": legacy_provider_json,
                "digest": legacy_v1["config_digest"],
            },
        )
    engine.dispose()

    command.upgrade(config, "head")
    local_settings = replace(
        settings, database_url=database_url, insecure_local_dev=True
    )
    policy_path = tmp_path / "profiles-v2-mixed.json"
    _write_policy(
        policy_path,
        schema_version=2,
        profiles=[_legacy_profile(selectable=False), _extended_profile()],
    )
    import_profiles(local_settings, str(policy_path))
    import_profiles(local_settings, str(policy_path))

    engine = create_engine(database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, kernel_name, kernel_display_name, python_version, "
                "enabled, selectable, private_disk_quota_enforced, "
                "provider_options_json "
                "FROM workspace_profiles ORDER BY id"
            )
        ).all()
    engine.dispose()
    assert len(rows) == 2
    legacy = next(row for row in rows if row.id == "python-local")
    assert (
        legacy.kernel_name,
        legacy.kernel_display_name,
        legacy.python_version,
        legacy.enabled,
        legacy.selectable,
        legacy.private_disk_quota_enforced,
    ) == ("python3", "Python 3", "legacy", 1, 0, 0)
    # The v2-only selectable metadata is not written into the immutable legacy raw
    # provider document; its original execution document remains byte-for-byte stable.
    assert legacy.provider_options_json == legacy_provider_json


def test_import_rejects_omitted_enabled_profile_without_mutating_database(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'enabled-orphan.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    local_settings = replace(
        settings, database_url=database_url, insecure_local_dev=True
    )
    policy_path = tmp_path / "profiles-v2.json"
    _write_policy(
        policy_path,
        schema_version=2,
        profiles=[_legacy_profile(selectable=False), _extended_profile()],
    )
    import_profiles(local_settings, str(policy_path))

    engine = create_engine(database_url)
    with engine.connect() as connection:
        before = connection.execute(
            text(
                "SELECT id, version, enabled, selectable, config_digest, "
                "provider_options_json FROM workspace_profiles ORDER BY id, version"
            )
        ).all()

    _write_policy(
        policy_path,
        schema_version=2,
        profiles=[_extended_profile()],
    )
    with pytest.raises(
        ValueError,
        match=r"omits enabled profile\(s\): python-local@1",
    ):
        import_profiles(local_settings, str(policy_path))

    with engine.connect() as connection:
        after = connection.execute(
            text(
                "SELECT id, version, enabled, selectable, config_digest, "
                "provider_options_json FROM workspace_profiles ORDER BY id, version"
            )
        ).all()
    engine.dispose()
    assert after == before


def test_import_allows_omitted_disabled_profile_and_keeps_it_unselectable(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'disabled-orphan.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    local_settings = replace(
        settings, database_url=database_url, insecure_local_dev=True
    )
    policy_path = tmp_path / "profiles-v2.json"
    retired = _legacy_profile(selectable=False)
    retired["enabled"] = False
    _write_policy(
        policy_path,
        schema_version=2,
        profiles=[retired, _extended_profile()],
    )
    import_profiles(local_settings, str(policy_path))

    _write_policy(
        policy_path,
        schema_version=2,
        profiles=[_extended_profile()],
    )
    import_profiles(local_settings, str(policy_path))

    engine = create_engine(database_url)
    with engine.connect() as connection:
        orphan = connection.execute(
            text(
                "SELECT enabled, selectable FROM workspace_profiles "
                "WHERE id = 'python-local' AND version = 1"
            )
        ).one()
    engine.dispose()
    assert tuple(orphan) == (0, 0)


def test_import_rejects_unsafe_v2_shape_and_in_place_execution_mutation(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'invalid.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    local_settings = replace(settings, database_url=database_url)
    policy_path = tmp_path / "profiles.json"

    legacy_selectable = _legacy_profile(selectable=True)
    _write_policy(policy_path, schema_version=2, profiles=[legacy_selectable])
    with pytest.raises(
        ValueError, match="selectable v2 profile requires exact runtime metadata"
    ):
        import_profiles(local_settings, str(policy_path))

    valid = _extended_profile()
    _write_policy(policy_path, schema_version=2, profiles=[valid])
    import_profiles(local_settings, str(policy_path))

    mutated = _extended_profile(cpu_limit=3.0)
    _write_policy(policy_path, schema_version=2, profiles=[mutated])
    with pytest.raises(ValueError, match="refusing in-place profile mutation"):
        import_profiles(local_settings, str(policy_path))

    engine = create_engine(database_url)
    with engine.connect() as connection:
        # The failed import's initial "hide all" update is part of the same
        # BEGIN IMMEDIATE transaction and must have rolled back.
        row = connection.execute(
            text(
                "SELECT cpu_limit, selectable FROM workspace_profiles "
                "WHERE id = 'python-312-balanced' AND version = 1"
            )
        ).one()
    engine.dispose()
    assert tuple(row) == ("2.0", 1)


@pytest.mark.parametrize("cpu_limit", [float("nan"), float("inf"), float("-inf")])
def test_import_rejects_non_finite_cpu_limits(
    settings, tmp_path, monkeypatch, cpu_limit: float
):
    database_url = f"sqlite:///{tmp_path / 'non-finite.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    policy_path = tmp_path / "non-finite.json"
    _write_policy(
        policy_path,
        schema_version=2,
        profiles=[_extended_profile(cpu_limit=cpu_limit)],
    )
    with pytest.raises(ValueError, match="cpu_limit must be finite and positive"):
        import_profiles(replace(settings, database_url=database_url), str(policy_path))


def test_false_disk_quota_claim_is_an_explicit_production_policy(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'quota-claim.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    policy_path = tmp_path / "quota-claim.json"
    profile = _extended_profile(quota_enforced=False)
    _write_policy(policy_path, schema_version=2, profiles=[profile])

    production = replace(settings, database_url=database_url)
    import_profiles(production, str(policy_path))
    engine = create_engine(database_url)
    with engine.connect() as connection:
        enforced = connection.execute(
            text(
                "SELECT private_disk_quota_enforced FROM workspace_profiles "
                "WHERE id = 'python-312-balanced'"
            )
        ).scalar_one()
    engine.dispose()
    assert enforced == 0


def test_import_rejects_sub_millicore_precision_and_unstartable_profiles(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'resource-budget.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    policy_path = tmp_path / "resource-budget.json"

    too_precise = _extended_profile(cpu_limit=0.0005)
    _write_policy(policy_path, schema_version=2, profiles=[too_precise])
    with pytest.raises(ValueError, match="smaller than one millicore"):
        import_profiles(replace(settings, database_url=database_url), str(policy_path))

    profile = _extended_profile()
    _write_policy(policy_path, schema_version=2, profiles=[profile])
    for constrained in (
        replace(
            settings,
            database_url=database_url,
            workspace_cpu_budget_millicores=1_999,
        ),
        replace(
            settings,
            database_url=database_url,
            workspace_memory_budget_mb=2_047,
        ),
    ):
        with pytest.raises(ValueError, match="exceeds aggregate workspace"):
            import_profiles(constrained, str(policy_path))


def test_import_rejects_invalid_container_limit_and_mount_fields(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'invalid-execution.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    policy_path = tmp_path / "invalid-execution.json"
    production = replace(settings, database_url=database_url)

    for field in (
        "memory_limit_bytes",
        "pids_limit",
        "private_disk_hard_limit_bytes",
        "writable_layer_size_bytes",
        "tmpfs_size_bytes",
        "shm_size_bytes",
        "log_max_size_bytes",
        "log_max_files",
        "uid",
        "gid",
    ):
        profile = _extended_profile()
        profile[field] = 0
        profile["config_digest"] = _profile_digest(profile)
        _write_policy(policy_path, schema_version=2, profiles=[profile])
        with pytest.raises(ValueError, match="must be a positive integer"):
            import_profiles(production, str(policy_path))

    for path in ("relative/work", "/", "/home", "/home/jovyan", "/work/../etc"):
        profile = _extended_profile()
        profile["private_mount_path"] = path
        profile["config_digest"] = _profile_digest(profile)
        _write_policy(policy_path, schema_version=2, profiles=[profile])
        with pytest.raises(ValueError, match="absolute POSIX|too broad|traversal"):
            import_profiles(production, str(policy_path))

    profile = _extended_profile()
    profile["private_mount_path"] = "/home/jovyan/shared/private"
    profile["config_digest"] = _profile_digest(profile)
    _write_policy(policy_path, schema_version=2, profiles=[profile])
    with pytest.raises(ValueError, match="private and shared mount paths overlap"):
        import_profiles(production, str(policy_path))

    profile = _extended_profile()
    profile["private_mount_path"] = "/home/jovyan/project"
    profile["config_digest"] = _profile_digest(profile)
    _write_policy(policy_path, schema_version=2, profiles=[profile])
    with pytest.raises(
        ValueError, match="private_mount_path must be /home/jovyan/work"
    ):
        import_profiles(production, str(policy_path))

    profile = _extended_profile()
    _write_policy(
        policy_path,
        schema_version=2,
        profiles=[profile],
        shared_mount_path="/home/jovyan/team",
    )
    with pytest.raises(ValueError, match="mount_path must be /home/jovyan/shared"):
        import_profiles(production, str(policy_path))


def test_import_uses_exact_production_image_and_kernel_version_contract(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'invalid-image-kernel.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    policy_path = tmp_path / "invalid-image-kernel.json"
    production = replace(settings, database_url=database_url)

    for image in (
        "registry.example/singleuser:latest",
        "registry.example/singleuser@sha256:" + "A" * 64,
        "registry.example/singleuser@sha256:" + "a" * 63,
        "registry.example/singleuser@sha256:" + "a" * 64 + ":tag",
    ):
        profile = _extended_profile()
        profile["image"] = image
        profile["config_digest"] = _profile_digest(profile)
        _write_policy(policy_path, schema_version=2, profiles=[profile])
        with pytest.raises(ValueError, match="registry digest or exact local image ID"):
            import_profiles(production, str(policy_path))

    profile = _extended_profile()
    profile["image"] = "sha256:" + "b" * 64
    profile["config_digest"] = _profile_digest(profile)
    _write_policy(policy_path, schema_version=2, profiles=[profile])
    import_profiles(production, str(policy_path))

    profile = _extended_profile()
    profile["kernels"][0]["executable"] = "/usr/bin/python3"  # type: ignore[index]
    profile["config_digest"] = _profile_digest(profile)
    _write_policy(policy_path, schema_version=2, profiles=[profile])
    with pytest.raises(ValueError, match="kernel executable is not allowlisted"):
        import_profiles(production, str(policy_path))

    profile = _extended_profile()
    profile["kernels"][0]["python_version"] = "3.11.9"  # type: ignore[index]
    profile["config_digest"] = _profile_digest(profile)
    _write_policy(policy_path, schema_version=2, profiles=[profile])
    with pytest.raises(ValueError, match="must match the default kernel version"):
        import_profiles(production, str(policy_path))


def test_repository_local_policy_matches_backend_contract(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'repository-policy.db'}"
    _set_migration_environment(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")
    repository_root = Path(__file__).resolve().parents[2]
    policy_path = repository_root / "infra/jupyterhub/profiles.local-dev.json"
    local = replace(settings, database_url=database_url, insecure_local_dev=True)
    import_profiles(local, str(policy_path))

    engine = create_engine(database_url)
    with engine.connect() as connection:
        total = connection.execute(
            text("SELECT COUNT(*) FROM workspace_profiles")
        ).scalar_one()
        selectable = connection.execute(
            text(
                "SELECT COUNT(*) FROM workspace_profiles "
                "WHERE enabled = 1 AND selectable = 1"
            )
        ).scalar_one()
        false_quota = connection.execute(
            text(
                "SELECT COUNT(*) FROM workspace_profiles "
                "WHERE selectable = 1 AND private_disk_quota_enforced = 0"
            )
        ).scalar_one()
        legacy = connection.execute(
            text(
                "SELECT enabled, selectable, kernel_name, python_version, "
                "private_disk_quota_enforced FROM workspace_profiles "
                "WHERE id = 'python-local' AND version = 1"
            )
        ).one()
        cpu_values = (
            connection.execute(
                text(
                    "SELECT DISTINCT CAST(cpu_limit * 1000 AS INTEGER) "
                    "FROM workspace_profiles WHERE enabled = 1 AND selectable = 1 "
                    "ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
        memory_values = (
            connection.execute(
                text(
                    "SELECT DISTINCT memory_limit_mb FROM workspace_profiles "
                    "WHERE enabled = 1 AND selectable = 1 ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
    engine.dispose()
    assert (total, selectable, false_quota) == (19, 18, 18)
    assert tuple(legacy) == (1, 0, "python3", "legacy", 0)
    assert cpu_values == [1000, 2000, 4000]
    assert memory_values == [1024, 2048, 4096]


def test_0005_upgrade_preserves_policy_and_enforces_kernel_idle_timeout(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'kernel-idle-0004.db'}"
    _set_migration_environment(monkeypatch, database_url)
    config = _alembic_config()
    command.upgrade(config, "0004")

    timestamp = "2026-08-11 00:00:00"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO resource_policies "
                "(id, version, cpu_budget_millicores, memory_budget_mb, "
                "selectable_cpu_millicores_json, selectable_memory_mb_json, "
                "updated_by_user_id, created_at, updated_at) VALUES "
                "(1, 7, 8000, 16384, '[1000,2000]', '[1024,2048]', NULL, "
                ":now, :now)"
            ),
            {"now": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "0005")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT version, cpu_budget_millicores, memory_budget_mb, "
                "selectable_cpu_millicores_json, selectable_memory_mb_json, "
                "kernel_idle_timeout_seconds FROM resource_policies WHERE id = 1"
            )
        ).one()
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    assert tuple(row) == (7, 8_000, 16_384, "[1000,2000]", "[1024,2048]", 3_600)
    assert revision == "0005"

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE resource_policies "
                    "SET kernel_idle_timeout_seconds = 299 WHERE id = 1"
                )
            )
    engine.dispose()

    command.downgrade(config, "0004")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(resource_policies)"))
        }
        preserved = connection.execute(
            text(
                "SELECT version, cpu_budget_millicores, memory_budget_mb, "
                "selectable_cpu_millicores_json, selectable_memory_mb_json "
                "FROM resource_policies WHERE id = 1"
            )
        ).one()
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()
    assert "kernel_idle_timeout_seconds" not in columns
    assert tuple(preserved) == (7, 8_000, 16_384, "[1000,2000]", "[1024,2048]")


def test_0003_upgrade_preserves_spawn_history_and_legacy_workspace_binding(
    settings, tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'populated-0003.db'}"
    _set_migration_environment(monkeypatch, database_url)
    config = _alembic_config()
    command.upgrade(config, "0003")
    ids = {
        "user": "11111111-1111-1111-1111-111111111111",
        "slot": "22222222-2222-2222-2222-222222222222",
        "workspace": "33333333-3333-3333-3333-333333333333",
        "operation": "44444444-4444-4444-4444-444444444444",
        "consumed": "55555555-5555-5555-5555-555555555555",
        "unconsumed": "66666666-6666-6666-6666-666666666666",
    }
    timestamp = "2026-08-11 00:00:00"
    digest = "sha256:" + "a" * 64
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, auth_provider, auth_subject, hub_username, "
                "display_name, role, status, created_at, updated_at) VALUES "
                "(:id, 'jupyterhub', 'legacy-user', 'legacy-user', NULL, 'USER', "
                "'ACTIVE', :now, :now)"
            ),
            {"id": ids["user"], "now": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO workspace_profiles (id, version, name, image_ref, "
                "cpu_limit, memory_limit_mb, pids_limit, private_disk_limit_mb, "
                "idle_timeout_seconds, provider_options_json, config_digest, enabled, "
                "kernel_name, private_disk_quota_enforced, selectable, python_version, "
                "kernel_display_name) VALUES ('retired-runtime', 7, 'Retired', "
                ":image, '1.0', 1024, 256, 1024, NULL, :provider, :digest, 1, "
                "'python3', 0, 0, '3.12.0', 'Python 3')"
            ),
            {
                "image": "example.invalid/singleuser@sha256:" + "b" * 64,
                "provider": '{"gid":100,"uid":1000}',
                "digest": digest,
            },
        )
        connection.execute(
            text(
                "INSERT INTO workspace_volume_slots (id, owner_user_id, slot_no, "
                "volume_name, quota_project_id, hard_limit_mb, provision_status, "
                "verified_at) VALUES (:id, :user, 1, 'legacy-volume', 10001, 1024, "
                "'PROVISIONED', :now)"
            ),
            {"id": ids["slot"], "user": ids["user"], "now": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO workspaces (id, owner_user_id, profile_id, "
                "profile_version, hub_target_key, hub_server_name, "
                "private_volume_slot_id, desired_state, observed_state, "
                "hub_server_url, progress_percent, stale, last_error_code, "
                "last_error_summary, hub_started_at, hub_last_activity_at, "
                "last_reconciled_at, deletion_started_at, deletion_checkpoint, "
                "spec_version, row_version, created_at, updated_at, archived_at) "
                "VALUES (:id, :user, 'retired-runtime', 7, 'legacy-target', "
                "'ws-legacy', :slot, 'RUNNING', 'RUNNING', NULL, 100, 0, NULL, "
                "NULL, :now, NULL, :now, NULL, NULL, 3, 2, :now, :now, NULL)"
            ),
            {
                "id": ids["workspace"],
                "user": ids["user"],
                "slot": ids["slot"],
                "now": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO operations (id, workspace_id, requested_by_user_id, "
                "auth_session_id_hash, operation_type, status, idempotency_key, "
                "attempts, transient_failures, error_code, error_summary, "
                "requested_at, started_at, completed_at, next_attempt_at, "
                "lease_owner, lease_expires_at) VALUES (:id, :workspace, :user, "
                "NULL, 'CREATE', 'SUCCEEDED', 'legacy-create', 2, 0, NULL, NULL, "
                ":now, :now, :now, NULL, NULL, NULL)"
            ),
            {
                "id": ids["operation"],
                "workspace": ids["workspace"],
                "user": ids["user"],
                "now": timestamp,
            },
        )
        for attempt, authorization_id in enumerate(
            (ids["consumed"], ids["unconsumed"]), start=1
        ):
            connection.execute(
                text(
                    "INSERT INTO spawn_authorizations (id, ticket_hash, operation_id, "
                    "attempt_no, workspace_id, owner_user_id, workspace_spec_version, "
                    "private_volume_slot_id, hub_username, hub_server_name, profile_id, "
                    "profile_version, profile_config_digest, expires_at, consumed_at, "
                    "revoked_at) VALUES (:id, :ticket, :operation, :attempt, "
                    ":workspace, :user, 3, :slot, 'legacy-user', 'ws-legacy', "
                    "'retired-runtime', 7, :digest, '2026-08-12 00:00:00', "
                    ":consumed, NULL)"
                ),
                {
                    "id": authorization_id,
                    "ticket": ("c" if attempt == 1 else "d") * 64,
                    "operation": ids["operation"],
                    "attempt": attempt,
                    "workspace": ids["workspace"],
                    "user": ids["user"],
                    "slot": ids["slot"],
                    "digest": digest,
                    "consumed": timestamp if attempt == 1 else None,
                },
            )
    engine.dispose()

    # Stop at the real previous release schema and populate the authorization
    # fields introduced there.  0005 batch-recreates this table, so defaults
    # inherited from a 0003 fixture would not prove those values survive.
    command.upgrade(config, "0004")
    consumed_environment_digest = "hmac-sha256:" + "e" * 64
    unconsumed_environment_digest = "hmac-sha256:" + "f" * 64
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET environment_generation = 3 WHERE id = :id"),
            {"id": ids["user"]},
        )
        connection.execute(
            text("UPDATE workspaces SET environment_generation = 7 " "WHERE id = :id"),
            {"id": ids["workspace"]},
        )
        connection.execute(
            text(
                "UPDATE spawn_authorizations SET "
                "user_environment_generation = 3, "
                "workspace_environment_generation = 7, "
                "environment_digest = :digest, "
                "environment_snapshot_cipher = NULL WHERE id = :id"
            ),
            {"digest": consumed_environment_digest, "id": ids["consumed"]},
        )
        connection.execute(
            text(
                "UPDATE spawn_authorizations SET "
                "user_environment_generation = 3, "
                "workspace_environment_generation = 7, "
                "environment_digest = :digest, "
                "environment_snapshot_cipher = 'encrypted-snapshot-preserved' "
                "WHERE id = :id"
            ),
            {"digest": unconsumed_environment_digest, "id": ids["unconsumed"]},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        authorization_rows = connection.execute(
            text(
                "SELECT id, consumed_at, revoked_at, user_environment_generation, "
                "workspace_environment_generation, environment_digest, "
                "environment_snapshot_cipher, kernel_idle_timeout_seconds "
                "FROM spawn_authorizations ORDER BY id"
            )
        ).all()
        workspace_row = connection.execute(
            text(
                "SELECT display_name, environment_generation, "
                "applied_user_environment_generation, "
                "applied_workspace_environment_generation, profile_offer_id, "
                "profile_offer_version, profile_offer_name_snapshot FROM workspaces "
                "WHERE id = :id"
            ),
            {"id": ids["workspace"]},
        ).one()
        actor_id = connection.execute(
            text("SELECT actor_user_id FROM operations WHERE id = :id"),
            {"id": ids["operation"]},
        ).scalar_one()
        foreign_key_errors = connection.execute(text("PRAGMA foreign_key_check")).all()
    engine.dispose()
    assert len(authorization_rows) == 2
    consumed = next(row for row in authorization_rows if row.id == ids["consumed"])
    unconsumed = next(row for row in authorization_rows if row.id == ids["unconsumed"])
    assert consumed.consumed_at is not None and consumed.revoked_at is None
    assert unconsumed.consumed_at is None and unconsumed.revoked_at is not None
    assert consumed.user_environment_generation == 3
    assert consumed.workspace_environment_generation == 7
    assert consumed.environment_digest == consumed_environment_digest
    assert consumed.environment_snapshot_cipher is None
    assert consumed.kernel_idle_timeout_seconds == 3_600
    assert unconsumed.user_environment_generation == 3
    assert unconsumed.workspace_environment_generation == 7
    assert unconsumed.environment_digest == unconsumed_environment_digest
    assert unconsumed.environment_snapshot_cipher == "encrypted-snapshot-preserved"
    assert unconsumed.kernel_idle_timeout_seconds == 3_600
    assert tuple(workspace_row) == ("환경-1", 7, 1, 1, None, None, None)
    assert actor_id == ids["user"]
    assert foreign_key_errors == []

    engine = create_engine(database_url)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE spawn_authorizations "
                    "SET kernel_idle_timeout_seconds = 299"
                )
            )
    engine.dispose()

    command.downgrade(config, "0003")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM spawn_authorizations")
            ).scalar_one()
            == 2
        )
        spawn_columns = {
            row[1]
            for row in connection.execute(
                text("PRAGMA table_info(spawn_authorizations)")
            )
        }
        assert "kernel_idle_timeout_seconds" not in spawn_columns
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()
