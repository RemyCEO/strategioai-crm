from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import os
import psycopg2
import psycopg2.extras
import uuid
import stripe
import base64
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from contextlib import contextmanager
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv()

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL")
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_REDIRECT_URI = os.getenv("GMAIL_REDIRECT_URI", "http://localhost:8000/api/gmail/callback")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]
_oauth_states = {}  # state -> code_verifier, for PKCE

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# --- Database ---

@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                company TEXT,
                email TEXT,
                phone TEXT,
                source TEXT DEFAULT 'manuell',
                status TEXT DEFAULT 'ny',
                notes TEXT,
                created_at TEXT DEFAULT (now() AT TIME ZONE 'utc'),
                updated_at TEXT DEFAULT (now() AT TIME ZONE 'utc')
            );
            CREATE TABLE IF NOT EXISTS deals (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL,
                title TEXT NOT NULL,
                value REAL DEFAULT 0,
                package TEXT,
                status TEXT DEFAULT 'ny',
                notes TEXT,
                type TEXT DEFAULT 'engang',
                recurring_amount REAL DEFAULT 0,
                created_at TEXT DEFAULT (now() AT TIME ZONE 'utc'),
                updated_at TEXT DEFAULT (now() AT TIME ZONE 'utc'),
                FOREIGN KEY (contact_id) REFERENCES contacts(id)
            );
            CREATE TABLE IF NOT EXISTS activities (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL,
                deal_id TEXT,
                type TEXT NOT NULL,
                note TEXT,
                created_at TEXT DEFAULT (now() AT TIME ZONE 'utc'),
                FOREIGN KEY (contact_id) REFERENCES contacts(id)
            );
        """)
        # Migrasjon: legg til kolonner hvis de mangler
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='deals' AND column_name='type') THEN
                    ALTER TABLE deals ADD COLUMN type TEXT DEFAULT 'engang';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='deals' AND column_name='recurring_amount') THEN
                    ALTER TABLE deals ADD COLUMN recurring_amount REAL DEFAULT 0;
                END IF;
            END$$;
        """)


# --- Telegram ---

async def telegram(msg: str):
    if not TELEGRAM_TOKEN:
        return
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )


# --- Models ---

class Contact(BaseModel):
    name: str
    company: str = None
    email: str = None
    phone: str = None
    source: str = "manuell"
    status: str = "ny"
    notes: str = None

class ContactUpdate(BaseModel):
    name: str = None
    company: str = None
    email: str = None
    phone: str = None
    source: str = None
    status: str = None
    notes: str = None

class Deal(BaseModel):
    contact_id: str
    title: str
    value: float = 0
    package: str = None
    status: str = "ny"
    notes: str = None
    type: str = "engang"
    recurring_amount: float = 0

class DealUpdate(BaseModel):
    title: str = None
    value: float = None
    package: str = None
    status: str = None
    notes: str = None
    type: str = None
    recurring_amount: float = None

class Activity(BaseModel):
    contact_id: str
    deal_id: str = None
    type: str
    note: str = None


# --- Frontend ---

@app.get("/")
def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


# --- Health ---

@app.get("/api/ping")
def health():
    return {"ok": True, "setup": False}


# --- Dashboard ---

@app.get("/api/stats")
def stats():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM contacts")
        contacts = cur.fetchall()
        cur.execute("SELECT status, value, type, recurring_amount FROM deals")
        deals = cur.fetchall()

    pipeline_value = sum(d["value"] or 0 for d in deals if d["status"] not in ["vunnet", "tapt"])
    won_value = sum(d["value"] or 0 for d in deals if d["status"] == "vunnet")
    mrr = sum(d["recurring_amount"] or 0 for d in deals if d.get("type") == "subscription" and d["status"] == "vunnet")

    status_count = {}
    for c in contacts:
        s = c["status"]
        status_count[s] = status_count.get(s, 0) + 1

    return {
        "total_contacts": len(contacts),
        "pipeline_value": pipeline_value,
        "won_value": won_value,
        "mrr": mrr,
        "by_status": status_count,
        "total_deals": len(deals),
    }


# --- Contacts ---

@app.get("/api/contacts")
def get_contacts(search: str = None, status: str = None):
    with get_conn() as conn:
        cur = conn.cursor()
        if status:
            cur.execute("SELECT * FROM contacts WHERE status=%s ORDER BY created_at DESC", (status,))
        else:
            cur.execute("SELECT * FROM contacts ORDER BY created_at DESC")
        data = cur.fetchall()
    result = [dict(r) for r in data]
    if search:
        s = search.lower()
        result = [c for c in result if
                  s in (c.get("name") or "").lower() or
                  s in (c.get("company") or "").lower() or
                  s in (c.get("email") or "").lower()]
    return result

