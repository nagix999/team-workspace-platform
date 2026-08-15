#!/bin/sh
set -eu

cert_path="${PLATFORM_TLS_CERT_PATH:-/run/platform-tls/tls.crt}"
key_path="${PLATFORM_TLS_KEY_PATH:-/run/platform-tls/tls.key}"
minimum_validity="${PLATFORM_TLS_MIN_VALIDITY_SECONDS:-86400}"
gateway_mode="${PLATFORM_GATEWAY_MODE:-production}"
baked_mode_path="/usr/local/share/platform-gateway-config-mode"

fail() {
    echo "gateway startup validation failed: $*" >&2
    exit 1
}

case "${minimum_validity}" in
    ''|*[!0-9]*) fail "PLATFORM_TLS_MIN_VALIDITY_SECONDS must be a non-negative integer" ;;
esac

for command_name in awk cat openssl nginx stat sed tr; do
    command -v "${command_name}" >/dev/null 2>&1 \
        || fail "required command is unavailable: ${command_name}"
done

test -f "${baked_mode_path}" && test -s "${baked_mode_path}" && test -r "${baked_mode_path}" \
    || fail "baked Gateway config-mode marker is missing, empty, or unreadable"
baked_mode="$(cat "${baked_mode_path}")"
case "${baked_mode}" in
    production|domain-test) ;;
    *) fail "baked Gateway config-mode marker is invalid" ;;
esac
test "${gateway_mode}" = "${baked_mode}" \
    || fail "PLATFORM_GATEWAY_MODE does not match the baked Nginx server config"

case "${gateway_mode}" in
    production)
        portal_san='DNS:platform.example.com'
        hub_san='DNS:hub.example.net'
        wildcard_hub_san='DNS:*.hub.example.net'
        cidr_path="${PLATFORM_INGRESS_CIDRS_PATH:-/run/platform-ingress/company-vpn-cidrs.txt}"
        test -f "${cidr_path}" && test -s "${cidr_path}" && test -r "${cidr_path}" \
            || fail "production company/VPN CIDR allowlist is missing, empty, or unreadable"
        if ! awk '
            function invalid() { exit 42 }
            BEGIN {
                print "geo $platform_ingress_allowed {"
                print "    default 0;"
            }
            {
                sub(/^[[:space:]]+/, "")
                sub(/[[:space:]]+$/, "")
            }
            $0 == "" || $0 ~ /^#/ { next }
            $0 !~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\/([0-9]|[12][0-9]|3[0-2])$/ { invalid() }
            {
                split($0, network, "/")
                if (network[2] == 0) invalid()
                split(network[1], octet, ".")
                for (part = 1; part <= 4; part++) {
                    if (octet[part] < 0 || octet[part] > 255) invalid()
                }
                print "    " $0 " 1;"
                count++
            }
            END {
                if (count < 1) exit 43
                print "}"
            }
        ' "${cidr_path}" > /tmp/company-vpn-allowlist.conf.tmp; then
            rm -f /tmp/company-vpn-allowlist.conf.tmp
            fail "production CIDR allowlist must contain only valid IPv4 CIDRs, at least one entry, and never 0.0.0.0/0"
        fi
        mv /tmp/company-vpn-allowlist.conf.tmp /tmp/company-vpn-allowlist.conf
        ;;
    domain-test)
        portal_san='DNS:platform.workspace.test'
        hub_san='DNS:hub.workspace.test'
        wildcard_hub_san='DNS:*.hub.workspace.test'
        ;;
    *) fail "PLATFORM_GATEWAY_MODE must be production or domain-test" ;;
esac

test -f "${cert_path}" && test -s "${cert_path}" && test -r "${cert_path}" \
    || fail "TLS certificate is not a readable non-empty regular file"
test -f "${key_path}" && test -s "${key_path}" && test -r "${key_path}" \
    || fail "TLS private key is not a readable non-empty regular file"

# The bind mount is read-only as a separate Compose invariant. On the host,
# also reject executable, group-writable, or world-readable private keys.
key_mode="$(stat -c '%a' "${key_path}")"
case "${key_mode}" in
    400|440|600|640) ;;
    *) fail "TLS private key mode must be one of 0400, 0440, 0600, or 0640" ;;
esac

openssl x509 -in "${cert_path}" -noout >/dev/null 2>&1 \
    || fail "TLS certificate cannot be parsed"
openssl pkey -in "${key_path}" -passin pass: -noout >/dev/null 2>&1 \
    || fail "TLS private key must be parseable and unencrypted"
openssl x509 -in "${cert_path}" -noout -checkend "${minimum_validity}" >/dev/null 2>&1 \
    || fail "TLS certificate is expired or expires inside the required validity window"

san_csv="$(
    openssl x509 -in "${cert_path}" -noout -ext subjectAltName 2>/dev/null \
        | sed '1d' \
        | tr -d '[:space:]'
)"
for required_san in \
    "${portal_san}" \
    "${hub_san}" \
    "${wildcard_hub_san}"
do
    if ! printf '%s' "${san_csv}" | awk -v required="${required_san}" '
        BEGIN { RS = "," }
        $0 == required { found = 1 }
        END { exit(found ? 0 : 1) }
    '; then
        fail "TLS leaf certificate is missing SAN ${required_san}"
    fi
done

cert_public_key_digest="$(
    openssl x509 -in "${cert_path}" -pubkey -noout \
        | openssl pkey -pubin -outform DER 2>/dev/null \
        | openssl dgst -sha256
)"
key_public_key_digest="$(
    openssl pkey -in "${key_path}" -passin pass: -pubout -outform DER 2>/dev/null \
        | openssl dgst -sha256
)"
test -n "${cert_public_key_digest}" \
    && test "${cert_public_key_digest}" = "${key_public_key_digest}" \
    || fail "TLS certificate and private key do not match"

nginx -t
exec "$@"
