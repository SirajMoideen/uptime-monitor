import os
import time
import logging
import threading
import subprocess
import platform
import socket

import requests
from requests.exceptions import ConnectionError, RequestException, SSLError, Timeout
import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

DATABASE_URL    = os.getenv("DATABASE_URL")
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
GC_WEBHOOK      = os.getenv("GOOGLE_CHAT_WEBHOOK")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
DB_POOL_MIN     = int(os.getenv("DB_POOL_MIN", "2"))
DB_POOL_MAX     = int(os.getenv("DB_POOL_MAX", "10"))

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
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(DB_POOL_MIN, DB_POOL_MAX, DATABASE_URL)
        log.info("Connection pool created (min=%d, max=%d)", DB_POOL_MIN, DB_POOL_MAX)
    return _pool


class db_conn:
    """Context manager: borrows a connection from the pool and returns it."""
    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.rollback()
        get_pool().putconn(self.conn)


# ── Startup ───────────────────────────────────────────────────────────────────

def wait_for_db(retries: int = 15, delay: float = 2.0) -> None:
    for attempt in range(1, retries + 1):
        try:
            with db_conn() as conn:
                conn.cursor().execute("SELECT 1")
            log.info("Database is ready.")
            return
        except psycopg2.OperationalError as exc:
            wait = delay * attempt
            log.warning("DB not ready (%d/%d): %s — retrying in %.0fs", attempt, retries, exc, wait)
            time.sleep(wait)
    raise RuntimeError("Could not connect to the database after multiple retries.")


def init_db() -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS urls (
                    id           SERIAL PRIMARY KEY,
                    url          TEXT UNIQUE NOT NULL,
                    check_type   TEXT        NOT NULL DEFAULT 'http',
                    status       TEXT        NOT NULL DEFAULT 'PENDING',
                    last_checked TIMESTAMP,
                    paused       BOOLEAN     NOT NULL DEFAULT FALSE
                )
            """)
            # Idempotent migrations for existing deployments
            for col, defn in [
                ("paused",     "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("check_type", "TEXT NOT NULL DEFAULT 'http'"),
                ("ignore_ssl", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ]:
                cur.execute(f"""
                    DO $$ BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name='urls' AND column_name='{col}'
                        ) THEN ALTER TABLE urls ADD COLUMN {col} {defn}; END IF;
                    END $$;
                """)
        conn.commit()
    log.info("Database initialised.")


# ── Alerting ──────────────────────────────────────────────────────────────────

def send_gchat_alert(target: str, detail: str) -> None:
    if not GC_WEBHOOK:
        return
    payload = {"text": f"🚨 *MONITOR ALERT*\n*Target:* {target}\n*Status:* DOWN\n*Detail:* {detail}"}
    try:
        requests.post(GC_WEBHOOK, json=payload, timeout=5).raise_for_status()
    except requests.RequestException as exc:
        log.error("GChat alert failed: %s", exc)


# ── Check logic ───────────────────────────────────────────────────────────────

def check_http(url: str, verify_ssl: bool = True) -> tuple[str, str]:
    """HTTP/HTTPS check. Set verify_ssl=False to skip certificate validation."""
    try:
        resp = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, verify=verify_ssl)
        if resp.status_code == 405:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, verify=verify_ssl)
        is_up = 200 <= resp.status_code < 400
        return ("UP" if is_up else "DOWN"), f"HTTP {resp.status_code}"
    except SSLError:
        return "DOWN", "SSL certificate error"
    except Timeout:
        return "DOWN", "Connection timed out"
    except ConnectionError:
        return "DOWN", "Connection error / DNS failure"
    except RequestException as exc:
        return "DOWN", str(exc)


def check_ping(host: str) -> tuple[str, str]:
    """
    ICMP ping check. Works on Linux (Docker) and macOS.
    host can be a bare hostname/IP — no scheme or port.
    """
    flag = "-n" if platform.system().lower() == "windows" else "-c"
    try:
        result = subprocess.run(
            ["ping", flag, "1", "-W", str(REQUEST_TIMEOUT), host],
            capture_output=True, timeout=REQUEST_TIMEOUT + 2
        )
        if result.returncode == 0:
            return "UP", "ICMP ping OK"
        return "DOWN", "Host unreachable (ping failed)"
    except subprocess.TimeoutExpired:
        return "DOWN", "Ping timed out"
    except FileNotFoundError:
        return "DOWN", "ping command not available"
    except Exception as exc:
        return "DOWN", str(exc)


def check_tcp(host: str, port: int) -> tuple[str, str]:
    """Raw TCP port check — useful for services that don't speak HTTP."""
    try:
        with socket.create_connection((host, port), timeout=REQUEST_TIMEOUT):
            return "UP", f"TCP port {port} open"
    except socket.timeout:
        return "DOWN", f"TCP port {port} timed out"
    except ConnectionRefusedError:
        return "DOWN", f"TCP port {port} refused"
    except OSError as exc:
        return "DOWN", str(exc)


def normalize_monitor_url(url: str, check_type: str) -> str:
    check_type = check_type.lower()
    if check_type in ("http", "https") and not url.startswith(("http://", "https://")):
        url = f"{check_type}://{url}"
    return url


