#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$PROJECT_DIR"

: "${DOMAIN_TEST_CA_CERT_FILE:=${PROJECT_DIR}/secrets/domain-test/ca.crt}"
: "${DOMAIN_TEST_TLS_CERT_FILE:=${PROJECT_DIR}/secrets/domain-test/tls.crt}"
: "${DOMAIN_TEST_TLS_KEY_FILE:=${PROJECT_DIR}/secrets/domain-test/tls.key}"
: "${DOMAIN_TEST_TLS_GID:=$(id -g)}"
export DOMAIN_TEST_CA_CERT_FILE
export DOMAIN_TEST_TLS_CERT_FILE
export DOMAIN_TEST_TLS_KEY_FILE
export DOMAIN_TEST_TLS_GID

for tls_file in "$DOMAIN_TEST_CA_CERT_FILE" "$DOMAIN_TEST_TLS_CERT_FILE" "$DOMAIN_TEST_TLS_KEY_FILE"; do
  if [ ! -f "$tls_file" ]; then
    echo "domain-test requires TLS files: ${tls_file} is missing" >&2
    echo "Run: bash scripts/init-domain-test-tls.sh" >&2
    exit 1
  fi
done

readonly default_users="${DOMAIN_TEST_USERS:-platform-admin}"
readonly project_name="${COMPOSE_PROJECT_NAME:-team-platform-local}"
readonly backup_parent="${DOMAIN_TEST_BACKUP_DIR_PARENT:-.runtime/backups}"
readonly long_running_static_services=(
  gateway
  worker
  reconciler
  api
  jupyterhub
  frontend
  egress-proxy
)

BACKUP_DIR=""
transition_is_destructive=false
original_stack_was_running=false

print_help() {
  cat <<'USAGE'
Usage: scripts/domain-test.sh [preflight|up|down|restore]
Environment:
  DOMAIN_TEST_USERS       comma-separated usernames (default: platform-admin)
  DOMAIN_TEST_BACKUP_DIR  verified backup bundle path for restore
USAGE
}

compose_base() {
  docker compose -f compose.yaml "$@"
}

compose_domain() {
  docker compose -f compose.yaml -f compose.domain-test.yaml "$@"
}

container_is_running() {
  local container_id=$1
  [ "$(docker inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null || true)" = "true" ]
}

any_long_running_static_service_up() {
  local service
  local container_id
  for service in "${long_running_static_services[@]}"; do
    container_id="$(compose_base ps -q "$service" 2>/dev/null || true)"
    if [ -n "$container_id" ] && container_is_running "$container_id"; then
      return 0
    fi
  done
  return 1
}

stop_long_running_static_services() {
  compose_base stop "${long_running_static_services[@]}"
}

await_long_running_services_stopped() {
  local service
  local container_id
  local attempt
  for attempt in {1..30}; do
    for service in "${long_running_static_services[@]}"; do
      container_id="$(compose_base ps -q "$service" 2>/dev/null || true)"
      if [ -n "$container_id" ] && container_is_running "$container_id"; then
        break
      fi
      container_id=""
    done
    if [ -z "$container_id" ]; then
      return 0
    fi
    sleep 1
  done
  echo "domain-test: static services did not stop within 30 seconds" >&2
  return 1
}

start_local_stack() {
  compose_base up -d --build
}

managed_domain_test_listener() {
  local container_id
  local service
  local mode
  local binding

  container_id="$(compose_domain ps -q gateway 2>/dev/null || true)"
  [ -n "$container_id" ] || return 1
  container_is_running "$container_id" || return 1
  service="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$container_id" 2>/dev/null || true)"
  mode="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container_id" 2>/dev/null | awk -F= '$1 == "PLATFORM_GATEWAY_MODE" {print $2}')"
  binding="$(docker port "$container_id" 3030/tcp 2>/dev/null || true)"
  [ "$service" = "gateway" ] && [ "$mode" = "domain-test" ] && [ "$binding" = "127.0.0.1:443" ]
}

