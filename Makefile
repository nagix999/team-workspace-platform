.PHONY: init-local legacy-host-network-create legacy-host-firewall-apply build up down logs ps list-users provision-user profile-matrix-check profile-image-check profile-deploy domain-test-tls domain-test-preflight domain-test-up domain-test-down domain-test-restore test-backend test-infra test-gateway test-frontend test config

LOCAL_PROJECT_ID_START ?= 10000
# Comma-separated usernames for domain-test checks. Keep defaults minimal so
# `make domain-test-up` works without manually passing USERS first.
USERS ?= platform-admin

init-local:
	bash scripts/init-local.sh

# Retained only for deployments that explicitly accept the older host-managed,
# ICC-disabled execution network. The default Compose stack never invokes these
# targets and never changes host firewall state.
legacy-host-network-create:
	python3 infra/host/create_jupyter_network.py --config infra/host/config.local-dev.json

legacy-host-firewall-apply:
	sudo python3 infra/host/rotate_docker_generation.py --config infra/host/config.local-dev.json
	sudo python3 infra/host/enforce_jupyter_bridge_policy.py --config infra/host/config.local-dev.json
	sudo python3 infra/host/apply_jupyter_firewall.py --config infra/host/config.local-dev.json
	sudo python3 infra/host/check_network_health.py --config infra/host/config.local-dev.json

build:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps

list-users:
	docker compose run --rm --no-deps api python -m app.admin list-users

provision-user:
	@test -n "$(USER_ID)" || (echo "USER_ID is required" >&2; exit 2)
	@test -n "$(USERNAME)" || (echo "USERNAME is required" >&2; exit 2)
	PLATFORM_LOCAL_PROJECT_ID_START="$(LOCAL_PROJECT_ID_START)" bash scripts/provision-local-user.sh "$(USER_ID)" "$(USERNAME)"

profile-matrix-check:
	python3 infra/jupyterhub/generate_local_profile_matrix.py

profile-image-check: profile-matrix-check
	docker compose build singleuser-image
	python3 infra/jupyterhub/profile_image_check.py \
		--policy infra/jupyterhub/profiles.local-dev.json \
		--allow-unsafe-policy

# Deploy immutable profile rows in dependency order. This intentionally recreates
# the API/Hub control plane and refuses to run while a single-user server is active.
profile-deploy:
	bash scripts/deploy-profiles.sh

domain-test-tls:
	bash scripts/init-domain-test-tls.sh

domain-test-preflight:
	DOMAIN_TEST_USERS="$(USERS)" bash scripts/domain-test.sh preflight

domain-test-up:
	DOMAIN_TEST_USERS="$(USERS)" bash scripts/domain-test.sh up

domain-test-down:
	bash scripts/domain-test.sh down

domain-test-restore:
	DOMAIN_TEST_BACKUP_DIR="$(BACKUP)" bash scripts/domain-test.sh restore

test-backend:
	cd backend && pytest -p no:rerunfailures

test-infra:
	python3 -m unittest discover -s infra/jupyterhub/tests -v
	python3 -m unittest discover -s infra/host/tests -v

test-gateway:
	python3 -m unittest discover -s gateway/tests -v
	python3 -m unittest discover -s scripts/tests -v

test-frontend:
	docker build --target test -t team-platform-frontend-test:local frontend

test: test-backend test-infra test-gateway test-frontend

config:
	docker compose config --quiet
