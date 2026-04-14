from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
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
from email.mime.image import MIMEImage
from datetime import datetime, timezone
from contextlib import contextmanager
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv()

app = FastAPI()

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

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
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                category TEXT DEFAULT 'email',
                image_filename TEXT,
                created_at TEXT DEFAULT (now() AT TIME ZONE 'utc')
            );
        """)
        # Migrasjon: legg til category og image_filename hvis de mangler
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='email_templates' AND column_name='category') THEN
                    ALTER TABLE email_templates ADD COLUMN category TEXT DEFAULT 'email';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='email_templates' AND column_name='image_filename') THEN
                    ALTER TABLE email_templates ADD COLUMN image_filename TEXT;
                END IF;
            END$$;
        """)
        # Seed templates fra gmail_templates.json (insert kun de som ikke finnes)
        templates_path = os.path.join(os.path.dirname(__file__), "gmail_templates.json")
        if os.path.exists(templates_path):
            with open(templates_path, encoding="utf-8") as f:
                raw = json.load(f)
            cur.execute("SELECT name FROM email_templates")
            existing_names = {r["name"] for r in cur.fetchall()}
            skip_subjects = {"Laras Kaker admin"}
            for t in raw:
                name = t.get("name", t["subject"])
                if name in skip_subjects or name in existing_names:
                    continue
                cur.execute(
                    "INSERT INTO email_templates (id, name, subject, body, category, image_filename) VALUES (%s,%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), name, t["subject"], t["body"], t.get("category", "email"), t.get("image_filename"))
                )
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
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='contacts' AND column_name='category') THEN
                    ALTER TABLE contacts ADD COLUMN category TEXT;
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
    category: str = None
    notes: str = None

class ContactUpdate(BaseModel):
    name: str = None
    company: str = None
    email: str = None
    phone: str = None
    source: str = None
    status: str = None
    category: str = None
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

class EmailTemplate(BaseModel):
    name: str
    subject: str
    body: str
    category: str = "email"
    image_filename: str = None


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
        cur.execute("SELECT status, category FROM contacts")
        contacts = cur.fetchall()
        cur.execute("SELECT status, value, type, recurring_amount FROM deals")
        deals = cur.fetchall()

    pipeline_value = sum(d["value"] or 0 for d in deals if d["status"] not in ["vunnet", "tapt"])
    won_value = sum(d["value"] or 0 for d in deals if d["status"] == "vunnet")
    mrr = sum(d["recurring_amount"] or 0 for d in deals if d.get("type") == "subscription" and d["status"] == "vunnet")

    status_count = {}
    category_count = {}
    for c in contacts:
        s = c["status"]
        status_count[s] = status_count.get(s, 0) + 1
        cat = c["category"]
        if cat:
            category_count[cat] = category_count.get(cat, 0) + 1

    return {
        "total_contacts": len(contacts),
        "pipeline_value": pipeline_value,
        "won_value": won_value,
        "mrr": mrr,
        "by_status": status_count,
        "by_category": category_count,
        "total_deals": len(deals),
    }


@app.get("/api/activities/recent")
def recent_activities(limit: int = 10):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT a.*, c.name as contact_name
            FROM activities a
            LEFT JOIN contacts c ON a.contact_id = c.id
            ORDER BY a.created_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


class BulkCategoryUpdate(BaseModel):
    ids: list
    category: str = None

@app.post("/api/contacts/bulk-category")
def bulk_category(data: BulkCategoryUpdate):
    if not data.ids:
        raise HTTPException(400, "Ingen IDer")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(data.ids))
        cur.execute(
            f"UPDATE contacts SET category=%s, updated_at=%s WHERE id IN ({placeholders})",
            [data.category, now] + list(data.ids)
        )
    return {"updated": len(data.ids)}


# --- Contacts ---

@app.get("/api/contacts")
def get_contacts(search: str = None, status: str = None, category: str = None):
    with get_conn() as conn:
        cur = conn.cursor()
        wheres, vals = [], []
        if status:
            wheres.append("status=%s"); vals.append(status)
        if category:
            wheres.append("category=%s"); vals.append(category)
        sql = "SELECT * FROM contacts"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY created_at DESC"
        cur.execute(sql, vals)
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
            "INSERT INTO contacts (id, name, company, email, phone, source, status, category, notes, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (new_id, d["name"], d["company"], d["email"], d["phone"],
             d["source"], d["status"], d["category"], d["notes"], now, now)
        )
        row = cur.fetchone()
    return dict(row)

