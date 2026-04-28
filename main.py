from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
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
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager, asynccontextmanager
import threading
import time
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv()

STRIPE_SYNC_INTERVAL = int(os.getenv("STRIPE_SYNC_INTERVAL", "3600"))  # sekunder, default 1 time
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
LEAD_SCRAPE_HOUR = int(os.getenv("LEAD_SCRAPE_HOUR", "6"))  # kjør kl 06:00 UTC (08:00 norsk tid)

# --- Lead scraping konfigurasjon ---

# Bransjer som trenger AI-resepsjonist (mye telefon/booking)
AI_RESEPSJONIST_BRANSJER = [
    "restaurant", "tannlege", "frisør", "treningssenter", "hotell",
    "klinikk", "fysioterapeut", "veterinær", "bilverksted", "eiendomsmegler",
    "advokatkontor", "regnskapsfører", "hudpleie", "massasje", "spa",
    "pizzeria", "kebab", "café", "bakeri", "blomsterbutikk",
    "rørlegger", "elektriker", "rengjøring", "vaktmester", "flyttebyrå",
]

# Norske byer å rotere gjennom
NORSKE_BYER = [
    "Oslo", "Bergen", "Trondheim", "Stavanger", "Drammen",
    "Kristiansand", "Tromsø", "Fredrikstad", "Sandnes", "Sarpsborg",
    "Bodø", "Ålesund", "Tønsberg", "Haugesund", "Sandefjord",
    "Moss", "Arendal", "Hamar", "Larvik", "Halden",
    "Molde", "Harstad", "Lillehammer", "Gjøvik", "Kongsberg",
    "Ski", "Asker", "Lørenskog", "Jessheim", "Lillestrøm",
]

def _lead_score(biz: dict) -> int:
    """Scorer en lead 0-100 basert på potensial."""
    score = 30  # base
    # Ingen nettside = trenger hjelp
    if not biz.get("website"):
        score += 25
    # Har telefon = kontaktbar
    if biz.get("phone"):
        score += 10
    # Lav rating = trenger forbedring
    rating = biz.get("rating", 0)
    if rating and rating < 3.5:
        score += 10
    elif rating and rating >= 4.5:
        score -= 5
    # Mange reviews = etablert bedrift med budsjett
    reviews = biz.get("user_ratings_total", 0)
    if reviews and reviews > 50:
        score += 10
    elif reviews and reviews > 20:
        score += 5
    # AI-resepsjonist bransje = høy verdi
    if biz.get("category") == "ai_resepsjonist":
        score += 15
    return min(100, max(0, score))

def _suggest_strategy(biz: dict) -> str:
    """Foreslår salgsstrategi basert på lead-data."""
    strategies = []
    if not biz.get("website"):
        strategies.append("Tilby nettside + AI-chatbot pakke")
    if biz.get("category") == "ai_resepsjonist":
        strategies.append(f"Pitch AI-resepsjonist — {biz.get('industry','')} har mye telefon/booking")
    if biz.get("rating") and biz["rating"] < 3.5:
        strategies.append("Vis hvordan AI kundeservice kan forbedre ratings")
    reviews = biz.get("user_ratings_total", 0)
    if reviews and reviews > 50:
        strategies.append("Etablert bedrift — fokuser på effektivisering og kostnadsbesparelse")
    if not strategies:
        strategies.append("Generell AI-tjeneste pitch — automatisering og vekst")
    return " | ".join(strategies)

def _categorize_lead(biz: dict, search_industry: str) -> str:
    """Kategoriserer lead i en av tre kategorier."""
    if not biz.get("website"):
        return "trenger_nettside"
    if search_industry.lower() in [b.lower() for b in AI_RESEPSJONIST_BRANSJER]:
        return "ai_resepsjonist"
    return "generell_tjeneste"

def _scrape_places(industry: str, city: str) -> list:
    """Søker Google Places API for bedrifter i en bransje+by."""
    if not GOOGLE_PLACES_API_KEY:
        return []
    import requests
    results = []
    query = f"{industry} i {city}"
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": query,
        "key": GOOGLE_PLACES_API_KEY,
        "language": "no",
        "region": "no",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        for place in data.get("results", []):
            place_id = place.get("place_id", "")
            # Hent detaljer for telefon og nettside
            detail = {}
            if place_id:
                detail_url = "https://maps.googleapis.com/maps/api/place/details/json"
                detail_params = {
                    "place_id": place_id,
                    "fields": "formatted_phone_number,website,url",
                    "key": GOOGLE_PLACES_API_KEY,
                    "language": "no",
                }
                try:
                    dr = requests.get(detail_url, params=detail_params, timeout=10)
                    detail = dr.json().get("result", {})
                except Exception:
                    pass

            biz = {
                "name": place.get("name", ""),
                "address": place.get("formatted_address", ""),
                "phone": detail.get("formatted_phone_number", ""),
                "website": detail.get("website", ""),
                "rating": place.get("rating", 0),
                "user_ratings_total": place.get("user_ratings_total", 0),
                "maps_url": detail.get("url", ""),
                "place_id": place_id,
                "industry": industry,
                "city": city,
            }
            biz["category"] = _categorize_lead(biz, industry)
            biz["score"] = _lead_score(biz)
            biz["strategy"] = _suggest_strategy(biz)
            results.append(biz)
    except Exception as e:
        print(f"[Lead scraper] Feil for {query}: {e}")
    return results

def _import_leads_to_crm(leads: list) -> dict:
    """Importerer leads til CRM-databasen. Returnerer statistikk."""
    imported = 0
    skipped = 0
    with get_conn() as conn:
        cur = conn.cursor()
        # Hent eksisterende for deduplisering
        cur.execute("SELECT name, phone FROM contacts")
        existing = set()
        for r in cur.fetchall():
            existing.add((r["name"] or "").lower())
            if r["phone"]:
                existing.add(r["phone"].replace(" ", ""))
        for lead in leads:
            name_key = lead["name"].lower()
            phone_key = (lead.get("phone") or "").replace(" ", "")
            if name_key in existing or (phone_key and phone_key in existing):
                skipped += 1
                continue
            new_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            notes = f"Score: {lead['score']}/100 | {lead['strategy']}"
            if lead.get("maps_url"):
                notes += f" | Maps: {lead['maps_url']}"
            if lead.get("rating"):
                notes += f" | Rating: {lead['rating']} ({lead.get('user_ratings_total',0)} reviews)"
            cur.execute(
                "INSERT INTO contacts (id, name, company, email, phone, source, status, category, notes, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (new_id, lead["name"], lead.get("industry",""), None, lead.get("phone"),
                 "google_places", "lead", lead["category"], notes, now, now)
            )
            existing.add(name_key)
            if phone_key:
                existing.add(phone_key)
            imported += 1
    return {"imported": imported, "skipped": skipped, "total": len(leads)}

def _daily_lead_scrape():
    """Bakgrunnstråd som scraper leads daglig kl LEAD_SCRAPE_HOUR UTC."""
    import random
    last_scrape_date = None
    while True:
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        if now.hour >= LEAD_SCRAPE_HOUR and last_scrape_date != today:
            last_scrape_date = today
            print(f"[Lead scraper] Starter daglig scraping {today}...")
            # Velg 3 tilfeldige bransjer og 3 tilfeldige byer
            industries = random.sample(AI_RESEPSJONIST_BRANSJER, min(5, len(AI_RESEPSJONIST_BRANSJER)))
            cities = random.sample(NORSKE_BYER, min(5, len(NORSKE_BYER)))
            all_leads = []
            report_lines = []
            for industry in industries:
                for city in cities:
                    leads = _scrape_places(industry, city)
                    all_leads.extend(leads)
                    print(f"  [Lead scraper] {industry} i {city}: {len(leads)} leads")
                    time.sleep(2)  # rate limit
            if all_leads:
                stats = _import_leads_to_crm(all_leads)
                # Kategoriser
                by_cat = {"ai_resepsjonist": [], "trenger_nettside": [], "generell_tjeneste": []}
                for l in all_leads:
                    by_cat.get(l["category"], by_cat["generell_tjeneste"]).append(l)
                # Topp 5 leads
                top5 = sorted(all_leads, key=lambda x: x["score"], reverse=True)[:5]
                # Lagre rapport til database
                report_data = {
                    "date": today,
                    "searches": [{"industry": i, "city": c} for i in industries for c in cities],
                    "stats": stats,
                    "by_category": {k: len(v) for k, v in by_cat.items()},
                    "top_leads": [{"name": l["name"], "score": l["score"], "category": l["category"],
                                   "phone": l.get("phone",""), "city": l["city"],
                                   "industry": l["industry"], "strategy": l["strategy"],
                                   "website": l.get("website",""), "rating": l.get("rating",0)} for l in top5],
                }
                try:
                    with get_conn() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            "INSERT INTO lead_reports (id, report_date, data, created_at) VALUES (%s,%s,%s,%s)",
                            (str(uuid.uuid4()), today, json.dumps(report_data, ensure_ascii=False),
                             datetime.now(timezone.utc).isoformat())
                        )
                except Exception as e:
                    print(f"[Lead scraper] Feil ved lagring av rapport: {e}")
                # Telegram-rapport
                msg = f"🔍 <b>Daglig Lead-rapport — {today}</b>\n\n"
                msg += f"📊 Søkte: {', '.join(industries)} i {', '.join(cities)}\n"
                msg += f"📥 Totalt funnet: <b>{stats['total']}</b>\n"
                msg += f"✅ Nye importert: <b>{stats['imported']}</b>\n"
                msg += f"⏭ Duplikater hoppet over: {stats['skipped']}\n\n"
                msg += f"📂 <b>Kategorier:</b>\n"
                msg += f"  🤖 AI-resepsjonist: {by_cat.get('ai_resepsjonist', []).__len__()}\n"
                msg += f"  🌐 Trenger nettside: {by_cat.get('trenger_nettside', []).__len__()}\n"
                msg += f"  💼 Generell: {by_cat.get('generell_tjeneste', []).__len__()}\n\n"
                if top5:
                    msg += "🏆 <b>Topp 5 leads å satse på:</b>\n"
                    for i, l in enumerate(top5, 1):
                        msg += f"\n<b>{i}. {l['name']}</b> ({l['city']})\n"
                        msg += f"   Score: {l['score']}/100 | {l['category']}\n"
                        if l.get("phone"):
                            msg += f"   📞 {l['phone']}\n"
                        msg += f"   💡 {l['strategy']}\n"
                try:
                    import requests as _req
                    if TELEGRAM_TOKEN:
                        _req.post(
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                            timeout=10,
                        )
                except Exception as e:
                    print(f"[Lead scraper] Telegram-feil: {e}")
                print(f"[Lead scraper] Ferdig: {stats['imported']} importert, {stats['skipped']} duplikater")
            else:
                print("[Lead scraper] Ingen leads funnet i dag")
        time.sleep(300)  # sjekk hvert 5. minutt

