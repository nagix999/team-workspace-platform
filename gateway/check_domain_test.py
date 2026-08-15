#!/usr/bin/env python3
"""Fail-closed preflight for the loopback exact-domain HTTPS rehearsal."""

from __future__ import annotations

import argparse
import ipaddress
import re
import socket
import subprocess
import sys
from pathlib import Path


PORTAL_HOST = "platform.workspace.test"
HUB_HOST = "hub.workspace.test"
WILDCARD_SAN = "*.hub.workspace.test"
USERNAME = re.compile(r"^(?!.*--)[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def expected_hosts(usernames: list[str]) -> list[str]:
    invalid = [name for name in usernames if not USERNAME.fullmatch(name)]
    if invalid:
        raise ValueError("invalid JupyterHub username(s): " + ", ".join(invalid))
    return [PORTAL_HOST, HUB_HOST, *(f"{name}.{HUB_HOST}" for name in usernames)]


def non_loopback_addresses(hostname: str) -> tuple[set[str], set[str]]:
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    }
    loopback = {
        address for address in addresses if ipaddress.ip_address(address).is_loopback
    }
    return loopback, addresses - loopback


def certificate_sans(path: Path) -> set[str]:
    result = subprocess.run(
        ["openssl", "x509", "-in", str(path), "-noout", "-ext", "subjectAltName"],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(re.findall(r"DNS:([^,\s]+)", result.stdout))


def port_443_is_free() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # A bind probe gives a false negative to an unprivileged caller when
            # net.ipv4.ip_unprivileged_port_start is 1024. Docker remains the
            # authoritative race-free binder; this is only an early LISTEN check.
            probe.settimeout(0.25)
            return probe.connect_ex(("127.0.0.1", 443)) != 0
    except OSError:
        # Restricted test sandboxes may forbid even a loopback connect probe.
        # Fail closed with the normal preflight error instead of a traceback.
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify exact domain-test names resolve only to loopback. "
            "Wildcard entries are not supported by /etc/hosts."
        )
    )
    parser.add_argument(
        "--user",
        action="append",
        required=True,
        help="approved Hub username; repeat for every user URL to rehearse",
    )
    parser.add_argument(
        "--certificate",
        type=Path,
        help="optional local-CA leaf/full-chain PEM to verify required SANs",
    )
    parser.add_argument(
        "--check-port-free",
        action="store_true",
        help="also fail if a loopback TCP/443 listener is already reachable",
    )
    args = parser.parse_args()

    try:
        hosts = expected_hosts(args.user)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    failures: list[str] = []
    for hostname in hosts:
        try:
            loopback, other = non_loopback_addresses(hostname)
        except socket.gaierror:
            failures.append(f"{hostname}: does not resolve")
            continue
        if not loopback:
            failures.append(f"{hostname}: has no loopback address")
        if other:
            failures.append(
                f"{hostname}: also resolves outside loopback ({', '.join(sorted(other))})"
            )

    if args.certificate:
        try:
            sans = certificate_sans(args.certificate)
        except (OSError, subprocess.CalledProcessError) as exc:
            failures.append(f"certificate cannot be parsed with openssl: {exc}")
        else:
            missing = {PORTAL_HOST, HUB_HOST, WILDCARD_SAN} - sans
            if missing:
                failures.append(
                    "certificate SANs are missing: " + ", ".join(sorted(missing))
                )

    if args.check_port_free and not port_443_is_free():
        failures.append(
            "127.0.0.1:443 already has a listener or the loopback probe is not permitted"
        )

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        print(
            "\nAdd exact test names (never a wildcard) to local DNS or /etc/hosts:",
            file=sys.stderr,
        )
        print("127.0.0.1 " + " ".join(hosts), file=sys.stderr)
        return 1

    print("domain-test host/certificate preflight passed")
    for hostname in hosts:
        print(f"  https://{hostname}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
