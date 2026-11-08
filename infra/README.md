# Infrastructure

Kubernetes cluster layout and the Ansible setup that prepares the Raspberry Pi workers.

```
PC (control plane, amd64) ──LAN── Raspberry Pi workers (arm64, Docker + containerd + kubelet)
```

- The **control plane runs on a PC**. It is set up by hand (`kubeadm init` + a CNI plugin); Ansible only
  uses it to create join tokens.
- The **workers are Raspberry Pis** on the same LAN. [`ansible/`](ansible) installs Docker (with buildx),
  containerd, kubelet and kubeadm on each Pi and joins it to the cluster.
- Workloads in [`../deploy/k8s`](../deploy/k8s) use `nodeSelector: kubernetes.io/arch: arm64`, so they land on the Pis.

## Layout

```
infra/
├── ansible/
│   ├── ansible.cfg            # inventory path, sudo on
│   ├── inventory.ini          # [master] the PC, [workers] the Pis
│   ├── workers.yml            # playbook: applies the pi_worker role to [workers]
│   └── roles/pi_worker/
│       ├── defaults/main.yml  # kube_minor, cmdline
│       ├── tasks/main.yml
│       └── handlers/main.yml
└── terraform/                 # (empty)
```

## Prerequisites

**Control machine** (where you run Ansible): `ansible-core` 2.15+ and SSH access to every host.

```bash
pip install ansible-core
```

**Raspberry Pis**
- 64-bit Raspberry Pi OS (Bookworm) or Ubuntu Server, arm64. The playbook stops on 32-bit images.
- SSH enabled, key-based login for the `pi` user (or change `ansible_user`), and passwordless `sudo`.
- A static IP or DHCP reservation per Pi.

**Control-plane PC**
- A working cluster (`kubeadm init` done) with a CNI plugin installed. Joined nodes stay `NotReady` without one.
- `kubeadm` available and passwordless `sudo` for the SSH user (used for `kubeadm token create`).

## Configure

Edit [`ansible/inventory.ini`](ansible/inventory.ini): set the IPs and SSH users.

```ini
[master]
k8s-master ansible_host=192.168.1.10 ansible_user=ball

[workers]
pi-worker-1 ansible_host=192.168.1.21
pi-worker-2 ansible_host=192.168.1.22
pi-worker-3 ansible_host=192.168.1.23

[workers:vars]
ansible_user=pi
```

Inventory names become the Kubernetes node names, so keep them unique and lowercase.

Role variables ([`defaults/main.yml`](ansible/roles/pi_worker/defaults/main.yml)):

| Variable      | Default                      | Meaning                                                             |
|---------------|------------------------------|---------------------------------------------------------------------|
| `kube_minor`  | `v1.34`                      | Kubernetes apt repo minor; must match the control plane (±1 minor)  |
| `cmdline`     | `/boot/firmware/cmdline.txt` | Pi kernel command line (use `/boot/cmdline.txt` on older images)    |

Override with `-e kube_minor=v1.33` or in the inventory.

## Run

```bash
cd infra/ansible
ansible all -m ping                                # check SSH access
ansible-playbook workers.yml --limit pi-worker-1   # try one node first
ansible-playbook workers.yml                       # then all workers
```

Verify on the control plane:

```bash
kubectl get nodes -o wide      # Pis should be Ready, ARCH arm64
```

## What the role does

1. Checks the node is `aarch64` and sets its hostname to the inventory name.
2. Adds `cgroup_enable=cpuset cgroup_memory=1 cgroup_enable=memory` to the kernel command line
   (off by default on Raspberry Pi OS) and **reboots once** if that changed.
3. Disables swap, loads `overlay` / `br_netfilter`, sets the bridge and `ip_forward` sysctls.
4. Adds the pkgs.k8s.io apt repo and installs `docker.io`, `docker-buildx`, `containerd`, `kubelet`, `kubeadm`.
5. Generates the containerd config with `SystemdCgroup = true` and restarts containerd.
6. Adds the SSH user to the `docker` group (log out and in again to use `docker` without sudo).
7. Creates a 30-minute join token on the master and runs `kubeadm join`. Nodes that already joined
   (`/etc/kubernetes/kubelet.conf` exists) are skipped, so re-running is safe.

## Build and deploy the app

Images are built for arm64 and pushed to your registry, then deployed to the Pis
(see the [`Makefile`](../Makefile) and [`deploy/k8s/vector-db/vector-db.yaml`](../deploy/k8s/vector-db/vector-db.yaml)):

```bash
make push-vector-db REGISTRY=<registry> VERSION=1.0.0     # multi-arch image (arm64 + amd64)
kubectl apply -f deploy/k8s/vector-db/vector-db.yaml      # set the image tag in the file first
```

Docker is installed on the Pis, so you can also build there natively (`make build-vector-db`).
The registry must be reachable from the Pis. A plain-HTTP registry is not configured by this role; add it
to the Docker and containerd configuration yourself.

## Troubleshooting

| Symptom                                        | Fix                                                                                       |
|------------------------------------------------|-------------------------------------------------------------------------------------------|
| `Require arm64` assertion fails                | Reflash with a 64-bit image.                                                              |
| kubelet crash-loops or memory not limited      | Check `cat /proc/cmdline` has the cgroup flags and the Pi was rebooted.                   |
| Node joins but stays `NotReady`                | Install a CNI plugin on the cluster.                                                      |
| `kubeadm token create` fails                   | `kubeadm` is missing on the PC, or the SSH user there lacks `sudo`.                       |
| Join fails with a version error                | Align `kube_minor` with the control plane.                                                |
| Join timed out / need to redo a node           | On the Pi: `sudo kubeadm reset -f`, then re-run the playbook for that node.               |
| Remove a node                                  | `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data && kubectl delete node <node>`, then `kubeadm reset -f` on the Pi. |