def _stripe_auto_sync():
    """Bakgrunnstråd som synkroniserer Stripe-kunder til CRM hver time."""
    while True:
        time.sleep(STRIPE_SYNC_INTERVAL)
        if not STRIPE_SECRET_KEY:
            continue
        try:
            stripe.api_key = STRIPE_SECRET_KEY
            customers = stripe.Customer.list(limit=100)
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT email FROM contacts")
                crm_emails = {(r["email"] or "").lower() for r in cur.fetchall()}
                imported = 0
                for c in customers.auto_paging_iter():
                    email = (c.email or "").lower()
                    if email and email in crm_emails:
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
                if imported > 0:
                    print(f"[Stripe auto-sync] Importerte {imported} nye kunder")
        except Exception as e:
            print(f"[Stripe auto-sync] Feil: {e}")

DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "7"))  # UTC, 07:00 = 09:00 norsk tid

def _daily_health_and_report():
    """Bakgrunnstråd: daglig helsesjekk + CRM-rapport til Telegram kl DAILY_REPORT_HOUR UTC."""
    import requests as _req
    last_report_date = None
    while True:
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        if now.hour >= DAILY_REPORT_HOUR and last_report_date != today:
            last_report_date = today
            print(f"[Daglig sjekk] Starter {today}...")
            errors = []
            # 1. DB-sjekk
            try:
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) AS n FROM contacts")
                    total = cur.fetchone()["n"]
                    cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE created_at::timestamptz >= now() - interval '24 hours'")
                    new_24h = cur.fetchone()["n"]
                    cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE created_at::timestamptz >= now() - interval '7 days'")
                    new_week = cur.fetchone()["n"]
                    cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE followup_date IS NOT NULL AND followup_date::text <= %s AND status NOT IN ('vunnet','tapt')", (today,))
                    overdue = cur.fetchone()["n"]
                    cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE status='kunde'")
                    customers = cur.fetchone()["n"]
                    cur.execute("SELECT COUNT(*) AS n FROM deals WHERE status NOT IN ('vunnet','tapt')")
                    active_deals = cur.fetchone()["n"]
                    cur.execute("SELECT COALESCE(SUM(value),0) AS v FROM deals WHERE status NOT IN ('vunnet','tapt')")
                    pipeline_value = cur.fetchone()["v"]
                    # Sjekk lead-scraper status
                    cur.execute("SELECT report_date, data FROM lead_reports ORDER BY report_date DESC LIMIT 1")
                    last_report = cur.fetchone()
            except Exception as e:
                errors.append(f"DB: {e}")
                print(f"[Daglig sjekk] DB-feil: {e}")
                total = new_24h = new_week = overdue = customers = active_deals = 0
                pipeline_value = 0
                last_report = None

            # 2. Stripe MRR-sjekk
            mrr = 0.0
            if STRIPE_SECRET_KEY:
                try:
                    subs = stripe.Subscription.list(status="active", limit=100)
                    for s in subs.auto_paging_iter():
                        amount, interval, _, _pe = _stripe_sub_info(s)
                        mrr += amount / 12 if interval == "year" else amount
                except Exception as e:
                    errors.append(f"Stripe: {e}")

            # 3. Bygg rapport
            status_icon = "🔴" if errors else "🟢"
            msg = f"{status_icon} <b>Daglig CRM-sjekk — {today}</b>\n\n"
            msg += f"👥 Totale leads: <b>{total}</b>\n"
            msg += f"🆕 Nye siste 24t: <b>{new_24h}</b>\n"
            msg += f"📅 Nye siste 7d: <b>{new_week}</b>\n"
            msg += f"🤝 Kunder: <b>{customers}</b>\n"
            msg += f"📊 Aktive deals: <b>{active_deals}</b> ({pipeline_value:,.0f} kr)\n"
            msg += f"💰 MRR: <b>{mrr:,.0f} kr</b>\n"
            if overdue > 0:
                msg += f"⏰ <b>Forfalt oppfølging: {overdue}</b>\n"
            # Lead-scraper status
            if last_report:
                lr_date = last_report["report_date"]
                lr_data = json.loads(last_report["data"]) if isinstance(last_report["data"], str) else last_report["data"]
                lr_stats = lr_data.get("stats", {})
                msg += f"\n🔍 Siste lead-scrape: {lr_date}\n"
                msg += f"   Importert: {lr_stats.get('imported', '?')} | Duplikater: {lr_stats.get('skipped', '?')}\n"
                if str(lr_date) < (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat():
                    msg += f"   ⚠️ <b>Lead-scraper har ikke kjørt på 2+ dager!</b>\n"
                    if not GOOGLE_PLACES_API_KEY:
                        msg += f"   → GOOGLE_PLACES_API_KEY mangler\n"
            elif not GOOGLE_PLACES_API_KEY:
                msg += f"\n⚠️ Lead-scraper: <b>DEAKTIVERT</b> (mangler GOOGLE_PLACES_API_KEY)\n"
            else:
                msg += f"\n⚠️ Ingen lead-rapporter funnet\n"

            if errors:
                msg += f"\n🚨 <b>Feil oppdaget:</b>\n"
                for e in errors:
                    msg += f"  • {e}\n"

            msg += f"\n🔗 <a href='https://crm.strategio.site'>Åpne CRM</a>"

            # 4. Send til Telegram
            try:
                if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                    _req.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
                        timeout=10,
                    )
                    print(f"[Daglig sjekk] Rapport sendt til Telegram")
                else:
                    print(f"[Daglig sjekk] Telegram ikke konfigurert (token={bool(TELEGRAM_TOKEN)}, chat_id={bool(TELEGRAM_CHAT_ID)})")
            except Exception as e:
                print(f"[Daglig sjekk] Telegram-feil: {e}")

        time.sleep(300)  # sjekk hvert 5. minutt


# --- Outreach email-kampanje ---
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
OUTREACH_HOUR = int(os.getenv("OUTREACH_HOUR", "7"))  # UTC, 07:00 = 09:00 norsk tid
def _load_outreach_start_date():
    """Last startdato fra DB eller env, så opptrapping overlever redeploy."""
    env_date = os.getenv("OUTREACH_START_DATE", "")
    if env_date:
        return env_date
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM app_settings WHERE key = 'outreach_start_date'")
            row = cur.fetchone()
            if row:
                return row["value"]
    except Exception:
        pass
    return ""

OUTREACH_START_DATE = _load_outreach_start_date()

def _get_outreach_limit():
    """Automatisk opptrapping: uke1=5, uke2=10, uke3=20, uke4+=30"""
    start = OUTREACH_START_DATE
    if not start:
        return 5
    try:
        start_date = datetime.fromisoformat(start).date()
        days_active = (datetime.now(timezone.utc).date() - start_date).days
        if days_active < 7:
            return 5
        elif days_active < 14:
            return 10
        elif days_active < 21:
            return 20
        else:
            return 30
    except Exception:
        return 5

