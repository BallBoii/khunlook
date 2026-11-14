# GitOps Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Ansible run builds a kubeadm cluster (UTM master + 5 Pis) with Argo CD; every push to `main` is built by GitHub Actions and rolled out by Argo CD.

**Architecture:** Ansible roles `common` → `master` → `pi_worker` → `argocd`; Kustomize root at `deploy/k8s` holds the only image tags; CI bumps those tags; Argo CD `Application` syncs `deploy/k8s`.

**Tech Stack:** Ansible (ansible-core + kubernetes.core), kubeadm v1.36, Flannel v0.28.10, Argo CD Helm chart 10.17.2 (app v3.6.1), Kustomize, GitHub Actions, GHCR, Docker buildx.

**Spec:** `docs/superpowers/specs/2026-11-13-gitops-infrastructure-design.md`

## Global Constraints

- Images: `ghcr.io/ballboii/khunlook-<svc>` for `frontend, api-gateway, rag, vector-db, model-service`; arm64 only.
- Namespace `khunlook`; app NodePort 30080; Argo CD HTTPS NodePort 30443, HTTP NodePort 30081 (chart default 30080 collides with the app).
- Pod CIDR `10.244.0.0/16`; controller-manager `node-monitor-grace-period=20s`.
- No plaintext secrets in Git: OpenRouter key and deploy key only in Ansible Vault.
- No Docker on the Pis; containerd with `SystemdCgroup = true`.
- Argo CD pods pinned to the master (nodeSelector `node-role.kubernetes.io/control-plane: ""` + control-plane toleration).

## Review Focus

- Re-running `site.yml` on a built cluster must be a no-op (no re-init, no re-join, Helm upgrade only).
- A Pi on Raspberry Pi OS (`/boot/firmware/cmdline.txt`) and on Ubuntu (`/boot/firmware/cmdline.txt` too, but no `dphys-swapfile`) must both work.
- CI on a push that touches only docs must not build or bump anything.
- Vault missing a key → playbook fails early with a clear message, not half-way.
- `kubectl kustomize deploy/k8s` must render every image with the same tag as the prepull DaemonSet.

---

### Task 1: Ansible roles and site playbook
**Files:** create `infra/ansible/{site.yml,requirements.yml,group_vars/all/main.yml,group_vars/all/vault.example.yml}`, roles `common`, `master`, `argocd` (+ templates); modify `pi_worker`, `inventory.ini`, `ansible.cfg`, `workers.yml`; `.gitignore` adds `infra/ansible/artifacts/`.
- [ ] Move node prep from `pi_worker` to `common`; drop docker packages; install kubelet/kubeadm/kubectl, hold them.
- [ ] `master`: kubeadm config template, `kubeadm init` guarded by `/etc/kubernetes/admin.conf`, kubeconfig for user, fetch to `artifacts/kubeconfig`, Flannel.
- [ ] `argocd`: preflight vault asserts, Helm install, chart values, namespace + `openrouter` secret, repo secret, `Application`.
- [ ] Verify: `ansible-playbook --syntax-check site.yml` and `ansible-lint` pass.
- [ ] Commit.

### Task 2: Kustomize root, manifests, prepull, Argo Application
**Files:** create `deploy/k8s/kustomization.yaml`, `deploy/k8s/prepull/prepull.yaml`, `deploy/argocd/khunlook-app.yaml`; modify the 5 service manifests.
- [ ] Verify: `kubectl kustomize deploy/k8s` renders 11 objects in namespace `khunlook`, all images `ghcr.io/ballboii/khunlook-*`, no `kubernetes.io/arch` selector.
- [ ] Commit.

### Task 3: CI workflow
**Files:** create `.github/workflows/build-deploy.yml`.
- [ ] Jobs `changes` (paths-filter) → `test` → `build` (matrix of changed) → `bump` (kustomize edit set image, commit `[skip ci]`).
- [ ] Verify: `actionlint` passes.
- [ ] Commit.

### Task 4: Makefile + infra README
- [ ] Makefile: `REGISTRY ?= ghcr.io/ballboii`, `PLATFORMS ?= linux/arm64`, image names `khunlook-<svc>`, targets `cluster`, `render`.
- [ ] Rewrite `infra/README.md` per spec section "infra/README.md".
- [ ] Commit.
