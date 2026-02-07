import os
import time
import secrets
import sqlite3
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

from azure.communication.email import EmailClient  # pip install azure-communication-email

app = FastAPI()

# --- App Service Linux persistent storage lives under /home
# Use Azure persistent storage when running in App Service, otherwise local ./data
DEFAULT_LOCAL_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATA_DIR = os.environ.get("DATA_DIR") or ("/home/site/data" if os.environ.get("WEBSITE_SITE_NAME") else DEFAULT_LOCAL_DATA_DIR)
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "app.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads") 
DB_PATH = os.path.join(DATA_DIR, "app.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)

BASE_URL = os.environ.get("BASE_URL")
ACS_EMAIL_CONNECTION_STRING = os.environ.get("ACS_EMAIL_CONNECTION_STRING")
FROM_EMAIL = os.environ.get("FROM_EMAIL")  # e.g. "DoNotReply@xxxx.azurecomm.net" or your verified sender
print("ACS set?", bool(os.environ.get("ACS_EMAIL_CONNECTION_STRING")))
print("FROM_EMAIL:", os.environ.get("FROM_EMAIL"))

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            token TEXT PRIMARY KEY,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            text TEXT NOT NULL,
            image_path TEXT,
            image_url TEXT,
            expires_at REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


@app.on_event("startup")
def on_startup():
    init_db()


def send_email_acs(to_email: str, subject: str, body_text: str):
    print("ACS set?", bool(os.environ.get("ACS_EMAIL_CONNECTION_STRING")))
    print("FROM_EMAIL:", os.environ.get("FROM_EMAIL"))
    if not ACS_EMAIL_CONNECTION_STRING or not FROM_EMAIL:
        raise RuntimeError("Missing ACS_EMAIL_CONNECTION_STRING or FROM_EMAIL env vars")

    client = EmailClient.from_connection_string(ACS_EMAIL_CONNECTION_STRING)

    message = {
        "senderAddress": FROM_EMAIL,
        "recipients": {"to": [{"address": to_email}]},
        "content": {
            "subject": subject,
            "plainText": body_text,
        },
    }

    poller = client.begin_send(message)
    # For MVP, wait for completion so you see failures immediately in logs
    poller.result()


def fetch_message(token: str) -> Optional[sqlite3.Row]:
    conn = get_db()
    row = conn.execute("SELECT * FROM messages WHERE token = ?", (token,)).fetchone()
    conn.close()
    return row


def delete_message(token: str):
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE token = ?", (token,))
    conn.commit()
    conn.close()


@app.post("/send")
async def send_link(
    to: str = Form(...),
    text: str = Form(...),
    ttl_seconds: int = Form(3600),
    subject: str = Form("Your secure link"),
    image: Optional[UploadFile] = File(None),
    image_url: Optional[str] = Form(None),
):
    """
    Send a personalized link that displays text + image.
    Provide either an uploaded image OR an image_url.
    """

    if not image and not image_url:
        raise HTTPException(status_code=400, detail="Provide image upload or image_url")

    token = secrets.token_urlsafe(24)
    now = time.time()
    expires_at = now + int(ttl_seconds)

    image_path = None
    if image:
        ext = os.path.splitext(image.filename or "")[1].lower() or ".png"
        image_path = os.path.join(UPLOAD_DIR, f"{token}{ext}")
        content = await image.read()
        with open(image_path, "wb") as f:
            f.write(content)

    # Store in SQLite
    conn = get_db()
    conn.execute(
        """
        INSERT INTO messages (token, recipient, subject, text, image_path, image_url, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (token, to, subject, text, image_path, image_url, expires_at, now),
    )
    conn.commit()
    conn.close()

    link = f"{BASE_URL}/view/{token}"
    email_body = f"Open your link (expires in {ttl_seconds} seconds):\n{link}"

    # Send via ACS Email
    send_email_acs(to, subject, email_body)

    return {"status": "sent", "link": link, "expires_at": expires_at}


@app.get("/image/{token}")
def get_image(token: str):
    row = fetch_message(token)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    if time.time() > row["expires_at"]:
        delete_message(token)
        raise HTTPException(status_code=410, detail="Expired")

    image_path = row["image_path"]
    if not image_path or not os.path.exists(image_path):
        raise HTTPException(status_code=404, detail="No stored image")

    return FileResponse(image_path)


from html import escape
from fastapi.responses import HTMLResponse

@app.get("/view/{token}", response_class=HTMLResponse)
def view(token: str):
    row = fetch_message(token)
    if not row:
        raise HTTPException(status_code=404, detail="Invalid link")

    if time.time() > row["expires_at"]:
        delete_message(token)
        return HTMLResponse("<h2>Link expired</h2>", status_code=410)

    # Escape user-provided text for safety
    safe_text = escape(row["text"]).replace("\n", "<br/>")

    # Prefer external image_url if provided, otherwise serve uploaded image from our endpoint
    img_src = row["image_url"] if row["image_url"] else f"/image/{token}"

    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width,initial-scale=1"/>
        <title>Your message</title>
        <style>
          body {{
            font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
            margin: 0;
            background: #f6f7fb;
            color: #111827;
          }}
          .wrap {{
            max-width: 860px;
            margin: 0 auto;
            padding: 32px 16px;
          }}
          .card {{
            background: white;
            border-radius: 18px;
            box-shadow: 0 10px 30px rgba(0,0,0,.08);
            overflow: hidden;
          }}
          .hero {{
            width: 100%;
            display: block;
            object-fit: cover;
            max-height: 420px;
            background: #e5e7eb;
          }}
          .content {{
            padding: 22px 22px 26px;
          }}
          h1 {{
            font-size: 22px;
            margin: 0 0 10px;
            letter-spacing: -0.01em;
          }}
          .p {{
            font-size: 16px;
            line-height: 1.6;
            margin: 0;
            color: #374151;
          }}
          .meta {{
            margin-top: 14px;
            font-size: 12px;
            color: #9ca3af;
          }}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="card">
            <img class="hero" src="{img_src}" alt="Image"/>
            <div class="content">
              <h1>To My Beloved...</h1>
              <p class="p">{safe_text}</p>
              <div class="meta">This link expires automatically.</div>
            </div>
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)