def _daily_outreach():
    """Bakgrunnstråd: sender outreach-emails til leads uten nettside, man-fre kl OUTREACH_HOUR UTC."""
    global OUTREACH_START_DATE
    import requests as _req
    last_outreach_date = None
    while True:
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        weekday = now.weekday()  # 0=mandag, 6=søndag
        if now.hour >= OUTREACH_HOUR and last_outreach_date != today and weekday < 5:
            # Sett startdato ved første kjøring
            if not OUTREACH_START_DATE:
                OUTREACH_START_DATE = today
                try:
                    with get_conn() as conn:
                        cur = conn.cursor()
                        cur.execute("INSERT INTO app_settings (key, value) VALUES ('outreach_start_date', %s) ON CONFLICT (key) DO UPDATE SET value = %s", (today, today))
                        conn.commit()
                except Exception:
                    pass
            daily_limit = _get_outreach_limit()
            last_outreach_date = today
            print(f"[Outreach] Starter daglig utsending {today} (limit: {daily_limit})...")
            sent_count = 0
            skipped_count = 0
            errors = []

            try:
                # 1. Hent leads med email, status=ny
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT id, name, company, email, city, website
                        FROM contacts
                        WHERE status = 'ny'
                          AND email IS NOT NULL AND email != ''
                          AND email NOT LIKE '%%,%%'
                          AND company IS NOT NULL AND company != ''
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (daily_limit * 3,))  # hent 3x for å ha buffer etter Google-sjekk
                    candidates = [dict(r) for r in cur.fetchall()]

                print(f"[Outreach] {len(candidates)} kandidater funnet")

                verified = []
                for lead in candidates:
                    if len(verified) >= daily_limit:
                        break

                    company = lead.get("company", "")
                    city = lead.get("city", "")

                    # 2. Google-søk for å verifisere at de ikke har nettside
                    try:
                        search_query = f"{company} {city}".strip()
                        resp = _req.get(
                            f"https://www.google.com/search?q={search_query}&num=5",
                            headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                "Accept-Language": "no,nb,nn;q=0.9"
                            },
                            timeout=10
                        )
                        html = resp.text.lower()
                        company_clean = company.lower().replace(" ", "").replace("-", "")

                        has_website = (
                            f"{company_clean}.no" in html or
                            f"{company_clean}.com" in html or
                            f"www.{company_clean}" in html
                        )

                        if has_website:
                            skipped_count += 1
                            print(f"[Outreach] SKIP {company} — har nettside")
                            # Marker i CRM at de har nettside
                            with get_conn() as conn:
                                cur = conn.cursor()
                                cur.execute("UPDATE contacts SET notes = COALESCE(notes,'') || %s WHERE id = %s",
                                           (f" | Har nettside (sjekket {today})", lead["id"]))
                                conn.commit()
                            continue

                        verified.append(lead)
                        time.sleep(2)  # vær snill mot Google

                    except Exception as e:
                        print(f"[Outreach] Google-sjekk feilet for {company}: {e}")
                        verified.append(lead)  # send likevel ved feil

                print(f"[Outreach] {len(verified)} verifiserte leads (uten nettside)")

                # 3. Send emails via Resend
                for lead in verified:
                    company = lead.get("company", lead.get("name", "din bedrift"))
                    email = lead["email"]

                    email_html = f"""<html><head><meta charset="utf-8"></head><body><div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.7;color:#222;max-width:600px">
<p>Hei du,</p>
<p>Vi fant ikke nettsiden til {company} online.</p>
<p>Denne uken hjelper vi 5 bedrifter med en SEO-optimalisert nettside for kun <strong>890,- /mnd</strong> — med <strong>EGET ADMIN-PANEL</strong> som er enkelt å bruke, der du kan oppdatere innhold, bilder og tekst selv. Du får se hjemmesiden din før du bestemmer deg — helt uforpliktende.</p>
<p>Vi integrerer sosiale medier, legger til bookingsystem på nettsiden, og kan hjelpe med markedsføring om du ønsker det.</p>
<p>Svar her så sender jeg et forslag.</p>
<p>Remy<br>StrategioAI<br><a href="https://www.strategioai.com" style="color:#1a7f4b">www.strategioai.com</a></p>
<hr style="border:none;border-top:1px solid #ddd;margin:20px 0">
<p style="font-size:11px;color:#999">Ønsker du ikke flere meldinger? Svar "stopp" så fjerner vi deg umiddelbart.</p>
</div></body></html>"""

                    try:
                        resp = _req.post(
                            "https://api.resend.com/emails",
                            headers={
                                "Authorization": f"Bearer {RESEND_API_KEY}",
                                "Content-Type": "application/json"
                            },
                            json={
                                "from": "Remy <remy@strategio.site>",
                                "to": [email],
                                "bcc": ["strategioai@strategioai.com"],
                                "subject": f"Nettside for {company}?",
                                "html": email_html,
                                "reply_to": "strategioai@strategioai.com",
                                "headers": {"X-Contact-Id": lead["id"]}
                            },
                            timeout=10
                        )

                        if resp.status_code in (200, 201):
                            sent_count += 1
                            resend_data = resp.json()
                            resend_id = resend_data.get("id", "")
                            # Oppdater lead status + lagre resend email ID
                            with get_conn() as conn:
                                cur = conn.cursor()
                                cur.execute("UPDATE contacts SET status='kontaktet', notes=COALESCE(notes,'') || %s WHERE id=%s",
                                           (f" | Outreach sendt {today} (id:{resend_id})", lead["id"]))
                                # Lagre email-tracking i egen tabell
                                cur.execute("""
                                    INSERT INTO outreach_emails (id, contact_id, email, company, resend_id, sent_at, status)
                                    VALUES (%s, %s, %s, %s, %s, %s, 'sent')
                                    ON CONFLICT DO NOTHING
                                """, (str(uuid.uuid4()), lead["id"], email, company, resend_id, datetime.now(timezone.utc).isoformat()))
                                conn.commit()
                            print(f"[Outreach] SENDT → {company} ({email}) [resend:{resend_id}]")
                        else:
                            errors.append(f"{company}: Resend {resp.status_code} - {resp.text[:100]}")
                            print(f"[Outreach] FEIL → {company}: {resp.status_code}")

                        time.sleep(1)  # rate limit

                    except Exception as e:
                        errors.append(f"{company}: {e}")
                        print(f"[Outreach] Send-feil → {company}: {e}")

            except Exception as e:
                errors.append(f"DB/generell: {e}")
                print(f"[Outreach] Generell feil: {e}")

            # 4. Rapport til Telegram
            try:
                if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                    msg = f"📧 <b>Outreach-rapport — {today}</b>\n\n"
                    msg += f"✅ Sendt: <b>{sent_count}</b>\n"
                    msg += f"⏭ Hoppet over (har nettside): <b>{skipped_count}</b>\n"
                    if errors:
                        msg += f"\n🚨 Feil ({len(errors)}):\n"
                        for e in errors[:5]:
                            msg += f"  • {e}\n"
                    _req.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                        timeout=10
                    )
            except Exception as e:
                print(f"[Outreach] Telegram-feil: {e}")

            print(f"[Outreach] Ferdig — sendt {sent_count}, hoppet over {skipped_count}")

        time.sleep(300)  # sjekk hvert 5. minutt


@asynccontextmanager
async def lifespan(app):
    t = threading.Thread(target=_stripe_auto_sync, daemon=True)
    t.start()
    print(f"[Stripe auto-sync] Startet — synkroniserer hver {STRIPE_SYNC_INTERVAL}s")
    if GOOGLE_PLACES_API_KEY:
        t2 = threading.Thread(target=_daily_lead_scrape, daemon=True)
        t2.start()
        print(f"[Lead scraper] Startet — kjører daglig kl {LEAD_SCRAPE_HOUR}:00 UTC")
    t3 = threading.Thread(target=_daily_health_and_report, daemon=True)
    t3.start()
    print(f"[Daglig sjekk] Startet — kjører kl {DAILY_REPORT_HOUR}:00 UTC daglig")
    if RESEND_API_KEY:
        t4 = threading.Thread(target=_daily_outreach, daemon=True)
        t4.start()
        print(f"[Outreach] Startet — auto-opptrapping (5→10→20→30) kl {OUTREACH_HOUR}:00 UTC (man-fre)")
    else:
        print("[Outreach] DEAKTIVERT — mangler RESEND_API_KEY")
    yield

