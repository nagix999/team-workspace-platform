# Render reviewed private-service tuples as paired Squid ACLs.
#
# Input is deliberately narrower than Squid's native ACL syntax: one canonical
# RFC1918 host route and one unprivileged TCP port per line.  Keeping the pair
# in a single generated http_access rule prevents the Cartesian-product access
# that two shared destination/port files would create.

function invalid(reason) {
    print "egress internal services: line " NR ": " reason > "/dev/stderr"
    failed = 1
    exit 64
}

function blocked_control_port(port) {
    return port == 2375 || port == 2376 || port == 2377 || \
        port == 3128 || port == 4243 || port == 6443 || port == 10250
}

function platform_destination(first, second, third) {
    return \
        (first == 172 && second == 24) || \
        (first == 172 && second == 25 && third == 0) || \
        (first == 172 && second == 26 && third == 0) || \
        (first == 172 && second == 27 && third == 0) || \
        (first == 172 && second == 28 && third == 0) || \
        (first == 172 && second == 30 && third == 0) || \
        (first == 172 && second == 29 && third >= 0 && third <= 4)
}

BEGIN {
    print "# Generated from the operator-owned internal-service allowlist."
}

{
    line = $0
    sub(/\r$/, "", line)
    sub(/^[ \t]+/, "", line)
    sub(/[ \t]+$/, "", line)

    if (line == "" || line ~ /^#/) {
        next
    }
    if (line !~ /^[0-9][0-9]?[0-9]?\.[0-9][0-9]?[0-9]?\.[0-9][0-9]?[0-9]?\.[0-9][0-9]?[0-9]?\/32[ \t]+[0-9][0-9]?[0-9]?[0-9]?[0-9]?$/) {
        invalid("expected exactly: canonical-private-IPv4/32 PORT")
    }

    field_count = split(line, fields, /[ \t]+/)
    if (field_count != 2) {
        invalid("expected exactly two fields")
    }
    cidr_count = split(fields[1], cidr, "/")
    octet_count = split(cidr[1], octets, /[.]/)
    if (cidr_count != 2 || cidr[2] != "32" || octet_count != 4) {
        invalid("only an exact IPv4 /32 is allowed")
    }
    for (octet_index = 1; octet_index <= 4; octet_index++) {
        if (octets[octet_index] + 0 > 255) {
            invalid("IPv4 octet is outside 0..255")
        }
    }
    canonical_ip = sprintf("%d.%d.%d.%d", octets[1] + 0, octets[2] + 0, octets[3] + 0, octets[4] + 0)
    if (canonical_ip "/32" != fields[1]) {
        invalid("IPv4 /32 must use canonical decimal notation")
    }

    first = octets[1] + 0
    second = octets[2] + 0
    if (!(first == 10 || (first == 172 && second >= 16 && second <= 31) || (first == 192 && second == 168))) {
        invalid("destination must be an RFC1918 private IPv4 /32")
    }
    if (platform_destination(first, second, octets[3] + 0)) {
        invalid("destination overlaps an immutable platform network")
    }

    port = fields[2] + 0
    if (sprintf("%d", port) != fields[2]) {
        invalid("port must use canonical decimal notation")
    }
    if (port < 1024 || port > 65535 || blocked_control_port(port)) {
        invalid("port is privileged, reserved for control-plane access, or out of range")
    }

    tuple = canonical_ip ":" port
    if (seen[tuple]++) {
        invalid("duplicate destination/port tuple")
    }
    if (++rule_count > 32) {
        invalid("at most 32 internal-service tuples are allowed")
    }

    printf "acl operator_internal_destination_%d dst %s/32\n", rule_count, canonical_ip
    printf "acl operator_internal_port_%d port %d\n", rule_count, port
    printf "http_access allow operator_internal_destination_%d operator_internal_port_%d\n", rule_count, rule_count
}

END {
    if (failed) {
        exit 64
    }
}
