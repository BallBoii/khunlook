# Build from the repo root so index/ is in the context:
#   docker buildx build -f services/vector-db/Dockerfile -t vector-db:dev .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INDEX_DIR=/app/index \
    MAX_TOP_K=20 \
    LOG_LEVEL=info

WORKDIR /app
COPY services/vector-db/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/vector-db/app ./app
COPY index ./index
# Refuse to build an image around a broken index.
RUN python app/search.py index

USER 10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