app = FastAPI(lifespan=lifespan)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
TWILIO_TWIML_APP_SID = os.getenv("TWILIO_TWIML_APP_SID", "")
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
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='contacts' AND column_name='followup_date') THEN
                    ALTER TABLE contacts ADD COLUMN followup_date TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='contacts' AND column_name='website') THEN
                    ALTER TABLE contacts ADD COLUMN website TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='contacts' AND column_name='org_nr') THEN
                    ALTER TABLE contacts ADD COLUMN org_nr TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='contacts' AND column_name='address') THEN
                    ALTER TABLE contacts ADD COLUMN address TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='contacts' AND column_name='city') THEN
                    ALTER TABLE contacts ADD COLUMN city TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='contacts' AND column_name='interest_level') THEN
                    ALTER TABLE contacts ADD COLUMN interest_level TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='contacts' AND column_name='service_interest') THEN
                    ALTER TABLE contacts ADD COLUMN service_interest TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='contacts' AND column_name='call_queue') THEN
                    ALTER TABLE contacts ADD COLUMN call_queue BOOLEAN DEFAULT false;
                END IF;
            END$$;
        """)
        # Lead-rapporter tabell
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lead_reports (
                id TEXT PRIMARY KEY,
                report_date TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT DEFAULT (now() AT TIME ZONE 'utc')
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                price REAL DEFAULT 0,
                price_type TEXT DEFAULT 'engang',
                category TEXT DEFAULT 'tjeneste',
                icon TEXT DEFAULT '📦',
                features TEXT,
                is_active BOOLEAN DEFAULT true,
                created_at TEXT DEFAULT (now() AT TIME ZONE 'utc'),
                updated_at TEXT DEFAULT (now() AT TIME ZONE 'utc')
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outreach_emails (
                id TEXT PRIMARY KEY,
                contact_id TEXT,
                email TEXT,
                company TEXT,
                resend_id TEXT,
                sent_at TEXT,
                status TEXT DEFAULT 'sent',
                opened_at TEXT,
                clicked_at TEXT,
                bounced_at TEXT
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_outreach_resend_id ON outreach_emails(resend_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_outreach_contact_id ON outreach_emails(contact_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_category ON contacts(category);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_city ON contacts(city);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_org_nr ON contacts(org_nr);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);")
        # Pitches tabell
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pitches (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                sections JSONB NOT NULL DEFAULT '[]',
                created_at TEXT DEFAULT (now() AT TIME ZONE 'utc'),
                updated_at TEXT DEFAULT (now() AT TIME ZONE 'utc')
            );
        """)
        # Pitch argumenter/innvendinger tabell
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pitch_arguments (
                id TEXT PRIMARY KEY,
                pitch_id TEXT REFERENCES pitches(id) ON DELETE CASCADE,
                objection TEXT NOT NULL,
                response TEXT NOT NULL,
                category TEXT DEFAULT 'generell',
                created_at TEXT DEFAULT (now() AT TIME ZONE 'utc')
            );
        """)
        # Seed standard-pitches hvis tom
        cur.execute("SELECT COUNT(*) as cnt FROM pitches")
        if cur.fetchone()["cnt"] == 0:
            cold_call_id = str(uuid.uuid4())
            ferdig_web_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO pitches (id, name, description, sections) VALUES (%s,%s,%s,%s)",
                (cold_call_id, "Cold Call Pitch", "Nettside-salg · Make the Winning Move", json.dumps([
                    {"title": "Åpning", "body": "Hei, prater jeg med …? Remy fra Strategio her, jeg gjorde litt research på tjenestene dine online. Og da fant jeg noe informasjon, men ingen offisiell nettside for …", "style": "quote"},
                    {"title": "Det jeg tilbyr", "body": "Det jeg gjør er å bygge nettsider som er laget for å rangere på Google. For dere ville det betydd én side som er optimalisert for de søkene som faktisk gir kunder. Målet er at når noen googler de tingene, så ser de dere, ikke konkurrentene.", "style": "quote"},
                    {"title": "Egen admin – full kontroll", "body": "Det du også får i tillegg til den egen nettside er din egen admin slik at du kan endre og redigere nettsiden din selv så mye du vil i ettertid. Da slipper du ekstra kostnader gjennom et byrå som tar timesbetalt for sånt.", "style": "text"},
                    {"title": "Prising", "body": "1990 NOK for å sette opp nettsiden, 890 NOK i måneden som dekker hosting, drift og at jeg løpende jobber med å forbedre rangeringen. Ingen skjulte kostnader, ingen bindingstid utover 12 måneder.", "style": "pricing"},
                    {"title": "Oppsummering før close", "body": "Da eier du din egen nettside som du kan endre og redigere som du vil, med egen admin, med mulighet for support fra StrategioAI om du skulle trenge.", "style": "text"},
                    {"title": "Close", "body": "Får jeg med deg på den?\nSkal vi gå for den?", "style": "quote"}
                ]))
            )
            cur.execute(
                "INSERT INTO pitches (id, name, description, sections) VALUES (%s,%s,%s,%s)",
                (ferdig_web_id, "Ferdig Webside Pitch", "SMS / Chat / DM · Kort og direkte", json.dumps([
                    {"title": "Åpning", "body": "Hei, Remy fra Strategio her. Jeg prøvde å finne dere på nett, men fant ikke websiden. Har du 30 sekunder å låne bort?", "style": "quote"},
                    {"title": "Hook – Ferdig webside", "body": "Jeg har faktisk allerede laget en ferdig webside til dere som du kan ta en titt på. Hvis du liker den får du den til en god pris. Hvis ikke – ingen forpliktelser, du har ikke tapt noe som helst.", "style": "quote"},
                    {"title": "Close – Møtebooking", "body": "Har du tid til et kort Teams-møte i løpet av dagen?", "style": "quote"}
                ]))
            )
            # Seed standard-argumenter
            default_args = [
                (cold_call_id, "Vi har ikke budsjett", "Jeg forstår. Men med 890 kr/mnd snakker vi under en lunsj i uka. Og nettsiden begynner å jobbe for deg fra dag én — den henter inn kunder mens du sover.", "pris"),
                (cold_call_id, "Vi bruker Facebook/Instagram", "Det er bra! Men sosiale medier viser kun innhold til de som allerede følger dere. En nettside fanger folk som aktivt søker etter tjenestene deres — det er en helt annen type kunde.", "konkurrent"),
                (cold_call_id, "Vi har nok kunder", "Flott å høre! Men hva om en nettside kunne gitt deg bedre kunder? De som googler og finner deg er ofte mer kjøpeklare enn de som kommer via anbefalinger.", "behov"),
                (cold_call_id, "Jeg må tenke på det", "Absolutt, ingen hastverk. Men kan jeg sende deg en link til en demo-side jeg har laget for dere? Da har du noe konkret å vurdere. Tar 10 sekunder å se på.", "utsettelse"),
                (cold_call_id, "Vi prøvde nettside før, fungerte ikke", "Det hører jeg ofte. Problemet er vanligvis at siden ikke var optimalisert for Google-søk. Min tilnærming er å bygge siden rundt de søkeordene kundene dine faktisk bruker.", "erfaring"),
                (ferdig_web_id, "Ikke interessert", "Helt greit! Men sjekk lenken først — den er allerede laget. Tar 10 sekunder. Ingen forpliktelser uansett.", "avvisning"),
                (ferdig_web_id, "Hva koster det?", "Nettsiden koster 1990,- i oppsett og 890,- i måneden for hosting og drift. Men ta en titt først — kanskje du liker den, kanskje ikke.", "pris"),
                (ferdig_web_id, "Send meg info på mail", "Selvfølgelig! Hva er e-postadressen? Jeg sender lenken til nettsiden med en gang.", "utsettelse"),
            ]
            for pitch_id, objection, response, cat in default_args:
                cur.execute(
                    "INSERT INTO pitch_arguments (id, pitch_id, objection, response, category) VALUES (%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), pitch_id, objection, response, cat)
                )
        # Seed standardprodukter
        cur.execute("SELECT COUNT(*) as cnt FROM products")
        if cur.fetchone()["cnt"] == 0:
            default_products = [
                (str(uuid.uuid4()), "AI Resepsjonist", "AI-drevet chat, telefon og WhatsApp-agent som svarer kunder 24/7, booker møter og fanger leads automatisk.", 4990, "mnd", "ai", "🤖", "24/7 tilgjengelighet|Chat + telefon + WhatsApp|Automatisk booking|Lead-fangst|Flerspråklig|Sentiment-analyse"),
                (str(uuid.uuid4()), "AI Resepsjonist Dashboard", "White-label kundeportal der bedriften ser samtaler, leads og statistikk i sanntid.", 1990, "mnd", "ai", "📊", "Sanntids samtalelogg|Lead-oversikt med status|Statistikk og grafer|White-label med eget logo|Webhook-integrasjon"),
                (str(uuid.uuid4()), "Ny Nettside", "Profesjonell AI-optimalisert nettside med innebygd chatbot, SEO og høy konverteringsrate.", 14990, "engang", "nettside", "🌐", "Responsivt design|Innebygd AI-chatbot|SEO-optimalisert|Rask lastetid|CMS for innhold|SSL-sertifikat"),
                (str(uuid.uuid4()), "Sosiale Medier", "Komplett pakke for automatisert innholdsproduksjon og publisering på sosiale medier.", 3990, "mnd", "sosiale_medier", "📱", "Auto-generert innhold|Planlagt publisering|Instagram + Facebook + LinkedIn|AI-genererte bilder|Engasjement-analyse|Månedlig rapport"),
                (str(uuid.uuid4()), "Lead Generering", "Automatisert lead-generering med pay-per-customer modell. Null risiko — betal kun for verifiserte kunder.", 0, "per_lead", "leads", "🎯", "Google Maps scraping|Automatisk lead scoring|Personalisert outreach|Verifiserte kontakter|Pay-per-customer|CRM-integrasjon"),
            ]
            for p in default_products:
                cur.execute(
                    "INSERT INTO products (id, name, description, price, price_type, category, icon, features) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    p
                )


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
    followup_date: str = None
    website: str = None
    org_nr: str = None
    address: str = None
    city: str = None
    interest_level: str = None  # hot, warm, cold
    service_interest: str = None  # comma-separated: ai_resepsjonist,nettside,lead_gen,automatisering,google_ads
    call_queue: bool = False

class ContactUpdate(BaseModel):
    name: str = None
    company: str = None
    email: str = None
    phone: str = None
    source: str = None
    status: str = None
    category: str = None
    notes: str = None
    followup_date: str = None
    website: str = None
    org_nr: str = None
    address: str = None
    city: str = None
    interest_level: str = None  # hot, warm, cold
    service_interest: str = None  # comma-separated: ai_resepsjonist,nettside,lead_gen,automatisering,google_ads
    call_queue: bool = None

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

class Pitch(BaseModel):
    name: str
    description: str = None
    sections: list = []

class PitchArgument(BaseModel):
    pitch_id: str
    objection: str
    response: str
    category: str = "generell"

class Product(BaseModel):
    name: str
    description: str = None
    price: float = 0
    price_type: str = "engang"
    category: str = "tjeneste"
    icon: str = "📦"
    features: str = None
    is_active: bool = True

class ProductUpdate(BaseModel):
    name: str = None
    description: str = None
    price: float = None
    price_type: str = None
    category: str = None
    icon: str = None
    features: str = None
    is_active: bool = None


# --- Frontend ---

@app.get("/")
def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


# --- Health ---

@app.get("/api/ping")
def health():
    return {"ok": True, "setup": False}


# --- Dashboard ---

def _get_outstanding():
    """Hent total utestående beløp fra åpne Stripe-fakturaer."""
    if not STRIPE_SECRET_KEY:
        return 0.0
    try:
        total = 0.0
        for inv in stripe.Invoice.list(status="open", limit=100).auto_paging_iter():
            total += (inv.amount_remaining or 0) / 100
        return total
    except Exception:
        return 0.0

@app.get("/api/stats")
def stats():
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, category, followup_date, call_queue FROM contacts")
            contacts = cur.fetchall()
            cur.execute("SELECT status, value, type, recurring_amount FROM deals")
            deals = cur.fetchall()
            cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE created_at::timestamptz >= now() - interval '7 days'")
            new_this_week = cur.fetchone()["n"]
    except Exception as e:
        print(f"[STATS] DB error: {e}")
        raise HTTPException(500, f"Database error: {e}")

    pipeline_value = sum(d["value"] or 0 for d in deals if d["status"] not in ["vunnet", "tapt"])
    won_value = sum(d["value"] or 0 for d in deals if d["status"] == "vunnet")

    # MRR fra Stripe hvis tilgjengelig, ellers fra deals
    stripe_mrr = 0.0
    if STRIPE_SECRET_KEY:
        try:
            subs = stripe.Subscription.list(status="active", limit=100)
            for s in subs.auto_paging_iter():
                amount, interval, _, _pe = _stripe_sub_info(s)
                stripe_mrr += amount / 12 if interval == "year" else amount
        except Exception as e:
            print(f"[STATS] Stripe MRR error (ignored): {e}")
    mrr = stripe_mrr if stripe_mrr > 0 else sum(
        d["recurring_amount"] or 0 for d in deals if d.get("type") == "subscription" and d["status"] == "vunnet"
    )

    status_count = {}
    category_count = {}
    overdue_followups = 0
    call_queue_count = 0
    for c in contacts:
        s = c["status"]
        status_count[s] = status_count.get(s, 0) + 1
        cat = c["category"]
        if cat:
            category_count[cat] = category_count.get(cat, 0) + 1
        fd = c.get("followup_date")
        if fd:
            fd_str = str(fd)[:10] if not isinstance(fd, str) else fd
            if fd_str <= today and s not in ("vunnet", "tapt"):
                overdue_followups += 1
        if c.get("call_queue"):
            call_queue_count += 1

    return {
        "total_contacts": len(contacts),
        "pipeline_value": pipeline_value,
        "won_value": won_value,
        "mrr": round(mrr, 2),
        "arr": round(mrr * 12, 2),
        "outstanding": round(_get_outstanding(), 2),
        "by_status": status_count,
        "by_category": category_count,
        "total_deals": len(deals),
        "new_this_week": new_this_week,
        "overdue_followups": overdue_followups,
        "call_queue_count": call_queue_count,
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

class BulkStatusUpdate(BaseModel):
    ids: list
    status: str

@app.post("/api/contacts/bulk-status")
def bulk_status(data: BulkStatusUpdate):
    if not data.ids:
        raise HTTPException(400, "Ingen IDer")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(data.ids))
        cur.execute(
            f"UPDATE contacts SET status=%s, updated_at=%s WHERE id IN ({placeholders})",
            [data.status, now] + list(data.ids)
        )
    return {"updated": len(data.ids)}

class BulkCallQueue(BaseModel):
    ids: list
    call_queue: bool = True

@app.post("/api/contacts/bulk-call-queue")
def bulk_call_queue(data: BulkCallQueue):
    if not data.ids:
        raise HTTPException(400, "Ingen IDer")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(data.ids))
        cur.execute(
            f"UPDATE contacts SET call_queue=%s, updated_at=%s WHERE id IN ({placeholders})",
            [data.call_queue, now] + list(data.ids)
        )
    return {"updated": len(data.ids)}

class BulkDelete(BaseModel):
    ids: list

@app.post("/api/contacts/bulk-delete")
def bulk_delete(data: BulkDelete):
    if not data.ids:
        raise HTTPException(400, "Ingen IDer")
    with get_conn() as conn:
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(data.ids))
        cur.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", data.ids)
    return {"deleted": len(data.ids)}

@app.get("/api/contacts/export")
def export_contacts(status: str = None, category: str = None):
    """Eksporter kontakter som CSV."""
    with get_conn() as conn:
        cur = conn.cursor()
        wheres, vals = [], []
        if status:
            wheres.append("status=%s"); vals.append(status)
        if category:
            wheres.append("category=%s"); vals.append(category)
        sql = "SELECT name, company, email, phone, website, org_nr, address, city, source, status, category, interest_level, service_interest, notes, followup_date, created_at FROM contacts"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY created_at DESC"
        cur.execute(sql, vals)
        rows = cur.fetchall()
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Navn", "Firma", "E-post", "Telefon", "Nettside", "Org.nr", "Adresse", "By", "Kilde", "Status", "Kategori", "Interesse", "Tjenester", "Notater", "Oppfølging", "Opprettet"])
    for r in rows:
        writer.writerow([r["name"], r["company"], r["email"], r["phone"], r["website"], r["org_nr"], r["address"], r["city"], r["source"], r["status"], r["category"], r["interest_level"], r["service_interest"], r["notes"], r["followup_date"], r["created_at"]])
    csv_content = output.getvalue()
    return Response(content=csv_content, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=kontakter.csv"})

@app.get("/api/contacts/check-duplicate")
def check_duplicate(phone: str = None, org_nr: str = None, email: str = None):
    """Sjekk om kontakt allerede finnes basert på telefon, org.nr eller email."""
    with get_conn() as conn:
        cur = conn.cursor()
        conditions, vals = [], []
        if phone and phone.strip():
            conditions.append("phone=%s")
            vals.append(phone.strip())
        if org_nr and org_nr.strip():
            conditions.append("org_nr=%s")
            vals.append(org_nr.strip())
        if email and email.strip():
            conditions.append("LOWER(email)=LOWER(%s)")
            vals.append(email.strip())
        if not conditions:
            return {"duplicates": []}
        sql = f"SELECT id, name, company, email, phone, org_nr FROM contacts WHERE {' OR '.join(conditions)} LIMIT 5"
        cur.execute(sql, vals)
        rows = cur.fetchall()
    return {"duplicates": [dict(r) for r in rows]}


# --- Contacts ---

@app.get("/api/contacts")
def get_contacts(search: str = None, status: str = None, category: str = None,
                 page: int = 0, limit: int = 50, minimal: str = None, has_email: str = None,
                 city: str = None, interest_level: str = None, call_queue: str = None):
    with get_conn() as conn:
        cur = conn.cursor()
        wheres, vals = [], []
        if status:
            wheres.append("status=%s"); vals.append(status)
        if category:
            wheres.append("category=%s"); vals.append(category)
        if has_email:
            wheres.append("email IS NOT NULL AND email != ''")
        if city:
            wheres.append("LOWER(city)=%s"); vals.append(city.lower())
        if interest_level:
            wheres.append("interest_level=%s"); vals.append(interest_level)
        if call_queue and call_queue.lower() == 'true':
            wheres.append("call_queue = true")
        if search:
            wheres.append("(LOWER(name) LIKE %s OR LOWER(company) LIKE %s OR LOWER(email) LIKE %s OR LOWER(phone) LIKE %s OR LOWER(org_nr) LIKE %s)")
            s = f"%{search.lower()}%"
            vals.extend([s, s, s, s, s])
        # Minimal mode: bare id, name, company, email (for dropdowns)
        if minimal:
            cols = "id, name, company, email"
        else:
            cols = "*"
        sql = f"SELECT {cols} FROM contacts"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        # Hent total count
        count_sql = "SELECT COUNT(*) FROM contacts"
        if wheres:
            count_sql += " WHERE " + " AND ".join(wheres)
        cur.execute(count_sql, vals)
        total = cur.fetchone()["count"]
        sql += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        vals.extend([limit, page * limit])
        cur.execute(sql, vals)
        data = cur.fetchall()
    result = [dict(r) for r in data]
    return {"items": result, "total": total, "page": page, "limit": limit}

@app.post("/api/contacts")
def create_contact(c: Contact):
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    d = c.model_dump()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO contacts (id, name, company, email, phone, source, status, category, notes, followup_date, website, org_nr, address, city, interest_level, service_interest, call_queue, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (new_id, d["name"], d["company"], d["email"], d["phone"],
             d["source"], d["status"], d["category"], d["notes"], d.get("followup_date"),
             d.get("website"), d.get("org_nr"), d.get("address"), d.get("city"),
             d.get("interest_level"), d.get("service_interest"), d.get("call_queue", False), now, now)
        )
        row = cur.fetchone()
    return dict(row)

@app.get("/api/contacts/lookup-by-name")
def lookup_by_name(name: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, notes FROM contacts WHERE LOWER(name)=LOWER(%s) LIMIT 1", (name,))
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT id, name, notes FROM contacts WHERE LOWER(name) LIKE LOWER(%s) LIMIT 1", (f"%{name}%",))
            row = cur.fetchone()
    if not row:
        return {"found": False}
    return {"found": True, "id": row["id"], "name": row["name"], "notes": row["notes"]}

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
    payload = {k: v for k, v in c.model_dump().items() if v is not None or k in ("followup_date", "website", "org_nr", "address", "city", "interest_level", "service_interest", "notes", "call_queue")}
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

@app.delete("/api/activities/{activity_id}")
def delete_activity(activity_id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM activities WHERE id=%s RETURNING id", (activity_id,))
        deleted = cur.fetchone()
    if not deleted:
        raise HTTPException(404, "Aktivitet ikke funnet")
    return {"ok": True}


# --- Stripe ---

def stripe_ok():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(400, "STRIPE_SECRET_KEY mangler i .env")

def _stripe_sub_info(s):
    """Hent pris-info og periode fra subscription-objekt (kompatibel med Stripe SDK v5+)."""
    items_data = s.items.data if hasattr(s.items, 'data') else []
    item = items_data[0] if items_data else None
    if item is None:
        return 0.0, "month", "", None
    price = item.price if hasattr(item, 'price') else None
    if price is None:
        return 0.0, "month", "", None
    amount = (price.unit_amount or 0) / 100
    recurring = price.recurring if hasattr(price, 'recurring') else None
    interval = recurring.interval if recurring and hasattr(recurring, 'interval') else "month"
    plan_name = (price.nickname or price.id or "") if hasattr(price, 'nickname') else ""
    # current_period_end er på item i nyere SDK, fallback til billing_cycle_anchor
    period_end = None
    if hasattr(item, 'current_period_end') and item.current_period_end:
        period_end = item.current_period_end
    elif hasattr(s, 'billing_cycle_anchor') and s.billing_cycle_anchor:
        period_end = s.billing_cycle_anchor
    return amount, interval, plan_name, period_end

_stripe_cache = {}
_STRIPE_CACHE_TTL = 60  # sekunder

def _cache_get(key):
    if key in _stripe_cache:
        data, ts = _stripe_cache[key]
        if time.time() - ts < _STRIPE_CACHE_TTL:
            return data
    return None

def _cache_set(key, data):
    _stripe_cache[key] = (data, time.time())

@app.post("/api/stripe/cache/clear")
def stripe_cache_clear():
    _stripe_cache.clear()
    return {"ok": True}

@app.get("/api/stripe/summary")
def stripe_summary():
    stripe_ok()
    cached = _cache_get("summary")
    if cached:
        return cached
    try:
        subs = stripe.Subscription.list(status="active", limit=100)
        mrr = 0.0
        active_sub_count = 0
        for s in subs.auto_paging_iter():
            active_sub_count += 1
            amount, interval, _, _pe = _stripe_sub_info(s)
            mrr += amount / 12 if interval == "year" else amount
    except Exception:
        mrr = 0.0
        active_sub_count = 0
    try:
        charges = stripe.Charge.list(limit=50, expand=["data.billing_details"])
        total_revenue = 0.0
        recent_payments = []
        for ch in charges.auto_paging_iter():
            if ch.status == "succeeded":
                total_revenue += ch.amount / 100
            try:
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
            except Exception:
                pass
    except Exception:
        total_revenue = 0.0
        recent_payments = []
    result = {
        "mrr": round(mrr, 2),
        "arr": round(mrr * 12, 2),
        "active_subs": active_sub_count,
        "total_revenue": round(total_revenue, 2),
        "recent_payments": recent_payments[:20],
    }
    _cache_set("summary", result)
    return result

@app.get("/api/stripe/customers")
def stripe_customers(limit: int = 100):
    stripe_ok()
    cached = _cache_get("customers")
    if cached:
        return cached
    sub_by_cid = {}
    try:
        subs = stripe.Subscription.list(status="active", limit=100)
        for s in subs.auto_paging_iter():
            try:
                cid = s.customer if isinstance(s.customer, str) else s.customer.id
                amount, interval, plan_name, period_end = _stripe_sub_info(s)
                sub_by_cid[cid] = {
                    "status": s.status,
                    "monthly": round(amount / 12 if interval == "year" else amount, 2),
                    "plan": plan_name,
                    "sub_id": s.id,
                    "period_end": datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat() if period_end else None,
                }
            except Exception:
                pass
    except Exception:
        pass
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
    _cache_set("customers", result)
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
    _stripe_cache.clear()
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
        amount, interval, plan_name, period_end = _stripe_sub_info(s)
        items_data = s.items.data if hasattr(s.items, 'data') else []
        item = items_data[0] if items_data else None
        price = item.price if item and hasattr(item, 'price') else None
        result.append({
            "id": s.id,
            "status": s.status,
            "plan": plan_name,
            "amount": amount,
            "currency": (price.currency if price and hasattr(price, 'currency') else "").upper(),
            "interval": interval,
            "current_period_end": datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat() if period_end else None,
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

@app.get("/api/stripe/invoices")
def stripe_all_invoices(limit: int = 50):
    stripe_ok()
    cached = _cache_get("all_invoices")
    if cached:
        return cached
    invoices = stripe.Invoice.list(limit=limit)
    result = []
    for inv in invoices.auto_paging_iter():
        customer_name = ""
        customer_email = ""
        if hasattr(inv, 'customer_name') and inv.customer_name:
            customer_name = inv.customer_name
        if hasattr(inv, 'customer_email') and inv.customer_email:
            customer_email = inv.customer_email
        result.append({
            "id": inv.id,
            "number": inv.number,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "amount_due": (inv.amount_due or 0) / 100,
            "amount_paid": (inv.amount_paid or 0) / 100,
            "currency": (inv.currency or "").upper(),
            "status": inv.status,
            "due_date": datetime.fromtimestamp(inv.due_date, tz=timezone.utc).isoformat() if inv.due_date else None,
            "created": datetime.fromtimestamp(inv.created, tz=timezone.utc).isoformat(),
            "invoice_pdf": inv.invoice_pdf,
            "hosted_invoice_url": inv.hosted_invoice_url,
        })
    _cache_set("all_invoices", result)
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
    bd = charge.billing_details
    email = (bd.email if bd and hasattr(bd, 'email') else None) or charge.receipt_email
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
    _, _, _, period_end = _stripe_sub_info(sub)
    cancel_at = datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat() if period_end else ""
    cust = stripe.Customer.retrieve(sub.customer if isinstance(sub.customer, str) else sub.customer.id)
    email = cust.email if hasattr(cust, 'email') else None
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

# --- Manuell trigger for outreach ---
@app.post("/api/outreach/trigger")
def trigger_outreach():
    """Kjør outreach manuelt (brukes for testing og on-demand)."""
    if not RESEND_API_KEY:
        raise HTTPException(400, "RESEND_API_KEY ikke satt")
    t = threading.Thread(target=_run_outreach_now, daemon=True)
    t.start()
    return {"ok": True, "message": "Outreach startet i bakgrunnen"}

def _run_outreach_now():
    """Kjører én outreach-runde uavhengig av klokkeslett."""
    import requests as _req
    global OUTREACH_START_DATE
    today = datetime.now(timezone.utc).date().isoformat()
    if not OUTREACH_START_DATE:
        OUTREACH_START_DATE = today
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("INSERT INTO app_settings (key, value) VALUES ('outreach_start_date', %s) ON CONFLICT (key) DO UPDATE SET value = %s", (today, today))
                conn.commit()
        except Exception:
            pass
    daily_limit = _get_outreach_limit()
    print(f"[Outreach MANUELL] Starter (limit: {daily_limit})...")
    sent_count = 0
    skipped_count = 0
    errors = []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, company, email, city, website
                FROM contacts
                WHERE status = 'ny'
                  AND email IS NOT NULL AND email != ''
                  AND email NOT LIKE '%%,%%'
                  AND company IS NOT NULL AND company != ''
                ORDER BY created_at DESC
                LIMIT %s
            """, (daily_limit * 3,))
            candidates = [dict(r) for r in cur.fetchall()]
        print(f"[Outreach MANUELL] {len(candidates)} kandidater")
        verified = []
        for lead in candidates:
            if len(verified) >= daily_limit:
                break
            company = lead.get("company", "")
            city = lead.get("city", "")
            try:
                search_query = f"{company} {city}".strip()
                resp = _req.get(
                    f"https://www.google.com/search?q={search_query}&num=5",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "no,nb,nn;q=0.9"},
                    timeout=10)
                html = resp.text.lower()
                company_clean = company.lower().replace(" ", "").replace("-", "")
                has_website = f"{company_clean}.no" in html or f"{company_clean}.com" in html or f"www.{company_clean}" in html
                if has_website:
                    skipped_count += 1
                    with get_conn() as conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE contacts SET notes = COALESCE(notes,'') || %s WHERE id = %s", (f" | Har nettside (sjekket {today})", lead["id"]))
                        conn.commit()
                    continue
                verified.append(lead)
                import time as _time
                _time.sleep(2)
            except Exception as e:
                verified.append(lead)
        print(f"[Outreach MANUELL] {len(verified)} verifiserte")
        for lead in verified:
            company = lead.get("company", lead.get("name", "din bedrift"))
            email = lead["email"]
            email_html = f"""<html><head><meta charset="utf-8"></head><body><div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.7;color:#222;max-width:600px">
<p>Hei du,</p>
<p>Vi fant ikke nettsiden til {company} online.</p>
<p>Denne uken hjelper vi 5 bedrifter med en SEO-optimalisert nettside for kun <strong>890,- /mnd</strong> — med <strong>EGET ADMIN-PANEL</strong> som er enkelt å bruke, der du kan oppdatere innhold, bilder og tekst selv. Du får se hjemmesiden din før du bestemmer deg — helt uforpliktende.</p>
<p>Vi integrerer sosiale medier, legger til bookingsystem på nettsiden, og kan hjelpe med markedsføring om du ønsker det.</p>
<p>Svar her så sender jeg et forslag.</p>
<p>Remy<br>StrategioAI<br><a href="https://www.strategioai.com" style="color:#1a7f4b">www.strategioai.com</a></p>
<hr style="border:none;border-top:1px solid #ddd;margin:20px 0">
<p style="font-size:11px;color:#999">Ønsker du ikke flere meldinger? Svar "stopp" så fjerner vi deg umiddelbart.</p>
</div></body></html>"""
            try:
                resp = _req.post("https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={"from": "Remy <remy@strategio.site>", "to": [email], "bcc": ["strategioai@strategioai.com"],
                          "subject": f"Nettside for {company}?", "html": email_html, "reply_to": "strategioai@strategioai.com",
                          "headers": {"X-Contact-Id": lead["id"]}}, timeout=10)
                if resp.status_code in (200, 201):
                    sent_count += 1
                    resend_data = resp.json()
                    resend_id = resend_data.get("id", "")
                    with get_conn() as conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE contacts SET status='kontaktet', notes=COALESCE(notes,'') || %s WHERE id=%s",
                                   (f" | Outreach sendt {today} (id:{resend_id})", lead["id"]))
                        cur.execute("INSERT INTO outreach_emails (id, contact_id, email, company, resend_id, sent_at, status) VALUES (%s,%s,%s,%s,%s,%s,'sent') ON CONFLICT DO NOTHING",
                                   (str(uuid.uuid4()), lead["id"], email, company, resend_id, datetime.now(timezone.utc).isoformat()))
                        conn.commit()
                    print(f"[Outreach MANUELL] SENDT → {company} ({email})")
                else:
                    errors.append(f"{company}: {resp.status_code}")
                import time as _time
                _time.sleep(1)
            except Exception as e:
                errors.append(f"{company}: {e}")
    except Exception as e:
        errors.append(f"Generell: {e}")
    # Telegram-rapport
    try:
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            msg = f"📧 <b>Outreach (manuell) — {today}</b>\n✅ Sendt: <b>{sent_count}</b>\n⏭ Hoppet over: <b>{skipped_count}</b>"
            if errors:
                msg += f"\n🚨 Feil: {', '.join(errors[:3])}"
            _req.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception:
        pass
    print(f"[Outreach MANUELL] Ferdig — sendt {sent_count}, hoppet over {skipped_count}")

