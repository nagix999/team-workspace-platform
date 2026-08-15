#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "${script_dir}/.." && pwd)"
secret_dir="${project_dir}/secrets/production"
runtime_dir="${project_dir}/.runtime/production"
backup_dir="${runtime_dir}/backups"
env_file="${project_dir}/.env.production"
env_example="${project_dir}/.env.production.example"

[[ -S /var/run/docker.sock ]] || { echo >&2 "ERROR: Docker socket is unavailable"; exit 1; }
command -v openssl >/dev/null || { echo >&2 "ERROR: openssl is required"; exit 1; }

mkdir -p "${secret_dir}" "${runtime_dir}" "${backup_dir}"
secret_gid="$(id -g)"
docker_gid="$(stat -c '%g' /var/run/docker.sock)"
if docker_group_entry="$(getent group docker 2>/dev/null)"; then
  IFS=: read -r _ _ docker_group_gid _ <<<"${docker_group_entry}"
  [[ "${docker_group_gid}" =~ ^[0-9]+$ ]] && docker_gid="${docker_group_gid}"
fi

chmod 0750 "${secret_dir}"
chgrp "${secret_gid}" "${runtime_dir}"
chmod 2770 "${runtime_dir}"
chmod 0700 "${backup_dir}"

if [[ ! -e "${env_file}" ]]; then
  cp "${env_example}" "${env_file}"
  chmod 0600 "${env_file}"
  echo "created ${env_file}"
else
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || {
    echo >&2 "ERROR: .env.production must be a regular non-symlink file"
    exit 1
  }
  chmod 0600 "${env_file}"
fi

set_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "${env_file}"; then
    sed -i "s/^${key}=.*/${key}=${value}/" "${env_file}"
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${env_file}"
  fi
}
set_env DOCKER_GID "${docker_gid}"
set_env PLATFORM_SECRET_GID "${secret_gid}"

write_hex_secret() {
  local target="$1"
  if [[ ! -s "${target}" ]]; then
    openssl rand -hex 32 >"${target}"
    echo "created ${target}"
  fi
  [[ -f "${target}" && ! -L "${target}" ]] || {
    echo >&2 "ERROR: unsafe production secret path: ${target}"
    exit 1
  }
  chmod 0440 "${target}"
}

write_urlsafe_secret() {
  local target="$1"
  if [[ ! -s "${target}" ]]; then
    openssl rand -base64 32 | tr '+/' '-_' >"${target}"
    echo "created ${target}"
  fi
  [[ -f "${target}" && ! -L "${target}" ]] || {
    echo >&2 "ERROR: unsafe production secret path: ${target}"
    exit 1
  }
  chmod 0440 "${target}"
}

for name in \
  jupyterhub_cookie_secret configproxy_auth_token oauth_client_secret \
  reconciler_token admin_lifecycle_token spawn_hmac_key session_hash_key
do
  write_hex_secret "${secret_dir}/${name}"
done
write_urlsafe_secret "${secret_dir}/token_encryption_key"

echo "production secret/runtime directories are ready"
echo "review ${env_file}, install the public certificate, and create the CIDR file before preflight"
