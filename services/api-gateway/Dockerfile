# api-gateway image. One Dockerfile, two targets:
#
#   gateway   (default, last stage)  the real gateway. This is the image that goes to the Pis.
#   rag-stub  (--target rag-stub)    fake rag-service for LOCAL TESTING ONLY (deploy/docker/compose.yaml)
#
# Build from the repo root, like vector-db:
#   make build-api-gateway                     (local)
#   make push-api-gateway REGISTRY=<registry>  (arm64 + amd64, for the Pis)

# ---- base: Python + dependencies, shared by both targets ---------------------------------------
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

# ---- rag-stub: local testing only, never pushed ------------------------------------------------
FROM base AS rag-stub
COPY services/api-gateway/stub ./stub
CMD ["uvicorn", "stub.rag_stub:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

# ---- gateway: production image -----------------------------------------------------------------
FROM base AS gateway
COPY services/api-gateway/app ./app
# One worker: the rate limits and in-flight counter live in this process's memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