# --- Resend webhook for email tracking ---
@app.post("/api/webhooks/resend")
async def resend_webhook(request: Request):
    """Mottar open/delivered/bounced events fra Resend."""
    try:
        data = await request.json()
        event_type = data.get("type", "")
        email_data = data.get("data", {})
        resend_id = email_data.get("email_id", "")

        if not resend_id:
            return {"ok": True}

        now = datetime.now(timezone.utc).isoformat()

        with get_conn() as conn:
            cur = conn.cursor()
            if event_type == "email.opened":
                cur.execute("UPDATE outreach_emails SET status='opened', opened_at=%s WHERE resend_id=%s AND opened_at IS NULL", (now, resend_id))
            elif event_type == "email.clicked":
                cur.execute("UPDATE outreach_emails SET clicked_at=%s WHERE resend_id=%s", (now, resend_id))
            elif event_type == "email.bounced":
                cur.execute("UPDATE outreach_emails SET status='bounced', bounced_at=%s WHERE resend_id=%s", (now, resend_id))
                # Marker kontakten som tapt ved bounce
                cur.execute("UPDATE contacts SET status='tapt', notes=COALESCE(notes,'') || %s WHERE id=(SELECT contact_id FROM outreach_emails WHERE resend_id=%s LIMIT 1)",
                           (f" | Email bounced {now[:10]}", resend_id))
            elif event_type == "email.delivered":
                cur.execute("UPDATE outreach_emails SET status='delivered' WHERE resend_id=%s AND status='sent'", (resend_id,))
            conn.commit()

        print(f"[Resend webhook] {event_type} for {resend_id}")
        return {"ok": True}
    except Exception as e:
        print(f"[Resend webhook] Feil: {e}")
        return {"ok": False}

