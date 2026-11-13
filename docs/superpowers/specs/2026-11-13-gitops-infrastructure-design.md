# GitOps infrastructure for KhunLook (Kubernetes + Ansible + Docker + Argo CD)

Date: 2026-11-13 · Repo submission: 2026-11-16 23:59 · Demo: 2026-11-17 13:00

## Intent

Run the KhunLook RAG chatbot (frontend → api-gateway → rag → vector-db + model-service → OpenRouter)
on a Kubernetes cluster of 5 Raspberry Pi 3 B+ workers and one master, so that the demo can show:

1. **Automatic deployment from GitHub** — a `git push` to `main` builds images and rolls them out to
   the cluster with no manual `kubectl`.
2. **Each request served by ≥ 2 containers** — already true (5-hop chain, `X-Served-By` / demo mode).
3. **Fault tolerance** — the instructor unplugs a random Pi; replacement pods come up on the remaining
   Pis and requests succeed again.

The instructor must also be able to rebuild the cluster and deploy the app from the README alone.

## Goals

- One Ansible command takes a fresh UTM VM + 5 freshly flashed Pis to a running cluster with Argo CD
  installed and pointed at this repo.
- From then on, Git is the source of truth: Argo CD keeps `deploy/k8s` in sync with the cluster.
- CI builds arm64 images on push, publishes them to GHCR, and commits the new tags to `deploy/k8s`.
- `infra/README.md` is a complete, step-by-step setup guide (VM, Pis, Ansible, secrets, CI, Argo CD,
  demo script, troubleshooting).

## Non-goals

- Highly available control plane (only Pis are pulled in the demo; one master is accepted as a SPOF).
- Terraform, Prometheus/Grafana (`infra/terraform`, `observability/` stay empty / out of scope).
- Changing application code or service behaviour.
- The top-level `README.md` (application overview) — separate piece of work; infra README links to it.

## Anti-goals

- Secrets (OpenRouter key, GitHub deploy key) committed to Git in plain text.
- App pods or Argo CD pods consuming Pi RAM they don't need (Pi 3 B+ has 1 GB).
- A demo that depends on someone typing `kubectl apply` by hand.

## Constraints

- **Master**: Ubuntu Server 24.04 **arm64** VM in UTM on the Apple Silicon Mac (16 GB / 10 cores),
  4 vCPU / 4 GB RAM / 30 GB disk, **bridged** networking, fixed IP via DHCP reservation on the TP-Link
  router. kubeadm cannot run on macOS, and Docker-Desktop clusters sit behind NAT, so a bridged VM is required.
- **Workers**: 5 × Raspberry Pi 3 B+ (1 GB RAM, arm64), 64-bit Ubuntu Server 24.04 or Raspberry Pi OS
  Bookworm 64-bit, wired preferred.
- Everything in the cluster is arm64 → images are built for `linux/arm64` only.
- The repo `BallBoii/khunlook` is private.
- The cluster is on a LAN behind NAT: nothing on the internet can reach it, so all sync is **pull-based**.
- Internet at the venue is required anyway (OpenRouter), so pulling from GitHub/GHCR is acceptable.

## Approaches considered

| | Approach | Trade-off |
|-|----------|-----------|
| A | kubeadm + Ansible does everything incl. `kubectl apply` of the app | Simplest, but "automatic deployment" = re-running a playbook |
| B | k3s + Ansible | Lighter on 1 GB Pis, but rewrites the working worker role 3 days before deadline |
| **C** | **A's cluster + GitHub Actions CI + Argo CD GitOps** | **Chosen by the user.** Best demo of automatic deployment; costs ~500 MB on the master, a CI pipeline, and 5–13 min push-to-cluster latency |

## Design

### Topology

```
                         TP-Link router (LAN, DHCP reservations)
        ┌──────────────────────┬──────────┬──────────┬──────────┬──────────┐
  Mac ─ UTM VM "k8s-master"   pi-worker-1 pi-worker-2 pi-worker-3 pi-worker-4 pi-worker-5
        control plane          kubelet + containerd + flannel on each Pi
        Argo CD (pinned here)  all app pods (2 replicas × 5 services)
        NodePort :30080 (app)  :30443 (Argo CD UI)
```

Browsers use `http://<master-ip>:30080` — the master never goes down, and kube-proxy forwards to frontend
pods on whichever Pis are alive.

### Ansible (`infra/ansible/`)

Run from the Mac (`pip install ansible-core` + `kubernetes.core` collection). One entry point:

```
ansible-playbook site.yml --ask-vault-pass
```

| Role | Hosts | Does |
|------|-------|------|
| `common` | all | swap off, `overlay`/`br_netfilter`, sysctls, **containerd only** (SystemdCgroup), pkgs.k8s.io repo, kubelet/kubeadm/kubectl pinned to `kube_minor`, held |
| `pi_worker` | workers | arm64 assert, hostname, cgroup cmdline + reboot, `kubeadm join` (token delegated to master); idempotent via `/etc/kubernetes/kubelet.conf` |
| `master` | master | `kubeadm init` with a config file (pod CIDR `10.244.0.0/16`, `node-monitor-grace-period=20s` on controller-manager), kubeconfig for the SSH user, fetch `admin.conf` to the Mac (`infra/ansible/artifacts/kubeconfig`, git-ignored), install **Flannel** (pinned manifest) |
| `argocd` | master | install Helm, install Argo CD Helm chart (pinned) with `global.nodeSelector`→master + control-plane toleration, Dex/notifications off, `argocd-server` NodePort 30443, reconciliation `timeout.reconciliation=60s`; create repo Secret (deploy key from vault); create `khunlook` namespace + `openrouter` Secret (from vault); apply the root `Application` |

