# api-gateway image. This is the image that goes to the Pis.
#
# Build from the repo root, like vector-db:
#   make build-api-gateway                     (local)
#   make push-api-gateway REGISTRY=<registry>  (arm64 + amd64, for the Pis)

# ---- base: Python + dependencies ---------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOG_LEVEL=info

WORKDIR /app
COPY services/api-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
USER 10001
EXPOSE 8080

# ---- gateway: production image -----------------------------------------------------------------
FROM base AS gateway
COPY services/api-gateway/app ./app
# One worker: the rate limits and in-flight counter live in this process's memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
