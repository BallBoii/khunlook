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

Steps 1–4 are done on the Mac and in a browser. **From step 5 on, every command is typed on the master
VM.** More detail and troubleshooting: **[infra/README.md](infra/README.md)**.

### 1. Create the master VM (on the Mac)

1. Connect the Mac and the Pis to the router **with cables**.
2. Download **Ubuntu Server 24.04 LTS for ARM** (`ubuntu-24.04.x-live-server-arm64.iso`).
3. UTM → **Create a New Virtual Machine** → **Virtualize** → **Linux** → select the ISO.
   Set **4 CPU cores, 4096 MB RAM, 30 GB** storage.
4. **Before the first boot**, open the VM's settings → **Network**:

   | Setting | Value |
   |---------|-------|
   | Network Mode | **Bridged (Advanced)** |
   | Bridged Interface | the Mac's **Ethernet** adapter, not Wi-Fi (list them with `networksetup -listallhardwareports`) |
   | Emulated Network Card | **virtio-net-pci** |

5. Install Ubuntu: user `ubuntu`, hostname `k8s-master`, tick **Install OpenSSH server**. Then eject the
   ISO (*CD/DVD → Clear*) and reboot.
6. Run `caffeinate -dimsu` in a Mac terminal and leave it open. If the Mac sleeps, the cluster stops.

### 2. Flash the Pis

Flash each SD card with **Raspberry Pi OS Lite (64-bit)** in Raspberry Pi Imager. In *Edit settings*:

- hostname `pi-worker-N` (a different number on each Pi)
- username `pi` and a password, the **same password on every Pi**
- *Services* → **Enable SSH** → **Use password authentication**

Plug in the network cable and power on.

### 3. Give every machine a fixed IP (on the router)

1. Open **http://192.168.0.1** → **DHCP → DHCP Client List**. Write down the IP of `k8s-master` and of
   each `pi-worker-N`.
2. **DHCP → Address Reservation → Add New** for each machine: its MAC address and its current IP →
   *Enabled* → **Save**.
3. Reboot the VM and the Pis and check the list shows the same IPs.

The master's IP must never change afterwards: a new IP means rebuilding the cluster.

### 4. Prepare GitHub (once per repository)

1. *Settings → Actions → General → Workflow permissions*: **Read and write**.
2. *Actions → build-deploy → Run workflow* to build all 5 images (~10–15 min).
3. *Packages* → each `khunlook-*` package → *Package settings* → make it **public**.

### 5. Install the tools (on the master VM)

Log in to the VM (the UTM window, or `ssh ubuntu@<master-ip>` from the Mac):

```bash
sudo apt update && sudo apt install -y git make sshpass pipx
pipx install ansible-core && pipx ensurepath && exec $SHELL -l
git clone https://github.com/BallBoii/khunlook.git && cd khunlook
```

### 6. Fill in the inventory and the secrets

1. Put the IPs from step 3 in `infra/ansible/inventory.ini`:

   ```ini
   [master]
   k8s-master  ansible_host=192.168.0.106 ansible_user=ubuntu ansible_connection=local

   [workers]
   pi-worker-1 ansible_host=192.168.0.103
   pi-worker-2 ansible_host=192.168.0.104
   ```

2. Create the secrets file:

   ```bash
   cp infra/ansible/vault.example.yml infra/ansible/group_vars/all/vault.yml
   nano infra/ansible/group_vars/all/vault.yml
   ```

   | Variable | Value |
   |----------|-------|
   | `vault_openrouter_api_key` | your key from https://openrouter.ai/keys |
   | `vault_master_become_password` | password of `ubuntu` on the VM |
   | `vault_workers_ssh_password` | password of `pi` on the Pis |
   | `vault_workers_become_password` | the same `pi` password |

   The file is plain text and git-ignored. Never commit it.

3. Check that Ansible reaches every machine:

   ```bash
   (cd infra/ansible && ansible all -m ping)      # every machine answers "pong"
   ```

### 7. Build the cluster and deploy

```bash
make cluster          # from the repo root, takes ~20–30 min
```

This installs Kubernetes on every node, joins the Pis and installs Argo CD, which then deploys the
application from this repository.

### 8. Check it

```bash
kubectl get nodes                     # master and every Pi are Ready
kubectl -n khunlook get pods -o wide  # 2 pods per service, spread over the Pis
make argocd-password                  # Argo CD login (user: admin)
```

Open in a browser, with your master's IP:

- **Chatbot:** http://192.168.0.106:30080/?demo
- **Argo CD:** https://192.168.0.106:30443

### 9. Deploy a change

Commit and `git push` to `main`. CI builds the changed services and Argo CD rolls them out within about
a minute. To roll back, `git revert` the `deploy:` commit.

---

## Controlling the cluster

Run these on the master VM, from the repo root.

### Install the Argo CD CLI and log in (once)

```bash
curl -sSL -o argocd https://github.com/argoproj/argo-cd/releases/download/v3.6.1/argocd-linux-arm64
sudo install -m 555 argocd /usr/local/bin/argocd && rm argocd

argocd login <master-ip>:30443 --username admin --password "$(make -s argocd-password)" --insecure
```

`--insecure` accepts Argo CD's self-signed certificate.

### Turn automatic sync off and on

```bash
argocd app get khunlook                                                  # sync policy, status and health
argocd app set khunlook --sync-policy none                               # OFF: Argo CD stops changing the cluster
argocd app set khunlook --sync-policy automated --auto-prune --self-heal # ON again
argocd app sync khunlook                                                 # one manual sync while it is off
```

While automatic sync is off, a `git push` is not deployed and manual `kubectl` changes are kept.
Turning it back on makes the cluster match Git again. `make cluster` and `make deploy` also turn it
back on, because they re-apply `deploy/argocd/khunlook-app.yaml`.

### Change the number of replicas

**Permanently (through Git).** Edit `replicas:` in the service's manifest, for example
`deploy/k8s/rag/rag.yaml`, then commit and `git push` to `main`. Argo CD applies it within about a
minute.

**Temporarily (by hand).** Turn automatic sync off first; otherwise Argo CD puts the number back within
seconds.

```bash
argocd app set khunlook --sync-policy none
kubectl -n khunlook scale deploy/rag --replicas=3         # one service
kubectl -n khunlook scale deploy --all --replicas=2       # every service
kubectl -n khunlook get pods -o wide                      # see where the pods landed
```

The services are `frontend`, `api-gateway`, `rag`, `vector-db` and `model-service`. When automatic
sync is turned back on, the replica counts return to the values in Git.

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
