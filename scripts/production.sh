#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "${script_dir}/.." && pwd)"
cd "${project_dir}"

readonly env_file="${project_dir}/.env.production"
readonly compose_file="${project_dir}/compose.production.yaml"
readonly docker27_compose_file="${project_dir}/compose.production.docker27.yaml"
readonly policy_file="${project_dir}/.runtime/production/profiles.json"
readonly candidate_policy_file="${project_dir}/.runtime/production/profiles.candidate.json"
readonly template_file="${project_dir}/infra/jupyterhub/profiles.local-dev.json"
readonly production_runtime_dir="${project_dir}/.runtime/production"
readonly backup_parent="${project_dir}/.runtime/production/backups"
readonly operator_lock_file="${project_dir}/.runtime/production/operator.lock"
compose_file_args=(-f "${compose_file}")
production_network_isolation_mode=isolated

die() { echo >&2 "production: $*"; exit 1; }

load_environment() {
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || die "run make production-init first"
  awk '
    $0 == "" || $0 ~ /^#/ { next }
    $0 !~ /^[A-Z][A-Z0-9_]*=[A-Za-z0-9_\.\/:@-]+$/ { exit 1 }
  ' "${env_file}" || die ".env.production contains an unsafe assignment"
  set -a
  # This operator-owned 0600 file is restricted to KEY=value assignments.
  # shellcheck disable=SC1090
  source "${env_file}"
  set +a
  : "${PRODUCTION_COMPOSE_PROJECT_NAME:?}"
  : "${PLATFORM_GATEWAY_BIND_IP:?}"
  : "${PLATFORM_TLS_CERT_FILE:?}"
  : "${PLATFORM_TLS_KEY_FILE:?}"
  : "${PLATFORM_INGRESS_CIDRS_FILE:?}"
  : "${PLATFORM_TLS_GID:?}"
}

compose() {
  docker compose --env-file "${env_file}" "${compose_file_args[@]}" "$@"
}

acquire_operator_lock() {
  mkdir -p -- "${production_runtime_dir}"
  command -v flock >/dev/null || die "flock is required for production operations"
  exec {production_operator_lock_fd}>"${operator_lock_file}"
  flock -n "${production_operator_lock_fd}" \
    || die "another production operation is already running"
}

