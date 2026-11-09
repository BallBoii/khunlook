# frontend image: nginx + the static chat page. No build step, no Node.js.
# The Pis run only nginx for this service (~10 MB image, a few MB of RAM).
#
# Build from the repo root, like the other services:
#   make build-frontend                       (local, your laptop's architecture)
#   make push-frontend REGISTRY=<registry>    (arm64 + amd64, for the Pis)

# Non-root nginx (user 101) listening on 8080; multi-arch, so it runs on the Pis.
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY services/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY services/frontend/site /usr/share/nginx/html

EXPOSE 8080
