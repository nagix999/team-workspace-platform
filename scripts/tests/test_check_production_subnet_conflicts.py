from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_production_subnet_conflicts",
    ROOT / "scripts" / "check_production_subnet_conflicts.py",
)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def managed(name: str, subnet: str, *, project: str = "workspace-prod") -> dict:
    return {
        "Id": hashlib.sha256(f"{project}:{name}".encode()).hexdigest(),
        "Name": (
            "platform-jupyter-compose-production"
            if name == "jupyter"
            else f"{project}_{name}"
        ),
        "Driver": "bridge",
        "Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.network": name,
        },
        "IPAM": {"Config": [{"Subnet": subnet}]},
        "Options": {},
    }


def test_exact_owned_networks_and_their_bridge_routes_are_allowed() -> None:
    networks = [
        managed(name, str(subnet)) for name, subnet in module.TARGET_NETWORKS.items()
    ]
    devices = module.validate_network_inventory(networks, "workspace-prod")
    routes = [
        {
            "dst": str(subnet),
            "dev": f"br-{hashlib.sha256(f'workspace-prod:{name}'.encode()).hexdigest()[:12]}",
        }
        for name, subnet in module.TARGET_NETWORKS.items()
    ]

    module.validate_host_routes(routes, devices)


@pytest.mark.parametrize(
    "network",
    [
        managed("jupyter", "172.29.0.0/16"),
        managed("jupyter", "172.29.0.0/24", project="foreign"),
        managed("edge", "172.29.4.0/24"),
    ],
)
def test_foreign_broad_or_wrongly_bound_docker_network_fails_closed(
    network: dict,
) -> None:
    with pytest.raises(module.ConflictError):
        module.validate_network_inventory([network], "workspace-prod")


@pytest.mark.parametrize(
    "route",
    [
        {"dst": "172.29.0.0/16", "dev": "eth0"},
        {"dst": "172.29.3.5/32", "dev": "tun0"},
        {"dst": "172.29.4.0/24"},
    ],
)
def test_host_route_overlap_fails_closed(route: dict) -> None:
    with pytest.raises(module.ConflictError):
        module.validate_host_routes([route], {})


def test_default_and_unrelated_routes_are_ignored() -> None:
    module.validate_host_routes(
        [{"dst": "default", "dev": "eth0"}, {"dst": "10.0.0.0/8", "dev": "eth0"}],
        {},
    )


def test_managed_bridge_rejects_broad_route_but_allows_owned_exact_drift() -> None:
    edge = managed("edge", "172.29.3.0/24")
    jupyter = managed("jupyter", "172.29.0.0/24")
    devices = module.validate_network_inventory([edge, jupyter], "workspace-prod")
    edge_device = f"br-{edge['Id'][:12]}"
    with pytest.raises(module.ConflictError):
        module.validate_host_routes(
            [{"dst": "172.29.0.0/16", "dev": edge_device}], devices
        )


def test_live_inventory_requests_full_docker_network_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str]) -> str:
        calls.append(arguments)
        if arguments[:3] == ["docker", "network", "ls"]:
            return ""
        if arguments[0] == "ip":
            return "[]"
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "_run", fake_run)
    module.check("workspace-prod")

    assert calls[0] == ["docker", "network", "ls", "--quiet", "--no-trunc"]
