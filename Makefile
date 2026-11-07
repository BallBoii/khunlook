# Data part: offline ingestion + vector-db. Every target is a thin wrapper, so on Windows
# without make you can run the command on the right directly.
PY        ?= python
REGISTRY  ?= CHANGE_ME_REGISTRY
VERSION   ?= 1.0.0
PLATFORMS ?= linux/arm64,linux/amd64
IDX       := $(shell $(PY) -c "import json;print(json.load(open('index/manifest.json'))['index_version'])" 2>/dev/null)
IMAGE     ?= $(REGISTRY)/vector-db:$(VERSION)-idx$(IDX)
DOCKER_BUILD = docker buildx build -f services/vector-db/Dockerfile -t $(IMAGE)

.PHONY: setup ingest estimate eval validate-index test build-vector-db push-vector-db run-vector-db

setup:            ## install dev deps
	$(PY) -m pip install -r ingest/requirements.txt -r services/vector-db/requirements.txt

ingest:           ## build index/ (free fake embedder unless PROVIDER=openrouter)
	$(PY) -m ingest ingest $(if $(PROVIDER),--provider $(PROVIDER))

estimate:         ## free dry run: what a paid openrouter run would send
	$(PY) -m ingest estimate

eval:
	$(PY) -m ingest eval

validate-index:
	$(PY) services/vector-db/app/search.py index

test:
	$(PY) -m pytest ingest/tests services/vector-db/tests -q

build-vector-db: validate-index   ## local single-arch image
	$(DOCKER_BUILD) --load .

push-vector-db: validate-index    ## multi-arch image to the team registry
	$(DOCKER_BUILD) --platform $(PLATFORMS) --push .

run-vector-db:
	docker run --rm -p 8080:8080 -e POD_NAME=local-vector-db $(IMAGE)
