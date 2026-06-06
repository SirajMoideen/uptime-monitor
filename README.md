# Uptime Monitor

A lightweight uptime monitoring application with a web UI. Checks HTTP/HTTPS, ICMP ping, and TCP targets, stores check history in PostgreSQL, and sends optional Google Chat alerts on failures.

Built as a **personal showcase project** to demonstrate end-to-end application development, containerization, CI/CD, and GitOps-based Kubernetes deployment.

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

The project spans two repositories:

| Repository | Purpose |
| :--- | :--- |
| [**uptime-monitor**](https://github.com/SirajMoideen/uptime-monitor) | Application source code, Dockerfile, and CI/CD workflows |
| [**uptime-monitor-gitops**](https://github.com/SirajMoideen/uptime-monitor-gitops) | Helm chart, ArgoCD Application manifests, and per-environment values |

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
    ArgoCD -->|auto-sync| DevNS
    ArgoCD -->|auto-sync| ProdNS
    GHCR --> DevNS
    GHCR --> ProdNS
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

Prometheus and Grafana integration for this stack is **in progress**. Related monitoring dashboards and PromQL reference queries live in the [SRE Runbooks](https://github.com/SirajMoideen/sre-runbooks) repository under `prometheus-grafana-dashboards/`.

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
- **Deployment:** Kubernetes, Helm, ArgoCD (GitOps)
- **Observability:** Prometheus + Grafana *(in progress)*

---

## Related Projects

- [**SRE Runbooks & Automation Toolkit**](https://github.com/SirajMoideen/sre-runbooks) — Production runbooks, GCP automation scripts, and monitoring dashboards used alongside this project

---

## Author

**Siraj** — Site Reliability Engineer

Cloud | Kubernetes | CI/CD | Infrastructure Automation
