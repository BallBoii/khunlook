# model-service image. Build from the repo root, like the other services:
#   make build-model-service                     (local)
#   make push-model-service REGISTRY=<registry>  (arm64 + amd64, for the Pis)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBED_MODEL=baai/bge-m3 \
    EMBED_DIM=1024 \
    CHAT_MODEL=google/gemma-3-4b-it \
    LOG_LEVEL=info

WORKDIR /app
COPY services/model/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/model/app ./app

USER 10001
EXPOSE 8080
# OPENROUTER_API_KEY is never baked in: pass it at runtime (env / Kubernetes Secret).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
