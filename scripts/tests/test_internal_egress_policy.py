from __future__ import annotations

import hashlib
import ipaddress
import re
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RENDERER = ROOT / "infra" / "egress-proxy" / "render-internal-services.awk"
SQUID_CONFIG = ROOT / "infra" / "egress-proxy" / "squid.conf"
IMMUTABLE_NETWORKS = ROOT / "infra" / "egress-proxy" / "immutable-platform-networks.txt"


def render(contents: str) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as policy:
        policy.write(contents)
        policy.flush()
        return subprocess.run(
            ["awk", "-f", str(RENDERER), policy.name],
            check=False,
            capture_output=True,
            text=True,
        )


def test_renderer_keeps_each_destination_and_port_as_one_exact_pair() -> None:
    result = render("10.255.255.254/32 8000\n192.168.255.254/32 8443\n")

    assert result.returncode == 0, result.stderr
    assert "acl operator_internal_destination_1 dst 10.255.255.254/32" in result.stdout
    assert "acl operator_internal_port_1 port 8000" in result.stdout
    assert (
        "http_access allow operator_internal_destination_1 operator_internal_port_1"
        in result.stdout
    )
    assert "acl operator_internal_destination_2 dst 192.168.255.254/32" in result.stdout
    assert "acl operator_internal_port_2 port 8443" in result.stdout
    assert (
        "operator_internal_destination_1 operator_internal_port_2" not in result.stdout
    )
    assert (
        "operator_internal_destination_2 operator_internal_port_1" not in result.stdout
    )


def test_comment_only_bootstrap_is_valid_deny_all() -> None:
    result = render("# no bootstrap exception\n\n")

    assert result.returncode == 0, result.stderr
    assert "http_access allow" not in result.stdout


def test_renderer_rejects_broad_non_private_noncanonical_and_injected_rules() -> None:
    invalid_rules = (
        "10.255.255.254/24 8000\n",
        "203.0.113.10/32 8000\n",
        "10.025.1.1/32 8000\n",
        "10.255.255.256/32 8000\n",
        "10.255.255.254/32 80\n",
        "10.255.255.254/32 2375\n",
        "10.255.255.254/32 3128\n",
        "10.255.255.254/32 65536\n",
        "10.255.255.254/32 08000\n",
        "10.255.255.254/32 8000 # inline input\n",
        "10.255.255.254/32 8000\n10.255.255.254/32 8000\n",
        "172.29.0.10/32 8000\n",
    )
    for rule in invalid_rules:
        result = render(rule)
        assert result.returncode != 0, rule
        assert "egress internal services: line" in result.stderr


def test_renderer_caps_policy_size_by_rule_count() -> None:
    rules = "".join(f"10.255.0.{index}/32 8000\n" for index in range(1, 34))

    result = render(rules)

    assert result.returncode != 0
    assert "at most 32" in result.stderr


def test_squid_keeps_immutable_denies_before_dynamic_exception() -> None:
    config = SQUID_CONFIG.read_text(encoding="utf-8")

    immutable = config.index("http_access deny immutable_forbidden_destination")
    dynamic = config.index("include /tmp/operator-internal-services.conf")
    private_default = config.index("http_access deny forbidden_destination")
    domain_allow = config.index("http_access allow approved_domains")
    assert immutable < dynamic < private_default < domain_allow
    source_deny = config.index("http_access deny !execution_clients")
    assert source_deny < immutable
    assert "acl execution_clients src 172.29.0.0/24 172.30.0.0/24" in config


def test_all_compose_subnets_are_immutable_in_api_renderer_and_squid() -> None:
    local = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    production = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    domain = (ROOT / "compose.domain-test.yaml").read_text(encoding="utf-8")
    configured = {
        ipaddress.ip_network(value)
        for value in re.findall(
            r"- subnet: (172\.\d+\.\d+\.0/\d+)", local + production + domain
        )
    }
    immutable = {
        ipaddress.ip_network(line)
        for line in IMMUTABLE_NETWORKS.read_text(encoding="ascii").splitlines()
        if line and not line.startswith("#")
    }
    assert immutable == configured

    backend_source = (
        ROOT / "backend" / "app" / "services" / "internal_egress.py"
    ).read_text(encoding="utf-8")
    constant_source = backend_source[
        backend_source.index("IMMUTABLE_PLATFORM_NETWORKS") : backend_source.index(
            "INTERNAL_EGRESS_ERROR_SUMMARIES"
        )
    ]
    assert set(re.findall(r'"(172\.\d+\.\d+\.0/\d+)"', constant_source)) == {
        str(value) for value in immutable
    }
    assert (
        'acl immutable_platform_destination dst "/etc/squid/immutable-platform-networks.txt"'
        in SQUID_CONFIG.read_text(encoding="utf-8")
    )
    for network in immutable:
        address = network.network_address + 1
        result = render(f"{address}/32 8000\n")
        assert result.returncode != 0, network
        assert "immutable platform network" in result.stderr


def test_dynamic_protocol_and_compose_mounts_are_explicit() -> None:
    entrypoint = (
        ROOT / "infra" / "egress-proxy" / "platform-squid-entrypoint"
    ).read_text(encoding="utf-8")
    production = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    local = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "PLATFORM_INTERNAL_EGRESS_V1" in entrypoint
    assert "PLATFORM_INTERNAL_EGRESS_ACK_V1" in entrypoint
    assert "squid -k parse" in entrypoint
    assert "squid -k reconfigure" in entrypoint
    assert entrypoint.index("if ! squid -k parse") < entrypoint.index(
        'if ! mv -- "${candidate_include}" "${active_include}"'
    )
    assert "SQUID_RECONFIGURE_FAILED" in entrypoint
    for compose in (production, local):
        assert (
            "egress_policy_desired:/var/lib/platform-egress-policy/desired:rw"
            in compose
        )
        assert (
            "egress_policy_desired:/var/lib/platform-egress-policy/desired:ro"
            in compose
        )
        assert "egress_policy_ack:/var/lib/platform-egress-policy/ack:ro" in compose
        assert "egress_policy_ack:/var/lib/platform-egress-policy/ack:rw" in compose
        assert (
            "PLATFORM_INTERNAL_EGRESS_POLICY_DIR: /var/lib/platform-egress-policy"
            in compose
        )
        assert 'entrypoint: ["/usr/local/bin/init-policy-volume"]' in compose
        assert 'group_add:\n      - "2999"' in compose
    assert "PLATFORM_EGRESS_INTERNAL_SERVICES_FILE" not in production
    assert "PLATFORM_EGRESS_INTERNAL_SERVICES_FILE" not in (
        ROOT / ".env.production.example"
    ).read_text(encoding="utf-8")


def test_empty_policy_digest_contract_is_stable() -> None:
    assert hashlib.sha256(b"").hexdigest() == (
        "e3b0c44298fc1c149afbf4c8996fb924" "27ae41e4649b934ca495991b7852b855"
    )


def test_supported_database_restore_discards_derived_runtime_policy() -> None:
    production = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")
    domain_test = (ROOT / "scripts" / "domain-test.sh").read_text(encoding="utf-8")

    restore_body = production[production.index("restore() {") :]
    assert "reset_internal_egress_runtime_state" in restore_body
    assert restore_body.index(
        "reset_internal_egress_runtime_state"
    ) < restore_body.index("python /snapshot-tool.py restore")
    for value in (production, domain_test):
        assert "_egress_policy_desired" in value
        assert "_egress_policy_ack" in value
        assert "docker volume rm" in value