@app.get("/api/contacts/{id}")
def get_contact(id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM contacts WHERE id=%s", (id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Ikke funnet")
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

def _stripe_sub_price(s):
    """Hent pris-info fra subscription-objekt (kompatibel med nye Stripe SDK)."""
    items_data = s.items.data if hasattr(s.items, 'data') else []
    item = items_data[0] if items_data else None
    if item is None:
        return 0.0, "month", ""
    price = item.price if hasattr(item, 'price') else None
    if price is None:
        return 0.0, "month", ""
    amount = (price.unit_amount or 0) / 100
    recurring = price.recurring if hasattr(price, 'recurring') else None
    interval = recurring.interval if recurring and hasattr(recurring, 'interval') else "month"
    plan_name = (price.nickname or price.id or "") if hasattr(price, 'nickname') else ""
    return amount, interval, plan_name

@app.get("/api/stripe/summary")
def stripe_summary():
    stripe_ok()
    subs = stripe.Subscription.list(status="active", limit=100)
    mrr = 0.0
    active_sub_count = 0
    for s in subs.auto_paging_iter():
        active_sub_count += 1
        amount, interval, _ = _stripe_sub_price(s)
        mrr += amount / 12 if interval == "year" else amount
    charges = stripe.Charge.list(limit=50, expand=["data.billing_details"])
    total_revenue = 0.0
    recent_payments = []
    for ch in charges.auto_paging_iter():
        if ch.status == "succeeded":
            total_revenue += ch.amount / 100
        bd = ch.billing_details
        email = (bd.email if bd and hasattr(bd, 'email') else None) or ch.receipt_email
        recent_payments.append({
            "id": ch.id,
            "amount": ch.amount / 100,
            "currency": ch.currency.upper(),
            "status": ch.status,
            "email": email,
            "description": ch.description,
            "date": datetime.fromtimestamp(ch.created, tz=timezone.utc).isoformat(),
            "receipt_url": ch.receipt_url,
            "refunded": ch.refunded,
        })
    return {
        "mrr": round(mrr, 2),
        "arr": round(mrr * 12, 2),
        "active_subs": active_sub_count,
        "total_revenue": round(total_revenue, 2),
        "recent_payments": recent_payments[:20],
    }

