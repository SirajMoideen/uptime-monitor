# uptime-monitor

A simple uptime monitor with a web UI. Checks HTTP, ping, and TCP targets and stores results in PostgreSQL.

## Quick start (Docker)

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
