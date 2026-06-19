# Uptime Monitor

A lightweight uptime monitoring application with a web UI. Checks HTTP/HTTPS, ICMP ping, and TCP targets, stores check history in PostgreSQL, and sends optional Google Chat alerts on failures.

Built as a **personal showcase project** to demonstrate end-to-end application development, containerization, CI/CD, GitOps-based Kubernetes deployment, and full **metrics + logging** observability on a private Kind lab.

> **Note:** This is a personal lab setup, not a production-grade deployment. Both dev and prod environments auto-deploy on push for convenience and demonstration purposes. In a real production environment, prod releases would typically require manual approval, staged rollouts, and stricter change control.

---

## Features

- **Multi-protocol checks** — HTTP/HTTPS (with optional SSL bypass), ICMP ping, and TCP port checks
- **Web UI** — Add, pause, and remove targets; view status and check history
- **PostgreSQL storage** — Connection pooling, configurable history retention
- **Google Chat alerts** — Optional webhook notifications when a target goes down
- **Configurable intervals** — Per-target check intervals with sensible min/max bounds

---

## Architecture

The project spans three repositories:

| Repository | Purpose |
| :--- | :--- |
| [**uptime-monitor**](https://github.com/SirajMoideen/uptime-monitor) | Application source code, Dockerfile, and CI/CD workflows |
| [**uptime-monitor-gitops**](https://github.com/SirajMoideen/uptime-monitor-gitops) | Helm chart, ArgoCD Application manifests, and per-environment values |
| [**kind-platform**](https://github.com/SirajMoideen/kind-platform) | Kind cluster bootstrap, ingress, Prometheus/Grafana, and Elasticsearch/Kibana/Filebeat GitOps |

```mermaid
flowchart LR
    subgraph app_repo ["uptime-monitor"]
        Code[Flask App]
        GHA[GitHub Actions]
        GHCR[GHCR Image]
    end

    subgraph gitops_repo ["uptime-monitor-gitops"]
        Helm[Helm Chart]
        DevVals[dev/values.yaml]
        ProdVals[prod/values.yaml]
    end

    subgraph infra_repo ["kind-platform"]
        Kind[Kind Cluster]
        Prom[Prometheus]
        Graf[Grafana]
        ES[Elasticsearch]
        Kib[Kibana]
        FB[Filebeat]
    end

    subgraph cluster ["Kubernetes Cluster"]
        ArgoCD[ArgoCD]
        DevNS[uptime-monitor-dev]
        ProdNS[uptime-monitor-prod]
    end

    Code --> GHA
    GHA -->|build & push| GHCR
    GHA -->|update image tag| DevVals
    GHA -->|update image tag| ProdVals
    Helm --> ArgoCD
    DevVals --> ArgoCD
    ProdVals --> ArgoCD
    Kind --> ArgoCD
    ArgoCD -->|auto-sync| DevNS
    ArgoCD -->|auto-sync| ProdNS
    ArgoCD --> Prom
    ArgoCD --> Graf
    ArgoCD --> ES
    ArgoCD --> Kib
    ArgoCD --> FB
    GHCR --> DevNS
    GHCR --> ProdNS
    FB -->|ship logs| ES
    Kib --> ES
    Graf --> Prom
```

### Environments

| Environment | Branch trigger | Kubernetes namespace | Replicas |
| :--- | :--- | :--- | :--- |
| **Dev** | `dev` | `uptime-monitor-dev` | 1 |
| **Prod** | `main` | `uptime-monitor-prod` | 2 |

ArgoCD watches the GitOps repository and automatically syncs changes (prune + self-heal enabled) for both environments.

---

## CI/CD Pipeline

The pipeline runs on a **self-hosted GitHub Actions runner** (private infrastructure) and is triggered on push to `dev` or `main`.

**Workflow:** `.github/workflows/docker-build-deploy.yml`

1. Checkout application code
2. Build Docker image and tag with the commit SHA
3. Push image to GitHub Container Registry (`ghcr.io`)
4. Clone the GitOps repository and update the image tag in the matching environment values file
5. Commit and push — ArgoCD picks up the change and deploys

```
push to dev  →  build image  →  update environments/dev/values.yaml  →  ArgoCD syncs dev
push to main →  build image  →  update environments/prod/values.yaml →  ArgoCD syncs prod
```

GitOps repository structure:

```
uptime-monitor-gitops/
├── applications/
│   ├── dev.yaml          # ArgoCD Application (dev)
│   └── prod.yaml         # ArgoCD Application (prod)
├── environments/
│   ├── dev/values.yaml   # Dev overrides (image tag, ingress, replicas)
│   └── prod/values.yaml  # Prod overrides
└── helm/uptime-monitor/  # Helm chart (Deployment, Service, Ingress)
```

---

## Observability

Metrics and logging run on the same Kind cluster as the app, deployed via ArgoCD from [kind-platform](https://github.com/SirajMoideen/kind-platform).

### Metrics stack

| Component | Namespace | Ingress |
| :--- | :--- | :--- |
| Prometheus | `monitoring` | `http://prometheus.local` |
| Grafana | `monitoring` | `http://grafana.local` |

Grafana is pre-wired with a Prometheus datasource. Reference dashboards and PromQL queries live in the [SRE Runbooks](https://github.com/SirajMoideen/sre-runbooks) repository under `prometheus-grafana-dashboards/`.

### Logging stack

| Component | Namespace | Ingress |
| :--- | :--- | :--- |
| Elasticsearch | `logging` | internal |
| Kibana | `logging` | `http://kibana.local` |
| Filebeat | `logging` | DaemonSet — ships container logs to Elasticsearch |

Add these hosts to `/etc/hosts` on your machine (alongside app ingress hosts):

```text
127.0.0.1 prometheus.local grafana.local kibana.local
```

For cluster bootstrap, health checks, and day-to-day ops, see the [kind-platform SOP](https://github.com/SirajMoideen/kind-platform#readme).

---

## Quick Start (Docker)

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/)

```bash
git clone https://github.com/SirajMoideen/uptime-monitor.git
cd uptime-monitor
```

Create a `.env` file in the project root:

```env
DATABASE_URL=postgresql://uptime-user:yourpassword@uptime-db:5432/uptime-db
CHECK_INTERVAL_SECONDS=60
GOOGLE_CHAT_WEBHOOK=
```

Start PostgreSQL and the app:

```bash
docker network create uptime-net

docker run -d --name uptime-db --network uptime-net \
  -e POSTGRES_USER=uptime-user \
  -e POSTGRES_PASSWORD=yourpassword \
  -e POSTGRES_DB=uptime-db \
  postgres:15-alpine

docker build -t uptime-monitor .

docker run -d --name uptime-monitor --network uptime-net \
  -p 5000:5000 --env-file .env uptime-monitor
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

`GOOGLE_CHAT_WEBHOOK` is optional — leave it empty to disable Google Chat alerts.

---

## Tech Stack

- **Application:** Python 3.11, Flask, psycopg2, requests
- **Database:** PostgreSQL 15
- **Container:** Docker (Alpine-based image)
- **CI/CD:** GitHub Actions (self-hosted runner)
- **Registry:** GitHub Container Registry (GHCR)
- **Deployment:** Kubernetes (Kind), Helm, ArgoCD (GitOps)
- **Metrics:** Prometheus, Grafana (via [kind-platform](https://github.com/SirajMoideen/kind-platform))
- **Logging:** Elasticsearch, Kibana, Filebeat (via [kind-platform](https://github.com/SirajMoideen/kind-platform))

---

## Related Projects

- [**kind-platform**](https://github.com/SirajMoideen/kind-platform) — Kind cluster, ingress, Prometheus/Grafana, and Elasticsearch/Kibana/Filebeat GitOps SOP
- [**uptime-monitor-gitops**](https://github.com/SirajMoideen/uptime-monitor-gitops) — Helm chart and ArgoCD application manifests for dev/prod
- [**SRE Runbooks & Automation Toolkit**](https://github.com/SirajMoideen/sre-runbooks) — Production runbooks, GCP automation scripts, and monitoring dashboards used alongside this project

---

## Author

**Siraj** — Site Reliability Engineer

Cloud | Kubernetes | CI/CD | Infrastructure Automation