def dispatch_check(url: str, check_type: str, ignore_ssl: bool = False) -> tuple[str, str]:
    """Route a monitor entry to the correct check function."""
    check_type = check_type.lower()

    if check_type == "ping":
        # Strip any scheme/port — just need the host
        host = url.replace("http://", "").replace("https://", "").split(":")[0].split("/")[0]
        return check_ping(host)

    if check_type == "tcp":
        # Expect url like "192.168.0.1:9000" or "hostname:9000"
        host_part = url.replace("http://", "").replace("https://", "").split("/")[0]
        if ":" in host_part:
            host, port_str = host_part.rsplit(":", 1)
            try:
                return check_tcp(host, int(port_str))
            except ValueError:
                return "DOWN", "Invalid port number"
        return "DOWN", "TCP check requires a port (host:port)"

    # Default: http or https
    return check_http(url, verify_ssl=not ignore_ssl)


# ── Monitor loop ──────────────────────────────────────────────────────────────

def monitor_loop() -> None:
    log.info("Monitor loop started (interval=%ds)", CHECK_INTERVAL)
    while True:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, url, check_type, status, paused, ignore_ssl FROM urls"
                    )
                    rows = cur.fetchall()

            for url_id, url, check_type, prev_status, paused, ignore_ssl in rows:
                if paused:
                    continue

                new_status, detail = dispatch_check(url, check_type, ignore_ssl)

                try:
                    with db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE urls SET status=%s, last_checked=CURRENT_TIMESTAMP WHERE id=%s",
                                (new_status, url_id),
                            )
                        conn.commit()
                except psycopg2.Error as exc:
                    log.error("DB update failed for %s: %s", url, exc)
                    continue

                if new_status == "DOWN" and prev_status != "DOWN":
                    log.warning("DOWN: %s (%s)", url, detail)
                    send_gchat_alert(url, detail)
                elif new_status == "UP" and prev_status == "DOWN":
                    log.info("RECOVERY: %s is back UP", url)

        except psycopg2.Error as exc:
            log.error("Monitor loop DB error: %s", exc)
        except Exception as exc:
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
                SELECT id, url, check_type, status, paused, ignore_ssl,
                       TO_CHAR(last_checked, 'YYYY-MM-DD HH24:MI:SS') AS last_checked
                FROM urls ORDER BY id DESC
            """)
            urls = cur.fetchall()
    return jsonify(urls)


VALID_CHECK_TYPES = {"http", "https", "ping", "tcp"}


def _parse_monitor_payload(data: dict) -> tuple[str, str, bool] | tuple[None, str, int]:
    url        = (data.get("url") or "").strip()
    check_type = (data.get("check_type") or "http").strip().lower()
    ignore_ssl = bool(data.get("ignore_ssl", False))

    if not url:
        return None, "URL / host is required", 400
    if check_type not in VALID_CHECK_TYPES:
        return None, f"check_type must be one of: {', '.join(sorted(VALID_CHECK_TYPES))}", 400
    if check_type != "https":
        ignore_ssl = False

    return normalize_monitor_url(url, check_type), check_type, ignore_ssl


@app.route("/api/urls", methods=["POST"])
def add_url():
    data = request.get_json(silent=True) or {}
    parsed = _parse_monitor_payload(data)
    if parsed[0] is None:
        return jsonify({"error": parsed[1]}), parsed[2]

    url, check_type, ignore_ssl = parsed

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO urls (url, check_type, status, ignore_ssl) VALUES (%s, %s, 'PENDING', %s)",
                    (url, check_type, ignore_ssl),
                )
            conn.commit()
        log.info("Added monitor [%s]: %s (ignore_ssl=%s)", check_type, url, ignore_ssl)
        return jsonify({"message": "Added successfully"}), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Already being monitored"}), 409
    except psycopg2.Error as exc:
        log.error("Failed to add URL: %s", exc)
        return jsonify({"error": "Database error"}), 500


@app.route("/api/urls/<int:url_id>", methods=["PUT"])
def update_url(url_id: int):
    data = request.get_json(silent=True) or {}
    parsed = _parse_monitor_payload(data)
    if parsed[0] is None:
        return jsonify({"error": parsed[1]}), parsed[2]

    url, check_type, ignore_ssl = parsed

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE urls
                       SET url=%s, check_type=%s, ignore_ssl=%s, status='PENDING'
                       WHERE id=%s RETURNING id""",
                    (url, check_type, ignore_ssl, url_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Not found"}), 404
            conn.commit()
        log.info("Updated monitor id=%d [%s]: %s (ignore_ssl=%s)", url_id, check_type, url, ignore_ssl)
        return jsonify({"message": "Updated successfully"})
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Another monitor already uses this endpoint"}), 409
    except psycopg2.Error as exc:
        log.error("Failed to update URL id=%d: %s", url_id, exc)
        return jsonify({"error": "Database error"}), 500


@app.route("/api/urls/<int:url_id>", methods=["DELETE"])
def delete_url(url_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM urls WHERE id=%s RETURNING id", (url_id,))
            if not cur.fetchone():
                return jsonify({"error": "Not found"}), 404
        conn.commit()
    log.info("Deleted monitor id=%d", url_id)
    return jsonify({"message": "Deleted successfully"})


@app.route("/api/urls/<int:url_id>/pause", methods=["PATCH"])
def toggle_pause(url_id: int):
    data   = request.get_json(silent=True) or {}
    paused = data.get("paused")
    if paused is None:
        return jsonify({"error": "'paused' is required"}), 400
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE urls SET paused=%s WHERE id=%s RETURNING id", (bool(paused), url_id))
            if not cur.fetchone():
                return jsonify({"error": "Not found"}), 404
        conn.commit()
    action = "paused" if paused else "resumed"
    log.info("Monitor id=%d %s", url_id, action)
    return jsonify({"message": action.capitalize()})


@app.route("/healthz")
def healthz():
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