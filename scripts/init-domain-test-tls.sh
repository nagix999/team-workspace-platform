#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "${script_dir}/.." && pwd)"
tls_dir_input="${DOMAIN_TEST_TLS_DIR:-${project_dir}/secrets/domain-test}"
group_input="${DOMAIN_TEST_TLS_GROUP:-$(id -g)}"
openssl_bin="${OPENSSL_BIN:-openssl}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

for required_command in "${openssl_bin}" getent id awk sort mktemp mv rm rmdir cat chmod chgrp; do
  command -v "${required_command}" >/dev/null 2>&1 || \
    die "required command is unavailable: ${required_command}"
done

resolve_group_gid() {
  local group_spec="$1"
  local group_entry
  local group_name
  local group_gid
  local unused_password
  local unused_members

  if [[ "${group_spec}" =~ ^[0-9]+$ ]]; then
    :
  elif [[ "${group_spec}" =~ ^[A-Za-z_][A-Za-z0-9_.-]*\$?$ ]]; then
    :
  else
    die "DOMAIN_TEST_TLS_GROUP must be an existing group name or numeric GID"
  fi

  if ! group_entry="$(getent group "${group_spec}")"; then
    die "DOMAIN_TEST_TLS_GROUP does not resolve to an existing group: ${group_spec}"
  fi
  IFS=: read -r group_name unused_password group_gid unused_members <<<"${group_entry}"
  if [[ -z "${group_name}" || ! "${group_gid}" =~ ^[0-9]+$ ]]; then
    die "could not resolve a numeric GID for DOMAIN_TEST_TLS_GROUP"
  fi

  if [[ "${EUID}" -ne 0 ]]; then
    case " $(id -G) " in
      *" ${group_gid} "*) ;;
      *)
        die "the current user cannot assign files to GID ${group_gid}; choose one of: $(id -G)"
        ;;
    esac
  fi

  printf '%s\n' "${group_gid}"
}

tls_gid="$(resolve_group_gid "${group_input}")"

