import os
import time
import threading
import sqlite3
import requests
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

# Load configuration
load_dotenv()
DB_FILE = 'monitor.db'
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
GC_WEBHOOK = os.getenv("GOOGLE_CHAT_WEBHOOK")

app = Flask(__name__)

def init_db():
    """Initializes the SQLite database."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS urls 
                        (id INTEGER PRIMARY KEY, url TEXT UNIQUE, status TEXT, last_checked TEXT)''')

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
        with sqlite3.connect(DB_FILE) as conn:
            urls = conn.execute("SELECT id, url, status FROM urls").fetchall()
        
        for url_id, url, prev_status in urls:
            try:
                # HEAD request first to save bandwidth
                resp = requests.head(url, timeout=5, allow_redirects=True)
                if resp.status_code == 405:
                    resp = requests.get(url, timeout=5)
                
                is_up = 200 <= resp.status_code < 400
                new_status = "UP" if is_up else "DOWN"
                detail = f"HTTP {resp.status_code}"
                
            except requests.RequestException:
                new_status = "DOWN"
                detail = "Connection Timeout / DNS Error"
            
            # Update the database with the latest check
            with sqlite3.connect(DB_FILE) as conn:
                conn.execute("UPDATE urls SET status=?, last_checked=datetime('now', 'localtime') WHERE id=?", 
                             (new_status, url_id))
            
            # Only trigger an alert if the site JUST went down (state changed)
            if new_status == "DOWN" and prev_status != "DOWN":
                send_gchat_alert(url, detail)

        time.sleep(CHECK_INTERVAL)

# --- WEB ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/urls', methods=['GET'])
def get_urls():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        urls = conn.execute("SELECT * FROM urls ORDER BY id DESC").fetchall()
        return jsonify([dict(u) for u in urls])

@app.route('/api/urls', methods=['POST'])
def add_url():
    url = request.json.get('url')
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT INTO urls (url, status, last_checked) VALUES (?, 'PENDING', 'Never')", (url,))
        return jsonify({"message": "Added successfully"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "URL already exists"}), 400

@app.route('/api/urls/<int:url_id>', methods=['DELETE'])
def delete_url(url_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM urls WHERE id=?", (url_id,))
    return jsonify({"message": "Deleted successfully"})

if __name__ == '__main__':
    init_db()
    # Start the monitoring loop in a background thread
    threading.Thread(target=monitor_loop, daemon=True).start()
    # Start the web server
    app.run(host='0.0.0.0', port=5000)
