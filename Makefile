# Data part: offline ingestion + vector-db. User-facing part: api-gateway + frontend.
# Every target is a thin wrapper, so on Windows without make you can run the command on the
# right directly.
PY        ?= python
REGISTRY  ?= CHANGE_ME_REGISTRY
VERSION   ?= 1.0.0
PLATFORMS ?= linux/arm64,linux/amd64
IDX       := $(shell $(PY) -c "import json;print(json.load(open('index/manifest.json'))['index_version'])" 2>/dev/null)
IMAGE     ?= $(REGISTRY)/vector-db:$(VERSION)-idx$(IDX)
DOCKER_BUILD = docker buildx build -f services/vector-db/Dockerfile -t $(IMAGE)
GATEWAY_IMAGE  ?= $(REGISTRY)/api-gateway:$(VERSION)
FRONTEND_IMAGE ?= $(REGISTRY)/frontend:$(VERSION)
MODEL_LOCAL    ?= model-service:$(VERSION)
MODEL_IMAGE    ?= $(REGISTRY)/model-service:$(VERSION)
COMPOSE        = docker compose -f deploy/docker/compose.yaml

.PHONY: setup ingest estimate eval validate-index test build-vector-db push-vector-db run-vector-db \
        build-api-gateway push-api-gateway build-frontend push-frontend build-model-service push-model-service run-model-service dev-up dev-down deploy-web

setup:            ## install dev deps
	$(PY) -m pip install -r ingest/requirements.txt -r services/vector-db/requirements.txt -r services/api-gateway/requirements.txt -r services/model/requirements.txt

ingest:           ## build index/ (free fake embedder unless PROVIDER=openrouter)
	$(PY) -m ingest ingest $(if $(PROVIDER),--provider $(PROVIDER))

estimate:         ## free dry run: what a paid openrouter run would send
	$(PY) -m ingest estimate

eval:
	$(PY) -m ingest eval

validate-index:
	$(PY) services/vector-db/app/search.py index

test:
	$(PY) -m pytest ingest/tests services/vector-db/tests services/api-gateway/tests services/model/tests -q

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

# ---- model-service (OpenRouter embeddings + LLM) -------------------------------------------
build-model-service:   ## local single-arch image
	docker buildx build -f services/model/Dockerfile -t $(MODEL_LOCAL) --load .

push-model-service:    ## multi-arch image to the team registry
	docker buildx build -f services/model/Dockerfile -t $(MODEL_IMAGE) --platform $(PLATFORMS) --push .

run-model-service:     ## needs OPENROUTER_API_KEY in your shell; listens on :8081
	docker run --rm -p 8081:8080 -e OPENROUTER_API_KEY -e POD_NAME=local-model-service $(MODEL_LOCAL)

dev-up:              ## frontend + 2 gateways + stub rag-service on http://localhost:8080
	$(COMPOSE) up --build -d

dev-down:
	$(COMPOSE) down

deploy-web:          ## apply both manifests together (nginx needs the api-gateway Service)
	kubectl apply -f deploy/k8s/api-gateway/api-gateway.yaml -f deploy/k8s/frontend/frontend.yaml
