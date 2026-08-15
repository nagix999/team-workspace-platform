#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: $0 <platform-user-uuid> <hub-username>" >&2
  exit 2
fi

user_id="$1"
username="$2"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "${script_dir}/.." && pwd)"
manifest_name="local-user-${user_id}.json"
manifest_host="${project_dir}/.runtime/${manifest_name}"

cd "${project_dir}"
python3 infra/host/provision_local_dev_volumes.py \
  --profile-policy infra/jupyterhub/profiles.local-dev.json \
  --user-id "${user_id}" \
  --username "${username}" \
  --project-id-start "${PLATFORM_LOCAL_PROJECT_ID_START:-10000}" \
  --output "${manifest_host}" \
  --i-understand-this-is-unsafe-local-dev

docker compose run --rm --no-deps api \
  python -m app.admin provision-user \
  --username "${username}" \
  --manifest "/runtime/${manifest_name}"

echo "${username} is ACTIVE in the explicit local-development stack"
