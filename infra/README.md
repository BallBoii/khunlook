# Infrastructure: cluster setup and GitOps deployment

This guide takes you from **one Mac + five freshly flashed Raspberry Pis** to the KhunLook chatbot
running on Kubernetes. After that, every `git push` to `main` is deployed automatically.

```
                          TP-Link router (192.168.0.0/24, DHCP reservations)
       ┌────────────────────────┬────────────┬────────────┬────────────┬────────────┐
 Mac ── UTM VM  k8s-master       pi-worker-1  pi-worker-2  pi-worker-3  pi-worker-4  pi-worker-5
       Ubuntu 24.04 arm64        Raspberry Pi 3 B+ (arm64, 1 GB), containerd + kubelet + Flannel
       control plane + Argo CD   all app pods: 5 services × 2 replicas, spread over the Pis
       :30080 app  :30443 Argo CD

 git push ─► GitHub Actions: test ─► build arm64 images ─► ghcr.io/ballboii/khunlook-<svc>:<sha>
                             └─► commit new tags to deploy/k8s/kustomization.yaml
 Argo CD (on the master) polls main every ~60 s ─► applies deploy/k8s ─► rolling update on the Pis
```

| Piece | Where | What it does |
|-------|-------|--------------|
| Ansible | [`ansible/`](ansible) | Prepares every node, runs `kubeadm init`/`join`, installs Flannel and Argo CD, creates the secrets |
| Docker images | `services/*/Dockerfile` | One image per service; built by CI (or `make build-*` locally) |
| Manifests | [`../deploy/k8s`](../deploy/k8s) | Deployments + Services + image-prepull DaemonSet; `kustomization.yaml` holds the image tags |
| Argo CD app | [`../deploy/argocd/khunlook-app.yaml`](../deploy/argocd/khunlook-app.yaml) | Keeps the cluster equal to `deploy/k8s` on `main` (auto-sync, prune, self-heal) |
| CI | [`../.github/workflows/build-deploy.yml`](../.github/workflows/build-deploy.yml) | Tests, builds changed services, pushes to GHCR, bumps tags |

More diagrams (what runs on each node, request path, failover timeline): [docs/infrastructure.md](../docs/infrastructure.md).

**Time needed:** about 1 hour the first time, mostly waiting for apt and image pulls on the Pis.

---

## 1. What you need

- **Mac** (Apple Silicon) with [UTM](https://mac.getutm.app), Docker Desktop (optional, for local
  builds) and `kubectl` (`brew install kubectl`).
- **Ansible** on the Mac: `brew install ansible` (or `pipx install ansible-core`). No extra collections needed.
- **5 × Raspberry Pi 3 B+** with power, SD cards and Ethernet cables, and the **TP-Link router**.
- Internet through the router. The Pis pull images from GHCR, Argo CD reads GitHub, and
  model-service calls OpenRouter.
