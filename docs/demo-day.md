# Demo day: script and emergency commands

Everything here is typed **on the master VM**, from the repo root (`~/khunlook`), unless it says
otherwise. `<master-ip>` is the VM's LAN address: `hostname -I | awk '{print $1}'`.
`ssh pi@<pi-ip>` asks for the Pis' password (the one in `vault.yml`).

---

## 1. Before the demo (10 minutes)

Do these in order. Stop at the first one that fails and go to [section 3](#3-emergencies).

| # | Check | Command | Good result |
|---|-------|---------|-------------|
| 1 | Mac stays awake | on the Mac: `caffeinate -dimsu` (leave the terminal open) | no output, keeps running |
| 2 | Master has the IP the cluster was built with | `hostname -I` and `grep server: ~/.kube/config` | the same IP in both |
| 3 | All nodes are up | `kubectl get nodes -o wide` | master and every Pi `Ready` |
| 4 | App pods are up and spread | `kubectl -n khunlook get pods -o wide` | 10 app pods `Running` (2 per service) on different Pis, plus one `image-prepull` per Pi |
| 5 | Argo CD is in sync | `kubectl -n argocd get application khunlook` | `Synced` and `Healthy` |
| 6 | Automatic sync is on | `argocd app get khunlook \| grep -i "sync policy"` | `Automated (Prune)` |
| 7 | The chatbot answers | open `http://<master-ip>:30080/?demo` and ask one question | an answer with sources and a served-by chain |

Keep two terminals open on the VM:

```bash
watch -n 2 kubectl get nodes                       # terminal 1
kubectl -n khunlook get pods -o wide -w            # terminal 2
```

Log in to the Argo CD CLI once (the session lasts 24 hours):

```bash
argocd login <master-ip>:30443 --username admin --password "$(make -s argocd-password)" --insecure
```

---

## 2. Demo script

### 2a. Requests pass through several containers

1. Open `http://<master-ip>:30080/?demo` and ask a question.
2. Show the served-by chain under the answer: frontend → api-gateway → rag → vector-db and
   model-service, with the pod names.
3. Show that those pods are on different Pis: `kubectl -n khunlook get pods -o wide`.

Do not send questions quickly one after another: the gateway allows **10 questions per minute per
client**. If you see "too many requests", wait one minute.

### 2b. Automatic deployment

The safe version needs no image build and finishes within about a minute:

1. Show Argo CD: `https://<master-ip>:30443` → app `khunlook` is *Synced*.
2. On your laptop, change `replicas:` in `deploy/k8s/frontend/frontend.yaml` (for example 2 → 3),
   commit and `git push` to `main`.
3. Watch terminal 2: a third frontend pod appears. To skip the wait: `argocd app sync khunlook`.

The full version (code change → CI builds an image → Argo CD rolls it out) takes **several minutes**
because the arm64 image is built under emulation. If you show it, push the change first and talk
through the other parts while CI runs:

1. Change a visible text in `services/frontend/site/index.html`, commit and push to `main`.
2. Show the GitHub Actions run, then the `deploy: frontend -> <sha>` commit it adds.
3. Show the new frontend pods rolling out, then reload the page.

The from-nothing version: `make app-down` removes every app pod, then `make deploy` brings the whole
application back from GitHub with one command.

### 2c. Fault tolerance

1. Point at terminal 2 and let the instructor pick a Pi. Unplug it.
2. After about 20 s the node shows `NotReady` in terminal 1.
3. About 20 s later its pods are recreated on the other Pis.
4. Ask a question while this happens: the second replica of every service keeps answering. A request
   that was already on the unplugged Pi can fail once; ask again.
5. Plug the Pi back in. It becomes `Ready` again by itself. Pods do **not** move back; that is normal
   (see "Pods are bunched on few Pis" below).

---

## 3. Emergencies

### First look: what is broken?

```bash
kubectl get nodes -o wide
kubectl -n khunlook get pods -o wide
kubectl -n argocd get application khunlook
kubectl -n khunlook get events --sort-by=.lastTimestamp | tail -15
```

### `kubectl` says "connection to the server localhost:8080 was refused"

`kubectl` has no kubeconfig. Either of these fixes it:

```bash
export KUBECONFIG=~/khunlook/infra/ansible/artifacts/kubeconfig
sudo kubectl --kubeconfig /etc/kubernetes/admin.conf get nodes      # always works on the master
```

### `kubectl` times out, or every node is `NotReady`: the master's IP changed

The cluster only works on the IP it was built with.

```bash
hostname -I                          # the IP now
grep server: ~/.kube/config          # the IP the cluster was built with
```

- **Different, and you can get the old IP back:** fix the router's DHCP reservation (or move back to
  the original network), then `sudo reboot` the VM. Nothing else is needed.