@app.get("/api/outreach/{contact_id}")
def get_outreach_status(contact_id: str):
    """Hent outreach-email status for en kontakt."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM outreach_emails WHERE contact_id=%s ORDER BY sent_at DESC", (contact_id,))
        return [dict(r) for r in cur.fetchall()]

@app.get("/api/outreach-stats")
def outreach_stats():
    """Oversikt over outreach-kampanjen."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM outreach_emails")
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) AS n FROM outreach_emails WHERE status='delivered'")
        delivered = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM outreach_emails WHERE status='opened'")
        opened = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM outreach_emails WHERE status='bounced'")
        bounced = cur.fetchone()["n"]
        cur.execute("SELECT sent_at, email, company, status, opened_at FROM outreach_emails ORDER BY sent_at DESC LIMIT 20")
        recent = [dict(r) for r in cur.fetchall()]
    return {
        "total": total,
        "delivered": delivered,
        "opened": opened,
        "bounced": bounced,
        "open_rate": round(opened / max(total, 1) * 100, 1),
        "recent": recent
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

    elif etype == "customer.created":
        obj = event["data"]["object"]
        email = (obj.get("email") or "").lower()
        if email:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id FROM contacts WHERE LOWER(email)=%s LIMIT 1", (email,))
                if not cur.fetchone():
                    new_id = str(uuid.uuid4())
                    now = datetime.now(timezone.utc).isoformat()
                    cur.execute(
                        "INSERT INTO contacts (id, name, company, email, phone, source, status, notes, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (new_id, obj.get("name") or email, None, obj.get("email"), obj.get("phone"),
                         "stripe", "lead", f"Stripe ID: {obj['id']}", now, now)
                    )
                    await telegram(f"Ny Stripe-kunde importert til CRM!\n<b>{obj.get('name') or email}</b>")

    elif etype == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer")
        if customer_id:
            cust = stripe.Customer.retrieve(customer_id)
            email = cust.email if hasattr(cust, 'email') else None
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
            email = cust.email if hasattr(cust, 'email') else None
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

    _stripe_cache.clear()
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


