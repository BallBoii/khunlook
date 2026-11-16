# Infrastructure diagrams

How KhunLook's infrastructure fits together: the machines, what runs where, how a request travels,
how code gets deployed, and what happens when a Pi is unplugged. Every IP, Service and port in detail: [network.md](network.md). Setup steps are in
[infra/README.md](../infra/README.md).

IPs below follow the plan in [`infra/ansible/inventory.ini`](../infra/ansible/inventory.ini)
(master `192.168.0.10`, Pis `.21`–`.25`); substitute your own.

---

## 1. Physical network

One Mac hosts the master as a bridged Ubuntu VM in UTM, so the VM sits on the router's LAN next to
the five Raspberry Pis. Only outbound internet is needed: nothing on the internet connects into the LAN.

```mermaid
flowchart LR
    subgraph Internet
        GH["GitHub<br/>repo + Actions"]
        GHCR["GHCR<br/>container images"]
        OR["OpenRouter<br/>embeddings + LLM"]
    end

    subgraph LAN["TP-Link router LAN 192.168.0.0/24"]
        R(("Router<br/>DHCP reservations"))
        subgraph MAC["Mac (Apple Silicon)"]
            VM["UTM VM: k8s-master<br/>Ubuntu 24.04 arm64<br/>4 vCPU / 4 GB<br/>192.168.0.10"]
        end
        P1["pi-worker-1<br/>.21"]
        P2["pi-worker-2<br/>.22"]
        P3["pi-worker-3<br/>.23"]
        P4["pi-worker-4<br/>.24"]
        P5["pi-worker-5<br/>.25"]
    end

    VM ---|bridged| R
    R --- P1 & P2 & P3 & P4 & P5
    R -->|outbound only| Internet
```

| Machine | Hardware | Role |
|---------|----------|------|
| `k8s-master` | UTM VM on the Mac, arm64, 4 vCPU, 4 GB, 30 GB | Kubernetes control plane + Argo CD. Never runs app pods |
| `pi-worker-1…5` | Raspberry Pi 3 B+, arm64, 1 GB | Runs all application pods |

---

## 2. What runs on each node

The master keeps kubeadm's `control-plane:NoSchedule` taint, so only system pods and Argo CD
(which tolerates the taint and is pinned there) run on it. Every app service has 2 replicas,
spread over different Pis by pod anti-affinity. Placement below is one example: the scheduler
decides, and it changes after a Pi fails.

```mermaid
flowchart TB
    subgraph M["k8s-master (VM)"]
        direction TB
        CP["Control plane<br/>kube-apiserver :6443 · etcd<br/>scheduler · controller-manager"]
        ARGO["Argo CD<br/>server · repo-server<br/>application-controller · redis<br/>applicationset"]
        SYSM["kubelet · containerd<br/>kube-proxy · flannel · coredns"]
    end

    subgraph W1["pi-worker-1"]
        A1["frontend · rag<br/>image-prepull"]
    end
    subgraph W2["pi-worker-2"]
        A2["api-gateway · vector-db<br/>image-prepull"]
    end
    subgraph W3["pi-worker-3"]
        A3["frontend · model-service<br/>image-prepull"]
    end
    subgraph W4["pi-worker-4"]
        A4["api-gateway · rag<br/>image-prepull"]
    end
    subgraph W5["pi-worker-5"]
        A5["vector-db · model-service<br/>image-prepull"]
    end

    CP <-->|"node status every 5 s<br/>pod specs"| W1 & W2 & W3 & W4 & W5
```

Every Pi also runs `kubelet`, `containerd`, `kube-proxy` and `flannel` (omitted above).
`image-prepull` is a DaemonSet that keeps all five app images cached on every Pi, so pods
recreated after a failure start without downloading anything.

| Namespace | Contents |
|-----------|----------|
| `kube-system` | control plane, kube-proxy, coredns |
| `kube-flannel` | Flannel pod network (`10.244.0.0/16`, VXLAN between nodes) |
| `argocd` | Argo CD (Helm chart 10.17.2), repo credentials secret |
| `khunlook` | the 5 app Deployments + Services, `image-prepull`, `openrouter` secret |

