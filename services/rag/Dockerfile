# Build from the repo root (same convention as vector-db):
#   docker buildx build -f services/rag/Dockerfile -t rag:dev .
#   docker buildx build -f services/rag/Dockerfile --platform linux/arm64,linux/amd64 -t REGISTRY/rag:1.0.0 --push .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VECTOR_DB_URL=http://vector-db:8080 \
    MODEL_SERVICE_URL=http://model-service:8080 \
    TOP_K=5 \
    MIN_SCORE=0.0 \
    MAX_CONTEXT_CHARS=6000 \
    KEYWORD_FALLBACK=true \
    LOG_LEVEL=info

WORKDIR /app
COPY services/rag/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/rag/app ./app

USER 10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
