import os
import time
import logging
import threading

import requests
import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

DATABASE_URL     = os.getenv("DATABASE_URL")
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
GC_WEBHOOK       = os.getenv("GOOGLE_CHAT_WEBHOOK")
REQUEST_TIMEOUT  = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
DB_POOL_MIN      = int(os.getenv("DB_POOL_MIN", "2"))
DB_POOL_MAX      = int(os.getenv("DB_POOL_MAX", "10"))

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── App & DB pool ─────────────────────────────────────────────────────────────

app = Flask(__name__)
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Return the shared connection pool, creating it on first call."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            DB_POOL_MIN, DB_POOL_MAX, DATABASE_URL
        )
        log.info("Connection pool created (min=%d, max=%d)", DB_POOL_MIN, DB_POOL_MAX)
    return _pool


class db_conn:
    """Context manager that borrows a connection from the pool and returns it."""
    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.rollback()
        get_pool().putconn(self.conn)


# ── Startup helpers ───────────────────────────────────────────────────────────

def wait_for_db(retries: int = 15, delay: float = 2.0) -> None:
    """Block until PostgreSQL is reachable, with exponential back-off."""
    for attempt in range(1, retries + 1):
        try:
            with db_conn() as conn:
                conn.cursor().execute("SELECT 1")
            log.info("Database is ready.")
            return
        except psycopg2.OperationalError as exc:
            wait = delay * attempt
            log.warning("DB not ready (attempt %d/%d): %s — retrying in %.0fs",
                        attempt, retries, exc, wait)
            time.sleep(wait)
    raise RuntimeError("Could not connect to the database after multiple retries.")


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS urls (
                    id           SERIAL PRIMARY KEY,
                    url          TEXT UNIQUE NOT NULL,
                    status       TEXT        NOT NULL DEFAULT 'PENDING',
                    last_checked TIMESTAMP,
                    paused       BOOLEAN     NOT NULL DEFAULT FALSE
                )
            """)
            # Idempotent column addition for existing deployments
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='urls' AND column_name='paused'
                    ) THEN
                        ALTER TABLE urls ADD COLUMN paused BOOLEAN NOT NULL DEFAULT FALSE;
                    END IF;
                END
                $$;
            """)
        conn.commit()
    log.info("Database initialised.")


# ── Alerting ──────────────────────────────────────────────────────────────────

def send_gchat_alert(url: str, detail: str) -> None:
    """Fire a Google Chat webhook alert. Silently skipped if not configured."""
    if not GC_WEBHOOK:
        return
    payload = {
        "text": f"🚨 *MONITOR ALERT*\n*URL:* {url}\n*Status:* DOWN\n*Detail:* {detail}"
    }
    try:
        resp = requests.post(GC_WEBHOOK, json=payload, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Failed to send GChat alert: %s", exc)


# ── Monitor loop ──────────────────────────────────────────────────────────────

def check_url(url: str) -> tuple[str, str]:
    """
    Return (status, detail) for a single URL.
    Tries HEAD first; falls back to GET on 405.
    """
    try:
        resp = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code == 405:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        is_up = 200 <= resp.status_code < 400
        return ("UP" if is_up else "DOWN"), f"HTTP {resp.status_code}"
    except requests.Timeout:
        return "DOWN", "Connection timed out"
    except requests.ConnectionError:
        return "DOWN", "Connection error / DNS failure"
    except requests.RequestException as exc:
        return "DOWN", str(exc)


def monitor_loop() -> None:
    """Background thread: check all active URLs and persist results."""
    log.info("Monitor loop started (interval=%ds, timeout=%ds)",
             CHECK_INTERVAL, REQUEST_TIMEOUT)

    while True:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, url, status, paused FROM urls")
                    rows = cur.fetchall()

            for url_id, url, prev_status, paused in rows:
                if paused:
                    continue

                new_status, detail = check_url(url)

                try:
                    with db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE urls
                                SET status=%s, last_checked=CURRENT_TIMESTAMP
                                WHERE id=%s
                                """,
                                (new_status, url_id),
                            )
                        conn.commit()
                except psycopg2.Error as exc:
                    log.error("DB update failed for %s: %s", url, exc)
                    continue

                if new_status == "DOWN" and prev_status != "DOWN":
                    log.warning("DOWN detected: %s (%s)", url, detail)
                    send_gchat_alert(url, detail)
                elif new_status == "UP" and prev_status == "DOWN":
                    log.info("RECOVERY: %s is back UP", url)

        except psycopg2.Error as exc:
            log.error("Monitor loop DB error: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected monitor loop error: %s", exc)

        time.sleep(CHECK_INTERVAL)


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/urls", methods=["GET"])
def get_urls():
    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    id,
                    url,
                    status,
                    paused,
                    TO_CHAR(last_checked, 'YYYY-MM-DD HH24:MI:SS') AS last_checked
                FROM urls
                ORDER BY id DESC
            """)
            urls = cur.fetchall()
    return jsonify(urls)


@app.route("/api/urls", methods=["POST"])
def add_url():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "URL must start with http:// or https://"}), 400

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO urls (url, status) VALUES (%s, 'PENDING')", (url,)
                )
            conn.commit()
        log.info("Added monitor: %s", url)
        return jsonify({"message": "Added successfully"}), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "URL is already being monitored"}), 409
    except psycopg2.Error as exc:
        log.error("Failed to add URL: %s", exc)
        return jsonify({"error": "Database error"}), 500


@app.route("/api/urls/<int:url_id>", methods=["DELETE"])
def delete_url(url_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM urls WHERE id=%s RETURNING id", (url_id,))
            if not cur.fetchone():
                return jsonify({"error": "URL not found"}), 404
        conn.commit()
    log.info("Deleted monitor id=%d", url_id)
    return jsonify({"message": "Deleted successfully"})


@app.route("/api/urls/<int:url_id>/pause", methods=["PATCH"])
def toggle_pause(url_id: int):
    data = request.get_json(silent=True) or {}
    paused = data.get("paused")

    if paused is None:
        return jsonify({"error": "'paused' field is required"}), 400

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE urls SET paused=%s WHERE id=%s RETURNING id",
                (bool(paused), url_id),
            )
            if not cur.fetchone():
                return jsonify({"error": "URL not found"}), 404
        conn.commit()

    action = "paused" if paused else "resumed"
    log.info("Monitor id=%d %s", url_id, action)
    return jsonify({"message": action.capitalize()})


@app.route("/healthz")
def healthz():
    """Lightweight liveness probe for Docker / load balancers."""
    try:
        with db_conn() as conn:
            conn.cursor().execute("SELECT 1")
        return jsonify({"status": "ok"}), 200
    except psycopg2.Error:
        return jsonify({"status": "db_unavailable"}), 503


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    wait_for_db()
    init_db()
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, use_reloader=False)