Changes to existing work: Docker and docker-buildx are **removed** from the Pis (saves RAM; images are
built in CI). Inventory grows to 5 Pis + the VM. Shared prep moves from `pi_worker` into `common`.

Secrets live in `infra/ansible/group_vars/all/vault.yml` (Ansible Vault, encrypted, committed) with a
`vault.example.yml` template: `openrouter_api_key`, `argocd_repo_ssh_key`, optional `ghcr_pull_token`.

### Container images (Docker)

Existing Dockerfiles stay as-is (already non-root, slim, index baked into vector-db).
Image names: `ghcr.io/ballboii/khunlook-<service>:<git-sha>` for
`frontend`, `api-gateway`, `rag`, `vector-db`, `model-service`.
`Makefile` `REGISTRY` default → `ghcr.io/ballboii`, `PLATFORMS` → `linux/arm64`; local `docker compose` dev flow unchanged.

### CI (`.github/workflows/build-deploy.yml`)

On push to `main` (and manual `workflow_dispatch`):

1. **test** — `make test` (pytest per service + node frontend tests).
2. **build** — matrix over the 5 services; only services whose files changed (service dir, plus `index/`
   for vector-db) are built. `docker/setup-qemu-action` + `buildx`, `--platform linux/arm64`, GHA cache,
   push `:<sha>` and `:latest` to GHCR with `GITHUB_TOKEN` (`packages: write`).
3. **bump** — `kustomize edit set image` for each built service in `deploy/k8s/kustomization.yaml`,
   commit `deploy: <services> → <sha>` back to `main` as `github-actions[bot]`.
   No loop: commits made with `GITHUB_TOKEN` don't trigger workflows, and `deploy/**` is path-ignored.

### Manifests (`deploy/k8s/`) and Argo CD

- New `deploy/k8s/kustomization.yaml`: namespace `khunlook`, lists the 5 service manifests +
  `prepull.yaml`, and an `images:` block that is the **only** place tags live.
- Manifests: image fields become `ghcr.io/ballboii/khunlook-<svc>` (tag set by kustomize);
  drop `nodeSelector: kubernetes.io/arch: arm64` (master is arm64 now) — the master's default
  `control-plane:NoSchedule` taint keeps app pods on the Pis.
- New `prepull.yaml`: a DaemonSet whose init containers are the 5 app images (each exits immediately)
  and whose main container is `pause`. Every Pi keeps every current image cached, so a replacement pod
  after a Pi is pulled starts without a download. Kustomize's `images:` keeps its tags in sync.
- New `deploy/argocd/khunlook-app.yaml`: Argo CD `Application` → repo `git@github.com:BallBoii/khunlook.git`,
  `targetRevision: main`, `path: deploy/k8s`, destination namespace `khunlook`,
  `syncPolicy.automated {prune: true, selfHeal: true}`.
- The `openrouter` Secret is created by Ansible, not Argo CD (it's not in Git).
- Existing replicas (2), anti-affinity, 20 s unreachable tolerations, probes are kept.

### Expected failover timing (Pi pulled)

node marked NotReady ≈ 20 s (grace period) → pods evicted after 20 s toleration → new pods start from
cached images (≈ 10–60 s on a Pi 3 B+ for Python services). Requests may fail during this window
(allowed by the rubric); with 2 replicas usually only the share routed to the dead Pi fails.

### `infra/README.md` (rewritten guide)

1. Architecture diagram + what runs where.
2. Hardware & network: router, DHCP reservations, IP plan table.
3. Master VM in UTM: download Ubuntu Server 24.04 arm64 ISO, VM settings (bridged, 4 vCPU/4 GB/30 GB),
   install with OpenSSH, copy SSH key.
4. Flash the Pis (Raspberry Pi Imager, 64-bit, hostname/user/SSH key preset).
5. GitHub: create deploy key (read-only), GHCR package visibility, Actions permissions (read & write).
6. Ansible: install, edit inventory, create vault, run `site.yml`, verify (`kubectl get nodes`).
7. Argo CD: open UI at `https://<master-ip>:30443`, initial admin password, see the app sync.
8. Day-to-day: push → CI → bump commit → Argo sync; manual "Sync now".
9. Demo script: open app with `?demo`, show served-by chain, pull a Pi, watch `kubectl get pods -o wide -w`.
10. Troubleshooting + reset (`kubeadm reset`, re-run playbook for one node).

## Decided by the user

- Single master, hosted as an Ubuntu VM in UTM on the Mac.
- Approach C (Argo CD GitOps + GitHub Actions CI).
- Setup guide lives in the infra README.

## Calls made by the designer (check these)

- kubeadm (not k3s), Flannel CNI, Kubernetes v1.34.
- GHCR as registry, packages made **public** so Pis need no pull secret (optional `ghcr_pull_token` path kept).
- arm64-only images; Docker removed from the Pis.
- Argo CD installed via Helm, pinned to the master, UI on NodePort 30443, 60 s reconciliation.
- CI commits tag bumps straight to `main` (assumes no branch protection on `main`).
- `node-monitor-grace-period=20s` to speed up failover.
- Image pre-pull DaemonSet.
- Namespace `khunlook`.

## Left to the builder

- Exact pinned versions (Argo CD chart, Flannel, actions) — latest stable at build time.
- Mechanism for "only changed services" in CI (e.g. `dorny/paths-filter`).
- Resource requests for the prepull DaemonSet (tiny).