@app.post("/api/contacts")
def create_contact(c: Contact):
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    d = c.model_dump()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO contacts (id, name, company, email, phone, source, status, notes, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (new_id, d["name"], d["company"], d["email"], d["phone"],
             d["source"], d["status"], d["notes"], now, now)
        )
        row = cur.fetchone()
    return dict(row)

@app.patch("/api/contacts/{id}")
async def update_contact(id: str, c: ContactUpdate):
    payload = {k: v for k, v in c.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(400, "No fields to update")
    now = datetime.now(timezone.utc).isoformat()
    payload["updated_at"] = now
    sets = ", ".join(f"{k}=%s" for k in payload)
    vals = list(payload.values()) + [id]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE contacts SET {sets} WHERE id=%s RETURNING *", vals)
        row = cur.fetchone()
    result = dict(row) if row else {}
    if payload.get("status") == "kunde":
        await telegram(f"Ny kunde!\n<b>{result.get('name','')}</b>\nKilde: {result.get('source','')}")
    return result

@app.delete("/api/contacts/{id}")
def delete_contact(id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM contacts WHERE id=%s", (id,))
    return {"ok": True}


# --- Deals ---

@app.get("/api/deals")
def get_deals(contact_id: str = None):
    with get_conn() as conn:
        cur = conn.cursor()
        if contact_id:
            cur.execute(
                """SELECT d.*, c.name as contact_name, c.company as contact_company
                   FROM deals d LEFT JOIN contacts c ON d.contact_id=c.id
                   WHERE d.contact_id=%s ORDER BY d.created_at DESC""",
                (contact_id,)
            )
        else:
            cur.execute(
                """SELECT d.*, c.name as contact_name, c.company as contact_company
                   FROM deals d LEFT JOIN contacts c ON d.contact_id=c.id
                   ORDER BY d.created_at DESC"""
            )
        rows = cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["contacts"] = {"name": d.pop("contact_name", None), "company": d.pop("contact_company", None)}
        result.append(d)
    return result

@app.post("/api/deals")
def create_deal(d: Deal):
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    data = d.model_dump()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO deals (id, contact_id, title, value, package, status, notes, type, recurring_amount, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (new_id, data["contact_id"], data["title"], data["value"],
             data["package"], data["status"], data["notes"],
             data["type"], data["recurring_amount"], now, now)
        )
        row = cur.fetchone()
    return dict(row)

@app.patch("/api/deals/{id}")
async def update_deal(id: str, d: DealUpdate):
    payload = {k: v for k, v in d.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(400, "No fields to update")
    now = datetime.now(timezone.utc).isoformat()
    payload["updated_at"] = now
    sets = ", ".join(f"{k}=%s" for k in payload)
    vals = list(payload.values()) + [id]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE deals SET {sets} WHERE id=%s RETURNING *", vals)
        row = cur.fetchone()
    result = dict(row) if row else {}
    if payload.get("status") == "vunnet":
        val = result.get("value", 0) or 0
        await telegram(f"Deal vunnet!\n<b>{result.get('title','')}</b>\nVerdi: {val:,.0f} kr")
    return result

@app.delete("/api/deals/{id}")
def delete_deal(id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM deals WHERE id=%s", (id,))
    return {"ok": True}


# --- Activities ---

@app.get("/api/activities/{contact_id}")
def get_activities(contact_id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM activities WHERE contact_id=%s ORDER BY created_at DESC", (contact_id,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.post("/api/activities")
def create_activity(a: Activity):
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    data = a.model_dump()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO activities (id, contact_id, deal_id, type, note, created_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            (new_id, data["contact_id"], data["deal_id"], data["type"], data["note"], now)
        )
        row = cur.fetchone()
    return dict(row)


# --- Stripe ---

def stripe_ok():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(400, "STRIPE_SECRET_KEY mangler i .env")

@app.get("/api/stripe/customers")
def stripe_customers(limit: int = 100):
    stripe_ok()
    customers = stripe.Customer.list(limit=limit)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT email FROM contacts WHERE email IS NOT NULL")
        crm_emails = {r["email"] for r in cur.fetchall()}
    result = []
    for c in customers.auto_paging_iter():
        result.append({
            "stripe_id": c.id,
            "name": c.name,
            "email": c.email,
            "phone": c.phone,
            "created": datetime.fromtimestamp(c.created, tz=timezone.utc).isoformat(),
            "in_crm": (c.email or "").lower() in {e.lower() for e in crm_emails},
        })
    return result

@app.post("/api/stripe/sync")
def stripe_sync():
    stripe_ok()
    customers = stripe.Customer.list(limit=100)
    imported = 0
    skipped = 0
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT email FROM contacts")
        crm_emails = {(r["email"] or "").lower() for r in cur.fetchall()}
        for c in customers.auto_paging_iter():
            email = (c.email or "").lower()
            if email and email in crm_emails:
                skipped += 1
                continue
            new_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            cur.execute(
                "INSERT INTO contacts (id, name, company, email, phone, source, status, notes, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (new_id, c.name or c.email or "Ukjent", None, c.email, c.phone,
                 "stripe", "lead", f"Stripe ID: {c.id}", now, now)
            )
            if email:
                crm_emails.add(email)
            imported += 1
    return {"imported": imported, "skipped": skipped}

@app.get("/api/stripe/payments/{email}")
def stripe_payments(email: str):
    stripe_ok()
    customers = stripe.Customer.list(email=email, limit=5)
    if not customers.data:
        return []
    customer = customers.data[0]
    charges = stripe.Charge.list(customer=customer.id, limit=50)
    result = []
    for ch in charges.auto_paging_iter():
        result.append({
            "id": ch.id,
            "amount": ch.amount / 100,
            "currency": ch.currency.upper(),
            "status": ch.status,
            "description": ch.description,
            "date": datetime.fromtimestamp(ch.created, tz=timezone.utc).isoformat(),
            "receipt_url": ch.receipt_url,
        })
    return result

@app.get("/api/stripe/subscriptions/{email}")
def stripe_subscriptions(email: str):
    stripe_ok()
    customers = stripe.Customer.list(email=email, limit=5)
    if not customers.data:
        return []
    customer = customers.data[0]
    subs = stripe.Subscription.list(customer=customer.id, limit=20)
    result = []
    for s in subs.auto_paging_iter():
        item = s["items"]["data"][0] if s["items"]["data"] else {}
        price = item.get("price", {})
        result.append({
            "id": s.id,
            "status": s.status,
            "plan": price.get("nickname") or price.get("id", ""),
            "amount": (price.get("unit_amount") or 0) / 100,
            "currency": (price.get("currency") or "").upper(),
            "interval": price.get("recurring", {}).get("interval", ""),
            "current_period_end": datetime.fromtimestamp(s.current_period_end, tz=timezone.utc).isoformat(),
        })
    return result

@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError:
            raise HTTPException(400, "Ugyldig webhook-signatur")
    else:
        import json
        event = json.loads(payload)

    etype = event["type"]
    if etype in ("payment_intent.succeeded", "charge.succeeded"):
        obj = event["data"]["object"]
        email = None
        if etype == "charge.succeeded":
            email = obj.get("billing_details", {}).get("email") or obj.get("receipt_email")
        elif etype == "payment_intent.succeeded":
            email = obj.get("receipt_email")
            if not email:
                charges = stripe.Charge.list(payment_intent=obj["id"], limit=1)
                if charges.data:
                    email = charges.data[0].get("billing_details", {}).get("email")
        if email:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, name, source, status FROM contacts WHERE LOWER(email)=%s LIMIT 1", (email.lower(),))
                contact = cur.fetchone()
                if contact:
                    contact = dict(contact)
                    amount = (obj.get("amount") or obj.get("amount_received") or 0) / 100
                    currency = (obj.get("currency") or "nok").upper()
                    now = datetime.now(timezone.utc).isoformat()
                    cur.execute(
                        "INSERT INTO activities (id, contact_id, deal_id, type, note, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        (str(uuid.uuid4()), contact["id"], None, "betaling",
                         f"Stripe-betaling mottatt: {amount:,.0f} {currency}", now)
                    )
                    if contact.get("status") not in ("kunde", "aktiv"):
                        cur.execute("UPDATE contacts SET status='kunde', updated_at=%s WHERE id=%s", (now, contact["id"]))
                    await telegram(f"Ny betaling via Stripe!\n<b>{contact['name']}</b>\nBeløp: {amount:,.0f} {currency}")

    elif etype == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer")
        if customer_id:
            cust = stripe.Customer.retrieve(customer_id)
            email = cust.get("email")
            if email:
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT id FROM contacts WHERE LOWER(email)=%s LIMIT 1", (email.lower(),))
                    row = cur.fetchone()
                    if row:
                        now = datetime.now(timezone.utc).isoformat()
                        cur.execute(
                            "INSERT INTO activities (id, contact_id, deal_id, type, note, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                            (str(uuid.uuid4()), row["id"], None, "abonnement", "Stripe-abonnement kansellert", now)
                        )

    return {"ok": True}


# --- Gmail ---

def gmail_client_config():
    return {
        "web": {
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GMAIL_REDIRECT_URI],
        }
    }

def _gmail_token_db_get():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'gmail_token'")
        row = cur.fetchone()
        cur.close(); conn.close()
        return json.loads(row[0]) if row else None
    except Exception:
        return None

def _gmail_token_db_set(token, refresh_token):
    try:
        conn = get_db()
        cur = conn.cursor()
        data = json.dumps({"token": token, "refresh_token": refresh_token})
        cur.execute("""
            INSERT INTO app_settings (key, value) VALUES ('gmail_token', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (data,))
        conn.commit(); cur.close(); conn.close()
    except Exception:
        pass

def get_gmail_creds():
    data = _gmail_token_db_get()
    if not data:
        return None
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=GMAIL_SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(GRequest())
        _gmail_token_db_set(creds.token, creds.refresh_token)
    return creds

@app.get("/api/gmail/status")
def gmail_status():
    if not GMAIL_CLIENT_ID or not GMAIL_CLIENT_SECRET:
        return {"connected": False, "reason": "no_credentials"}
    creds = get_gmail_creds()
    if creds and (creds.valid or creds.refresh_token):
        return {"connected": True}
    return {"connected": False, "reason": "not_authorized"}

@app.get("/api/gmail/auth")
def gmail_auth():
    if not GMAIL_CLIENT_ID or not GMAIL_CLIENT_SECRET:
        raise HTTPException(400, "GMAIL_CLIENT_ID og GMAIL_CLIENT_SECRET mangler i .env")
    flow = Flow.from_client_config(gmail_client_config(), scopes=GMAIL_SCOPES, redirect_uri=GMAIL_REDIRECT_URI)
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    _oauth_states[state] = getattr(flow, 'code_verifier', None)
    return {"url": auth_url}

@app.get("/api/gmail/callback")
def gmail_callback(code: str, state: str = None):
    code_verifier = _oauth_states.pop(state, None) if state else None
    flow = Flow.from_client_config(gmail_client_config(), scopes=GMAIL_SCOPES, redirect_uri=GMAIL_REDIRECT_URI)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    _gmail_token_db_set(creds.token, creds.refresh_token)
    return RedirectResponse("/?gmail=connected")

@app.get("/api/gmail/threads")
def gmail_threads(email: str):
    creds = get_gmail_creds()
    if not creds:
        raise HTTPException(400, "Gmail ikke koblet til")
    service = build("gmail", "v1", credentials=creds)
    q = f"to:{email} OR from:{email}"
    res = service.users().messages().list(userId="me", q=q, maxResults=15).execute()
    messages = res.get("messages", [])
    threads = []
    for msg in messages:
        m = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["Subject", "From", "To", "Date"]
        ).execute()
        hdrs = {h["name"]: h["value"] for h in m["payload"]["headers"]}
        threads.append({
            "id": msg["id"],
            "subject": hdrs.get("Subject", "(ingen emne)"),
            "from": hdrs.get("From", ""),
            "to": hdrs.get("To", ""),
            "date": hdrs.get("Date", ""),
            "snippet": m.get("snippet", ""),
        })
    return threads

class GmailSend(BaseModel):
    to: str
    subject: str
    body: str
    contact_id: str = None

@app.post("/api/gmail/send")
async def gmail_send(data: GmailSend):
    creds = get_gmail_creds()
    if not creds:
        raise HTTPException(400, "Gmail ikke koblet til")
    service = build("gmail", "v1", credentials=creds)
    msg = MIMEMultipart("alternative")
    msg["to"] = data.to
    msg["subject"] = data.subject
    msg.attach(MIMEText(data.body, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    if data.contact_id:
        new_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO activities (id, contact_id, type, note, created_at) VALUES (%s,%s,%s,%s,%s)",
                (new_id, data.contact_id, "email", f"Gmail sendt til {data.to} — {data.subject}", now)
            )
    return {"ok": True}


# --- Init ---
init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