---

## 3. A chat request

The browser only ever talks to the master's IP. kube-proxy forwards NodePort 30080 to a frontend
pod on whichever Pi is alive; from there each hop goes through a ClusterIP Service, which picks a
ready replica, usually on a different Pi. Each answer passes through at least 4 containers.

```mermaid
flowchart LR
    B["Browser<br/>http://192.168.0.10:30080/?demo"]
    subgraph K["Cluster: namespace khunlook"]
        FE["frontend<br/>nginx"]
        GW["api-gateway<br/>rate limit · timeouts · retry"]
        RAG["rag"]
        VDB["vector-db<br/>index in image"]
        MS["model-service<br/>holds API key"]
    end
    OR["OpenRouter"]

    B -->|"NodePort 30080"| FE
    FE -->|"/api/*"| GW
    GW --> RAG
    RAG -->|"1 embed question"| MS
    RAG -->|"2 search top-k"| VDB
    RAG -->|"3 prompt → answer"| MS
    MS -->|HTTPS| OR
```

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant FE as frontend
    participant GW as api-gateway
    participant R as rag
    participant M as model-service
    participant V as vector-db
    participant O as OpenRouter

    B->>FE: POST /api/v1/ask
    FE->>GW: same request + X-Request-ID
    GW->>R: POST /v1/answer
    R->>M: POST /v1/embed (question)
    M->>O: embeddings (baai/bge-m3)
    O-->>M: 1024-dim vector
    M-->>R: vector
    R->>V: POST /v1/search (vector, top_k)
    V-->>R: best passages
    R->>M: POST /v1/chat (prompt with passages)
    M->>O: chat (gemma-3-4b-it)
    O-->>M: answer
    M-->>R: answer
    R-->>GW: answer + sources + served_by
    GW-->>FE: JSON
    FE-->>B: answer, citations, pod chain (?demo)
```

---

## 4. Deploying a change (GitOps)

Git is the source of truth. CI turns code into images and records their tags in Git; Argo CD,
running inside the cluster, pulls Git and makes the cluster match. Nothing pushes into the LAN.

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant GH as GitHub (main)
    participant CI as GitHub Actions
    participant REG as GHCR
    participant A as Argo CD (master)
    participant K as Kubernetes API
    participant P as Pis

    Dev->>GH: git push (services/…)
    GH->>CI: trigger build-deploy
    CI->>CI: make test
    CI->>CI: build changed services (linux/arm64)
    CI->>REG: push khunlook-{svc}:{sha}
    CI->>GH: commit "deploy: {svc} -> {sha}"<br/>(deploy/k8s/kustomization.yaml)
    loop every ~60 s
        A->>GH: fetch main (HTTPS, public repo: no key)
    end
    A->>A: kustomize build deploy/k8s, diff with live state
    A->>K: apply changed Deployments (OutOfSync → Synced)
    K->>P: rolling update: new pods
    P->>REG: pull new images
    P-->>K: new pods Ready, old pods removed
    A-->>Dev: UI: Synced / Healthy
```

```mermaid
flowchart LR
    subgraph Git["Git repository (main)"]
        SRC["services/*<br/>index/"]
        KUS["deploy/k8s/<br/>kustomization.yaml<br/>(image tags)"]
        APP["deploy/argocd/<br/>khunlook-app.yaml"]
    end
    CI["GitHub Actions<br/>test · build · push · bump"]
    REG[("GHCR")]
    ARGO["Argo CD<br/>auto-sync · prune · self-heal"]
    CL["Cluster<br/>namespace khunlook"]

    SRC -->|push| CI
    CI -->|images :sha| REG
    CI -->|commit new tags| KUS
    KUS -->|polled| ARGO
    APP -.->|"applied once by Ansible"| ARGO
    ARGO -->|kubectl apply| CL
    REG -->|image pull| CL
```

