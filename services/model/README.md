# model-service

The only service that talks to [OpenRouter](https://openrouter.ai). rag-service calls it to
embed the user's question and to get the LLM answer. Stateless, so any replica on any Pi can serve any request.

| Purpose   | Model                      | Setting       |
|-----------|----------------------------|---------------|
| Embedding | `baai/bge-m3` (1024 dims)  | `EMBED_MODEL` |
| LLM       | `google/gemma-3-4b-it`     | `CHAT_MODEL`  |

> **The embedding model must be the one that built the vector-db index.** Re-ingest with
> `embedding.openrouter.model: baai/bge-m3` and `dim: 1024` in `ingest/config.yaml` before expecting real
> answers; the current placeholder index (`fake-hash-v1`) will make rag report `embedding_mismatch`.

## API (matches `services/rag/app/clients.py`)

Every response carries `X-Served-By` (pod name) and `X-Request-ID` (echoed, or generated). Errors are
`{"error": "<code>", "detail": "<text>"}`.

### `POST /v1/embed`
```json
{"input": "When is the demo?"}
```
```json
{"embedding": [0.02, -0.11, ...], "model": "baai/bge-m3", "dim": 1024}
```
Vectors are L2-normalized. `input` may also be a list of strings; the reply then has `"embeddings": [[...], ...]`.

### `POST /v1/chat`
```json
{"messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
 "temperature": 0.2, "max_tokens": 512}
```
```json
{"answer": "The demo is on 17 November ...", "model": "google/gemma-3-4b-it", "usage": {"prompt_tokens": 812, "completion_tokens": 64}}
```
`temperature` (0-2, default 0.2) and `max_tokens` are optional; `max_tokens` is capped by `MAX_TOKENS_CAP`.

### Health
- `GET /healthz`: process is alive (liveness).
- `GET /readyz`: ready if `OPENROUTER_API_KEY` is set. It never calls OpenRouter, so an outage there can't
  take every pod out of the Service.
- `GET /v1/info`: configured models.

### Errors
| Status | `error`                                        | Meaning                                              |
|--------|------------------------------------------------|------------------------------------------------------|
| 400    | `invalid_request`, `input_too_long`            | bad body                                             |
| 429    | `rate_limited`                                 | OpenRouter 429 persisted after retries               |
| 502    | `upstream_auth`                                | OpenRouter rejected our key                          |
| 502    | `upstream_rejected`, `upstream_error`, `upstream_unreachable`, `empty_answer`, `dimension_mismatch` | OpenRouter failed or returned something unusable |
| 503    | `not_configured`                               | `OPENROUTER_API_KEY` missing                         |
| 504    | `upstream_timeout`                             | no answer within the timeout                         |

Transient failures (429, 5xx, network) are retried up to `MAX_RETRIES` times inside one overall deadline.

## Configuration (env)

| Variable             | Default                      | Notes                                              |
|----------------------|------------------------------|----------------------------------------------------|
| `OPENROUTER_API_KEY` | (required)                   | never logged or baked into the image               |
| `EMBED_MODEL`        | `baai/bge-m3`                |                                                    |
| `EMBED_DIM`          | `1024`                       | returned vectors are checked; `0` disables         |
| `CHAT_MODEL`         | `google/gemma-3-4b-it`       |                                                    |
| `EMBED_TIMEOUT_S`    | `8`                          | keep below rag's `EMBED_TIMEOUT_S` (10)            |
| `CHAT_TIMEOUT_S`     | `55`                         | keep below rag's `CHAT_TIMEOUT_S` (60)             |
| `MAX_RETRIES`        | `2`                          |                                                    |
| `MAX_CONCURRENCY`    | `8`                          | simultaneous OpenRouter calls per pod              |
| `MAX_INPUT_CHARS`    | `8000`                       | embed input limit (chat allows 4x)                 |
| `MAX_TOKENS_CAP`     | `1024`                       | upper bound on answer length                       |
| `OPENROUTER_BASE_URL`| `https://openrouter.ai/api/v1` |                                                  |
| `LOG_LEVEL`          | `info`                       | JSON logs: request id, endpoint, status, latency, model, token counts |

## Run, test, deploy

```bash
# tests (no network or key needed; OpenRouter is faked)
python -m pytest services/model/tests -q

# local
make build-model-service
OPENROUTER_API_KEY=sk-or-... make run-model-service       # http://localhost:8081
curl -s localhost:8081/v1/embed -H 'content-type: application/json' -d '{"input":"hello"}'

# cluster (arm64 + amd64 image, then the manifest)
make push-model-service REGISTRY=<registry> VERSION=1.0.0
kubectl create secret generic openrouter --from-literal=api-key=sk-or-...
kubectl apply -f deploy/k8s/model-service/model-service.yaml     # set the image in the file first
```

The Service is named `model-service`, which is rag-service's default `MODEL_SERVICE_URL`
(`http://model-service:8080`).

## OpenRouter reference (original notes)

Embeddings (`POST https://openrouter.ai/api/v1/embeddings`, model `baai/bge-m3`, `encoding_format: float`,
`input` may be a string or a list) and chat (`google/gemma-3-4b-it`) via the OpenAI-compatible
`/chat/completions`. Auth is `Authorization: Bearer $OPENROUTER_API_KEY`; the optional `HTTP-Referer` and
`X-OpenRouter-Title` headers are sent from `OPENROUTER_REFERER` / `OPENROUTER_TITLE`.
