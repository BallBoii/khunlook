# KhunLook: a course Q&A chatbot on a Raspberry Pi Kubernetes cluster

2110415 Software-Defined Systems, final project (Semester 1/2026).

KhunLook answers questions about Chulalongkorn University courses: what a course covers, its
prerequisites and similar. It is a **RAG (retrieval-augmented generation)** chatbot. It searches a
knowledge base of **251 course descriptions** (783 indexed passages) and has an LLM write an answer
that cites those passages. The embedding model and the LLM run in the cloud via
[OpenRouter](https://openrouter.ai); everything else runs on a Kubernetes cluster of Raspberry Pis.

## Architecture

Five services, each its own container image, all stateless with 2 replicas spread over the Pis:

```
 browser ─► frontend ─► api-gateway ─► rag ─┬─► vector-db       (search the course index)
            (nginx)     (rate limits,       └─► model-service ─► OpenRouter
                         timeouts, retry)        (embeddings: baai/bge-m3, LLM: gemma-3-4b-it)
```

| Service | Image | Role |
|---------|-------|------|
| [frontend](services/frontend) | `khunlook-frontend` | Chat page (nginx); forwards `/api/*` to the gateway. The only service exposed outside the cluster (NodePort 30080) |
| [api-gateway](services/api-gateway) | `khunlook-api-gateway` | Single entry to the backend: per-client rate limits, timeouts, one safe retry, request IDs |
| [rag](services/rag) | `khunlook-rag` | Embeds the question, searches vector-db, builds the prompt, asks the LLM, returns answer + sources |
| [vector-db](services/vector-db) | `khunlook-vector-db` | Read-only cosine search over the index built into the image (`index/`) |
| [model-service](services/model) | `khunlook-model-service` | The only pod holding the OpenRouter key; serves embeddings and chat completions |

**Every request passes through at least 4 containers** (frontend → api-gateway → rag → vector-db +
model-service). The containers are usually on different Pis. Open the page with **`?demo`** to see
which pod served each hop.

**Fault tolerance.** Every service runs 2 replicas, spread over different Pis. When a Pi is
unplugged, it is marked `NotReady` after ~20 s. Its pods are recreated on the remaining Pis about 20 s
later, and they start quickly because every image is already cached on every Pi. Until the
replacements are up, requests that reach the dead pods may fail; after that, every request succeeds again.

## Infrastructure

Diagrams of the network, node layout, request path, GitOps flow and failover:
**[docs/infrastructure.md](docs/infrastructure.md)**. Every IP, Service, DNS name and port:
**[docs/network.md](docs/network.md)**.

| Layer | Tooling |
|-------|---------|
| Cluster | 1 master: an Ubuntu 24.04 arm64 VM in UTM on a Mac (bridged to the LAN). 5 workers: Raspberry Pi 3 B+. Kubernetes v1.36 (kubeadm), Flannel network |
| Provisioning | **Ansible**: one playbook prepares every node, creates the cluster, joins the Pis and installs Argo CD |
| Images | **Docker** (multi-stage where useful, non-root, `linux/arm64`), published to GitHub Container Registry |
| Deployment | **GitOps**: GitHub Actions builds and tags images on every push; **Argo CD** on the master syncs `deploy/k8s` to the cluster |

```
git push ─► GitHub Actions: test ─► build changed images ─► GHCR ─► commit new tags to deploy/k8s
                                                                          │
              Argo CD (master VM) polls main every ~60 s ◄────────────────┘ ─► rolling update on the Pis
```

---

## How to set up the cluster and deploy the application

The full step-by-step guide, with troubleshooting, is
**[infra/README.md](infra/README.md)**. In short:

### 1. Prepare the hardware

- Connect the Mac and the 5 Pis to the TP-Link router (cables recommended). Reserve fixed IPs:
  master VM `192.168.0.10`, Pis `192.168.0.21`–`.25`
  (or edit [`infra/ansible/inventory.ini`](infra/ansible/inventory.ini)).
- **Master:** in UTM, create an **Ubuntu Server 24.04 ARM64** VM (4 CPU, 4 GB RAM, 30 GB disk) with
  network mode **Bridged**, user `ubuntu`, OpenSSH enabled. Copy your SSH key: `ssh-copy-id ubuntu@192.168.0.10`.
- **Workers:** flash each Pi with **Raspberry Pi OS Lite (64-bit)** using Raspberry Pi Imager. Set the
  hostname `pi-worker-N`, user `pi`, and SSH with your public key.
- On the Mac: `brew install ansible kubectl`, then check access with `cd infra/ansible && ansible all -m ping`.

### 2. Prepare GitHub (once per repository)

1. Create a read-only **deploy key** for Argo CD:
   `ssh-keygen -t ed25519 -N "" -f ~/.ssh/argocd-deploy-key`, and add the `.pub` under
   *Settings → Deploy keys*.
2. *Settings → Actions → General → Workflow permissions*: **Read and write**.
3. *Actions → build-deploy → Run workflow* to build all 5 images once (~10–15 min).
4. Make the 5 `khunlook-*` packages **public** (*Packages → Package settings*), or use a pull token
   (see the guide).

> Using a fork? Replace `ballboii` / `BallBoii/khunlook` with your account and repository name in
> `deploy/`, the workflow and `infra/ansible/group_vars/all/main.yml`
> ([details](infra/README.md#5-github-repository-registry-and-ci-settings)).

### 3. Add the secrets

```bash
cd infra/ansible
cp vault.example.yml group_vars/all/vault.yml
$EDITOR group_vars/all/vault.yml           # OpenRouter API key + private deploy key
ansible-vault encrypt group_vars/all/vault.yml
```

### 4. Build the cluster and deploy (one command)

```bash
make cluster          # from the repo root; asks for the vault password, takes ~20–30 min
```

This installs containerd and Kubernetes on every node, initialises the master, joins the 5 Pis,
installs Argo CD and points it at this repository. Argo CD then deploys the application.

### 5. Use it

```bash
export KUBECONFIG=$PWD/infra/ansible/artifacts/kubeconfig
kubectl get nodes                     # master + 5 Pis Ready
kubectl -n khunlook get pods -o wide  # 2 pods per service, spread over the Pis
make argocd-password                  # Argo CD login (user: admin)
```

- **Chatbot:** http://192.168.0.10:30080/?demo
- **Argo CD:** https://192.168.0.10:30443

### 6. Deploy a change

Commit and `git push` to `main`. CI tests and builds the changed services and commits the new image
tags; Argo CD rolls them out within about a minute. No `kubectl` is needed. To roll back,
`git revert` the `deploy:` commit.

---

## Running locally (without a cluster)

Requires Docker and an OpenRouter key:

```bash
cp services/model/.env.example services/model/.env    # set OPENROUTER_API_KEY
make dev-up                                            # http://localhost:8080/?demo
make dev-down
```

Tests (Python 3.12 + Node 18+): `make setup PY=python3 && make test PY=python3`.

## Repository layout

```
services/       the 5 services: code, tests and Dockerfile for each
index/          the built vector index (baked into the vector-db image)
ingest/, corpus/  offline pipeline that builds index/ from the course documents
deploy/k8s/     Kubernetes manifests + kustomization.yaml (image tags) - what Argo CD deploys
deploy/argocd/  the Argo CD Application
deploy/docker/  docker compose for local runs
infra/ansible/  cluster provisioning (site.yml) - see infra/README.md
.github/workflows/build-deploy.yml   CI: test, build, push, bump tags
```