- **Different, and you cannot:** rebuild on the new IP. Put the new master IP and the Pis' IPs in
  `infra/ansible/inventory.ini`, then run the full rebuild at the end of this section.

### A Pi stays `NotReady` although it is plugged in

```bash
ping -c 3 <pi-ip>                                             # is it on the network at all?
ssh pi@<pi-ip> 'sudo systemctl restart containerd kubelet'    # restart the node services
kubectl -n kube-flannel get pods -o wide                      # the flannel pod on that Pi must be Running
```

Still `NotReady` after two minutes: rejoin that one Pi.

```bash
ssh pi@<pi-ip> 'sudo kubeadm reset -f && sudo rm -rf /etc/cni/net.d && sudo reboot'
kubectl delete node pi-worker-N
(cd infra/ansible && ansible-playbook site.yml -l k8s-master,pi-worker-N)
```

The demo works with one Pi missing. If time is short, carry on without it.

### Pods are not `Running`

```bash
kubectl -n khunlook describe pod <pod-name> | tail -20      # the reason is in Events at the bottom
kubectl -n khunlook logs <pod-name> --tail=30               # for CrashLoopBackOff
```

| Status | Meaning | Fix |
|--------|---------|-----|
| `Pending` | no Pi can take the pod | check `kubectl get nodes`; bring a Pi back |
| `ImagePullBackOff` | the Pi cannot download the image | check the Pi's internet (`ssh pi@<pi-ip> 'ping -c 2 ghcr.io'`); the GHCR packages must be public |
| `CrashLoopBackOff` | the container starts and dies | read the logs; `kubectl -n khunlook rollout restart deploy/<service>` |
| `ContainerCreating` for minutes | a Pi is still pulling images | wait, or look at `describe pod` |

Restart one service, or all of them:

```bash
kubectl -n khunlook rollout restart deploy/rag
kubectl -n khunlook rollout restart deploy
```

### The page opens but the chatbot returns an error

```bash
kubectl -n khunlook logs deploy/api-gateway --tail=20
kubectl -n khunlook logs deploy/rag --tail=20
kubectl -n khunlook logs deploy/model-service --tail=20
```

- **"Too many requests" (429):** the rate limit. Wait one minute.
- **model-service logs show 401 or 402 from OpenRouter:** the API key is wrong or out of credit. Put a
  working key in `infra/ansible/group_vars/all/vault.yml`, then:
  ```bash
  (cd infra/ansible && ansible-playbook site.yml -l master)
  kubectl -n khunlook rollout restart deploy/model-service
  ```
- **Timeouts:** the Pis or the VM lost internet. Check `ping -c 2 openrouter.ai` on the VM.

### The page does not open at all

```bash
kubectl -n khunlook get svc frontend                 # NodePort must be 30080
curl -s -o /dev/null -w '%{http_code}\n' http://<master-ip>:30080/     # 200 = the app is fine
```

If `curl` on the VM says 200, the problem is the laptop's network: it must be on the same LAN as the
cluster. Any node's IP works in place of the master's: `http://<pi-ip>:30080/?demo`.

### Argo CD does not deploy a pushed change

```bash
argocd app get khunlook                                   # sync policy, status, last error
argocd app sync khunlook                                  # sync now
argocd app set khunlook --sync-policy automated --auto-prune --self-heal    # if it was left off
```

No CLI login? The same without it:

```bash
kubectl -n argocd annotate application khunlook argocd.argoproj.io/refresh=hard --overwrite
make deploy                                               # re-applies the Application, sync on
```

If the app is `Unknown` or shows a repository error, the VM has no internet or cannot reach GitHub.

### A deployed change broke the app: roll back

On your laptop:

```bash
git log --oneline -5                 # find the bad commit (yours, or CI's "deploy: ..." commit)
git revert --no-edit <commit>
git push
```

Then on the VM, to skip the wait: `argocd app sync khunlook`.

### Pods are bunched on few Pis after a Pi came back

Normal: Kubernetes does not move running pods. To spread them again:

```bash
kubectl -n khunlook rollout restart deploy
```

### The VM or the Mac restarted

The cluster starts by itself. Wait two to three minutes, then run the first-look commands. If the
Mac was asleep, run `caffeinate -dimsu` again.

### Last resort: rebuild the whole cluster

Wipes Kubernetes on the master and every reachable Pi, then builds it again. The packages and images
are already on the machines, so this is much faster than the first build, but allow 15 minutes.

```bash
cd ~/khunlook
make cluster-reset          # answer y
make cluster
kubectl get nodes
kubectl -n khunlook get pods -o wide
```

Afterwards the Argo CD admin password is a new one (`make argocd-password`), and you must run
`argocd login` again.
