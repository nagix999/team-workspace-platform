#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

readonly profile_policy="infra/jupyterhub/profiles.local-dev.json"
allow_unhealthy_jupyterhub="${PROFILE_DEPLOY_ALLOW_UNHEALTHY_JUPYTERHUB:-false}"

validate_deploy_options() {
  case "${allow_unhealthy_jupyterhub}" in
    true | false) ;;
    *)
      echo >&2 "PROFILE_DEPLOY_ALLOW_UNHEALTHY_JUPYTERHUB must be true or false"
      return 2
      ;;
  esac
}

active_singleusers() {
  docker ps \
    --filter label=platform.kind=jupyter-singleuser \
    --format '{{.Names}}'
}

require_no_active_singleusers() {
  local active
  active="$(active_singleusers)"
  if [[ -n "${active}" ]]; then
    echo >&2 "profile deploy refused: stop every workspace first"
    echo >&2 "running single-user containers:"
    echo >&2 "${active}"
    return 1
  fi
}

require_control_plane_ready() {
  local service container_id health
  for service in api gateway worker; do
    container_id="$(docker compose ps --status running -q "${service}")"
    if [[ -z "${container_id}" ]]; then
      echo >&2 "profile deploy refused: ${service} is not running"
      return 1
    fi
    if [[ "${service}" != worker ]]; then
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${container_id}")"
      if [[ "${health}" != healthy ]]; then
        echo >&2 "profile deploy refused: ${service} health is ${health}"
        return 1
      fi
    fi
  done

  container_id="$(docker compose ps --status running -q frontend)"
  if [[ -z "${container_id}" ]]; then
    echo >&2 "profile deploy refused: frontend is not running"
    return 1
  fi
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${container_id}")"
  if [[ "${health}" == missing ]]; then
    # Older frontend containers predate Docker health metadata. Require the
    # actual in-container endpoint before allowing the one-time upgrade.
    if ! docker compose exec -T frontend \
      wget -q -O /dev/null http://127.0.0.1:8080/healthz; then
      echo >&2 "profile deploy refused: legacy frontend health endpoint failed"
      return 1
    fi
  elif [[ "${health}" != healthy ]]; then
    echo >&2 "profile deploy refused: frontend health is ${health}"
    return 1
  fi

  container_id="$(docker compose ps --status running -q jupyterhub)"
  if [[ -n "${container_id}" ]]; then
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${container_id}")"
  else
    health="not-running"
  fi
  if [[ "${health}" != healthy ]]; then
    if [[ "${allow_unhealthy_jupyterhub}" != true ]]; then
      echo >&2 "profile deploy refused: jupyterhub health is ${health}"
      return 1
    fi
    container_id="$(docker compose ps --all -q jupyterhub)"
    if [[ -z "${container_id}" ]]; then
      echo >&2 "profile deploy recovery refused: jupyterhub container is missing"
      return 1
    fi
    echo >&2 "WARNING: explicit recovery mode accepts jupyterhub health=${health}"
  fi

  container_id="$(docker compose ps --status running -q reconciler)"
  if [[ -n "${container_id}" ]]; then
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${container_id}")"
    if [[ "${health}" != healthy ]]; then
      echo >&2 "profile deploy refused: reconciler health is ${health}"
      return 1
    fi
  elif [[ -n "$(docker compose ps --all -q reconciler)" ]]; then
    echo >&2 "profile deploy refused: reconciler is not running"
    return 1
  else
    # The only accepted absence is the one-time rollout from a Compose version
    # that did not yet define the reconciler. Post-deploy health is mandatory.
    echo >&2 "WARNING: reconciler is absent before its first rollout"
  fi
}

preflight_backend_profile_import() {
  docker compose --profile maintenance run --rm --no-deps migration-preflight
}

control_stopped=false
deployment_complete=false
migration_started=false
hub_recreate_started=false
frontend_recreate_started=false

restore_control_on_failure() {
  local status=$?
  trap - EXIT
  if [[ "${control_stopped}" == true && "${deployment_complete}" != true ]]; then
    set +e
    echo >&2 "profile deploy failed; attempting control-plane recovery"
    if [[ "${migration_started}" == true ]]; then
      docker compose up -d --no-deps --force-recreate --wait api
    else
      # Before a live migration starts, preserve the exact stopped API/worker
      # containers instead of pairing a newly built image with the old schema.
      docker compose start api
    fi
    if [[ "${hub_recreate_started}" == true ]]; then
      docker compose up -d --no-deps --force-recreate --wait jupyterhub
    fi
    if [[ "${frontend_recreate_started}" == true ]]; then
      docker compose up -d --no-deps --force-recreate --wait frontend
    fi
    if [[ "${migration_started}" == true ]]; then
      docker compose up -d --no-deps --force-recreate worker
      docker compose up -d --no-deps --force-recreate --wait reconciler
    else
      docker compose start worker
      docker compose start reconciler
    fi
    docker compose start gateway
    set -e
  fi
  exit "${status}"
}
main() {
  validate_deploy_options
  trap restore_control_on_failure EXIT

  # Everything below this point is read-only with respect to the live databases
  # and running containers until every build/schema/image check has passed.
  docker compose config --quiet
  python3 infra/jupyterhub/profile_image_check.py \
    --policy "${profile_policy}" \
    --allow-unsafe-policy \
    --validate-only
  docker compose build singleuser-image api jupyterhub frontend
  python3 infra/jupyterhub/profile_image_check.py \
    --policy "${profile_policy}" \
    --allow-unsafe-policy
  preflight_backend_profile_import

  # Recovery mode never bypasses the no-active-workspace invariant.
  require_no_active_singleusers
  require_control_plane_ready

  # Close ingress plus the API/worker mutation race, then check again for a
  # spawn already in flight. Hub stays internal until its final recreate.
  control_stopped=true
  docker compose stop gateway worker reconciler api
  sleep 2
  require_no_active_singleusers

  migration_started=true
  docker compose run --rm --no-deps migrate
  docker compose run --rm --no-deps bootstrap-profile
  docker compose up -d --no-deps --force-recreate --wait api
  hub_recreate_started=true
  docker compose up -d --no-deps --force-recreate --wait jupyterhub
  frontend_recreate_started=true
  docker compose up -d --no-deps --force-recreate --wait frontend
  docker compose up -d --no-deps --force-recreate worker
  docker compose up -d --no-deps --force-recreate --wait reconciler
  docker compose up -d --no-deps --wait gateway

  deployment_complete=true
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
