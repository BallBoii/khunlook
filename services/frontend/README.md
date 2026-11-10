# frontend

The chat web page, served by nginx. The page is plain HTML, CSS and JavaScript (no framework, no
build step). nginx also forwards `/api/*` to the api-gateway. This is the only Service reachable
from outside the cluster (NodePort 30080). 2 replicas on different Pis.

```
browser → frontend (nginx) → api-gateway → rag-service → vector-db / model-service
```

## What the user sees

**Normal mode:** ask a question (English or Thai) and get the answer with its sources
(`SDS Final Project · p.10`), or "Not covered in the course slides". Errors appear as short
messages, e.g. "Too many questions, please wait 6 s".

**Demo mode** (the switch in the header, or open the page with `?demo`; remembered per browser).
For the fault-tolerance demo:

- **Served by** under every answer: the 5 pods that handled it, e.g.
  `frontend › gateway › rag › vector-db › model`, plus the response time and request ID.
- **Live trace panel:** a cheap check through the whole chain every 2 s (no LLM call), shown as
  green/red ticks, with the latest pod names. A pod name never seen before is highlighted for
  10 s, which is how a replacement pod shows up after a Pi is pulled. Keep the page on screen
  during the demo: checks pause while the tab is hidden.

## How it works

- The page only talks to its own nginx, so there's no CORS and no gateway address in the code.
- nginx adds `X-Served-By: <pod name>` to every response; the gateway and the services behind
  it report themselves in the body's `served_by`. Together that's the chain.
- **During a failover:** nginx retries a failed connection to a gateway once. If a question
  still fails, the page shows "Recovering, retrying…" and asks once more after 2 s.

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
| `../../deploy/docker/compose.yaml` | Runs everything on a laptop with a fake rag-service | No, local testing only |

## Chula CI

- **Colours:** CU Pink `#DE5C8E` for accents, Engineering maroon `#8B2332` for buttons and text.
- **Fonts:** Chulalongkorn (headings) and Chulacharas New (text), served from the image.
- **Logo:** the official Faculty of Engineering file, unmodified apart from trimmed padding.

The fonts and logo are university material: keep the repo private.

## Run locally

From the repo root, with Docker Desktop running:

```bash
make dev-up      # frontend + 2 gateways + fake rag-service
                 # open http://localhost:8080/?demo
make dev-down
```

To see a failover locally, stop one gateway while asking questions:
`docker stop khunlook-dev-api-gateway-1`, then `docker start khunlook-dev-api-gateway-1`.

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