require_regular_file() {
  local path="$1" label="$2"
  [[ "${path}" = /* && -f "${path}" && ! -L "${path}" && -r "${path}" ]] \
    || die "${label} must be one readable absolute regular non-symlink file: ${path}"
}

validate_docker_engine_contract() {
  local server_version server_major server_minor server_patch
  if ! server_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null)"; then
    die "could not query the Docker Server Engine version"
  fi
  if [[ ! "${server_version}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(\+[0-9A-Za-z][0-9A-Za-z._-]*|-[0-9][0-9A-Za-z._+~:-]*)?$ ]]; then
    die "Docker Server Engine returned an unrecognized version"
  fi
  server_major="${BASH_REMATCH[1]}"
  server_minor="${BASH_REMATCH[2]}"
  server_patch="${BASH_REMATCH[3]}"
  if ((
    10#${server_major} < 27
    || (
      10#${server_major} == 27
      && (
        10#${server_minor} < 1
        || (10#${server_minor} == 1 && 10#${server_patch} < 2)
      )
    )
  )); then
    die "Docker Server Engine 27.1.2 or newer is required by the production network contract (found ${server_version})"
  fi
  if ((
    10#${server_major} == 27
    && (
      10#${server_minor} < 5
      || (10#${server_minor} == 5 && 10#${server_patch} < 1)
    )
  )); then
    echo >&2 "production: WARNING: Docker Server Engine ${server_version} is supported, but upgrade to the final patched 27.5.1 release is strongly recommended"
  fi
  compose_file_args=(-f "${compose_file}")
  production_network_isolation_mode=isolated
  if ((10#${server_major} == 27)); then
    validate_docker_compose_override_contract
    compose_file_args+=(-f "${docker27_compose_file}")
    production_network_isolation_mode=inhibit-ipv4
  fi
}

validate_docker_compose_override_contract() {
  local compose_version compose_major compose_minor compose_patch
  if ! compose_version="$(docker compose version --short 2>/dev/null)"; then
    die "could not query the Docker Compose version required for Engine 27"
  fi
  if [[ ! "${compose_version}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(\+[0-9A-Za-z][0-9A-Za-z._-]*|-[0-9][0-9A-Za-z._+~:-]*)?$ ]]; then
    die "Docker Compose returned an unrecognized version"
  fi
  compose_major="${BASH_REMATCH[1]}"
  compose_minor="${BASH_REMATCH[2]}"
  compose_patch="${BASH_REMATCH[3]}"
  if ((
    10#${compose_major} < 2
    || (
      10#${compose_major} == 2
      && (
        10#${compose_minor} < 24
        || (10#${compose_minor} == 24 && 10#${compose_patch} < 4)
      )
    )
  )); then
    die "Docker Compose 2.24.4 or newer is required for the Engine 27 compatibility overlay (found ${compose_version})"
  fi
}

production_network_contract() {
  python3 scripts/validate_production_network.py "$@" \
    --network-name platform-jupyter-compose-production \
    --compose-project "${PRODUCTION_COMPOSE_PROJECT_NAME}" \
    --isolation-mode "${production_network_isolation_mode}" \
    --subnet 172.40.0.0/24 \
    --ip-range 172.40.0.128/25 \
    --required-endpoint 172.40.0.10 \
    --required-endpoint 172.40.0.20
}

validate_gateway_health() {
  curl --fail --silent --show-error \
    --connect-to platform.cyberailabs.team:443:"${PLATFORM_GATEWAY_BIND_IP}":3030 \
    https://platform.cyberailabs.team/healthz >/dev/null
  curl --fail --silent --show-error \
    --connect-to cyberailabs.team:443:"${PLATFORM_GATEWAY_BIND_IP}":3030 \
    https://cyberailabs.team/healthz >/dev/null
}

stop_gateway_fail_closed() {
  local running
  compose stop gateway >/dev/null 2>&1 || return 1
  running="$(compose ps --status running -q gateway 2>/dev/null)" || return 1
  [[ -z "${running}" ]]
}

stop_gateway_or_die() {
  stop_gateway_fail_closed \
    || die "could not stop gateway or verify that its public listener is closed"
}

start_gateway_checked() {
  if ! compose up -d --no-deps --wait "$@" gateway; then
    if stop_gateway_fail_closed; then
      echo >&2 "production: gateway startup failed; gateway was stopped"
    else
      echo >&2 "production: CRITICAL: gateway startup failed and its stopped state could not be verified"
    fi
    return 1
  fi
  if ! validate_gateway_health; then
    if stop_gateway_fail_closed; then
      echo >&2 "production: gateway health validation failed; gateway was stopped"
    else
      echo >&2 "production: CRITICAL: gateway health validation failed and its stopped state could not be verified"
    fi
    return 1
  fi
}

require_running_control_plane() {
  local service container_id
  for service in api frontend egress-proxy jupyterhub; do
    container_id="$(compose ps --status running -q "${service}")"
    [[ "${container_id}" =~ ^[0-9a-f]{12,64}$ ]] \
      || die "production service ${service} must be running before recreating gateway"
  done
}

restart_account_control_plane_checked() {
  if ! compose up -d --wait jupyterhub reconciler worker >/dev/null; then
    echo >&2 "production: account-administration control-plane restart failed"
    stop_gateway_fail_closed \
      || echo >&2 "production: CRITICAL: gateway stopped state could not be verified"
    return 1
  fi
  if ! production_network_contract validate \
    --probe-image team-workspace-backend:production
  then
    echo >&2 "production: account-administration network validation failed"
    stop_gateway_fail_closed \
      || echo >&2 "production: CRITICAL: gateway stopped state could not be verified"
    return 1
  fi
  start_gateway_checked
}

validate_host_contract() {
  local certificate_sans cert_key file_key key_mode san
  validate_docker_engine_contract
  [[ "${PLATFORM_GATEWAY_BIND_IP}" == "10.155.1.24" ]] \
    || die "PLATFORM_GATEWAY_BIND_IP must remain 10.155.1.24 for the reviewed VIP contract"
  ip -4 -o address show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "${PLATFORM_GATEWAY_BIND_IP}" \
    || die "this host does not own ${PLATFORM_GATEWAY_BIND_IP}; deploy on the production server"
  require_regular_file "${PLATFORM_TLS_CERT_FILE}" "TLS fullchain"
  require_regular_file "${PLATFORM_TLS_KEY_FILE}" "TLS private key"
  require_regular_file "${PLATFORM_INGRESS_CIDRS_FILE}" "ingress CIDR allowlist"
  key_mode="$(stat -c '%a' "${PLATFORM_TLS_KEY_FILE}")"
  case "${key_mode}" in 400|440|600|640) ;; *) die "TLS key mode must be 0400, 0440, 0600 or 0640" ;; esac
  [[ "$(stat -c '%g' "${PLATFORM_TLS_KEY_FILE}")" == "${PLATFORM_TLS_GID}" ]] \
    || die "PLATFORM_TLS_GID does not match the TLS key group"
  openssl x509 -in "${PLATFORM_TLS_CERT_FILE}" -noout -checkend 86400 >/dev/null \
    || die "TLS certificate is invalid, expired, or expires within 24 hours"
  cert_key="$(openssl x509 -in "${PLATFORM_TLS_CERT_FILE}" -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | openssl dgst -sha256)"
  file_key="$(openssl pkey -in "${PLATFORM_TLS_KEY_FILE}" -passin pass: -pubout -outform DER 2>/dev/null | openssl dgst -sha256)"
  [[ -n "${cert_key}" && "${cert_key}" == "${file_key}" ]] || die "TLS certificate and key do not match"
  openssl x509 -in "${PLATFORM_TLS_CERT_FILE}" -noout \
    -checkhost platform.cyberailabs.team >/dev/null \
    || die "TLS certificate does not cover portal host platform.cyberailabs.team"
  certificate_sans="$(
    openssl x509 -in "${PLATFORM_TLS_CERT_FILE}" -noout -ext subjectAltName \
      | sed '1d' | tr -d '[:space:]'
  )" || die "TLS certificate SAN extension could not be read"
  for san in 'DNS:cyberailabs.team' 'DNS:*.cyberailabs.team'; do
    tr ',' '\n' <<<"${certificate_sans}" | grep -Fxq "${san}" \
      || die "TLS certificate is missing SAN ${san}"
  done
  awk '
    function invalid() { exit 1 }
    { sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, "") }
    $0 == "" || $0 ~ /^#/ { next }
    $0 !~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\/([0-9]|[12][0-9]|3[0-2])$/ { invalid() }
    { split($0, item, "/"); if (item[2] == 0) invalid(); count++ }
    END { if (count < 1) invalid() }
  ' "${PLATFORM_INGRESS_CIDRS_FILE}" || die "ingress CIDR file is empty or invalid"
}

require_idle() {
  active="$(docker ps --filter label=platform.kind=jupyter-singleuser --format '{{.Names}}')"
  [[ -z "${active}" ]] || {
    echo >&2 "${active}"
    die "stop every workspace before production deployment"
  }
  foreign="$(docker ps --filter label=com.docker.compose.project=team-platform-local --format '{{.Label "com.docker.compose.project"}} {{.Names}}')"
  [[ -z "${foreign}" ]] || {
    echo >&2 "${foreign}"
    die "another Compose stack is running; stop the local/domain-test stack before production deployment"
  }
}

require_offline_recovery_state() {
  local production_containers singleuser_containers volume_name volume_users
  production_containers="$(docker ps --all \
    --filter "label=com.docker.compose.project=${PRODUCTION_COMPOSE_PROJECT_NAME}" \
    --format '{{.ID}} {{.Names}}')"
  singleuser_containers="$(docker ps --all \
    --filter label=platform.kind=jupyter-singleuser \
    --format '{{.ID}} {{.Names}}')"
  if [[ -n "${production_containers}" ]]; then
    echo >&2 "${production_containers}"
    die "offline recovery requires every production Compose container to be absent"
  fi
  if [[ -n "${singleuser_containers}" ]]; then
    echo >&2 "${singleuser_containers}"
    die "offline recovery requires every single-user container to be absent"
  fi
  [[ "$(database_volume_count)" == 2 ]] \
    || die "offline recovery requires the exact Platform and JupyterHub database volume pair"
  while read -r volume_name; do
    volume_users="$(docker ps --all \
      --filter "volume=${volume_name}" \
      --format '{{.ID}} {{.Names}}')"
    if [[ -n "${volume_users}" ]]; then
      echo >&2 "${volume_users}"
      die "offline recovery requires no container to mount database volume ${volume_name}"
    fi
  done < <(database_volume_names)
}

prepare_images_and_policy() {
  compose build singleuser-image
  image_id="$(docker image inspect team-workspace-singleuser:production-current --format '{{.Id}}')"
  [[ "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] || die "single-user build did not produce an exact image ID"
  args=(
    --template "${template_file}"
    --image-id "${image_id}"
    --shared-volume jupyter-shared
    --output "${candidate_policy_file}"
  )
  [[ ! -s "${policy_file}" ]] || args+=(--previous "${policy_file}")
  python3 infra/jupyterhub/generate_production_profile_policy.py "${args[@]}"
  while read -r retained_id; do
    docker image inspect "${retained_id}" >/dev/null \
      || die "retained production profile image is missing locally: ${retained_id}"
    docker tag "${retained_id}" "team-workspace-singleuser:retained-${retained_id#sha256:}"
  done < <(python3 -c 'import json,sys; print("\n".join(sorted({p["image"] for p in json.load(open(sys.argv[1]))["profiles"]})))' "${candidate_policy_file}")
  python3 infra/jupyterhub/profile_image_check.py --policy "${candidate_policy_file}"
  compose build api jupyterhub frontend egress-proxy gateway
  compose config --quiet
}

database_volume_names() {
  printf '%s\n' \
    "${PRODUCTION_COMPOSE_PROJECT_NAME}_platform_data" \
    "${PRODUCTION_COMPOSE_PROJECT_NAME}_jupyterhub_data"
}

database_volume_count() {
  local present=0 volume_name
  while read -r volume_name; do
    docker volume inspect "${volume_name}" >/dev/null 2>&1 && present=$((present + 1))
  done < <(database_volume_names)
  printf '%s\n' "${present}"
}

validate_database_inventory() {
  local present
  present="$(database_volume_count)"
  [[ "${present}" != 1 ]] \
    || die "only one production database volume exists; refusing split-brain startup"
  if [[ "${present}" == 2 ]]; then
    [[ -f "${policy_file}" && ! -L "${policy_file}" && -s "${policy_file}" ]] \
      || die "existing production databases require the retained profile policy"
  fi
}

snapshot_one() {
  local volume_name="$1" source_name="$2" output_name="$3" bundle="$4"
  docker run --rm \
    --network none \
    --read-only \
    --user 0:0 \
    --cap-drop ALL \
    --cap-add CHOWN \
    --cap-add DAC_OVERRIDE \
    --security-opt no-new-privileges:true \
    --volume "${volume_name}:/source:ro" \
    --volume "${bundle}:/snapshot" \
    --volume "${project_dir}/scripts/domain_test_database_snapshot.py:/snapshot-tool.py:ro" \
    team-workspace-backend:production \
    python /snapshot-tool.py snapshot \
      --source "/source/${source_name}" \
      --output "/snapshot/${output_name}" \
      --owner-uid "$(id -u)" \
      --owner-gid "$(id -g)"
}

production_database_is_idle() {
  local present platform_volume check_dir snapshot_file status=0
  present="$(database_volume_count)"
  [[ "${present}" == 0 ]] && return 0
  [[ "${present}" == 2 ]] || {
    echo >&2 "production: cannot check an incomplete database volume pair"
    return 1
  }

  platform_volume="${PRODUCTION_COMPOSE_PROJECT_NAME}_platform_data"
  check_dir="$(mktemp -d "${production_runtime_dir}/idle-check.XXXXXX")" || {
    echo >&2 "production: could not create the private idle-check directory"
    return 1
  }
  snapshot_file="${check_dir}/platform.sqlite"
  if ! snapshot_one \
    "${platform_volume}" platform.db platform.sqlite "${check_dir}" >/dev/null
  then
    echo >&2 "production: could not snapshot the Platform DB for the idle check"
    status=1
  elif ! python3 scripts/check-domain-test-idle.py \
    --context production \
    --database "${snapshot_file}"
  then
    status=1
  fi
  rm -f -- "${snapshot_file}"
  rmdir -- "${check_dir}" 2>/dev/null || true
  return "${status}"
}

snapshot_existing_databases() {
  local platform_volume hub_volume present=0 bundle_name bundle
  readarray -t volumes < <(database_volume_names)
  platform_volume="${volumes[0]}"
  hub_volume="${volumes[1]}"
  docker volume inspect "${platform_volume}" >/dev/null 2>&1 && present=$((present + 1))
  docker volume inspect "${hub_volume}" >/dev/null 2>&1 && present=$((present + 1))
  if [[ "${present}" == 0 ]]; then
    PRODUCTION_FRESH_DATABASES=true
    echo "production: new database volumes will be initialized"
    return 0
  fi
  PRODUCTION_FRESH_DATABASES=false
  [[ "${present}" == 2 ]] || die "only one production database volume exists; refusing split-brain startup"
  bundle_name="production-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  bundle="$(python3 scripts/domain_test_database_snapshot.py prepare --parent "${backup_parent}" --name "${bundle_name}")"
  snapshot_one "${platform_volume}" platform.db platform.sqlite "${bundle}"
  snapshot_one "${hub_volume}" jupyterhub.sqlite jupyterhub.sqlite "${bundle}"
  python3 scripts/domain_test_database_snapshot.py finalize --bundle "${bundle}" >/dev/null
  echo "production database backup: ${bundle}"
  PRODUCTION_LAST_BACKUP="${bundle}"
}

image_identity() {
  local image="$1" identity
  identity="$(docker run --rm \
    --network none \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --entrypoint /bin/sh \
    "${image}" \
    -ceu 'printf "%s:%s\n" "$(id -u)" "$(id -g)"')"
  [[ "${identity}" =~ ^[0-9]+:[0-9]+$ ]] \
    || die "could not resolve the runtime UID/GID for ${image}"
  printf '%s\n' "${identity}"
}

preflight_impl() {
  load_environment
  validate_host_contract
  require_idle
  compose config --quiet
  production_network_contract prepare
  validate_database_inventory
  prepare_images_and_policy
  production_database_is_idle \
    || die "finish every lifecycle/provisioning/deletion job before deployment"
  echo "production preflight passed"
}

preflight() {
  acquire_operator_lock
  preflight_impl
}

start() {
  local policy_existed_before=false old_gateway_running=false service old_running_output
  local -a old_running_services=() old_internal_services=()
  acquire_operator_lock
  preflight_impl
  [[ ! -e "${policy_file}" ]] || policy_existed_before=true
  old_running_output="$(compose ps --status running --services)" \
    || die "could not inventory the original production services"
  if [[ -n "${old_running_output}" ]]; then
    mapfile -t old_running_services <<<"${old_running_output}"
  fi
  for service in "${old_running_services[@]}"; do
    if [[ "${service}" == gateway ]]; then
      old_gateway_running=true
    else
      old_internal_services+=("${service}")
    fi
  done
  restart_original_services() {
    if [[ "${#old_internal_services[@]}" -gt 0 ]]; then
      compose up -d --no-deps --no-recreate --wait \
        "${old_internal_services[@]}" >/dev/null || return 1
    fi
    if [[ "${old_gateway_running}" == true ]]; then
      production_network_contract validate \
        --probe-image team-workspace-backend:production || return 1
      start_gateway_checked --no-recreate || return 1
    fi
  }
  report_pre_mutation_failure() {
    local reason="$1"
    if restart_original_services; then
      die "${reason}; original services were restarted"
    fi
    stop_gateway_fail_closed \
      || echo >&2 "production: CRITICAL: gateway stopped state could not be verified"
    die "${reason}; safe restart failed and gateway was not reopened"
  }
  stop_gateway_or_die
  if ! compose stop gateway worker reconciler api jupyterhub frontend egress-proxy >/dev/null; then
    report_pre_mutation_failure "could not stop the original control plane"
  fi
  if ! production_database_is_idle; then
    report_pre_mutation_failure \
      "database became busy during the maintenance transition"
  fi
  if ! snapshot_existing_databases; then
    report_pre_mutation_failure "database snapshot failed"
  fi
  migration_started=false
  restore_on_failure() {
    status=$?
    trap - EXIT
    if [[ "${migration_started}" == true ]] && ! stop_gateway_fail_closed; then
      echo >&2 "production: CRITICAL: failed deployment did not prove that gateway is stopped"
    fi
    if [[ "${migration_started}" == false ]]; then
      restart_original_services \
        || echo >&2 "production: original services could not be restarted safely"
    elif [[ "${PRODUCTION_FRESH_DATABASES:-false}" == true ]]; then
      compose down >/dev/null 2>&1 || true
      while read -r fresh_volume; do
        docker volume rm "${fresh_volume}" >/dev/null 2>&1 || true
      done < <(database_volume_names)
      if [[ "${policy_existed_before}" == false ]]; then
        rm -f -- "${policy_file}"
      fi
      echo >&2 "fresh production database volumes were rolled back"
    else
      echo >&2 "production deployment stopped after database mutation"
      [[ -z "${PRODUCTION_LAST_BACKUP:-}" ]] || echo >&2 "restore with: BACKUP=${PRODUCTION_LAST_BACKUP} make production-restore"
    fi
    exit "${status}"
  }
  trap restore_on_failure EXIT
  migration_started=true
  python3 infra/jupyterhub/generate_production_profile_policy.py \
    --promote "${candidate_policy_file}" \
    --output "${policy_file}"
  compose run --rm --no-deps migrate
  compose run --rm --no-deps bootstrap-profile
  compose up -d --build --wait \
    api worker reconciler frontend egress-proxy jupyterhub
  production_network_contract validate \
    --probe-image team-workspace-backend:production
  start_gateway_checked || die "gateway did not start safely"
  compose ps
  echo "production control plane is healthy on ${PLATFORM_GATEWAY_BIND_IP}:3030"
  trap - EXIT
}

stop() {
  acquire_operator_lock
  load_environment
  validate_docker_engine_contract
  require_idle
  compose down
}

show_status() {
  load_environment
  validate_docker_engine_contract
  compose ps
}

logs() {
  load_environment
  validate_docker_engine_contract
  compose logs -f --tail=200
}

recreate_gateway() {
  recreate_gateway_failure() {
    local status=$?
    trap - EXIT
    if ! stop_gateway_fail_closed; then
      echo >&2 "production: CRITICAL: gateway recreation failure did not prove that gateway is stopped"
    fi
    exit "${status}"
  }
  acquire_operator_lock
  load_environment
  validate_host_contract
  trap recreate_gateway_failure EXIT
  stop_gateway_or_die
  require_running_control_plane
  production_network_contract validate \
    --probe-image team-workspace-backend:production
  start_gateway_checked --force-recreate \
    || die "gateway did not recreate safely"
  trap - EXIT
  echo "production gateway was recreated and is healthy"
}

create_user() {
  local require_empty_flag=()
  acquire_operator_lock
  load_environment
  validate_host_contract
  require_idle
  validate_database_inventory
  if [[ "${PRODUCTION_REQUIRE_EMPTY:-false}" == "true" && -z "${PRODUCTION_TARGET_USERNAME:-}" ]]; then
    PRODUCTION_TARGET_USERNAME="${PLATFORM_ADMIN_USERNAME}"
  fi
  : "${PRODUCTION_TARGET_USERNAME:?Set the exact production username}"
  [[ "${PRODUCTION_TARGET_USERNAME}" =~ ^[a-z]([a-z0-9-]{0,30}[a-z0-9])?$ ]] \
    && [[ "${PRODUCTION_TARGET_USERNAME}" != *--* ]] \
    || die "PRODUCTION_TARGET_USERNAME is invalid"
  [[ "${PRODUCTION_REQUIRE_EMPTY:-false}" == "true" || "${PRODUCTION_REQUIRE_EMPTY:-false}" == "false" ]] \
    || die "PRODUCTION_REQUIRE_EMPTY must be true or false"
  [[ -n "$(compose ps --status running -q jupyterhub)" ]] \
    || die "JupyterHub is not running; run make production-up first"
  [[ "${PRODUCTION_REQUIRE_EMPTY:-false}" != "true" || "${PRODUCTION_TARGET_USERNAME}" == "${PLATFORM_ADMIN_USERNAME}" ]] \
    || die "only the configured admin can be the initial account"
  production_network_contract validate \
    --probe-image team-workspace-backend:production

  stop_gateway_or_die
  restart_after_user_admin() {
    status=$?
    trap - EXIT
    restart_account_control_plane_checked \
      || echo >&2 "production: control-plane restart after account administration failed"
    exit "${status}"
  }
  trap restart_after_user_admin EXIT
  compose stop worker reconciler jupyterhub >/dev/null \
    || die "could not stop the account-administration control plane"
  snapshot_existing_databases \
    || die "database snapshot failed before account administration"
  [[ "${PRODUCTION_REQUIRE_EMPTY:-false}" != "true" ]] || require_empty_flag+=(--require-empty)
  compose --profile operator run --rm --no-deps native-user-admin \
    --database /srv/jupyterhub/jupyterhub.sqlite \
    --username "${PRODUCTION_TARGET_USERNAME}" \
    --admin-username "${PLATFORM_ADMIN_USERNAME}" \
    "${require_empty_flag[@]}"
  trap - EXIT
  restart_account_control_plane_checked \
    || die "control-plane restart after account administration failed"
  echo "production account is ready: ${PRODUCTION_TARGET_USERNAME}"
}

offline_quiesce() {
  local mode="$1" bundle_basename
  acquire_operator_lock
  load_environment
  validate_docker_engine_contract
  [[ "${mode}" == "dry-run" || "${mode}" == "apply" ]] \
    || die "offline quiesce mode must be dry-run or apply"
  if [[ "${mode}" == "apply" ]]; then
    : "${PRODUCTION_EXPECTED_COUNT:?Set EXPECTED_COUNT to the exact dry-run count}"
    [[ "${PRODUCTION_EXPECTED_COUNT}" =~ ^[1-9][0-9]*$ ]] \
      || die "EXPECTED_COUNT must be a positive integer"
  fi

  require_offline_recovery_state
  compose --profile operator build offline-maintenance
  require_offline_recovery_state

  if [[ "${mode}" == "dry-run" ]]; then
    compose --profile operator run --rm --no-deps offline-maintenance \
      quiesce-stopped-intent
    echo "production offline quiesce dry-run completed; no database row was changed"
    return 0
  fi

  snapshot_existing_databases
  [[ "${PRODUCTION_FRESH_DATABASES}" == false && -n "${PRODUCTION_LAST_BACKUP:-}" ]] \
    || die "offline recovery did not produce a database backup"
  python3 scripts/domain_test_database_snapshot.py verify \
    --bundle "${PRODUCTION_LAST_BACKUP}" >/dev/null
  bundle_basename="$(basename -- "${PRODUCTION_LAST_BACKUP}")"

  compose --profile operator run --rm --no-deps offline-maintenance \
    quiesce-stopped-intent
  require_offline_recovery_state
  if ! compose --profile operator run --rm --no-deps offline-maintenance \
    quiesce-stopped-intent \
    --apply \
    --expected-count "${PRODUCTION_EXPECTED_COUNT}" \
    --backup-bundle-id "${bundle_basename}"
  then
    die "offline quiesce failed; inspect the transaction and preserve backup ${PRODUCTION_LAST_BACKUP}"
  fi
  production_database_is_idle \
    || die "offline quiesce did not reach an idle database; preserve backup ${PRODUCTION_LAST_BACKUP}"
  echo "production offline quiesce completed; database backup: ${PRODUCTION_LAST_BACKUP}"
  echo "run make production-preflight and make production-up next"
}

restore() {
  local platform_identity hub_identity
  acquire_operator_lock
  load_environment
  validate_docker_engine_contract
  : "${PRODUCTION_BACKUP_DIR:?Set PRODUCTION_BACKUP_DIR to a verified backup bundle}"
  require_idle
  python3 scripts/domain_test_database_snapshot.py verify --bundle "${PRODUCTION_BACKUP_DIR}" >/dev/null
  compose down
  platform_identity="$(image_identity team-workspace-backend:production)"
  hub_identity="$(image_identity team-workspace-jupyterhub:production)"
  readarray -t volumes < <(database_volume_names)
  for item in \
    "platform.sqlite:${volumes[0]}:platform.db:${platform_identity}" \
    "jupyterhub.sqlite:${volumes[1]}:jupyterhub.sqlite:${hub_identity}"
  do
    IFS=: read -r input volume destination owner_uid owner_gid <<<"${item}"
    digest="$(python3 scripts/domain_test_database_snapshot.py digest --bundle "${PRODUCTION_BACKUP_DIR}" --filename "${input}")"
    docker run --rm \
      --network none \
      --read-only \
      --user 0:0 \
      --cap-drop ALL \
      --cap-add CHOWN \
      --cap-add DAC_OVERRIDE \
      --security-opt no-new-privileges:true \
      --env "PLATFORM_RESTORE_EXPECTED_SHA256=${digest}" \
      --volume "${volume}:/source" \
      --volume "${PRODUCTION_BACKUP_DIR}/${input}:/restore/input.sqlite:ro" \
      --volume "${project_dir}/scripts/domain_test_database_snapshot.py:/snapshot-tool.py:ro" \
      team-workspace-backend:production \
      python /snapshot-tool.py restore \
        --source /restore/input.sqlite \
        --destination "/source/${destination}" \
        --owner-uid "${owner_uid}" \
        --owner-gid "${owner_gid}"
  done
  echo "production databases restored; run make production-up after reviewing the failed rollout"
}

case "${1:-}" in
  preflight) preflight ;;
  up) start ;;
  down) stop ;;
  ps) show_status ;;
  logs) logs ;;
  recreate-gateway) recreate_gateway ;;
  create-user) create_user ;;
  offline-quiesce-dry-run) offline_quiesce dry-run ;;
  offline-quiesce) offline_quiesce apply ;;
  restore) restore ;;
  *) die "usage: scripts/production.sh preflight|up|down|ps|logs|recreate-gateway|create-user|offline-quiesce-dry-run|offline-quiesce|restore" ;;
esac
