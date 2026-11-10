# rag-service

Takes a question from the API gateway and returns an answer grounded in our course documents.

```
api-gateway ──POST /v1/answer──▶ rag-service
                                 1. embed question ───────▶ model-service  POST /v1/embed   
                                 2. vector search ────────▶ vector-db      POST /v1/search
                                 3. build prompt (numbered passages + question)
                                 4. ask the LLM ──────────▶ model-service  POST /v1/chat    
                                 5. answer + sources + served_by ──▶ api-gateway
```

Stateless: no data is kept between requests, so any replica on any Pi can answer and a replacement pod works
as soon as it is ready.

## API

`POST /v1/ask` (and `POST /v1/answer`, see below)

```json
{"question": "When is the final project demo?", "top_k": 5}
```

`top_k` is optional (1-20, default `TOP_K`). Response:

```json
{
  "answer": "The demo is on 17 November 2026, 13:00-16:00 [1].",
  "sources": [{"n": 1, "id": "...", "doc_id": "sds-final-project-2026", "doc_title": "SDS Final Project",
               "page": 10, "score": 0.455, "text": "..."}],
  "retrieval": "vector",
  "served_by": {"rag-service": "rag-7c9f-abcde", "model-service (embed)": "...", "vector-db": "...",
                "model-service (chat)": "..."},
  "timings": {"embed_ms": 17.2, "search_ms": 20.8, "chat_ms": 950.1},
  "request_id": "4545c98e..."
}
```

`served_by` shows every pod that handled the request, which is the demo proof that a request goes through
2 or more containers. Send `X-Request-ID` to trace one request through every service's logs.

Errors use the same shape as vector-db, `{"error": "...", "detail": "..."}`:

| status | error | when |
|---|---|---|
| 400 | `invalid_request` | missing/empty question, bad `top_k` |
| 502 | `vector_db_unavailable` | vector-db unreachable or failed |
| 502 | `model_service_unavailable` | chat failed (body still has `sources`), or embed failed with `KEYWORD_FALLBACK=false` |
| 502 | `embedding_mismatch` | model-service's embedding model is not the one that built the index |

If model-service can't embed, rag falls back to vector-db's keyword (BM25) search, and `retrieval` says `"keyword"`.

## API-gateway contract

api-gateway calls `POST /v1/answer` (same request and logic as `/v1/ask`) and `GET /v1/trace`.

- `/v1/answer` returns `served_by` as a list, `[{"service": "rag-service", "pod": "..."}, {"service": "vector-db", ...},
  {"service": "model-service", ...}]`, which is what the gateway and the frontend show. `/v1/ask` keeps the
  detailed dict (with `model-service (embed)` / `(chat)`) for debugging.
- `GET /v1/trace` checks vector-db and model-service `/readyz` (no LLM call, no cost) and returns
  `{"ok": true, "served_by": [...]}`, or 502 with `ok: false` and a `detail` naming what is down.

## model-service contract

The real service is `services/model`. Defined in one place, `app/clients.py` (`EMBED_PATH`, `CHAT_PATH`, `_parse_embed_response`, `_parse_chat_response`):

- `POST /v1/embed` `{"input": "text"}` → `{"embedding": [..floats..], "model": "..."}`
  It must use the **same embedding model** as `ingest/config.yaml`, or search results are meaningless.
- `POST /v1/chat` `{"messages": [{"role": "system", ...}, {"role": "user", ...}], "temperature": 0.2}` → `{"answer": "..."}`

## Settings (env vars)

| var | default | |
|---|---|---|
| `VECTOR_DB_URL` | `http://vector-db:8080` | |
| `MODEL_SERVICE_URL` | `http://model-service:8080` | |
| `TOP_K` | `5` | chunks to retrieve |
| `MIN_SCORE` | `0.0` | drop weaker matches (raise to ~0.3 with a real embedding model) |
| `MAX_CONTEXT_CHARS` | `6000` | prompt budget for passages |
| `KEYWORD_FALLBACK` | `true` | use BM25 search when embedding fails |
| `EMBED_TIMEOUT_S` / `CHAT_TIMEOUT_S` / `VECTOR_DB_TIMEOUT_S` | `10` / `60` / `5` | |

## Run locally (no real model-service needed)

`dev/stub_model_service.py` fakes model-service: it embeds with the same free `fake-hash-v1` embedder that
built the current placeholder index, and its "chat" just echoes the prompt so you can check it.

From the repo root, in three terminals (PowerShell):

```powershell
pip install -r services/rag/requirements.txt -r services/vector-db/requirements.txt -r ingest/requirements.txt

# 1. vector-db
cd services/vector-db; $env:INDEX_DIR="..\..\index"; uvicorn app.main:app --port 8081
# 2. stub model-service (from the repo root)
uvicorn services.rag.dev.stub_model_service:app --port 8082
# 3. rag-service
cd services/rag; $env:VECTOR_DB_URL="http://127.0.0.1:8081"; $env:MODEL_SERVICE_URL="http://127.0.0.1:8082"; uvicorn app.main:app --port 8083
```

Then open http://localhost:8083/docs and try `POST /v1/ask`.

## Tests

```
python -m pytest services/rag/tests -q
```

vector-db and model-service are faked with `httpx.MockTransport`, so tests need no network.

## Build and deploy

```
docker buildx build -f services/rag/Dockerfile --platform linux/arm64,linux/amd64 -t REGISTRY/rag:1.0.0 --push .
kubectl apply -f deploy/k8s/rag/rag.yaml      # set the image first
```
