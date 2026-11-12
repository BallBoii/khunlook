# frontend

The chat web page, served by nginx. The page is plain HTML, CSS and JavaScript (no framework, no
build step). nginx also forwards `/api/*` to the api-gateway. This is the only Service reachable
from outside the cluster (NodePort 30080). 2 replicas on different Pis.

```
browser → frontend (nginx) → api-gateway → rag-service → vector-db / model-service
```

## What the user sees

**Normal mode:** KhunLook / CU Course Assistant answers questions in English or Thai about
the available CU course documents. Every returned passage keeps its citation number, e.g.
`[2] 2110221 · Computer Engineering Essentials`. Expand it to read the source text. Several
passages from the same course remain separate so `[1]` / `[2]` in the answer can be identified.
Seven-digit course IDs follow the current corpus convention: their `page: 1` is a placeholder
and is not shown. Other document IDs retain real page numbers (e.g. slide decks).

If no sources are returned, the page says "No supporting passages were returned". If generation
fails after retrieval, the error card still shows the retrieved passages, labelled as an
unavailable answer. Short error messages cover rate limits, busy services and search/model failures.

**Demo mode** (the switch in the header, or open the page with `?demo`; remembered per browser).
For the fault-tolerance demo:

- **Served by** under answers and errors: the pods reached, e.g.
  `frontend › gateway › rag › vector-db › model`, plus the response time and full request ID.
  Error codes and technical details appear here too. Embed and chat can reach distinct model pods.
- **Live trace panel:** a cheap check through the whole chain, 2 s after each check finishes (no LLM call), shown as
  green/red ticks, with the latest pod names. A pod name never seen before is highlighted for
  10 s, which is how a replacement pod shows up after a Pi is pulled. Keep the page on screen
  during the demo: checks pause while the tab is hidden. Failed checks show the reported dependency
  or connection failure. Recovery clears the message; unreachable chains do not retain stale pods.

## How it works

- The page only talks to its own nginx, so there's no CORS and no gateway address in the code.
- nginx adds `X-Served-By: <pod name>` to every response; the gateway and the services behind
  it report themselves in the body's `served_by`. Together that's the chain.
- **During a failover:** nginx retries a failed connection to a gateway once. If a question
  still fails, the page shows "Recovering, retrying…" and asks once more after 2 s.
- **Timeout budget:** gateway 90 s overall → nginx 100 s read timeout → browser 110 s.
  The trace browser timeout is 10 s, above the gateway's 5 s overall deadline. Chat server errors
  returned by the gateway and browser aborts are not automatically retried.

## Files

| File | Used for | Goes to the cluster? |
| --- | --- | --- |
| `site/index.html` | Page layout | **Yes** (in the image) |
| `site/app.js` | Behaviour: chat, errors, retry, demo mode, trace panel | **Yes** |
| `site/style.css` | Look, per the Chula CI | **Yes** |
| `site/assets/chula-engineering-logo.png` | Faculty of Engineering logo | **Yes** |
| `site/fonts/*.woff2` | Chulalongkorn + Chulacharas New fonts | **Yes** |
| `nginx.conf` | Serves the page, forwards `/api/*`, `/healthz` probe | **Yes** |
| `Dockerfile` | Builds the image (nginx + the files above) | **Yes** |
| `../../deploy/k8s/frontend/frontend.yaml` | Kubernetes Deployment + NodePort Service | **Yes** |
| `tests/test_app.cjs` | Browser behaviour with a fake DOM, timers and API replies | No, tests only |
| `../../deploy/docker/compose.yaml` | Runs all five real services on a laptop | No, local testing only |

## Chula CI

- **Colours:** CU Pink `#DE5C8E` for accents, Engineering maroon `#8B2332` for buttons and text.
- **Fonts:** Chulalongkorn (headings) and Chulacharas New (text), served from the image.
- **Logo:** the official Faculty of Engineering file, unmodified apart from trimmed padding.

The fonts and logo are university material: keep the repo private.

## Run locally

From the repo root, with Docker Desktop running:

```bash
make dev-up      # all five services; requires services/model/.env and a real API key
                 # open http://localhost:8080/?demo
make dev-down
```

Questions in this stack call the real model service and can incur OpenRouter charges. The current
Compose file has one gateway replica. For a local failover rehearsal, start two before stopping one:

```bash
docker compose -f deploy/docker/compose.yaml up -d --scale api-gateway=2
docker stop khunlook-dev-api-gateway-1
docker start khunlook-dev-api-gateway-1
```

## Test without API calls

Node.js 18+ is needed only for development tests; the deployed frontend still runs nginx alone.
The tests execute the actual browser script through form/toggle events with fake API replies,
including Thai input, repeated source documents, partial failures, rate limits and trace recovery.

```bash
make test-frontend
# Windows / without make:
node --test services/frontend/tests/test_app.cjs
```

The RAG owner's system prompt still describes the SDS course. It needs a corresponding update
to match the current CU course corpus; that business-logic change is outside the frontend.

## Deploy to the cluster

```bash
make push-frontend REGISTRY=<registry> VERSION=1.0.0   # arm64 + amd64 image
# set that tag in deploy/k8s/frontend/frontend.yaml
make deploy-web                                        # gateway + frontend together
```

Open `http://<master-VM-IP>:30080/?demo`, not a Pi's IP: a NodePort answers on every node, so
the master's address keeps working whichever Pi is pulled.
Bump `VERSION` for every new push (the manifest uses `imagePullPolicy: IfNotPresent`).

## Next steps

- [ ] Push the image and set the tag once the team picks a registry.
- [ ] Deploy, open the page through a master VM's IP, check 2 pods on different Pis.
- [ ] With the real rag-service: check the chain shows real pod names and the trace panel stays green.
- [ ] Failover rehearsal: demo mode on, page on screen, pull each Pi in turn.