run_idle_check() {
  compose_base --profile maintenance run --rm --no-deps \
    --entrypoint python3 \
    migration-preflight \
    /opt/platform-maintenance/check-domain-test-idle.py \
    --database /source/platform.db
}

assert_no_active_singleuser() {
  local active
  active="$(docker ps -q --filter label=platform.kind=jupyter-singleuser)"
  if [ -n "$active" ]; then
    echo "domain-test preflight blocked: active Jupyter single-user container: $active" >&2
    return 1
  fi
}

run_migration_rehearsal() {
  compose_base --profile maintenance run --rm --no-deps \
    --entrypoint python3 \
    migration-preflight \
    /opt/platform/backend/preflight_profile_import.py \
    --policy /etc/platform/profiles.local-dev.json \
    --source /source/platform.db \
    --allow-fresh
}

validate_gateway_image() {
  docker run --rm --network none \
    --group-add "$DOMAIN_TEST_TLS_GID" \
    --add-host api:127.0.0.1 \
    --add-host frontend:127.0.0.1 \
    --add-host jupyterhub:127.0.0.1 \
    --env PLATFORM_GATEWAY_MODE=domain-test \
    --env PLATFORM_TLS_MIN_VALIDITY_SECONDS=3600 \
    --volume "${DOMAIN_TEST_TLS_CERT_FILE}:/run/platform-tls/tls.crt:ro" \
    --volume "${DOMAIN_TEST_TLS_KEY_FILE}:/run/platform-tls/tls.key:ro" \
    team-platform-gateway:domain-test \
    nginx -t
}

preflight() {
  local users="${1:-$default_users}"
  local -a port_guard=(--check-port-free)
  local -a user_args=()
  local -a host_args=()
  local user

  if managed_domain_test_listener; then
    port_guard=()
  fi

  compose_base config --quiet
  compose_domain config --quiet
  assert_no_active_singleuser
  run_idle_check

  [ -n "$users" ] || users="platform-admin"
  IFS=',' read -r -a user_args <<<"$users"
  for user in "${user_args[@]}"; do
    user="${user// /}"
    [ -n "$user" ] && host_args+=(--user "$user")
  done
  if [ "${DOMAIN_TEST_SKIP_PORT_CHECK:-0}" != "1" ] && [ "${#port_guard[@]}" -gt 0 ]; then
    host_args+=("${port_guard[@]}")
  fi
  DOMAIN_TEST_USERS="$users" python3 gateway/check_domain_test.py "${host_args[@]}"

  run_migration_rehearsal

  # Builds never attach containers to the live edge network. This is essential:
  # the domain overlay changes that network's IPAM and may only replace it after
  # the verified database snapshots have been created and compose down runs.
  compose_domain build singleuser-image api jupyterhub frontend gateway egress-proxy
  compose_base run --rm --no-deps singleuser-image
  validate_gateway_image

  echo "domain-test preflight passed"
}

snapshot_service() {
  local service=$1
  local output_file=$2
  local source_file=$3
  local snapshot_dir
  local snapshot_name
  local operator_uid
  local operator_gid

  if [ -e "$output_file" ]; then
    echo "snapshot output already exists: $output_file" >&2
    return 1
  fi

  snapshot_dir="$(cd "$(dirname "$output_file")" && pwd -P)"
  snapshot_name="$(basename "$output_file")"
  operator_uid="$(id -u)"
  operator_gid="$(id -g)"

  # The output is written directly to the verified private host directory.
  # docker cp cannot reliably archive tmpfs/volume-backed paths. The helper is
  # networkless, reads only the exact source volume, and chowns the resulting
  # regular file back to the invoking operator before it exits.
  if ! compose_base --profile maintenance run --rm --no-deps \
    --user 0:0 \
    --cap-add DAC_OVERRIDE \
    --cap-add CHOWN \
    --volume "${snapshot_dir}:/snapshot" \
    --entrypoint python3 \
    "$service" \
    /opt/platform-maintenance/domain_test_database_snapshot.py \
    snapshot \
    --source "$source_file" \
    --output "/snapshot/$snapshot_name" \
    --owner-uid "$operator_uid" \
    --owner-gid "$operator_gid"; then
    echo "snapshot container failed: service=$service source=$source_file" >&2
    return 1
  fi

  [ -s "$output_file" ] || {
    echo "snapshot output is missing or empty: $output_file" >&2
    return 1
  }
}

