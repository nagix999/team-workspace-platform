FROM nginx:1.30.4-alpine

COPY nginx-main.conf /etc/nginx/nginx.conf
COPY dev.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1

USER nginx
