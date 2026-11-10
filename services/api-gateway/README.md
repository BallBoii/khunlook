# api-gateway

The single entry point for the chat API. The frontend's nginx forwards `/api/*` here, and the
gateway forwards questions to rag-service. It doesn't answer questions itself: it adds the rules
around them. Python + FastAPI, stateless, 2 replicas on different Pis.

```
browser → frontend (nginx) → api-gateway → rag-service → vector-db / model-service
```

## How it works

For every `POST /api/v1/chat`:

1. **Validate:** the question must be text, 1–1000 characters after trimming. English or Thai.
2. **Rate limit:** 10 questions/min per client (burst 5) and 30/min per replica for everyone,
   to protect the paid LLM. Over the limit → `429` with `Retry-After`.
3. **Busy limit:** at most 8 questions waiting on rag-service per replica, to protect the Pi's
   1 GB of RAM. Over → `503 busy`.
4. **Call rag-service** `POST /v1/answer`, with 2 s to connect and 45 s to answer.
   If the *connection* fails (e.g. a pod on a Pi that was just pulled), retry once on a new
   connection. Slow answers and server errors are never retried.
5. **Return** the answer and sources, with this gateway added to the front of the `served_by` chain.

Every request gets an ID (`X-Request-ID`), an `X-Served-By: <pod>` header and one JSON log line.

## Files

| File | Used for | Goes to the cluster? |
| --- | --- | --- |
| `app/main.py` | The gateway | **Yes** |
| `requirements.txt` | Python libraries (pinned versions) | **Yes** (installed in the image) |
| `Dockerfile` | Builds the image. Default target `gateway` | **Yes**, the `gateway` target |
| `../../deploy/k8s/api-gateway/api-gateway.yaml` | Kubernetes Deployment + Service | **Yes** |
| `stub/rag_stub.py` | Fake rag-service (`--target rag-stub`) | No, local testing only |
| `../../deploy/docker/compose.yaml` | Runs everything on a laptop | No, local testing only |
| `tests/test_gateway.py` | 26 automated tests | No, run with `make test` |

## API

| Method | Path | What it does | Calls rag-service |
| --- | --- | --- | --- |
| POST | `/api/v1/chat` | `{"question": "..."}` → answer, sources, pod chain | `POST /v1/answer` |
| GET | `/api/v1/trace` | Cheap end-to-end check for the demo panel, no LLM | `GET /v1/trace` |
| GET | `/api/v1/info` | Version, pod name, current limits | – |
| GET | `/healthz`, `/readyz` | Kubernetes probes. Never call rag-service | – |

Chat response:

```json
{
  "request_id": "ec1a3dac…",
  "answer": "The on-site demo is on November 17, 2026, 13:00–16:00.",
  "sources": [{"doc_title": "SDS Final Project", "page": 10}],
  "served_by": [{"service": "api-gateway", "pod": "api-gateway-5d2f8-xk9pq"}, "…rag-service, vector-db, model-service"]
}
```

What the gateway expects from rag-service (agreed contract):

```
POST /v1/answer  {"question": "...", "top_k": 5?}  →  {"answer", "sources": [...], "served_by": [...]}
GET  /v1/trace                                       →  {"ok": true, "served_by": [...]}
```

Errors always look like `{"error": "<code>", "detail": "<text>", "request_id": "…"}`:

| Situation | Status | `error` |
| --- | --- | --- |
| Empty, too long or malformed question | 400 | `invalid_request` |
| Over the per-client or global rate limit | 429 + `Retry-After` | `rate_limited` |
| Too many questions in progress | 503 | `busy` |
| rag-service unreachable after one retry | 503 | `upstream_unavailable` |
| rag-service slower than 45 s | 504 | `upstream_timeout` |
| rag-service error or unreadable reply | 502 | `upstream_error` |
| rag-service rejected the question (4xx) | passed through | rag-service's own code |

## Settings (environment variables, set in the k8s manifest)

| Variable | Default | Meaning |
| --- | --- | --- |
| `RAG_URL` | `http://rag-service:8080` | rag-service address |
| `RATE_PER_MIN` / `RATE_BURST` | `10` / `5` | Per-client limit (client = `X-Real-IP` set by nginx) |
| `GLOBAL_PER_MIN` | `30` | All clients, per replica |
| `MAX_INFLIGHT` | `8` | Questions in progress, per replica |
| `CONNECT_TIMEOUT_S` / `CHAT_TIMEOUT_S` / `TRACE_TIMEOUT_S` | `2` / `45` / `3` | Timeouts |
| `MAX_QUESTION_CHARS` / `MAX_TOP_K` | `1000` / `10` | Input limits |
| `POD_NAME` | hostname | Pod name, from the manifest |

## Run and test locally

From the repo root:

```bash
make setup     # once: Python packages (use a virtualenv)
make test      # all tests, including these 26
make dev-up    # frontend + 2 gateways + fake rag-service → http://localhost:8080/?demo
make dev-down
```

The fake rag-service gives the same canned answer to every question (see `stub/rag_stub.py`).

## Deploy to the cluster

```bash
make push-api-gateway REGISTRY=<registry> VERSION=1.0.0   # arm64 + amd64 image
# set that tag in deploy/k8s/api-gateway/api-gateway.yaml
make deploy-web                                           # gateway + frontend together
kubectl get pods -l app=api-gateway -o wide                # 2 pods, on different Pis
kubectl logs -l app=api-gateway                            # one JSON line per request
```

Bump `VERSION` for every new push: the manifest uses `imagePullPolicy: IfNotPresent`, so a Pi
that already has a tag won't download a changed image with the same tag.

## Next steps

- [ ] rag-service team: implement `POST /v1/answer` and `GET /v1/trace` as above
      (until `/v1/trace` exists, the demo panel shows red).
- [ ] Team: pick the image registry, then push and set the image tag.
- [ ] Deploy and check both replicas run on different Pis.
- [ ] Failover rehearsal with the real services: pull each Pi, note the gap in the trace panel.