- An **OpenRouter API key** (https://openrouter.ai/keys).

## 2. Network plan

Connect everything to the router by cable (Wi-Fi works, but cables are more reliable for the demo). In the router's
admin page (http://192.168.0.1 → *DHCP → Address Reservation*), give each machine a fixed IP:

| Host | IP | User | Notes |
|------|----|------|-------|
| `k8s-master` (UTM VM) | 192.168.0.10 | `ubuntu` | **Must never change**: it is baked into the cluster certificates |
| `pi-worker-1` … `pi-worker-5` | 192.168.0.21 … .25 | `pi` | |

Using other IPs or users? Edit [`ansible/inventory.ini`](ansible/inventory.ini) to match.

## 3. Master: Ubuntu VM in UTM

kubeadm needs Linux, so the master is a VM. It must be **bridged** onto the LAN: the Pis have to reach
it directly, which rules out NAT and Docker Desktop.

1. Download **Ubuntu Server 24.04 LTS for ARM** (`ubuntu-24.04.x-live-server-arm64.iso`).
2. UTM → **Create a New Virtual Machine** → **Virtualize** → **Linux** → select the ISO.
3. Hardware: **4 CPU cores, 4096 MB RAM**. Storage: **30 GB**.
4. Before the first boot, open the VM's settings → **Network** → Network Mode **Bridged (Advanced)** →
   Bridged Interface = the Mac interface that is connected to the TP-Link router (`en0` / the USB
   Ethernet adapter). Note the VM's **MAC address** shown there for the DHCP reservation.
5. Install Ubuntu: user `ubuntu`, hostname `k8s-master`, tick **Install OpenSSH server**. No snaps needed.
   After installing, eject the ISO (CD/DVD → Clear) and reboot.
6. On the router, reserve **192.168.0.10** for the VM's MAC, then reboot the VM and check:
   ```bash
   ssh-copy-id ubuntu@192.168.0.10      # from the Mac
   ssh ubuntu@192.168.0.10 'ip -4 addr show | grep 192.168.0.10 && sudo -n true && echo sudo-ok'
   ```
   If `sudo -n true` fails, allow passwordless sudo:
   `echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ubuntu`.
7. **Keep the Mac awake** while the cluster runs, e.g. by running `caffeinate -dims` in a terminal.
   If the Mac sleeps, the control plane stops.

## 4. Workers: flash the Pis

Use **Raspberry Pi Imager** for each SD card:

- OS: **Raspberry Pi OS Lite (64-bit)** (recommended for 1 GB of RAM) or **Ubuntu Server 24.04 LTS (64-bit)**.
  A 32-bit image will not work; the playbook stops on it.
- *Edit settings* (OS customisation):
  - hostname `pi-worker-N`, username **`pi`** with a password
  - **Services → Enable SSH → Allow public-key authentication only**, paste your Mac's `~/.ssh/id_ed25519.pub`
  - no Wi-Fi if you use cables

Boot all five Pis and reserve their IPs on the router. Check from the Mac:

```bash
cd infra/ansible
ansible all -m ping          # every host must answer "pong"
ansible all -b -m command -a 'true'   # and passwordless sudo must work
```

### Preparing the Pis before the master exists (optional)

The Pis can be prepared without the master: Ansible does everything except joining.

```bash
cd infra/ansible
ansible workers -m ping                                         # the 5 Pis answer "pong"
ansible-playbook site.yml --limit workers --skip-tags join      # add --ask-vault-pass once vault.yml exists
```

This sets the hostname, turns swap off, enables memory cgroups (**each Pi reboots once**), and
installs containerd and kubelet/kubeadm/kubectl v1.36. The kubelet restarts in a loop until the Pi
joins; that is normal. Once the master VM is up, `make cluster` (step 7) skips everything already done
and only joins the Pis.

## 5. GitHub: repository, registry and CI settings

Run these steps once per repository (a fork needs them too).

**a. Deploy key for Argo CD** (read-only access to this private repo):

```bash
ssh-keygen -t ed25519 -N "" -C argocd -f ~/.ssh/argocd-deploy-key
cat ~/.ssh/argocd-deploy-key.pub
```

GitHub → repo **Settings → Deploy keys → Add deploy key** → paste the `.pub`. Leave *Allow write access* **off**.

**b. Let CI push the tag bumps**: **Settings → Actions → General → Workflow permissions →
Read and write permissions**. If `main` has branch protection, allow GitHub Actions to bypass it,
or CI cannot commit the tag bumps.

**c. Build the images once**: merge this setup into `main`, then **Actions → build-deploy → Run workflow**.
This builds all 5 images (about 10–15 min under QEMU) and commits their tags to `deploy/k8s/kustomization.yaml`.

**d. Make the images pullable** (pick one):
- *Public images (simplest)*: GitHub → your profile → **Packages** → open each `khunlook-*` package →
  **Package settings → Change visibility → Public**. The repo itself stays private.
- *Private images*: create a classic PAT with `read:packages` and set `vault_ghcr_pull_user` /
  `vault_ghcr_pull_token` in step 6. Ansible then creates a pull secret for the app namespace.

**e. Optional, faster builds**: if your plan offers arm64 runners, set **Settings → Secrets and variables →
Actions → Variables → `ARM_RUNNER` = `ubuntu-24.04-arm`**. Builds then run natively instead of under QEMU.

> Forked or renamed repository? Replace `ballboii`/`BallBoii/khunlook` in
> `deploy/k8s/kustomization.yaml`, `deploy/k8s/*/*.yaml`, `deploy/k8s/prepull/prepull.yaml`,
> `deploy/argocd/khunlook-app.yaml`, `IMAGE_PREFIX` in the workflow, `REGISTRY` in the Makefile, and
> `app_repo_url` in `ansible/group_vars/all/main.yml`. GHCR names must be lowercase.

## 6. Secrets (Ansible Vault)

```bash
cd infra/ansible
cp vault.example.yml group_vars/all/vault.yml
$EDITOR group_vars/all/vault.yml     # OpenRouter key + the PRIVATE deploy key (~/.ssh/argocd-deploy-key)
ansible-vault encrypt group_vars/all/vault.yml
```

If the master and the Pis need **different sudo passwords**, set `vault_master_become_password` and
`vault_workers_become_password` in the vault (leave `""` for passwordless sudo). Don't pass
`--ask-become-pass`: it sends one password to every host. To add them to an existing vault:
`ansible-vault edit group_vars/all/vault.yml`.

`group_vars/all/vault.yml` is git-ignored. Nothing secret is ever committed. If a value is missing, the
playbook stops at the very first task.

## 7. Build the cluster (one command)

```bash
make cluster            # = cd infra/ansible && ansible-playbook site.yml --ask-vault-pass
```

What happens, in order:

| Play | Hosts | Steps |
|------|-------|-------|
| Preflight | Mac | Checks the vault secrets are filled in |
| `common` | all | hostname, swap off, kernel modules + sysctls, **containerd** (systemd cgroups), kubelet/kubeadm/kubectl from pkgs.k8s.io **v1.36**, held at that version |
| `master` | VM | `kubeadm init` (pod CIDR 10.244.0.0/16, node grace period 20 s), kubeconfig for `ubuntu`, copies it to `infra/ansible/artifacts/kubeconfig`, installs **Flannel v0.28.10** |
| `pi_worker` | Pis | arm64 check, memory cgroups on the kernel cmdline (**reboots once**), `kubeadm join`, waits for `Ready` |
| `argocd` | VM | Helm, **Argo CD** chart 10.17.2 pinned to the master, `khunlook` namespace, `openrouter` + repo secrets, the Application |

The playbook is safe to re-run: initialised or joined nodes are skipped and Helm only upgrades what changed.
To try a single Pi first, run `ansible-playbook site.yml --ask-vault-pass --limit k8s-master,pi-worker-1`.

## 8. Check it

```bash
export KUBECONFIG=$PWD/infra/ansible/artifacts/kubeconfig     # from the repo root
kubectl get nodes -o wide                  # k8s-master + 5 Pis, all Ready
kubectl -n khunlook get pods -o wide       # 10 app pods on the Pis + 5 image-prepull pods
make argocd-password                       # Argo CD admin password
```

- **App:** http://192.168.0.10:30080/?demo (`?demo` shows which pods served each request)
- **Argo CD:** https://192.168.0.10:30443 (user `admin`; accept the self-signed certificate). The app
  `khunlook` should be **Synced / Healthy**.

## 9. Deploying changes (day to day)

1. Change code under `services/` (or rebuild `index/`), commit, `git push` to `main`.
2. GitHub Actions runs the tests, builds **only the services that changed**, pushes
   `ghcr.io/ballboii/khunlook-<svc>:<commit-sha>` and commits `deploy: <svc> -> <sha>` to `main`.
3. Argo CD notices within ~60 s and does a rolling update. To skip the wait, click **Refresh**
   (or **Sync**) on the app in the Argo CD UI.

- **Changing manifests** (replicas, env vars): edit `deploy/k8s/**` and push. Argo CD applies the change; no CI run is needed.
- **Manual `kubectl edit`s** are reverted by self-heal. Git is the source of truth.
- **Rollback:** `git revert` the `deploy:` commit and push.
- **Preview a change:** `make render` prints exactly what Argo CD will apply.

## 10. Demo script

1. **Automatic deployment.** Show the Argo CD UI (Synced). Push a small visible change, e.g. a text in
   `services/frontend/site/index.html`. Show the Actions run, the `deploy:` commit, then Argo CD rolling
   the new frontend pods. Reload the page.
2. **Requests through ≥ 2 containers.** Open `http://192.168.0.10:30080/?demo` and ask a question. The
   served-by chain lists frontend → api-gateway → rag → vector-db + model-service pods, which are on different Pis.
3. **Fault tolerance.** In a terminal:
   ```bash
   kubectl -n khunlook get pods -o wide -w
   ```
   Unplug the Pi the instructor picks. After ~20 s the node is `NotReady`. ~20 s later its pods are
   recreated on the other Pis, which already have the images cached by `image-prepull`, so they start
   in seconds. Ask a question again: it succeeds. Plug the Pi back in and it rejoins on its own.
   Pods do not move back automatically; that is expected.

## Layout

```
infra/ansible/
├── ansible.cfg              # inventory, roles path, sudo
├── inventory.ini            # [master] VM, [workers] Pis, [cluster] = both
├── site.yml                 # the one playbook: preflight → common → master → pi_worker → argocd
├── vault.example.yml        # template for group_vars/all/vault.yml (git-ignored)
├── group_vars/all/main.yml  # versions, CIDR, timings, repo URL
├── artifacts/               # kubeconfig fetched from the master (git-ignored)
└── roles/
    ├── common/              # every node: kernel prep, containerd, kube packages
    ├── master/              # kubeadm init (templates/kubeadm-config.yaml.j2), Flannel
    ├── pi_worker/           # cgroups + reboot, kubeadm join
    └── argocd/              # Helm, Argo CD values, secrets, Application
```

Main settings ([`ansible/group_vars/all/main.yml`](ansible/group_vars/all/main.yml)):

| Variable | Default | Meaning |
|----------|---------|---------|
| `kube_minor` | `v1.36` | Kubernetes package repo; all nodes get the same version |
| `flannel_version` | `v0.28.10` | CNI manifest version |
| `argocd_chart_version` | `10.17.2` | Argo CD Helm chart (Argo CD v3.6.1) |
| `node_monitor_grace_period` | `20s` | How fast a pulled Pi is marked NotReady (Kubernetes default 50 s) |
| `node_status_update_frequency` | `5s` | How often kubelets report (default 10 s) |
| `app_repo_url` / `app_repo_revision` | `git@github.com:BallBoii/khunlook.git` / `main` | What Argo CD watches |

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Require 64-bit OS` fails | Reflash the Pi with a 64-bit image |
| Preflight assertion fails | Fill `group_vars/all/vault.yml` (step 6) and pass `--ask-vault-pass` |
| `ansible all -m ping` fails for the VM | VM not bridged, or IP changed; check UTM network mode and the DHCP reservation |
| Node joins but stays `NotReady` | `kubectl -n kube-flannel get pods -o wide`; on a Pi with both Wi-Fi and Ethernet, disable the one you don't use so Flannel picks the right interface |
| kubelet crash-loops on a Pi | `cat /proc/cmdline` must contain `cgroup_memory=1 cgroup_enable=memory`; reboot the Pi |
| Pods `ImagePullBackOff` | CI has not built that image yet (step 5c), or the GHCR package is private without a pull token (5d) |
| Argo CD app `Unknown` / repo error | Deploy key missing or wrong: Settings → Repositories in the Argo CD UI shows the error; fix the vault value and re-run `make cluster` |
| CI `bump` job fails to push | Workflow permissions not read/write, or branch protection on `main` (step 5b) |
| Nodes flap `NotReady` | Unstable network (Wi-Fi). Use cables, or raise `node_monitor_grace_period` to `40s` |
| Master IP changed | Cluster certificates are bound to the old IP: restore the reservation, or reset (below) |
| Everything stops | The Mac went to sleep: run `caffeinate -dims` |

**Redo one Pi:** on the Pi, `sudo kubeadm reset -f && sudo rm -rf /etc/cni/net.d`. On the Mac,
`kubectl delete node pi-worker-N`, then `make cluster` (or `--limit k8s-master,pi-worker-N`).

**Start over completely:**
```bash
cd infra/ansible
ansible cluster -b -m shell -a 'kubeadm reset -f; rm -rf /etc/cni/net.d'
make -C ../.. cluster
```
