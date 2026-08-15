ARG SQUID_BASE_IMAGE=ubuntu/squid:latest
FROM ${SQUID_BASE_IMAGE} AS runtime

ARG SQUID_BASE_IMAGE
ARG ALLOW_MUTABLE_BASE_IMAGE=false
RUN image="${SQUID_BASE_IMAGE}"; digest="${image##*@sha256:}"; \
    case "${image}" in \
      *@sha256:*) \
        test "${#digest}" -eq 64 && \
        case "${digest}" in *[!0-9a-f]*) false ;; *) true ;; esac ;; \
      *) test "${ALLOW_MUTABLE_BASE_IMAGE}" = "true" || \
         { echo >&2 "production SQUID_BASE_IMAGE must use @sha256:<64 lowercase hex>"; exit 1; } ;; \
    esac

COPY squid.conf /etc/squid/squid.conf

EXPOSE 3128

FROM runtime AS test

FROM runtime AS final
