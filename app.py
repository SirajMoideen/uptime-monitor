import os
import time
import threading
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

# Load configuration
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
GC_WEBHOOK = os.getenv("GOOGLE_CHAT_WEBHOOK")

app = Flask(__name__)

def get_db_connection():
    """Establishes a connection to PostgreSQL."""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Initializes the PostgreSQL database."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS urls (
                    id SERIAL PRIMARY KEY, 
                    url TEXT UNIQUE, 
                    status TEXT, 
                    last_checked TIMESTAMP
                )
            ''')
        conn.commit()

def send_gchat_alert(url, error_detail):
    """Sends a webhook message to Google Chat."""
    if not GC_WEBHOOK:
        print("⚠️ Google Chat Webhook not configured.")
        return
    
    message = {
        "text": f"🚨 *MONITOR ALERT* 🚨\n*URL:* {url}\n*Status:* DOWN\n*Detail:* {error_detail}"
    }
    try:
        requests.post(GC_WEBHOOK, json=message)
    except Exception as e:
        print(f"Failed to send GChat alert: {e}")

def monitor_loop():
    """Background task that checks URLs and updates the DB."""
    while True:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, url, status FROM urls")
                urls = cur.fetchall()
        
        for url_id, url, prev_status in urls:
            try:
                resp = requests.head(url, timeout=5, allow_redirects=True)
                if resp.status_code == 405:
                    resp = requests.get(url, timeout=5)
                
                is_up = 200 <= resp.status_code < 400
                new_status = "UP" if is_up else "DOWN"
                detail = f"HTTP {resp.status_code}"
                
            except requests.RequestException:
                new_status = "DOWN"
                detail = "Connection Timeout / DNS Error"
            
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute('''
                        UPDATE urls 
                        SET status=%s, last_checked=CURRENT_TIMESTAMP 
                        WHERE id=%s
                    ''', (new_status, url_id))
                conn.commit()
            
            if new_status == "DOWN" and prev_status != "DOWN":
                send_gchat_alert(url, detail)

        time.sleep(CHECK_INTERVAL)

# --- WEB ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/urls', methods=['GET'])
def get_urls():
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, url, status, TO_CHAR(last_checked, 'YYYY-MM-DD HH24:MI:SS') as last_checked FROM urls ORDER BY id DESC")
            urls = cur.fetchall()
    return jsonify(urls)

@app.route('/api/urls', methods=['POST'])
def add_url():
    url = request.json.get('url')
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO urls (url, status) VALUES (%s, 'PENDING')", (url,))
            conn.commit()
        return jsonify({"message": "Added successfully"}), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "URL already exists"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/urls/<int:url_id>', methods=['DELETE'])
def delete_url(url_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM urls WHERE id=%s", (url_id,))
        conn.commit()
    return jsonify({"message": "Deleted successfully"})

if __name__ == '__main__':
    # Add a slight delay to allow the PostgreSQL container to boot up first
    time.sleep(3)
    init_db()
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
