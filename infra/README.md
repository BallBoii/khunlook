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
IPs, Services, DNS and ports in detail: [docs/network.md](../docs/network.md).

**Time needed:** about 1 hour the first time, mostly waiting for apt and image pulls on the Pis.

---

## 1. What you need

- **Mac** (Apple Silicon) with [UTM](https://mac.getutm.app) and Docker Desktop (optional, for local
  builds). The Mac only hosts the master VM: it needs no Ansible and no `kubectl`.
- **Ansible and `kubectl` run on the master VM** (set up in step 3). You work in the VM's terminal:
  the UTM window, or `ssh ubuntu@192.168.0.106` from the Mac.
- **5 × Raspberry Pi 3 B+** with power, SD cards and Ethernet cables, and the **TP-Link router**.
- Internet through the router. The Pis pull images from GHCR, Argo CD reads GitHub, and
  model-service calls OpenRouter.
- An **OpenRouter API key** (https://openrouter.ai/keys).

## 2. Network plan

Connect everything to the router by cable. The Mac in particular must be on a cable: the master VM is
bridged onto the Mac's connection, and a VM bridged over Wi-Fi keeps dropping off the network.

Each machine gets its IP from the router. Find it, then make it permanent, in the router's admin page
**http://192.168.0.1**:

1. **DHCP → DHCP Client List** shows every device's hostname, MAC address and current IP. Pick out
   `k8s-master` (its MAC is shown in UTM's network settings) and the Pis (hostnames `pi-worker-N`;
   Raspberry Pi MACs start with `b8:27:eb`, `dc:a6:32`, `d8:3a:dd` or `2c:cf:67`). You can also ask the
   machine itself: `ip -4 addr` on the VM, `hostname -I` on a Pi.
2. **DHCP → Address Reservation → Add New**: each machine's MAC + its IP → *Enabled* → **Save**.
   Leave *ARP Binding* off; it is not needed.
3. Reboot the machines and check the client list shows the same IPs.

Example (this cluster):

| Host | IP | User | Notes |
|------|----|------|-------|
| `k8s-master` (UTM VM) | 192.168.0.106 | `ubuntu` | **Must never change**: it is baked into the cluster certificates |
| `pi-worker-2`, `-3`, `-4` | 192.168.0.103, .104, .105 | `pi` | from the client list |

Put the IPs in [`ansible/inventory.ini`](ansible/inventory.ini).

## 3. Master: Ubuntu VM in UTM

kubeadm needs Linux, so the master is a VM. It must be **bridged** onto the LAN: the Pis have to reach
it directly, which rules out NAT and Docker Desktop.

1. Download **Ubuntu Server 24.04 LTS for ARM** (`ubuntu-24.04.x-live-server-arm64.iso`).
2. UTM → **Create a New Virtual Machine** → **Virtualize** → **Linux** → select the ISO.
3. Hardware: **4 CPU cores, 4096 MB RAM**. Storage: **30 GB**.
4. Before the first boot, open the VM's settings → **Network**:
   - Network Mode: **Bridged (Advanced)**. *Shared Network* (NAT) hides the VM from the Pis and does not work.
   - Bridged Interface: the Mac's **Ethernet** adapter that is cabled to the router (find it with
     `networksetup -listallhardwareports`; on a MacBook `en0` is **Wi-Fi**, so don't pick it).
   - Emulated Network Card: **virtio-net-pci**.
   - Note the VM's **MAC address** shown there for the DHCP reservation (step 2).
5. Install Ubuntu: user `ubuntu`, hostname `k8s-master`, tick **Install OpenSSH server**. No snaps needed.
   After installing, eject the ISO (CD/DVD → Clear) and reboot.
6. On the router, reserve the VM's IP (e.g. **192.168.0.106**) for the VM's MAC, then reboot the VM and
   check, in the VM:
   ```bash
   ip -4 addr show | grep 192.168.0.106 && sudo -n true && echo sudo-ok
   ```
   If `sudo -n true` fails, allow passwordless sudo:
   `echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ubuntu`
   (or put the password in the vault as `vault_master_become_password`, step 6).
7. **Make the VM the Ansible runner.** Everything from here on is typed in the VM, in its clone of
   this repository:
   ```bash
   sudo apt update && sudo apt install -y git make sshpass pipx
   pipx install ansible-core && pipx ensurepath && exec $SHELL -l
   git clone https://github.com/BallBoii/khunlook.git && cd khunlook
   ```
   Ansible manages the master over a local connection (`ansible_connection=local` in the inventory)
   and the Pis over SSH with the password from `vault.yml` (step 6; that is what `sshpass` is for).
8. **Keep the Mac awake** while the cluster runs, e.g. by running `caffeinate -dims` in a terminal.
   If the Mac sleeps, the control plane stops.

## 4. Workers: flash the Pis

Use **Raspberry Pi Imager** for each SD card:

- OS: **Raspberry Pi OS Lite (64-bit)** (recommended for 1 GB of RAM) or **Ubuntu Server 24.04 LTS (64-bit)**.
  A 32-bit image will not work; the playbook stops on it.
- *Edit settings* (OS customisation):
  - hostname `pi-worker-N`, username **`pi`** with a password (the **same password on every Pi**)
  - **Services → Enable SSH → Use password authentication**. Ansible logs in with this password
    (`vault_workers_ssh_password`, step 6)
  - no Wi-Fi if you use cables

Boot all five Pis and reserve their IPs on the router. Once `vault.yml` holds the passwords (step 6),
check from the master VM:

```bash
cd infra/ansible
ansible all -m ping          # every host must answer "pong"
ansible all -b -m command -a 'true'   # and sudo must work
```

### Preparing the Pis before the master exists (optional)

The Pis can be prepared without the master: Ansible does everything except joining.

```bash
cd infra/ansible
ansible workers -m ping                                         # the 5 Pis answer "pong"
ansible-playbook site.yml --limit workers --skip-tags join      # needs vault.yml (step 6) for the Pi password
```

This sets the hostname, turns swap off, enables memory cgroups (**each Pi reboots once**), and
installs containerd and kubelet/kubeadm/kubectl v1.36. The kubelet restarts in a loop until the Pi
joins; that is normal. Once the master VM is up, `make cluster` (step 7) skips everything already done
and only joins the Pis.

## 5. GitHub: repository, registry and CI settings

Run these steps once per repository (a fork needs them too).

**Argo CD needs no key.** The repository is public, so Argo CD reads it anonymously over HTTPS
(`repoURL: https://github.com/BallBoii/khunlook.git` in
[`deploy/argocd/khunlook-app.yaml`](../deploy/argocd/khunlook-app.yaml)). If you ever make the repo
private, Argo CD can no longer read it: add the repository with a read-only token in the Argo CD UI
(*Settings → Repositories*), or make it public again.

**a. Let CI push the tag bumps**: **Settings → Actions → General → Workflow permissions →
Read and write permissions**. If `main` has branch protection, allow GitHub Actions to bypass it,
or CI cannot commit the tag bumps.

**b. Build the images once**: merge this setup into `main`, then **Actions → build-deploy → Run workflow**.
This builds all 5 images (about 10–15 min under QEMU) and commits their tags to `deploy/k8s/kustomization.yaml`.

**c. Make the images pullable** (pick one):
- *Public images (simplest)*: GitHub → your profile → **Packages** → open each `khunlook-*` package →
  **Package settings → Change visibility → Public**.
- *Private images*: create a classic PAT with `read:packages` and set `vault_ghcr_pull_user` /
  `vault_ghcr_pull_token` in step 6. Ansible then creates a pull secret for the app namespace.

**d. Optional, faster builds**: if your plan offers arm64 runners, set **Settings → Secrets and variables →
Actions → Variables → `ARM_RUNNER` = `ubuntu-24.04-arm`**. Builds then run natively instead of under QEMU.

> Forked or renamed repository? Replace `ballboii`/`BallBoii/khunlook` in
> `deploy/k8s/kustomization.yaml`, `deploy/k8s/*/*.yaml`, `deploy/k8s/prepull/prepull.yaml`,
> `deploy/argocd/khunlook-app.yaml` (`repoURL`), `IMAGE_PREFIX` in the workflow and `REGISTRY` in the
> Makefile. GHCR names must be lowercase.

## 6. Secrets (`vault.yml`)

On the master VM:

```bash
cd infra/ansible
cp vault.example.yml group_vars/all/vault.yml
nano group_vars/all/vault.yml
```

| Variable | Value |
|----------|-------|
| `vault_openrouter_api_key` | your OpenRouter API key |
| `vault_master_become_password` | password of `ubuntu` on the VM (`""` if sudo asks for none) |
| `vault_workers_ssh_password` | password of `pi` on the Pis |
| `vault_workers_become_password` | the same `pi` password (`""` if sudo asks for none) |

Don't pass `--ask-become-pass`: it sends one password to every host.

The file is **plain text, not encrypted**. It is git-ignored, so it is never committed: keep it on the
master VM only. If the OpenRouter key is missing, the playbook stops at the very first task.

## 7. Build the cluster (one command)

On the master VM, from the repo root:

```bash
make cluster            # = cd infra/ansible && ansible-playbook site.yml
```

What happens, in order:

| Play | Hosts | Steps |
|------|-------|-------|
| Preflight | VM (local) | Checks the vault secrets are filled in |
| `common` | all | hostname, swap off, kernel modules + sysctls, **containerd** (systemd cgroups), kubelet/kubeadm/kubectl from pkgs.k8s.io **v1.36**, held at that version |
| `master` | VM | `kubeadm init` (pod CIDR 10.244.0.0/16, node grace period 20 s), kubeconfig for `ubuntu` (`~/.kube/config`) plus a copy in `infra/ansible/artifacts/kubeconfig` for the `make` targets, installs **Flannel v0.28.10** |
| `pi_worker` | Pis | arm64 check, memory cgroups on the kernel cmdline (**reboots once**), `kubeadm join`, waits for `Ready` |
| `argocd` | VM | Helm, **Argo CD** chart 10.17.2 pinned to the master, `khunlook` namespace, `openrouter` + repo secrets, the Application |

The playbook is safe to re-run: initialised or joined nodes are skipped and Helm only upgrades what changed.
To try a single Pi first, run `ansible-playbook site.yml --limit k8s-master,pi-worker-1`.

## 8. Check it

On the master VM (`kubectl` works as is: the playbook wrote `~/.kube/config`):

```bash
kubectl get nodes -o wide                  # k8s-master + 5 Pis, all Ready
kubectl -n khunlook get pods -o wide       # 10 app pods on the Pis + 5 image-prepull pods
make argocd-password                       # Argo CD admin password
```

Open these in a browser on the Mac (or any machine on the LAN):

- **App:** http://192.168.0.106:30080/?demo (`?demo` shows which pods served each request)
- **Argo CD:** https://192.168.0.106:30443 (user `admin`; accept the self-signed certificate). The app
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
2. **Requests through ≥ 2 containers.** Open `http://192.168.0.106:30080/?demo` and ask a question. The
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
├── inventory.ini            # [master] VM (local connection), [workers] Pis (SSH), [cluster] = both
├── site.yml                 # the one playbook: preflight → common → master → pi_worker → argocd
├── vault.example.yml        # template for group_vars/all/vault.yml (git-ignored)
├── group_vars/all/main.yml  # versions, CIDR, timings, repo URL
├── artifacts/               # copy of the master's admin kubeconfig (git-ignored)
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

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Require 64-bit OS` fails | Reflash the Pi with a 64-bit image |
| Preflight assertion fails | Fill `group_vars/all/vault.yml` (step 6) |
| `ansible all -m ping` fails for a Pi | Wrong IP in the inventory, wrong `vault_workers_ssh_password`, password login not enabled on the Pi, `sshpass` missing on the VM, or the VM is not bridged (check UTM network mode) |
| `ansible-playbook: command not found` on the VM | Step 3.7 was skipped, or the shell predates `pipx ensurepath`: open a new one |
| Node joins but stays `NotReady` | `kubectl -n kube-flannel get pods -o wide`; on a Pi with both Wi-Fi and Ethernet, disable the one you don't use so Flannel picks the right interface |
| kubelet crash-loops on a Pi | `cat /proc/cmdline` must contain `cgroup_memory=1 cgroup_enable=memory`; reboot the Pi |
| Pods `ImagePullBackOff` | CI has not built that image yet (step 5b), or the GHCR package is private without a pull token (5c) |
| Argo CD app `Unknown` / repo error | The repo is no longer public, or `repoURL` is wrong. *Settings → Repositories* in the Argo CD UI shows the error |
| CI `bump` job fails to push | Workflow permissions not read/write, or branch protection on `main` (step 5a) |
| Nodes flap `NotReady` | Unstable network (Wi-Fi). Use cables, or raise `node_monitor_grace_period` to `40s` |
| Master IP changed | Cluster certificates are bound to the old IP: restore the reservation, or reset (below) |
| Everything stops | The Mac went to sleep: run `caffeinate -dims` |

**Redo one Pi:** on the Pi, `sudo kubeadm reset -f && sudo rm -rf /etc/cni/net.d`. On the master VM,
`kubectl delete node pi-worker-N`, then `make cluster` (or `--limit k8s-master,pi-worker-N`).

**Start over completely:**
```bash
cd infra/ansible
ansible cluster -b -m shell -a 'kubeadm reset -f; rm -rf /etc/cni/net.d'
make -C ../.. cluster
```