[[ -n "${tls_dir_input}" ]] || die "DOMAIN_TEST_TLS_DIR must not be empty"
if [[ "${tls_dir_input}" = /* ]]; then
  tls_dir="${tls_dir_input}"
else
  tls_dir="${project_dir}/${tls_dir_input}"
fi

if [[ -L "${tls_dir}" ]]; then
  die "refusing a symlink TLS directory: ${tls_dir}"
fi
if [[ -e "${tls_dir}" && ! -d "${tls_dir}" ]]; then
  die "TLS output path exists but is not a directory: ${tls_dir}"
fi
mkdir -p -- "${tls_dir}"
if [[ -L "${tls_dir}" ]]; then
  die "refusing a symlink TLS directory: ${tls_dir}"
fi
tls_dir="$(cd -- "${tls_dir}" && pwd -P)"
[[ "${tls_dir}" != "/" ]] || die "refusing to use the filesystem root as the TLS directory"

ca_key="${tls_dir}/ca.key"
ca_cert="${tls_dir}/ca.crt"
tls_key="${tls_dir}/tls.key"
tls_cert="${tls_dir}/tls.crt"
material_paths=("${ca_key}" "${ca_cert}" "${tls_key}" "${tls_cert}")

pem_certificate_count() {
  awk '$0 == "-----BEGIN CERTIFICATE-----" { count += 1 } END { print count + 0 }' "$1"
}

pem_certificate_end_count() {
  awk '$0 == "-----END CERTIFICATE-----" { count += 1 } END { print count + 0 }' "$1"
}

extract_pem_certificate() {
  local source_file="$1"
  local wanted_index="$2"

  awk -v wanted="${wanted_index}" '
    $0 == "-----BEGIN CERTIFICATE-----" {
      seen += 1
      if (seen == wanted) {
        copying = 1
      }
    }
    copying { print }
    copying && $0 == "-----END CERTIFICATE-----" { exit }
  ' "${source_file}"
}

validation_error=""
validate_material() {
  local candidate_ca_key="$1"
  local candidate_ca_cert="$2"
  local candidate_tls_key="$3"
  local candidate_tls_cert="$4"
  local ca_public_key
  local ca_private_public_key
  local tls_public_key
  local tls_private_public_key
  local ca_constraints
  local tls_constraints
  local actual_sans
  local expected_sans
  local ca_fingerprint
  local chain_ca_fingerprint
  local begin_count
  local end_count

  for candidate in \
    "${candidate_ca_key}" \
    "${candidate_ca_cert}" \
    "${candidate_tls_key}" \
    "${candidate_tls_cert}"; do
    if [[ -L "${candidate}" || ! -f "${candidate}" ]]; then
      validation_error="expected a regular, non-symlink file: ${candidate}"
      return 1
    fi
  done

  begin_count="$(pem_certificate_count "${candidate_ca_cert}")"
  end_count="$(pem_certificate_end_count "${candidate_ca_cert}")"
  if [[ "${begin_count}" != "1" || "${end_count}" != "1" ]]; then
    validation_error="ca.crt must contain exactly one PEM certificate"
    return 1
  fi

  begin_count="$(pem_certificate_count "${candidate_tls_cert}")"
  end_count="$(pem_certificate_end_count "${candidate_tls_cert}")"
  if [[ "${begin_count}" != "2" || "${end_count}" != "2" ]]; then
    validation_error="tls.crt must contain exactly the leaf and local CA certificates"
    return 1
  fi

  if ! ca_public_key="$("${openssl_bin}" x509 -in "${candidate_ca_cert}" -noout -pubkey 2>/dev/null)"; then
    validation_error="ca.crt is not a readable X.509 certificate"
    return 1
  fi
  if ! ca_private_public_key="$("${openssl_bin}" pkey -in "${candidate_ca_key}" -passin pass: -pubout 2>/dev/null)"; then
    validation_error="ca.key is not a readable, unencrypted private key"
    return 1
  fi
  if [[ -z "${ca_public_key}" || "${ca_public_key}" != "${ca_private_public_key}" ]]; then
    validation_error="ca.key does not match ca.crt"
    return 1
  fi

  if ! tls_public_key="$("${openssl_bin}" x509 -in "${candidate_tls_cert}" -noout -pubkey 2>/dev/null)"; then
    validation_error="the first certificate in tls.crt is not readable"
    return 1
  fi
  if ! tls_private_public_key="$("${openssl_bin}" pkey -in "${candidate_tls_key}" -passin pass: -pubout 2>/dev/null)"; then
    validation_error="tls.key is not a readable, unencrypted private key"
    return 1
  fi
  if [[ -z "${tls_public_key}" || "${tls_public_key}" != "${tls_private_public_key}" ]]; then
    validation_error="tls.key does not match the leaf certificate in tls.crt"
    return 1
  fi

  if ! ca_constraints="$("${openssl_bin}" x509 -in "${candidate_ca_cert}" -noout -ext basicConstraints 2>/dev/null)" || \
    [[ "${ca_constraints}" != *"CA:TRUE"* ]]; then
    validation_error="ca.crt is not marked as a certificate authority"
    return 1
  fi
  if ! tls_constraints="$("${openssl_bin}" x509 -in "${candidate_tls_cert}" -noout -ext basicConstraints 2>/dev/null)" || \
    [[ "${tls_constraints}" != *"CA:FALSE"* ]]; then
    validation_error="the leaf certificate in tls.crt is not marked CA:FALSE"
    return 1
  fi

  if ! actual_sans="$(
    "${openssl_bin}" x509 -in "${candidate_tls_cert}" -noout -ext subjectAltName 2>/dev/null |
      awk '
        NR == 1 { next }
        {
          item_count = split($0, items, ",")
          for (item_index = 1; item_index <= item_count; item_index += 1) {
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", items[item_index])
            if (items[item_index] != "") {
              print items[item_index]
            }
          }
        }
      ' |
      LC_ALL=C sort
  )"; then
    validation_error="could not read the leaf certificate SAN extension"
    return 1
  fi
  expected_sans="$(
    printf '%s\n' \
      'DNS:platform.workspace.test' \
      'DNS:hub.workspace.test' \
      'DNS:*.hub.workspace.test' |
      LC_ALL=C sort
  )"
  if [[ "${actual_sans}" != "${expected_sans}" ]]; then
    validation_error="the leaf certificate SAN set is not the required three-name set"
    return 1
  fi

  if ! "${openssl_bin}" x509 -in "${candidate_ca_cert}" -noout -checkend 0 >/dev/null 2>&1; then
    validation_error="ca.crt is not currently valid"
    return 1
  fi
  if ! "${openssl_bin}" x509 -in "${candidate_tls_cert}" -noout -checkend 0 >/dev/null 2>&1; then
    validation_error="the leaf certificate in tls.crt is not currently valid"
    return 1
  fi
  if ! "${openssl_bin}" verify -CAfile "${candidate_ca_cert}" "${candidate_ca_cert}" >/dev/null 2>&1; then
    validation_error="ca.crt is not a valid self-signed local CA certificate"
    return 1
  fi
  if ! "${openssl_bin}" verify -purpose sslserver -CAfile "${candidate_ca_cert}" "${candidate_tls_cert}" >/dev/null 2>&1; then
    validation_error="the leaf certificate is not a valid TLS server certificate issued by ca.crt"
    return 1
  fi

  if ! ca_fingerprint="$(
    "${openssl_bin}" x509 -in "${candidate_ca_cert}" -outform DER 2>/dev/null |
      "${openssl_bin}" dgst -sha256 2>/dev/null
  )"; then
    validation_error="could not fingerprint ca.crt"
    return 1
  fi
  if ! chain_ca_fingerprint="$(
    extract_pem_certificate "${candidate_tls_cert}" 2 |
      "${openssl_bin}" x509 -outform DER 2>/dev/null |
      "${openssl_bin}" dgst -sha256 2>/dev/null
  )"; then
    validation_error="could not read the CA certificate appended to tls.crt"
    return 1
  fi
  if [[ -z "${ca_fingerprint}" || "${ca_fingerprint}" != "${chain_ca_fingerprint}" ]]; then
    validation_error="the second certificate in tls.crt is not ca.crt"
    return 1
  fi

  return 0
}

apply_secure_permissions() {
  chgrp -- "${tls_gid}" "${tls_dir}" "${ca_key}" "${ca_cert}" "${tls_key}" "${tls_cert}" || \
    die "could not assign domain-test TLS material to GID ${tls_gid}"
  chmod 0750 "${tls_dir}"
  chmod 0600 "${ca_key}"
  chmod 0640 "${tls_key}"
  chmod 0644 "${ca_cert}" "${tls_cert}"
}

present_count=0
for material_path in "${material_paths[@]}"; do
  if [[ -e "${material_path}" || -L "${material_path}" ]]; then
    present_count=$((present_count + 1))
  fi
done

material_state=""
if [[ "${present_count}" -eq "${#material_paths[@]}" ]]; then
  if ! validate_material "${ca_key}" "${ca_cert}" "${tls_key}" "${tls_cert}"; then
    die "existing domain-test TLS material failed validation (${validation_error}); refusing to overwrite it"
  fi
  apply_secure_permissions
  material_state="validated existing"
elif [[ "${present_count}" -ne 0 ]]; then
  die "found only ${present_count} of ${#material_paths[@]} expected TLS files in ${tls_dir}; refusing to create or overwrite material"
else
  temp_dir="$(mktemp -d -- "${tls_dir}/.init.XXXXXXXX")"

  cleanup_temp_dir() {
    if [[ -n "${temp_dir:-}" && -d "${temp_dir}" ]]; then
      rm -f -- \
        "${temp_dir}/ca.cnf" \
        "${temp_dir}/leaf.cnf" \
        "${temp_dir}/ca.key" \
        "${temp_dir}/ca.crt" \
        "${temp_dir}/tls.key" \
        "${temp_dir}/tls.csr" \
        "${temp_dir}/leaf.crt" \
        "${temp_dir}/tls.crt"
      rmdir -- "${temp_dir}" 2>/dev/null || true
    fi
  }
  trap cleanup_temp_dir EXIT

  {
    printf '%s\n' \
      '[req]' \
      'prompt = no' \
      'distinguished_name = distinguished_name' \
      'x509_extensions = ca_extensions' \
      '[distinguished_name]' \
      'CN = Team Workspace Domain Test Local CA' \
      '[ca_extensions]' \
      'basicConstraints = critical, CA:TRUE, pathlen:0' \
      'keyUsage = critical, keyCertSign, cRLSign' \
      'subjectKeyIdentifier = hash' \
      'authorityKeyIdentifier = keyid:always, issuer'
  } >"${temp_dir}/ca.cnf"

  {
    printf '%s\n' \
      '[req]' \
      'prompt = no' \
      'distinguished_name = distinguished_name' \
      'req_extensions = request_extensions' \
      '[distinguished_name]' \
      'CN = platform.workspace.test' \
      '[request_extensions]' \
      'subjectAltName = @subject_alt_names' \
      '[leaf_extensions]' \
      'basicConstraints = critical, CA:FALSE' \
      'keyUsage = critical, digitalSignature, keyEncipherment' \
      'extendedKeyUsage = serverAuth' \
      'subjectKeyIdentifier = hash' \
      'authorityKeyIdentifier = keyid, issuer' \
      'subjectAltName = @subject_alt_names' \
      '[subject_alt_names]' \
      'DNS.1 = platform.workspace.test' \
      'DNS.2 = hub.workspace.test' \
      'DNS.3 = *.hub.workspace.test'
  } >"${temp_dir}/leaf.cnf"

  if ! "${openssl_bin}" genpkey \
    -algorithm RSA \
    -pkeyopt rsa_keygen_bits:3072 \
    -out "${temp_dir}/ca.key" >/dev/null 2>&1; then
    die "OpenSSL failed while generating the local CA key"
  fi
  if ! "${openssl_bin}" req \
    -new \
    -x509 \
    -sha256 \
    -days 3650 \
    -key "${temp_dir}/ca.key" \
    -config "${temp_dir}/ca.cnf" \
    -out "${temp_dir}/ca.crt" >/dev/null 2>&1; then
    die "OpenSSL failed while generating the local CA certificate"
  fi
  if ! "${openssl_bin}" genpkey \
    -algorithm RSA \
    -pkeyopt rsa_keygen_bits:3072 \
    -out "${temp_dir}/tls.key" >/dev/null 2>&1; then
    die "OpenSSL failed while generating the TLS key"
  fi
  if ! "${openssl_bin}" req \
    -new \
    -sha256 \
    -key "${temp_dir}/tls.key" \
    -config "${temp_dir}/leaf.cnf" \
    -out "${temp_dir}/tls.csr" >/dev/null 2>&1; then
    die "OpenSSL failed while generating the TLS certificate request"
  fi
  certificate_serial="$("${openssl_bin}" rand -hex 16)"
  if ! "${openssl_bin}" x509 \
    -req \
    -sha256 \
    -days 397 \
    -in "${temp_dir}/tls.csr" \
    -CA "${temp_dir}/ca.crt" \
    -CAkey "${temp_dir}/ca.key" \
    -set_serial "0x${certificate_serial}" \
    -extfile "${temp_dir}/leaf.cnf" \
    -extensions leaf_extensions \
    -out "${temp_dir}/leaf.crt" >/dev/null 2>&1; then
    die "OpenSSL failed while signing the TLS certificate"
  fi
  {
    command cat -- "${temp_dir}/leaf.crt"
    command cat -- "${temp_dir}/ca.crt"
  } >"${temp_dir}/tls.crt"

  if ! validate_material \
    "${temp_dir}/ca.key" \
    "${temp_dir}/ca.crt" \
    "${temp_dir}/tls.key" \
    "${temp_dir}/tls.crt"; then
    die "generated TLS material failed self-validation (${validation_error})"
  fi

  chmod 0600 "${temp_dir}/ca.key"
  chmod 0640 "${temp_dir}/tls.key"
  chmod 0644 "${temp_dir}/ca.crt" "${temp_dir}/tls.crt"
  chgrp -- "${tls_gid}" \
    "${temp_dir}/ca.key" \
    "${temp_dir}/ca.crt" \
    "${temp_dir}/tls.key" \
    "${temp_dir}/tls.crt" || \
    die "could not assign generated TLS material to GID ${tls_gid}"

  for material_name in ca.key ca.crt tls.key tls.crt; do
    destination="${tls_dir}/${material_name}"
    if [[ -e "${destination}" || -L "${destination}" ]]; then
      die "TLS destination appeared during generation; refusing to overwrite it: ${destination}"
    fi
    mv --no-clobber -- "${temp_dir}/${material_name}" "${destination}"
    if [[ -e "${temp_dir}/${material_name}" || -L "${temp_dir}/${material_name}" ]]; then
      die "TLS destination appeared during installation; no existing file was overwritten: ${destination}"
    fi
  done

  apply_secure_permissions
  material_state="created"
fi

printf 'Domain-test TLS material: %s\n' "${material_state}"
printf '  CA certificate: %s\n' "${ca_cert}"
printf '  TLS full chain: %s\n' "${tls_cert}"
printf '  TLS private key: %s\n' "${tls_key}"
printf '  TLS readable GID: %s\n' "${tls_gid}"
printf '\nUse these values for the loopback-only domain-test stack:\n'
printf '  export DOMAIN_TEST_CA_CERT_FILE=%q\n' "${ca_cert}"
printf '  export DOMAIN_TEST_TLS_CERT_FILE=%q\n' "${tls_cert}"
printf '  export DOMAIN_TEST_TLS_KEY_FILE=%q\n' "${tls_key}"
printf '  export DOMAIN_TEST_TLS_GID=%q\n' "${tls_gid}"
printf '\nThis script did not modify any OS or browser trust store.\n'
printf 'Prefer clients configured with --cacert %q; import only ca.crt manually if browser testing requires it.\n' "${ca_cert}"