create_database_backup() {
  local backup_timestamp
  local snapshot_dir
  local platform_snapshot
  local jupyterhub_snapshot

  backup_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  if ! snapshot_dir="$(python3 scripts/domain_test_database_snapshot.py prepare \
    --parent "$backup_parent" --name "domain-test-${backup_timestamp}")"; then
    echo "database snapshot: backup preparation failed" >&2
    return 1
  fi
  snapshot_dir="$(printf '%s\n' "$snapshot_dir" | tail -n 1 | tr -d '\r')"
  BACKUP_DIR="$snapshot_dir"
  platform_snapshot="${snapshot_dir}/platform.sqlite"
  jupyterhub_snapshot="${snapshot_dir}/jupyterhub.sqlite"

  if ! snapshot_service snapshot-platform-database "$platform_snapshot" /source/platform.db \
    || ! snapshot_service snapshot-jupyterhub-database "$jupyterhub_snapshot" /source/jupyterhub.sqlite; then
    echo "database snapshot failed; incomplete bundle retained for diagnostics: $snapshot_dir" >&2
    return 1
  fi

  if ! python3 scripts/domain_test_database_snapshot.py finalize --bundle "$snapshot_dir" \
    || ! python3 scripts/domain_test_database_snapshot.py verify --bundle "$snapshot_dir"; then
    echo "database snapshot validation failed; bundle retained: $snapshot_dir" >&2
    return 1
  fi

  echo "database snapshot verified: $snapshot_dir"
}