@app.get("/api/stripe/customers")
def stripe_customers(limit: int = 100):
    stripe_ok()
    subs = stripe.Subscription.list(status="active", limit=100)
    sub_by_cid = {}
    for s in subs.auto_paging_iter():
        cid = s.customer if isinstance(s.customer, str) else s.customer.id
        amount, interval, plan_name = _stripe_sub_price(s)
        sub_by_cid[cid] = {
            "status": s.status,
            "monthly": round(amount / 12 if interval == "year" else amount, 2),
            "plan": plan_name,
            "sub_id": s.id,
            "period_end": datetime.fromtimestamp(s.current_period_end, tz=timezone.utc).isoformat(),
        }
    customers = stripe.Customer.list(limit=limit)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT email FROM contacts WHERE email IS NOT NULL")
        crm_emails = {r["email"].lower() for r in cur.fetchall()}
    result = []
    for c in customers.auto_paging_iter():
        sub = sub_by_cid.get(c.id)
        result.append({
            "stripe_id": c.id,
            "name": c.name,
            "email": c.email,
            "phone": c.phone,
            "created": datetime.fromtimestamp(c.created, tz=timezone.utc).isoformat(),
            "in_crm": (c.email or "").lower() in crm_emails,
            "subscription": sub,
            "balance": (c.balance or 0) / 100,
            "description": c.description,
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

@app.get("/api/stripe/invoices/{email}")
def stripe_invoices(email: str):
    stripe_ok()
    customers = stripe.Customer.list(email=email, limit=5)
    if not customers.data:
        return []
    customer = customers.data[0]
    invoices = stripe.Invoice.list(customer=customer.id, limit=50)
    result = []
    for inv in invoices.auto_paging_iter():
        result.append({
            "id": inv.id,
            "number": inv.number,
            "amount_due": (inv.amount_due or 0) / 100,
            "amount_paid": (inv.amount_paid or 0) / 100,
            "currency": (inv.currency or "").upper(),
            "status": inv.status,
            "due_date": datetime.fromtimestamp(inv.due_date, tz=timezone.utc).isoformat() if inv.due_date else None,
            "created": datetime.fromtimestamp(inv.created, tz=timezone.utc).isoformat(),
            "invoice_pdf": inv.invoice_pdf,
            "hosted_invoice_url": inv.hosted_invoice_url,
        })
    return result

class RefundRequest(BaseModel):
    charge_id: str
    amount: float = None  # None = full refund

@app.post("/api/stripe/refund")
def stripe_refund(data: RefundRequest):
    stripe_ok()
    params = {"charge": data.charge_id}
    if data.amount:
        params["amount"] = int(data.amount * 100)
    refund = stripe.Refund.create(**params)
    # Log CRM activity if contact found
    charge = stripe.Charge.retrieve(data.charge_id)
    email = (charge.billing_details or {}).get("email") or charge.receipt_email
    if email:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM contacts WHERE LOWER(email)=%s LIMIT 1", (email.lower(),))
            row = cur.fetchone()
            if row:
                now = datetime.now(timezone.utc).isoformat()
                amount_str = f"{refund.amount/100:,.0f} {charge.currency.upper()}"
                cur.execute(
                    "INSERT INTO activities (id, contact_id, deal_id, type, note, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), row["id"], None, "betaling", f"Refusjon sendt: {amount_str}", now)
                )
    return {"ok": True, "refund_id": refund.id, "status": refund.status, "amount": refund.amount / 100}

class CancelSubRequest(BaseModel):
    subscription_id: str

@app.post("/api/stripe/subscriptions/cancel")
def stripe_cancel_sub(data: CancelSubRequest):
    stripe_ok()
    sub = stripe.Subscription.modify(data.subscription_id, cancel_at_period_end=True)
    cancel_at = datetime.fromtimestamp(sub.current_period_end, tz=timezone.utc).isoformat()
    # Find customer email and log activity
    cust = stripe.Customer.retrieve(sub.customer if isinstance(sub.customer, str) else sub.customer.id)
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
                    (str(uuid.uuid4()), row["id"], None, "abonnement", f"Abonnement kansellert (avsluttes {cancel_at[:10]})", now)
                )
    return {"ok": True, "cancel_at": cancel_at}

class InvoiceRemindRequest(BaseModel):
    invoice_id: str

@app.post("/api/stripe/invoices/remind")
def stripe_invoice_remind(data: InvoiceRemindRequest):
    stripe_ok()
    inv = stripe.Invoice.retrieve(data.invoice_id)
    if inv.status != "open":
        raise HTTPException(400, f"Faktura er ikke åpen (status: {inv.status})")
    stripe.Invoice.send_invoice(data.invoice_id)
    return {"ok": True}

class CreateInvoiceRequest(BaseModel):
    email: str
    amount: float
    description: str
    currency: str = "nok"
    days_until_due: int = 14

@app.post("/api/stripe/create-invoice")
def stripe_create_invoice(data: CreateInvoiceRequest):
    stripe_ok()
    customers = stripe.Customer.list(email=data.email, limit=1)
    if customers.data:
        customer_id = customers.data[0].id
    else:
        cust = stripe.Customer.create(email=data.email)
        customer_id = cust.id
    stripe.InvoiceItem.create(
        customer=customer_id,
        amount=int(data.amount * 100),
        currency=data.currency,
        description=data.description,
    )
    invoice = stripe.Invoice.create(
        customer=customer_id,
        auto_advance=True,
        collection_method="send_invoice",
        days_until_due=data.days_until_due,
    )
    invoice = stripe.Invoice.finalize_invoice(invoice.id)
    stripe.Invoice.send_invoice(invoice.id)
    return {
        "ok": True,
        "invoice_id": invoice.id,
        "hosted_url": invoice.hosted_invoice_url,
        "pdf": invoice.invoice_pdf,
        "status": invoice.status,
    }

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

    elif etype == "invoice.payment_failed":
        obj = event["data"]["object"]
        customer_id = obj.get("customer")
        amount = (obj.get("amount_due") or 0) / 100
        currency = (obj.get("currency") or "nok").upper()
        if customer_id:
            cust = stripe.Customer.retrieve(customer_id)
            email = cust.get("email")
            if email:
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT id, name FROM contacts WHERE LOWER(email)=%s LIMIT 1", (email.lower(),))
                    row = cur.fetchone()
                    if row:
                        now = datetime.now(timezone.utc).isoformat()
                        cur.execute(
                            "INSERT INTO activities (id, contact_id, deal_id, type, note, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                            (str(uuid.uuid4()), row["id"], None, "betaling", f"Stripe-betaling feilet: {amount:,.0f} {currency}", now)
                        )
                        await telegram(f"⚠️ Betaling feilet!\n<b>{row['name']}</b>\nBeløp: {amount:,.0f} {currency}")

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
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'gmail_token'")
        row = cur.fetchone()
        cur.close(); conn.close()
        return json.loads(row[0]) if row else None
    except Exception as e:
        import logging
        logging.getLogger("crm").warning(f"_gmail_token_db_get error: {e}")
        return None

def _gmail_token_db_set(token, refresh_token):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        data = json.dumps({"token": token, "refresh_token": refresh_token})
        cur.execute("""
            INSERT INTO app_settings (key, value) VALUES ('gmail_token', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (data,))
        conn.commit(); cur.close(); conn.close()
    except Exception:
        pass

_gmail_creds_cache = {"creds": None, "expires_at": 0.0}

def get_gmail_creds():
    import time
    data = _gmail_token_db_get()
    if not data or not data.get("refresh_token"):
        return None
    now = time.time()
    # Returner cachet token hvis det er gyldig i minst 5 min til
    if _gmail_creds_cache["creds"] and _gmail_creds_cache["expires_at"] > now + 300:
        return _gmail_creds_cache["creds"]
    # Refresh access token
    try:
        resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "refresh_token": data["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=10)
        if resp.status_code == 200:
            td = resp.json()
            new_token = td.get("access_token")
            expires_in = td.get("expires_in", 3600)
            _gmail_token_db_set(new_token, data["refresh_token"])
            creds = Credentials(
                token=new_token,
                refresh_token=data["refresh_token"],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=GMAIL_CLIENT_ID,
                client_secret=GMAIL_CLIENT_SECRET,
                scopes=GMAIL_SCOPES,
            )
            _gmail_creds_cache["creds"] = creds
            _gmail_creds_cache["expires_at"] = now + expires_in
            return creds
        elif resp.status_code in (400, 401):
            # Refresh token ugyldig/revokert — slett fra DB
            _gmail_creds_cache["creds"] = None
            _gmail_creds_cache["expires_at"] = 0.0
            return None
    except Exception:
        pass
    # Fallback: bruk eksisterende token fra DB uten refresh
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=GMAIL_SCOPES,
    )

@app.get("/api/gmail/status")
def gmail_status():
    if not GMAIL_CLIENT_ID or not GMAIL_CLIENT_SECRET:
        return {"connected": False, "reason": "no_credentials"}
    db_data = _gmail_token_db_get()
    if not db_data or not db_data.get("refresh_token"):
        return {"connected": False, "reason": "not_authorized"}
    creds = get_gmail_creds()
    if creds and creds.refresh_token:
        return {"connected": True, "email": "strategioai@strategioai.com"}
    return {"connected": False, "reason": "token_expired"}

@app.get("/api/stripe/status")
def stripe_status():
    if not STRIPE_SECRET_KEY:
        return {"connected": False, "reason": "no_key"}
    try:
        # Use Customer.list instead of Account.retrieve — works with restricted keys too
        stripe.Customer.list(limit=1)
        return {"connected": True, "account_id": "", "email": ""}
    except stripe.error.AuthenticationError:
        return {"connected": False, "reason": "invalid_key"}
    except Exception as e:
        return {"connected": False, "reason": str(e)[:80]}

@app.get("/api/gmail/auth")
def gmail_auth():
    if not GMAIL_CLIENT_ID or not GMAIL_CLIENT_SECRET:
        raise HTTPException(400, "GMAIL_CLIENT_ID og GMAIL_CLIENT_SECRET mangler i .env")
    import urllib.parse as _urlparse, secrets as _sec
    params = {
        "client_id": GMAIL_CLIENT_ID,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": _sec.token_urlsafe(16),
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + _urlparse.urlencode(params)
    return {"url": auth_url}

@app.get("/api/gmail/callback")
def gmail_callback(code: str, state: str = None, iss: str = None, scope: str = None):
    resp = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    token_data = resp.json()
    _gmail_token_db_set(token_data.get("access_token"), token_data.get("refresh_token"))
    # Nullstill cache så neste kall refresher
    _gmail_creds_cache["creds"] = None
    _gmail_creds_cache["expires_at"] = 0.0
    return RedirectResponse("/?gmail=connected")

@app.get("/api/gmail/threads")
def gmail_threads(email: str):
    creds = get_gmail_creds()
    if not creds:
        raise HTTPException(401, "Gmail ikke koblet til")
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
    contact_id: str = ""
    image_filename: str = None

@app.post("/api/gmail/send")
async def gmail_send(data: GmailSend):
    creds = get_gmail_creds()
    if not creds:
        raise HTTPException(400, "Gmail ikke koblet til")
    service = build("gmail", "v1", credentials=creds)

    logo_path = os.path.join(os.path.dirname(__file__), "static", "strategio-logo.jpg")
    logo_data = open(logo_path, "rb").read() if os.path.exists(logo_path) else None

    escaped = data.body.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    html_body = f"<pre style='font-family:Arial,sans-serif;font-size:14px;white-space:pre-wrap'>{escaped}</pre>"

    msg = MIMEMultipart("related")
    msg["To"] = data.to
    msg["Subject"] = data.subject
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(data.body, "plain", "utf-8"))

    # Legg til innholdsbilde (f.eks. markeds-banner) om det finnes
    if data.image_filename:
        img_path = os.path.join(os.path.dirname(__file__), "static", os.path.basename(data.image_filename))
        if os.path.exists(img_path):
            with open(img_path, "rb") as f:
                img_data = f.read()
            html_body += '<br><img src="cid:content_img" style="max-width:600px;width:100%;border-radius:8px">'
            content_img = MIMEImage(img_data)
            content_img.add_header("Content-ID", "<content_img>")
            content_img.add_header("Content-Disposition", "inline")
        else:
            img_data = None
    else:
        img_data = None

    # Logo alltid nederst
    if logo_data:
        html_body += (
            '<div style="margin-top:32px;padding-top:20px;border-top:1px solid #eee;text-align:center">'
            '<img src="cid:strategio_logo" style="width:140px;height:auto">'
            '</div>'
        )

    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    if img_data:
        msg.attach(content_img)

    if logo_data:
        logo_mime = MIMEImage(logo_data)
        logo_mime.add_header("Content-ID", "<strategio_logo>")
        logo_mime.add_header("Content-Disposition", "inline")
        msg.attach(logo_mime)

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


# --- Email Templates ---

@app.get("/api/email-templates")
def get_email_templates(category: str = None):
    with get_conn() as conn:
        cur = conn.cursor()
        if category:
            cur.execute("SELECT * FROM email_templates WHERE category=%s ORDER BY created_at ASC", (category,))
        else:
            cur.execute("SELECT * FROM email_templates ORDER BY created_at ASC")
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.post("/api/email-templates")
def create_email_template(t: EmailTemplate):
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO email_templates (id, name, subject, body, category, image_filename, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (new_id, t.name, t.subject, t.body, t.category, t.image_filename, now)
        )
        row = cur.fetchone()
    return dict(row)

@app.put("/api/email-templates/{id}")
def update_email_template(id: str, t: EmailTemplate):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE email_templates SET name=%s, subject=%s, body=%s, category=%s, image_filename=%s WHERE id=%s RETURNING *",
            (t.name, t.subject, t.body, t.category, t.image_filename, id)
        )
        row = cur.fetchone()
    return dict(row)

@app.delete("/api/email-templates/{id}")
def delete_email_template(id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM email_templates WHERE id=%s", (id,))
    return {"ok": True}


@app.post("/api/upload-image")
async def upload_image(file: UploadFile = File(...)):
    import imghdr
    data = await file.read()
    ext = os.path.splitext(file.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        ext = ".png"
    filename = str(uuid.uuid4()) + ext
    path = os.path.join(os.path.dirname(__file__), "static", filename)
    with open(path, "wb") as f:
        f.write(data)
    return {"filename": filename}


# --- Init ---
init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
