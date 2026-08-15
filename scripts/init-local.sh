#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "${script_dir}/.." && pwd)"
secret_dir="${project_dir}/secrets"
env_file="${project_dir}/.env"
env_example="${project_dir}/.env.example"

if [[ ! -S /var/run/docker.sock ]]; then
  echo "ERROR: /var/run/docker.sock is unavailable" >&2
  exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR: openssl is required" >&2
  exit 1
fi

mkdir -p "${secret_dir}" "${project_dir}/.runtime"
docker_gid="$(stat -c '%g' /var/run/docker.sock)"
# Managed/sandboxed shells can map the root-owned socket group to nogroup even
# though the Docker daemon and containers see the host's real docker GID.
if docker_group_entry="$(getent group docker 2>/dev/null)"; then
  IFS=: read -r _ _ docker_group_gid _ <<<"${docker_group_entry}"
  if [[ "${docker_group_gid}" =~ ^[0-9]+$ ]]; then
    docker_gid="${docker_group_gid}"
  fi
fi
secret_gid="$(id -g)"

if [[ ! -e "${env_file}" ]]; then
  cp "${env_example}" "${env_file}"
  echo "created ${env_file}"
else
  echo "kept existing ${env_file}" >&2
fi

set_numeric_env() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "${env_file}"; then
    sed -i "s/^${key}=.*/${key}=${value}/" "${env_file}"
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${env_file}"
  fi
}

set_numeric_env DOCKER_GID "${docker_gid}"
set_numeric_env PLATFORM_SECRET_GID "${secret_gid}"
chmod 0600 "${env_file}"
chmod 0750 "${secret_dir}"
# The host CLI and the Hub-managed local provisioner share one allocator
# registry. setgid keeps files created by either UID in the same private group;
# the API sees this directory through its existing read-only mount.
chgrp "${secret_gid}" "${project_dir}/.runtime"
chmod 2770 "${project_dir}/.runtime"
find "${project_dir}/.runtime" -maxdepth 1 -type f -user "$(id -u)" \
  -exec chgrp "${secret_gid}" {} + -exec chmod 0660 {} +

write_hex_secret() {
  local target="$1"
  if [[ ! -s "${target}" ]]; then
    openssl rand -hex 32 >"${target}"
    echo "created ${target}"
  fi
  # Distinct non-root container UIDs receive only this host group as a
  # supplemental group. Other host users cannot read local secrets.
  chmod 0440 "${target}"
}

write_urlsafe_key() {
  local target="$1"
  if [[ ! -s "${target}" ]]; then
    openssl rand -base64 32 | tr '+/' '-_' >"${target}"
    echo "created ${target}"
  fi
  chmod 0440 "${target}"
}

write_hex_secret "${secret_dir}/jupyterhub_cookie_secret"
write_hex_secret "${secret_dir}/configproxy_auth_token"
write_hex_secret "${secret_dir}/oauth_client_secret"
write_hex_secret "${secret_dir}/reconciler_token"
write_hex_secret "${secret_dir}/admin_lifecycle_token"
write_hex_secret "${secret_dir}/spawn_hmac_key"
write_hex_secret "${secret_dir}/session_hash_key"
write_urlsafe_key "${secret_dir}/token_encryption_key"

echo "local bootstrap files are ready"