# --- Pitches ---

@app.get("/api/pitches")
def get_pitches():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pitches ORDER BY created_at ASC")
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.get("/api/pitches/{id}")
def get_pitch(id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pitches WHERE id=%s", (id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pitch not found")
    return dict(row)

@app.post("/api/pitches")
def create_pitch(p: Pitch):
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pitches (id, name, description, sections, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            (new_id, p.name, p.description, json.dumps(p.sections), now, now)
        )
        row = cur.fetchone()
    return dict(row)

@app.put("/api/pitches/{id}")
def update_pitch(id: str, p: Pitch):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pitches SET name=%s, description=%s, sections=%s, updated_at=%s WHERE id=%s RETURNING *",
            (p.name, p.description, json.dumps(p.sections), now, id)
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pitch not found")
    return dict(row)

@app.delete("/api/pitches/{id}")
def delete_pitch(id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM pitches WHERE id=%s", (id,))
    return {"ok": True}

# --- Pitch Arguments ---

@app.get("/api/pitches/{pitch_id}/arguments")
def get_pitch_arguments(pitch_id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pitch_arguments WHERE pitch_id=%s ORDER BY category, created_at ASC", (pitch_id,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.post("/api/pitch-arguments")
def create_pitch_argument(a: PitchArgument):
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pitch_arguments (id, pitch_id, objection, response, category, created_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            (new_id, a.pitch_id, a.objection, a.response, a.category, now)
        )
        row = cur.fetchone()
    return dict(row)

@app.put("/api/pitch-arguments/{id}")
def update_pitch_argument(id: str, a: PitchArgument):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pitch_arguments SET objection=%s, response=%s, category=%s WHERE id=%s RETURNING *",
            (a.objection, a.response, a.category, id)
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Argument not found")
    return dict(row)

@app.delete("/api/pitch-arguments/{id}")
def delete_pitch_argument(id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM pitch_arguments WHERE id=%s", (id,))
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


# --- Oppfølgingsdato ---

@app.get("/api/contacts/followups")
def get_followups():
    """Returnerer leads med oppfølgingsdato i dag eller tidligere (ikke vunnet/tapt)."""
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM contacts
            WHERE followup_date IS NOT NULL
              AND followup_date <= %s
              AND status NOT IN ('vunnet','tapt')
            ORDER BY followup_date ASC
        """, (today,))
        return [dict(r) for r in cur.fetchall()]


# --- Daglig Telegram-rapport ---

async def send_daily_report():
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM contacts")
        total = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE created_at::timestamptz >= now() - interval '7 days'")
        new_week = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE followup_date <= %s AND status NOT IN ('vunnet','tapt')", (today,))
        overdue = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE status='kunde'")
        customers = cur.fetchone()["n"]

    mrr = 0.0
    if STRIPE_SECRET_KEY:
        try:
            subs = stripe.Subscription.list(status="active", limit=100)
            for s in subs.auto_paging_iter():
                amount, interval, _, _pe = _stripe_sub_info(s)
                mrr += amount / 12 if interval == "year" else amount
        except Exception:
            pass

    msg = (
        f"📊 <b>Daglig CRM-rapport</b>\n\n"
        f"👥 Totale leads: <b>{total}</b>\n"
        f"🆕 Nye siste 7 dager: <b>{new_week}</b>\n"
        f"🤝 Kunder: <b>{customers}</b>\n"
        f"⏰ Oppfølginger forfalt: <b>{overdue}</b>\n"
        f"💰 MRR: <b>{mrr:,.0f} kr</b>\n"
        f"📅 {today}"
    )
    await telegram(msg)

@app.post("/api/daily-report")
async def trigger_daily_report():
    """Trigger daglig rapport manuelt (kalles av Railway cron eller n8n)."""
    await send_daily_report()
    return {"ok": True}

@app.post("/api/health-check")
async def trigger_health_check():
    """Manuell trigger av daglig helsesjekk + rapport til Telegram."""
    import requests as _req
    today = datetime.now(timezone.utc).date().isoformat()
    errors = []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM contacts")
            total = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE created_at::timestamptz >= now() - interval '24 hours'")
            new_24h = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE status='kunde'")
            customers = cur.fetchone()["n"]
    except Exception as e:
        errors.append(f"DB: {e}")
        total = new_24h = customers = 0
    status = "ok" if not errors else "error"
    msg = f"🔧 <b>Manuell helsesjekk — {today}</b>\n\n"
    msg += f"👥 Totale leads: <b>{total}</b>\n"
    msg += f"🆕 Nye siste 24t: <b>{new_24h}</b>\n"
    msg += f"🤝 Kunder: <b>{customers}</b>\n"
    if errors:
        msg += f"\n🚨 Feil: {', '.join(errors)}\n"
    else:
        msg += f"\n✅ Alt fungerer\n"
    msg += f"\n🔗 <a href='https://crm.strategio.site'>Åpne CRM</a>"
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        _req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    return {"ok": True, "status": status, "total": total, "errors": errors}


# --- Lead-rapporter API ---

@app.get("/api/lead-reports")
def get_lead_reports():
    """Hent alle lead-rapporter, nyeste først."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, report_date, data, created_at FROM lead_reports ORDER BY report_date DESC LIMIT 30")
        rows = cur.fetchall()
    result = []
    for r in rows:
        try:
            data = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
        except Exception:
            data = {}
        result.append({
            "id": r["id"],
            "report_date": r["report_date"],
            "data": data,
            "created_at": r["created_at"],
        })
    return result

@app.get("/api/lead-reports/{report_date}")
def get_lead_report(report_date: str):
    """Hent en spesifikk lead-rapport etter dato (YYYY-MM-DD)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, report_date, data, created_at FROM lead_reports WHERE report_date = %s", (report_date,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Rapport ikke funnet")
    try:
        data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
    except Exception:
        data = {}
    return {"id": row["id"], "report_date": row["report_date"], "data": data, "created_at": row["created_at"]}

@app.post("/api/lead-scrape")
def trigger_lead_scrape(industries: list[str] = None, cities: list[str] = None):
    """Kjør lead-scraping manuelt med valgfrie bransjer og byer."""
    import random
    if not GOOGLE_PLACES_API_KEY:
        raise HTTPException(400, "GOOGLE_PLACES_API_KEY ikke konfigurert")
    if not industries:
        industries = random.sample(AI_RESEPSJONIST_BRANSJER, min(5, len(AI_RESEPSJONIST_BRANSJER)))
    if not cities:
        cities = random.sample(NORSKE_BYER, min(5, len(NORSKE_BYER)))
    all_leads = []
    for industry in industries:
        for city in cities:
            leads = _scrape_places(industry, city)
            all_leads.extend(leads)
    stats = _import_leads_to_crm(all_leads) if all_leads else {"imported": 0, "skipped": 0, "total": 0}
    # Kategoriser
    by_cat = {"ai_resepsjonist": 0, "trenger_nettside": 0, "generell_tjeneste": 0}
    for l in all_leads:
        cat = l.get("category", "generell_tjeneste")
        by_cat[cat] = by_cat.get(cat, 0) + 1
    top5 = sorted(all_leads, key=lambda x: x["score"], reverse=True)[:5]
    # Lagre rapport
    today = datetime.now(timezone.utc).date().isoformat()
    report_data = {
        "date": today,
        "searches": [{"industry": i, "city": c} for i in industries for c in cities],
        "stats": stats,
        "by_category": by_cat,
        "top_leads": [{"name": l["name"], "score": l["score"], "category": l["category"],
                       "phone": l.get("phone",""), "city": l["city"],
                       "industry": l["industry"], "strategy": l["strategy"],
                       "website": l.get("website",""), "rating": l.get("rating",0)} for l in top5],
    }
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO lead_reports (id, report_date, data, created_at) VALUES (%s,%s,%s,%s)",
                (str(uuid.uuid4()), today, json.dumps(report_data, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat())
            )
    except Exception:
        pass
    return {"ok": True, "stats": stats, "by_category": by_cat, "top_leads": report_data["top_leads"],
            "searches": report_data["searches"]}

@app.get("/api/lead-stats")
def get_lead_stats():
    """Statistikk over leads fra scraping."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE source='google_places'")
        total = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE source='google_places' AND created_at::timestamptz >= now() - interval '24 hours'")
        today = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM contacts WHERE source='google_places' AND created_at::timestamptz >= now() - interval '7 days'")
        week = cur.fetchone()["n"]
        cur.execute("SELECT category, COUNT(*) AS n FROM contacts WHERE source='google_places' GROUP BY category")
        cats = {r["category"]: r["n"] for r in cur.fetchall()}
    return {"total": total, "today": today, "this_week": week, "by_category": cats}


# --- Produkter/Tjenester ---

@app.get("/api/products")
def get_products():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM products ORDER BY created_at")
        return cur.fetchall()

@app.post("/api/products")
def create_product(p: Product):
    pid = str(uuid.uuid4())
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO products (id, name, description, price, price_type, category, icon, features, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (pid, p.name, p.description, p.price, p.price_type, p.category, p.icon, p.features, p.is_active)
        )
    return {"id": pid}

@app.put("/api/products/{pid}")
def update_product(pid: str, p: ProductUpdate):
    fields, vals = [], []
    for k, v in p.model_dump(exclude_none=True).items():
        fields.append(f"{k} = %s")
        vals.append(v)
    if not fields:
        raise HTTPException(400, "Ingen felter å oppdatere")
    fields.append("updated_at = now() AT TIME ZONE 'utc'")
    vals.append(pid)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE products SET {', '.join(fields)} WHERE id = %s", vals)
    return {"ok": True}

@app.delete("/api/products/{pid}")
def delete_product(pid: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM products WHERE id = %s", (pid,))
    return {"ok": True}


# --- Twilio Browser Calling ---

@app.get("/api/twilio/token")
def twilio_token():
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise HTTPException(400, "Twilio ikke konfigurert")
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant
    token = AccessToken(
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN,  # API Key SID — bruker main auth token som fallback
        TWILIO_AUTH_TOKEN,  # API Key Secret
        identity="strategio-crm",
        ttl=3600,
    )
    voice_grant = VoiceGrant(
        outgoing_application_sid=TWILIO_TWIML_APP_SID,
        incoming_allow=False,
    )
    token.add_grant(voice_grant)
    return {"token": token.to_jwt()}

@app.post("/api/twilio/voice")
async def twilio_voice(request: Request):
    """TwiML webhook — Twilio kaller denne når et outbound call starter."""
    form = await request.form()
    to_number = form.get("To", "")
    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    if to_number:
        dial = resp.dial(caller_id=TWILIO_PHONE_NUMBER)
        dial.number(to_number)
    else:
        resp.say("Ingen nummer oppgitt.", language="nb-NO")
    return Response(content=str(resp), media_type="application/xml")

@app.post("/api/twilio/call")
def twilio_call_rest(data: dict):
    """REST-basert utgående samtale (alternativ til browser SDK)."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise HTTPException(400, "Twilio ikke konfigurert")
    to_number = data.get("to")
    contact_id = data.get("contact_id")
    if not to_number:
        raise HTTPException(400, "Mangler telefonnummer")
    from twilio.rest import Client
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    call = client.calls.create(
        to=to_number,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{os.getenv('RAILWAY_PUBLIC_DOMAIN', 'https://crm.strategio.site')}/api/twilio/voice",
    )
    # Logg samtale som aktivitet
    if contact_id:
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO activities (id, contact_id, type, note, created_at) VALUES (%s,%s,%s,%s,%s)",
                (str(uuid.uuid4()), contact_id, "call", f"Utgående samtale til {to_number} (Twilio SID: {call.sid})", now)
            )
    return {"ok": True, "call_sid": call.sid}

@app.get("/api/twilio/status")
def twilio_status():
    """Sjekk om Twilio er konfigurert."""
    configured = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER)
    return {"configured": configured, "phone_number": TWILIO_PHONE_NUMBER if configured else None}


# --- Init ---
init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
