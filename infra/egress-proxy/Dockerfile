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
COPY internal-services.bootstrap.txt /etc/squid/egress-internal-services.txt
COPY immutable-platform-networks.txt /etc/squid/immutable-platform-networks.txt
COPY render-internal-services.awk /usr/local/lib/platform/render-internal-services.awk
COPY platform-squid-entrypoint /usr/local/bin/platform-squid-entrypoint
COPY init-policy-volume /usr/local/bin/init-policy-volume

RUN chmod 0555 /usr/local/bin/platform-squid-entrypoint && \
    chmod 0555 /usr/local/bin/init-policy-volume && \
    chmod 0444 /usr/local/lib/platform/render-internal-services.awk && \
    chmod 0444 /etc/squid/egress-internal-services.txt \
        /etc/squid/immutable-platform-networks.txt

ENTRYPOINT ["/usr/local/bin/platform-squid-entrypoint"]
CMD ["-f", "/etc/squid/squid.conf", "-NYC"]

EXPOSE 3128

FROM runtime AS test

FROM runtime AS final
