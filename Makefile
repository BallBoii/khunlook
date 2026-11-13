# Data part: offline ingestion + vector-db. User-facing part: api-gateway + frontend.
# Every target is a thin wrapper, so on Windows without make you can run the command on the
# right directly.
PY        ?= python
NODE      ?= node
REGISTRY  ?= ghcr.io/ballboii
VERSION   ?= 1.0.0
# The whole cluster (UTM master VM on Apple Silicon + Pis) is arm64.
PLATFORMS ?= linux/arm64
IDX       := $(shell $(PY) -c "import json;print(json.load(open('index/manifest.json'))['index_version'])" 2>/dev/null)
IMAGE     ?= $(REGISTRY)/khunlook-vector-db:$(VERSION)-idx$(IDX)
DOCKER_BUILD = docker buildx build -f services/vector-db/Dockerfile -t $(IMAGE)
GATEWAY_IMAGE  ?= $(REGISTRY)/khunlook-api-gateway:$(VERSION)
FRONTEND_IMAGE ?= $(REGISTRY)/khunlook-frontend:$(VERSION)
RAG_LOCAL      ?= rag:$(VERSION)
RAG_IMAGE      ?= $(REGISTRY)/khunlook-rag:$(VERSION)
MODEL_LOCAL    ?= model-service:$(VERSION)
MODEL_IMAGE    ?= $(REGISTRY)/khunlook-model-service:$(VERSION)
COMPOSE        = docker compose -f deploy/docker/compose.yaml

.PHONY: setup ingest estimate eval validate-index test build-vector-db push-vector-db run-vector-db \
        build-api-gateway push-api-gateway build-frontend push-frontend test-frontend build-rag push-rag build-model-service push-model-service run-model-service dev-up dev-down \
        cluster render argocd-password

setup:            ## install dev deps
	$(PY) -m pip install -r ingest/requirements.txt -r services/vector-db/requirements.txt -r services/api-gateway/requirements.txt -r services/model/requirements.txt -r services/rag/requirements.txt

ingest:           ## build index/ (free fake embedder unless PROVIDER=openrouter)
	$(PY) -m ingest ingest $(if $(PROVIDER),--provider $(PROVIDER))

estimate:         ## free dry run: what a paid openrouter run would send
	$(PY) -m ingest estimate

eval:
	$(PY) -m ingest eval

validate-index:
	$(PY) services/vector-db/app/search.py index

test:
	@# one pytest run per service: each service is a package named `app`, so they can't share a process
	$(PY) -m pytest ingest/tests -q
	@for s in vector-db api-gateway model rag; do $(PY) -m pytest services/$$s/tests -q || exit 1; done
	$(MAKE) test-frontend

test-frontend:     ## browser behaviour with fake API replies; Node.js 18+ (no npm packages)
	$(NODE) --test services/frontend/tests/test_app.cjs

build-vector-db: validate-index   ## local single-arch image
	$(DOCKER_BUILD) --load .

push-vector-db: validate-index    ## multi-arch image to the team registry
	$(DOCKER_BUILD) --platform $(PLATFORMS) --push .

run-vector-db:
	docker run --rm -p 8080:8080 -e POD_NAME=local-vector-db $(IMAGE)

# ---- api-gateway + frontend ----------------------------------------------------------------
build-api-gateway:   ## local single-arch image
	docker buildx build -f services/api-gateway/Dockerfile --target gateway -t $(GATEWAY_IMAGE) --load .

push-api-gateway:    ## multi-arch image to the team registry
	docker buildx build -f services/api-gateway/Dockerfile --target gateway -t $(GATEWAY_IMAGE) --platform $(PLATFORMS) --push .

build-frontend:
	docker buildx build -f services/frontend/Dockerfile -t $(FRONTEND_IMAGE) --load .

push-frontend:
	docker buildx build -f services/frontend/Dockerfile -t $(FRONTEND_IMAGE) --platform $(PLATFORMS) --push .

# ---- rag-service ---------------------------------------------------------------------------
build-rag:             ## local single-arch image
	docker buildx build -f services/rag/Dockerfile -t $(RAG_LOCAL) --load .

push-rag:              ## multi-arch image to the team registry
	docker buildx build -f services/rag/Dockerfile -t $(RAG_IMAGE) --platform $(PLATFORMS) --push .

# ---- model-service (OpenRouter embeddings + LLM) -------------------------------------------
build-model-service:   ## local single-arch image
	docker buildx build -f services/model/Dockerfile -t $(MODEL_LOCAL) --load .

push-model-service:    ## multi-arch image to the team registry
	docker buildx build -f services/model/Dockerfile -t $(MODEL_IMAGE) --platform $(PLATFORMS) --push .

run-model-service:     ## needs OPENROUTER_API_KEY in your shell; listens on :8081
	docker run --rm -p 8081:8080 -e OPENROUTER_API_KEY -e POD_NAME=local-model-service $(MODEL_LOCAL)

dev-up:              ## whole stack (frontend, gateway, rag, vector-db, model-service) on http://localhost:8080; needs services/model/.env
	$(COMPOSE) up --build -d

dev-down:
	$(COMPOSE) down

# ---- cluster (infra/README.md) -------------------------------------------------------------
# Normal deploys need none of these: push to main, CI builds and bumps deploy/k8s, Argo CD syncs.
# `make push-*` is only a manual fallback (images go to $(REGISTRY), the same names CI uses).
KUBECONFIG_LOCAL ?= infra/ansible/artifacts/kubeconfig

cluster:             ## build/refresh the whole cluster + Argo CD (needs ansible-core, vault password)
	cd infra/ansible && ansible-playbook site.yml --ask-vault-pass

render:              ## print exactly what Argo CD will apply
	kubectl kustomize deploy/k8s

argocd-password:     ## initial Argo CD admin password
	@kubectl --kubeconfig $(KUBECONFIG_LOCAL) -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