wait_for_service() {
  local service=$1
  local desired=$2
  local container_id
  local state
  local health
  local attempt

  for attempt in {1..150}; do
    container_id="$(compose_domain ps -q "$service" 2>/dev/null || true)"
    if [ -n "$container_id" ]; then
      state="$(docker inspect -f '{{.State.Status}}' "$container_id" 2>/dev/null || true)"
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      if [ "$desired" = "running" ] && [ "$state" = "running" ]; then
        return 0
      fi
      if [ "$desired" = "healthy" ] && [ "$state" = "running" ] && [ "$health" = "healthy" ]; then
        return 0
      fi
      if [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
        echo "service $service terminated while waiting for $desired" >&2
        compose_domain logs --tail=120 "$service" >&2 || true
        return 1
      fi
    fi
    sleep 1
  done

  echo "service $service did not reach $desired" >&2
  compose_domain logs --tail=120 "$service" >&2 || true
  return 1
}

require_completed_service() {
  local service=$1
  local container_id
  local state
  local exit_code
  container_id="$(compose_domain ps -aq "$service" 2>/dev/null || true)"
  [ -n "$container_id" ] || {
    echo "one-shot service is missing: $service" >&2
    return 1
  }
  state="$(docker inspect -f '{{.State.Status}}' "$container_id" 2>/dev/null || true)"
  exit_code="$(docker inspect -f '{{.State.ExitCode}}' "$container_id" 2>/dev/null || true)"
  if [ "$state" != "exited" ] || [ "$exit_code" != "0" ]; then
    echo "one-shot service failed: $service state=$state exit=$exit_code" >&2
    compose_domain logs --tail=120 "$service" >&2 || true
    return 1
  fi
}

wait_for_domain_test_stack() {
  require_completed_service migrate
  require_completed_service bootstrap-profile
  require_completed_service singleuser-image
  for service in api frontend egress-proxy jupyterhub reconciler gateway; do
    wait_for_service "$service" healthy
  done
  wait_for_service worker running
}

report_transition_failure() {
  echo "domain-test transition failed" >&2
  if [ "$transition_is_destructive" = true ]; then
    echo "database writers remain stopped to avoid an unsafe implicit rollback" >&2
    if [ -n "$BACKUP_DIR" ]; then
      echo "verified restore command:" >&2
      echo "  make domain-test-restore BACKUP=$BACKUP_DIR" >&2
    fi
    compose_domain ps -a >&2 || true
    return 0
  fi

  if [ "$original_stack_was_running" = true ]; then
    echo "restarting the original local stack" >&2
    start_local_stack || true
  fi
}

start_domain_test() {
  local users=$1

  if any_long_running_static_service_up; then
    original_stack_was_running=true
  fi

  preflight "$users"

  stop_long_running_static_services
  await_long_running_services_stopped
  # Close the race between the first idle check and stopping the writers.
  run_idle_check
  assert_no_active_singleuser

  if ! create_database_backup; then
    report_transition_failure
    return 1
  fi

  if ! compose_domain down --remove-orphans; then
    transition_is_destructive=true
    report_transition_failure
    return 1
  fi
  transition_is_destructive=true

  if ! compose_domain up -d --build; then
    report_transition_failure
    return 1
  fi
  if ! wait_for_domain_test_stack; then
    report_transition_failure
    return 1
  fi

  echo "domain-test is ready: https://platform.workspace.test"
  echo "verified pre-transition backup: $BACKUP_DIR"
}

restore_database_backup() {
  local backup_dir=$1
  local platform_sha
  local jupyterhub_sha

  python3 scripts/domain_test_database_snapshot.py verify --bundle "$backup_dir"

  if docker ps --all --filter "volume=${project_name}_platform_data" --format '{{.ID}}' | grep -q .; then
    echo "database restore blocked: platform_data volume is in use" >&2
    return 1
  fi
  if docker ps --all --filter "volume=${project_name}_jupyterhub_data" --format '{{.ID}}' | grep -q .; then
    echo "database restore blocked: jupyterhub_data volume is in use" >&2
    return 1
  fi

  platform_sha="$(python3 scripts/domain_test_database_snapshot.py digest --bundle "$backup_dir" --filename platform.sqlite)"
  jupyterhub_sha="$(python3 scripts/domain_test_database_snapshot.py digest --bundle "$backup_dir" --filename jupyterhub.sqlite)"

  # Reset DB-derived policy state before either database is mutated. A partial
  # DB restore can then only restart with the image's deny-all bootstrap.
  for policy_volume in \
    "${project_name}_egress_policy_desired" \
    "${project_name}_egress_policy_ack"
  do
    if docker volume inspect "${policy_volume}" >/dev/null 2>&1; then
      docker volume rm "${policy_volume}" >/dev/null \
        || { echo >&2 "database restore blocked: could not reset ${policy_volume}"; return 1; }
    fi
  done

  compose_base --profile maintenance run --rm --no-deps --user 0:0 \
    --volume "$backup_dir/platform.sqlite:/restore/input.sqlite:ro" \
    --env "PLATFORM_RESTORE_EXPECTED_SHA256=$platform_sha" \
    restore-platform-database
  compose_base --profile maintenance run --rm --no-deps --user 0:0 \
    --volume "$backup_dir/jupyterhub.sqlite:/restore/input.sqlite:ro" \
    --env "PLATFORM_RESTORE_EXPECTED_SHA256=$jupyterhub_sha" \
    restore-jupyterhub-database

  echo "database restore completed: $backup_dir"
}

restore_local() {
  stop_long_running_static_services || true
  await_long_running_services_stopped || true
  compose_domain down --remove-orphans
  start_local_stack
  echo "local stack restored: http://platform.localhost:8080"
}

action="${1:-help}"
case "$action" in
  preflight)
    preflight "$default_users"
    ;;
  up)
    start_domain_test "$default_users"
    ;;
  down)
    restore_local
    ;;
  restore)
    if [ -z "${DOMAIN_TEST_BACKUP_DIR:-}" ]; then
      echo "restore needs DOMAIN_TEST_BACKUP_DIR (Make: BACKUP=path)" >&2
      exit 1
    fi
    restore_database_backup "$DOMAIN_TEST_BACKUP_DIR"
    ;;
  help|*)
    print_help
    ;;
esac
