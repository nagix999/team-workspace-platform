# Production builds MUST pass a digest-pinned JUPYTERHUB_BASE_IMAGE. The mutable
# default exists only so Docker can parse FROM; the RUN guard fails unless the
# explicit local-development escape hatch is set.
ARG JUPYTERHUB_BASE_IMAGE=quay.io/jupyterhub/jupyterhub:5.5.0
FROM ${JUPYTERHUB_BASE_IMAGE} AS runtime

ARG JUPYTERHUB_BASE_IMAGE
ARG ALLOW_MUTABLE_BASE_IMAGE=false

RUN image="${JUPYTERHUB_BASE_IMAGE}"; digest="${image##*@sha256:}"; \
    case "${image}" in \
      *@sha256:*) \
        test "${#digest}" -eq 64 && \
        case "${digest}" in *[!0-9a-f]*) false ;; *) true ;; esac ;; \
      *) test "${ALLOW_MUTABLE_BASE_IMAGE}" = "true" || \
         { echo >&2 "production JUPYTERHUB_BASE_IMAGE must use @sha256:<64 lowercase hex>"; exit 1; } ;; \
    esac

COPY requirements.txt /tmp/platform-jupyterhub-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/platform-jupyterhub-requirements.txt \
 && rm /tmp/platform-jupyterhub-requirements.txt \
 && groupadd --gid 10001 jupyterhub \
 && useradd --uid 10001 --gid 10001 --home-dir /srv/jupyterhub \
      --create-home --shell /usr/sbin/nologin jupyterhub \
 && install -d -o 10001 -g 10001 -m 0700 /srv/jupyterhub

COPY --chown=10001:10001 jupyterhub_config.py native_user_admin.py nativeauth_compat.py oauth_log_policy.py rbac_policy.py spawn_guard.py platform_spawner.py profile_policy.py public_url_policy.py local_volume_policy.py local_docker_provisioner.py local_provisioner_agent.py /etc/jupyterhub/

USER 10001:10001
WORKDIR /srv/jupyterhub

EXPOSE 8000 8081
ENTRYPOINT ["jupyterhub", "-f", "/etc/jupyterhub/jupyterhub_config.py"]

FROM runtime AS test

FROM runtime AS final