What Argo CD does **not** manage: its own installation (Helm, via Ansible) and the `openrouter`
API key secret, which Ansible creates from the git-ignored `vault.yml` so it never appears in Git. Argo CD needs no
secret to read the repository: it is public and read over HTTPS.

---

## 5. Building the cluster (Ansible)

One playbook, run on the master VM (local connection to itself, SSH to the Pis), takes fresh machines to a cluster that deploys itself.

```mermaid
flowchart TB
    START(["make cluster<br/>ansible-playbook site.yml"])
    PRE["Preflight (master VM)<br/>vault secrets present?"]
    COM["common (all nodes)<br/>swap off · kernel modules · sysctls<br/>containerd · kubelet/kubeadm/kubectl v1.36"]
    MAS["master (VM)<br/>kubeadm init · kubeconfig for ubuntu<br/>Flannel"]
    WRK["pi_worker (Pis)<br/>arm64 check · memory cgroups + reboot<br/>kubeadm join · wait Ready"]
    ACD["argocd (VM)<br/>Helm · Argo CD chart<br/>namespace · secrets · Application"]
    DONE(["Argo CD syncs deploy/k8s<br/>app running on the Pis"])

    START --> PRE --> COM --> MAS --> WRK --> ACD --> DONE
```

---

## 6. How you reach the cluster

```mermaid
flowchart LR
    subgraph MAC["Mac"]
        TERM["Terminal<br/>UTM window or ssh"]
        BR["Browser"]
    end
    subgraph VM["k8s-master 192.168.0.10"]
        ANS["Ansible<br/>repo clone"]
        KC["kubectl<br/>~/.kube/config"]
        API["kube-apiserver :6443"]
        ETCD[("etcd")]
        NP["NodePorts<br/>:30080 app · :30443 Argo CD"]
    end
    PIS["Pis<br/>kubelet :10250"]

    TERM -->|"SSH :22"| ANS
    TERM --> KC
    ANS -->|"local, no SSH"| API
    ANS -->|"SSH :22"| PIS
    KC -->|"HTTPS :6443<br/>admin client cert"| API
    API --- ETCD
    API -->|"logs / exec"| PIS
    BR --> NP
    NP -->|kube-proxy| PIS
```

| Port | On | Used by |
|------|----|---------|
| 22 | Pis | Ansible on the master (SSH) |
| 22 | master | your terminal from the Mac (optional; the UTM window works too) |
| 6443 | master | `kubectl`, kubelets, Argo CD → API server |
| 10250 | all nodes | API server → kubelet (logs, exec) |
| 8472/udp | all nodes | Flannel VXLAN pod traffic between nodes |
| 30080 | every node (use the master's IP) | Chatbot |
| 30443 | every node (use the master's IP) | Argo CD UI |

---

## 7. When a Pi is unplugged

Kubernetes, not Argo CD, does the recovery; Argo CD only reports health. Timings come from
`node_monitor_grace_period: 20s` (Ansible) and `tolerationSeconds: 20` (manifests).

```mermaid
sequenceDiagram
    autonumber
    participant P as pi-worker-3 (unplugged)
    participant CM as controller-manager (master)
    participant S as scheduler (master)
    participant O as Other Pis
    participant U as User

    Note over P: t = 0 s: power/cable pulled
    U->>O: requests mostly still succeed (other replica)
    Note over CM: no node status for 20 s
    CM->>CM: t ≈ 20 s: mark pi-worker-3 NotReady,<br/>taint unreachable:NoExecute
    Note over CM: pods tolerate it for 20 s
    CM->>CM: t ≈ 40 s: evict pi-worker-3's pods
    CM->>S: ReplicaSets create replacements
    S->>O: schedule on remaining Pis
    O->>O: start from cached images (image-prepull)
    O-->>U: t ≈ 50–90 s: replacements Ready, all requests succeed
    Note over P: plugged back in: rejoins automatically.<br/>Pods do not move back (expected)
```
