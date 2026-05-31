#!/usr/bin/env python3
"""
StockPilot NSE/BSE Filing Bot v3.7
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v3.7 — HALLUCINATION GUARD + SMART ANALYSIS:
  ✅ Anti-hallucination: AI told NEVER to invent numbers
  ✅ Confidence scoring: HIGH/MEDIUM/LOW based on filing type
  ✅ PDF content extraction from NSE/BSE filing links
  ✅ Generic filings flagged as SPECULATIVE in analysis
  ✅ AI disclaimer on all alerts
  ✅ Smarter prompt: distinguish between rich vs thin filings
  ✅ All v3.6 features retained
"""

import os, time, sqlite3, hashlib, json, re, logging, sys
from datetime import datetime, timedelta
import pytz
import requests

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("StockPilot")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "").strip()
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "").strip()
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "300"))
DB_PATH         = os.environ.get("DB_PATH", "filings.db")
IST             = pytz.timezone("Asia/Kolkata")
FILING_MAX_AGE_DAYS = 7

GROQ_MODEL_HIGH = "llama-3.1-70b-versatile"
GROQ_MODEL_STD  = "llama-3.1-8b-instant"

def validate_config():
    missing = []
    if not TELEGRAM_TOKEN: missing.append("TELEGRAM_TOKEN")
    if not CHAT_ID:        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        log.error(f"Missing: {', '.join(missing)}")
        sys.exit(1)
    if GROQ_API_KEY:   log.info("Groq AI ready ✅ (70B for HIGH | 8B for others)")
    elif GEMINI_API_KEY: log.info("Gemini AI ready ✅")
    else:              log.warning("No AI key — add GROQ_API_KEY")

# ─────────────────────────────────────────────────────────────────────────────
# FILING CONFIDENCE SCORING
# How much real content does this filing type carry?
# ─────────────────────────────────────────────────────────────────────────────
# HIGH confidence = filing has specific disclosed information
HIGH_CONFIDENCE_TYPES = [
    "financial result", "board meeting", "dividend", "bonus", "split",
    "buyback", "merger", "acquisition", "rights", "order", "contract",
    "scheme", "allotment", "record date", "press release",
]
# LOW confidence = filing is just a notification, no disclosed content
LOW_CONFIDENCE_TYPES = [
    "analyst", "investor meet", "con. call", "general update",
    "basmati", "general announcement", "updates",
]

def get_filing_confidence(title, cat_label):
    """
    Returns 'HIGH', 'MEDIUM', or 'LOW' confidence level.
    This determines how much the AI should trust its own analysis.
    """
    combined = (title + " " + cat_label).lower()
    if any(t in combined for t in HIGH_CONFIDENCE_TYPES):
        return "HIGH"
    if any(t in combined for t in LOW_CONFIDENCE_TYPES):
        return "LOW"
    return "MEDIUM"

# ─────────────────────────────────────────────────────────────────────────────
# PDF CONTENT EXTRACTOR — tries to get text from filing attachment
# ─────────────────────────────────────────────────────────────────────────────
def extract_filing_text(link):
    """
    Attempts to extract text from NSE/BSE filing PDF.
    Returns first 1500 chars of content, or None if fails.
    Only tries for direct PDF links (not generic page links).
    """
    if not link or "nsearchives" not in link and "AttachLive" not in link:
        return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/pdf,*/*",
        }
        r = requests.get(link, headers=headers, timeout=12, stream=True)
        if not r.ok:
            return None
        content_type = r.headers.get("content-type", "").lower()
        if "pdf" not in content_type:
            return None
        # Read first 50KB only (enough for first few pages)
        raw = b""
        for chunk in r.iter_content(chunk_size=8192):
            raw += chunk
            if len(raw) > 51200:
                break
        # Simple PDF text extraction — look for readable strings
        text_parts = re.findall(
            rb'BT\s*(.*?)\s*ET',
            raw, re.DOTALL
        )
        if not text_parts:
            # Fallback: grab printable ASCII sequences > 4 chars
            text_parts = re.findall(rb'[A-Za-z0-9\s\.\,\:\;\-\%\₹\(\)]{5,}', raw)
        decoded = []
        for part in text_parts[:50]:
            try:
                s = part.decode("latin-1", errors="ignore").strip()
                if len(s) > 4:
                    decoded.append(s)
            except:
                pass
        full_text = " ".join(decoded)
        # Clean up
        full_text = re.sub(r'\s+', ' ', full_text).strip()
        if len(full_text) < 50:
            return None
        return full_text[:1500]
    except Exception as e:
        log.debug(f"PDF extract failed: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# DATE UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
    "%d %b %Y",
]

def parse_date(s):
    if not s: return None
    s = str(s).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None

def is_recent(date_str, days=FILING_MAX_AGE_DAYS):
    dt = parse_date(date_str)
    if dt is None: return True
    return dt >= datetime.now() - timedelta(days=days)

# ─────────────────────────────────────────────────────────────────────────────
# MARKET STATUS
# ─────────────────────────────────────────────────────────────────────────────
def get_market_status():
    now = datetime.now(IST)
    if now.weekday() >= 5: return False, "WEEKEND"
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now < open_t:  return False, "PRE_MARKET"
    if now > close_t: return False, "AFTER_HOURS"
    return True, "LIVE"

def market_flag(status):
    if status == "LIVE":        return ""
    if status == "AFTER_HOURS": return "\n🌙 <b>AFTER HOURS</b> — Will impact tomorrow's opening price"
    if status == "WEEKEND":     return "\n📅 <b>WEEKEND FILING</b> — Will impact Monday's opening price"
    if status == "PRE_MARKET":
        now = datetime.now(IST)
        mins = max(0, int((now.replace(hour=9,minute=15,second=0) - now).total_seconds()/60))
        return f"\n🌅 <b>PRE-MARKET</b> — Market opens in {mins} min — watch for gap"
    return ""

# ─────────────────────────────────────────────────────────────────────────────
# EX-DATE URGENCY
# ─────────────────────────────────────────────────────────────────────────────
def get_urgency(title):
    patterns = [
        r'ex.?date[:\s]+(\d{1,2}[-/\s]\w+[-/\s]\d{2,4})',
        r'ex.?date[:\s]+(\d{4}-\d{2}-\d{2})',
        r'record\s+date[:\s]+(\d{1,2}[-/\s]\w+[-/\s]\d{2,4})',
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            dt = parse_date(m.group(1))
            if dt:
                days = (dt.date() - datetime.now(IST).date()).days
                if days < 0: continue
                if days <= 2: return f"🚨 <b>URGENT — Ex/Record Date in {days} day{'s' if days!=1 else ''}!</b>", days
                if days <= 7: return f"⏰ <b>UPCOMING — Ex/Record Date in {days} days</b>", days
    return None, None

# ─────────────────────────────────────────────────────────────────────────────
# STOCKS — with avg + qty for P&L
# ─────────────────────────────────────────────────────────────────────────────
PORTFOLIO = [
    dict(ticker="ADVAIT",     name="Advait Infratech",       nse="ADVAIT",      bse="543259", sector="Infrastructure",       cat="PORTFOLIO", avg=2153.00, qty=9),
    dict(ticker="ANANTRAJ",   name="Anant Raj Ltd",          nse="ANANTRAJ",    bse="515055", sector="Real Estate",          cat="PORTFOLIO", avg=567.00,  qty=36),
    dict(ticker="APOLLO",     name="Apollo Micro Systems",   nse="APOLLOMICRO", bse="543288", sector="Defence Electronics",  cat="PORTFOLIO", avg=271.00,  qty=62),
    dict(ticker="BEL",        name="Bharat Electronics",     nse="BEL",         bse="500049", sector="Defence PSU",          cat="PORTFOLIO", avg=412.50,  qty=28),
    dict(ticker="CDSL",       name="CDSL",                   nse="CDSL",        bse="543272", sector="Financial Services",   cat="PORTFOLIO", avg=443.76,  qty=30),
    dict(ticker="HAL",        name="Hindustan Aeronautics",  nse="HAL",         bse="541154", sector="Defence Aerospace",    cat="PORTFOLIO", avg=2005.55, qty=10),
    dict(ticker="HAPPSTMNDS", name="Happiest Minds",         nse="HAPPSTMNDS",  bse="543227", sector="IT Services",          cat="PORTFOLIO", avg=880.72,  qty=16),
    dict(ticker="IFCI",       name="IFCI Ltd",               nse="IFCI",        bse="500106", sector="NBFC",                 cat="PORTFOLIO", avg=264.09,  qty=24),
    dict(ticker="INOXINDIA",  name="INOX India",             nse="INOXINDIA",   bse="543716", sector="Industrial Gas",       cat="PORTFOLIO", avg=1221.10, qty=2),
    dict(ticker="IZMO",       name="Izmo Ltd",               nse="IZMO",        bse="532341", sector="Auto Technology",      cat="PORTFOLIO", avg=948.40,  qty=16),
    dict(ticker="KPEL",       name="K.P. Energy",            nse="KPEL",        bse="540698", sector="Renewable Energy",     cat="PORTFOLIO", avg=543.00,  qty=30),
    dict(ticker="NETWEB",     name="Netweb Technologies",    nse="NETWEB",      bse="543920", sector="IT Hardware",          cat="PORTFOLIO", avg=3255.00, qty=1),
    dict(ticker="PENNARINT",  name="Pennar Industries",      nse="PENNARINT",   bse="513228", sector="Steel & Engineering",  cat="PORTFOLIO", avg=235.42,  qty=55),
    dict(ticker="PGEL",       name="PG Electroplast",        nse="PGEL",        bse="543594", sector="Electronics",          cat="PORTFOLIO", avg=796.30,  qty=23),
    dict(ticker="REMSONSIND", name="Remsons Industries",     nse="REMSONSIND",  bse="517437", sector="Auto Components",      cat="PORTFOLIO", avg=131.78,  qty=116),
    dict(ticker="RVNL",       name="Rail Vikas Nigam",       nse="RVNL",        bse="542649", sector="Railways & Infra",     cat="PORTFOLIO", avg=133.04,  qty=136),
]

WATCHLIST = [
    dict(ticker="JAINRESOUR", name="Jain Resource Recycl",   nse=None,          bse="533289", sector="Recycling",           cat="WATCHLIST"),
    dict(ticker="IREDA",      name="Indian Renewable Energy", nse="IREDA",       bse="544124", sector="Renewable Energy",    cat="WATCHLIST"),
    dict(ticker="ONEGLOBAL",  name="One Global Service",      nse="ONEGLOBAL",   bse=None,     sector="Business Services",   cat="WATCHLIST"),
    dict(ticker="DOMS",       name="DOMS Industries",         nse="DOMS",        bse="544045", sector="Consumer Stationery", cat="WATCHLIST"),
    dict(ticker="LANCER",     name="Lancer Container",        nse=None,          bse="526807", sector="Packaging",           cat="WATCHLIST"),
    dict(ticker="HFCL",       name="HFCL Ltd",                nse="HFCL",        bse="500183", sector="Telecom Infra",       cat="WATCHLIST"),
    dict(ticker="BORORENEW",  name="Boro Renewables",         nse="BORORENEW",   bse=None,     sector="Renewable Energy",    cat="WATCHLIST"),
    dict(ticker="IDEAFORGE",  name="ideaForge Technology",    nse="IDEAFORGE",   bse="543932", sector="Defence Drones",      cat="WATCHLIST"),
    dict(ticker="NAVKARCORP", name="Navkar Corporation",      nse="NAVKARCORP",  bse="539332", sector="Logistics",           cat="WATCHLIST"),
    dict(ticker="RKFORGE",    name="Ramkrishna Forgings",     nse="RKFORGE",     bse="500368", sector="Auto Forgings",       cat="WATCHLIST"),
    dict(ticker="SIS",        name="SIS Ltd",                 nse="SIS",         bse="540673", sector="Security Services",   cat="WATCHLIST"),
    dict(ticker="IBULLSLTD",  name="Indiabulls Ltd",          nse="IBULLSLTD",   bse="535789", sector="NBFC",                cat="WATCHLIST"),
    dict(ticker="FABTECH",    name="Fabtech Technologies",    nse="FABTECH",     bse=None,     sector="Engineering",         cat="WATCHLIST"),
    dict(ticker="E2E",        name="E2E Networks",            nse="E2ENETWORKS", bse="543421", sector="Cloud Infra",         cat="WATCHLIST"),
    dict(ticker="NPL",        name="NPL",                     nse="NPL",         bse=None,     sector="Manufacturing",       cat="WATCHLIST"),
    dict(ticker="AURUM",      name="Aurum PropTech",          nse="AURUM",       bse="543088", sector="PropTech",            cat="WATCHLIST"),
    dict(ticker="MARSONS",    name="Marsons Ltd",             nse="MARSONS",     bse="522080", sector="Electrical Equip",    cat="WATCHLIST"),
    dict(ticker="HARSHA",     name="Harsha Engineers",        nse="HARSHA",      bse="543457", sector="Precision Engg",      cat="WATCHLIST"),
    dict(ticker="RAYMOND",    name="Raymond Ltd",             nse="RAYMOND",     bse="500330", sector="Lifestyle & RE",      cat="WATCHLIST"),
    dict(ticker="MARINE",     name="Marine Electricals",      nse="MARINE",      bse=None,     sector="Electrical Equip",    cat="WATCHLIST"),
    dict(ticker="KMEW",       name="KMEW",                    nse="KMEW",        bse=None,     sector="Manufacturing",       cat="WATCHLIST"),
    dict(ticker="MODISONLTD", name="Modison Ltd",             nse="MODISONLTD",  bse=None,     sector="Electrical Contacts", cat="WATCHLIST"),
    dict(ticker="RATEGAIN",   name="RateGain Travel Tech",    nse="RATEGAIN",    bse="543417", sector="Travel Tech SaaS",    cat="WATCHLIST"),
]

ALL_STOCKS = PORTFOLIO + WATCHLIST

# ─────────────────────────────────────────────────────────────────────────────
# CLASSIFIER
# ─────────────────────────────────────────────────────────────────────────────
CATEGORIES = {
    "Result":               ("📊 Financial Result",        "HIGH"),
    "Board Meeting":        ("🗓 Board Meeting",            "HIGH"),
    "Dividend":             ("💰 Dividend",                 "HIGH"),
    "Bonus":                ("🎁 Bonus Shares",             "HIGH"),
    "Split":                ("✂️ Stock Split",              "HIGH"),
    "Buyback":              ("♻️ Buyback",                  "HIGH"),
    "Merger":               ("🔀 Merger/Acquisition",       "HIGH"),
    "Acquisition":          ("🔀 Merger/Acquisition",       "HIGH"),
    "Rights":               ("📝 Rights Issue",             "HIGH"),
    "Order":                ("🏆 Order/Contract Win",       "HIGH"),
    "Contract":             ("🏆 Order/Contract Win",       "HIGH"),
    "Scheme":               ("📋 Scheme of Arrangement",   "HIGH"),
    "AGM":                  ("🏛 AGM/EGM",                 "MEDIUM"),
    "EGM":                  ("🏛 AGM/EGM",                 "MEDIUM"),
    "Appointment":          ("👤 Board Change",             "MEDIUM"),
    "Cessation":            ("👤 Board Change",             "MEDIUM"),
    "Change in Management": ("👤 Management Change",        "MEDIUM"),
    "Insider":              ("🔍 Insider Trading",          "MEDIUM"),
    "Analyst":              ("📊 Analyst/Investor Meet",    "MEDIUM"),
    "Investor":             ("📊 Analyst/Investor Meet",    "MEDIUM"),
    "Press Release":        ("📰 Press Release",            "MEDIUM"),
    "Update":               ("📢 Business Update",          "MEDIUM"),
    "Litigation":           ("⚖️ Litigation",              "MEDIUM"),
    "General":              ("📢 General Announcement",     "MEDIUM"),
    "Basmati":              ("📢 General Announcement",     "MEDIUM"),
    "Record Date":          ("📅 Record Date",              "MEDIUM"),
    "Allotment":            ("📋 Share Allotment",          "MEDIUM"),
    "Spurt":                ("📈 Volume Spurt",             "MEDIUM"),
    "Price":                ("📈 Price Movement",           "MEDIUM"),
}

SKIP = [
    "certificate under sebi", "trading window",
    "newspaper publication", "copy of newspaper",
    "registrar & share transfer", "reconciliation of share capital",
    "loss of share certificate", "sebi (depositories",
    "compliances-reg.", "reg. 74", "reg. 76", "reg. 57", "reg. 40",
]

def classify(title, cat_raw=""):
    combined = (title + " " + cat_raw).lower()
    if any(s in combined for s in SKIP): return None, None
    for kw, (label, imp) in CATEGORIES.items():
        if kw.lower() in combined: return label, imp
    return "📢 Corporate Filing", "MEDIUM"

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS sent_filings (
        hash TEXT PRIMARY KEY, ticker TEXT, nse_sym TEXT,
        title TEXT, source TEXT, sent_at INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT, msg TEXT, ts INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS weekly_digest_sent (
        week_id TEXT PRIMARY KEY, sent_at INTEGER)""")
    conn.execute("DELETE FROM sent_filings WHERE sent_at < ?",
                 (int(time.time()) - 30*86400,))
    conn.commit()
    log.info("Database ready ✅")
    return conn

def _h(nse_sym_or_ticker, title, source):
    return hashlib.sha256(
        f"{source}:{nse_sym_or_ticker}:{title.strip().lower()}".encode()
    ).hexdigest()

def is_dup(conn, stock, title, source):
    k = stock.get("nse") or stock["ticker"]
    return conn.execute("SELECT 1 FROM sent_filings WHERE hash=?",
                        (_h(k,title,source),)).fetchone() is not None

def mark(conn, stock, title, source):
    k = stock.get("nse") or stock["ticker"]
    conn.execute("INSERT OR IGNORE INTO sent_filings VALUES (?,?,?,?,?,?)",
                 (_h(k,title,source), stock["ticker"], k, title[:200],
                  source, int(time.time())))
    conn.commit()

def is_semantic_dup(conn, stock, title, source):
    k = stock.get("nse") or stock["ticker"]
    cutoff = int(time.time()) - 1800
    recent = conn.execute(
        "SELECT title FROM sent_filings WHERE nse_sym=? AND sent_at>? AND source!=?",
        (k, cutoff, source)).fetchall()
    if not recent: return False
    tw = set(re.findall(r'\w+', title.lower()))
    for (rt,) in recent:
        rw = set(re.findall(r'\w+', rt.lower()))
        if tw and len(tw & rw) / len(tw) > 0.75:
            return True
    return False

def log_err(conn, msg):
    conn.execute("INSERT INTO errors VALUES (NULL,?,?)",
                 (msg[:500], int(time.time())))
    conn.commit()

# ─────────────────────────────────────────────────────────────────────────────
# NSE SESSION
# ─────────────────────────────────────────────────────────────────────────────
class NSESession:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        })
        self.warmed = False
        self._last  = 0

    def warm(self):
        try:
            self.s.get("https://www.nseindia.com/", timeout=15)
            time.sleep(2)
            self.s.get("https://www.nseindia.com/companies-listing/"
                       "corporate-filings-announcements", timeout=12)
            time.sleep(1)
            self.warmed = True
            self._last  = time.time()
            log.info("NSE session warmed ✅")
        except Exception as e:
            log.warning(f"NSE warmup: {e}")

    def get(self, url):
        if not self.warmed or time.time() - self._last > 1800:
            self.warm()
        try:
            r = self.s.get(url, timeout=15)
            if r.status_code == 401:
                self.warmed = False
                self.warm()
                r = self.s.get(url, timeout=15)
            return r
        except Exception as e:
            log.debug(f"NSE: {e}")
            return None

nse = NSESession()
BSE_H = {"User-Agent":"Mozilla/5.0","Referer":"https://www.bseindia.com/","Origin":"https://www.bseindia.com"}

# ─────────────────────────────────────────────────────────────────────────────
# NSE LIVE QUOTE
# ─────────────────────────────────────────────────────────────────────────────
def fetch_quote(nse_sym):
    if not nse_sym: return None
    r = nse.get(f"https://www.nseindia.com/api/quote-equity?symbol={nse_sym}")
    if not r or not r.ok: return None
    try:
        d   = r.json()
        pi  = d.get("priceInfo",  {})
        ti  = d.get("tradedInfo", {})
        whl = pi.get("weekHighLow", {})
        ltp      = pi.get("lastPrice")
        chg_pct  = pi.get("pChange")
        w52_hi   = whl.get("max")
        w52_lo   = whl.get("min")
        vol_today= ti.get("totalTradedVolume")
        avg_vol  = ti.get("tottrdqty")
        range_pct= vol_ratio = None
        if ltp and w52_hi and w52_lo:
            try:
                span = float(w52_hi) - float(w52_lo)
                if span > 0:
                    range_pct = round(((float(ltp) - float(w52_lo)) / span) * 100, 1)
            except: pass
        if vol_today and avg_vol:
            try:
                vol_ratio = round(float(vol_today) / float(avg_vol) * 100, 1)
            except: pass
        return dict(ltp=ltp, chg_pct=chg_pct, w52_hi=w52_hi, w52_lo=w52_lo,
                    vol_today=vol_today, avg_vol=avg_vol,
                    range_pct=range_pct, vol_ratio=vol_ratio)
    except Exception as e:
        log.debug(f"Quote {nse_sym}: {e}")
        return None

def fmt_quote(q):
    if not q: return "Live data unavailable"
    ltp  = f"₹{q['ltp']}"     if q.get("ltp")     else "N/A"
    chg  = f"{q['chg_pct']}%" if q.get("chg_pct") else "N/A"
    hi52 = f"₹{q['w52_hi']}"  if q.get("w52_hi")  else "N/A"
    lo52 = f"₹{q['w52_lo']}"  if q.get("w52_lo")  else "N/A"
    if q.get("range_pct") is not None:
        rp   = q["range_pct"]
        zone = "near 52w HIGH — extended" if rp > 70 else \
               "near 52w LOW — value zone" if rp < 30 else "mid-range"
        rng  = f"{rp}% above 52w low ({zone})"
    else:
        rng = "N/A"
    if q.get("vol_ratio") is not None:
        vr  = q["vol_ratio"]
        sig = "HIGH — institutional activity possible" if vr > 150 else \
              "LOW — limited interest" if vr < 50 else "normal"
        vol = f"{int(q['vol_today']):,} ({vr}% of annual avg — {sig})"
    else:
        vol = str(q.get("vol_today","N/A"))
    return (f"Price: {ltp} ({chg} today)\n"
            f"52w: {lo52} → {hi52} | Position: {rng}\n"
            f"Volume: {vol}")

def fmt_pnl(stock, ltp):
    if not stock.get("avg") or not stock.get("qty") or not ltp: return None
    avg = stock["avg"]; qty = stock["qty"]
    inv = qty * avg; cur = qty * float(ltp)
    pnl = cur - inv; pct = (pnl / inv) * 100
    arrow = "📈" if pnl >= 0 else "📉"
    s     = "+" if pnl >= 0 else ""
    return (f"{arrow} <b>Your Position:</b> {qty} shares @ ₹{avg:,.2f}\n"
            f"   Invested ₹{inv:,.0f} → Now ₹{cur:,.0f} | "
            f"P&amp;L: <b>{s}₹{pnl:,.0f} ({s}{pct:.1f}%)</b>")

# ─────────────────────────────────────────────────────────────────────────────
# FETCHERS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_nse_filings(sym):
    if not sym: return []
    r = nse.get(f"https://www.nseindia.com/api/corporate-announcements"
                f"?index=equities&symbol={sym}")
    if not r or not r.ok: return []
    try:
        out = []
        for a in r.json()[:20]:
            t = (a.get("desc") or a.get("sm_name") or "").strip()
            if not t: continue
            att = a.get("attchmnt") or ""
            out.append(dict(
                title=t,
                link=(f"https://nsearchives.nseindia.com/corporate/xbrl/{att}"
                      if att else
                      f"https://www.nseindia.com/companies-listing/"
                      f"corporate-filings-announcements?symbol={sym}"),
                category=(a.get("subject") or a.get("Categorycode") or ""),
                date=(a.get("sort_date") or a.get("an_dt") or ""),
                has_attachment=bool(att)
            ))
        return out
    except Exception as e:
        log.debug(f"NSE {sym}: {e}")
        return []

def fetch_bse_filings(code):
    if not code: return []
    out = []
    for dur in ["D","W"]:
        try:
            r = requests.get(
                f"https://api.bseindia.com/BseIndiaAPI/api/"
                f"AnnGetAnnouncementDet/w?scripcd={code}&dur={dur}",
                headers=BSE_H, timeout=15)
            if not r.ok: continue
            for a in (r.json().get("Table") or [])[:20]:
                t = (a.get("HEADLINE") or a.get("NEWSSUB") or "").strip()
                if not t: continue
                att = a.get("ATTACHMENTNAME") or ""
                out.append(dict(
                    title=t,
                    link=(f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{att}"
                          if att else
                          f"https://www.bseindia.com/corporates/ann.html?scripcd={code}"),
                    category=(a.get("CATEGORYNAME") or ""),
                    date=(a.get("NEWS_DT") or a.get("DTIME") or ""),
                    has_attachment=bool(att)
                ))
            if dur == "D" and out: break
        except Exception as e:
            log.debug(f"BSE {code}: {e}")
    return out

def fetch_bse_ca(code):
    if not code: return []
    try:
        r = requests.get(
            f"https://api.bseindia.com/BseIndiaAPI/api/"
            f"DefaultData/w?scripcd={code}&type=CA",
            headers=BSE_H, timeout=12)
        if not r.ok: return []
        out = []
        for row in (r.json().get("Table") or [])[:5]:
            p = (row.get("PURPOSE") or "").strip()
            if not p: continue
            ex = row.get("EX_DATE") or row.get("EXDATE") or ""
            rc = row.get("REC_DATE") or ""
            t  = p + (f" | Ex-Date: {ex}" if ex else "") + \
                      (f" | Record Date: {rc}" if rc else "")
            out.append(dict(
                title=t,
                link=f"https://www.bseindia.com/stock-share-price/corporate-actions/{code}",
                category="Corporate Action", date=ex, has_attachment=False
            ))
        return out
    except Exception as e:
        log.debug(f"BSE CA {code}: {e}")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# AI ANALYSIS — with anti-hallucination guardrails
# ─────────────────────────────────────────────────────────────────────────────
# Prompt for HIGH confidence filings (with real content)
PROMPT_RICH = """You are a senior equity research analyst at a top Indian institutional fund.

━━ FILING ━━
Headline: "{title}"
Company: {company} | Sector: {sector} | Filing Type: {category}

━━ FILING CONTENT (from document) ━━
{pdf_text}

━━ LIVE MARKET DATA ━━
{market_ctx}

━━ STRICT RULES — MUST FOLLOW ━━
1. ONLY cite numbers that appear in the filing content above
2. NEVER invent EPS growth %, target prices, or P/E multiples unless stated in the filing
3. If the filing content is empty or vague, say "filing details not yet disclosed"
4. Base volume/price analysis ONLY on the live market data provided

Respond ONLY with JSON:
{{"summary":"3-4 sentences based ONLY on disclosed filing content — fundamental impact, institutional angle","volume_signal":"1 sentence from actual volume data above","price_setup":"1 sentence from actual 52w data above","sector_view":"1 sentence macro context","sentiment":"bullish OR bearish OR neutral","impact":"high OR medium OR low","action":"BUY MORE OR HOLD OR WATCH OR REDUCE OR AVOID","horizon":"short_term OR medium_term OR long_term","reason":"Specific actionable sentence — use price levels ONLY if they appear in the filing"}}"""

# Prompt for LOW confidence filings (title only, no content)
PROMPT_THIN = """You are a senior equity research analyst. A filing notification has arrived but its content is NOT YET DISCLOSED.

━━ FILING ━━
Headline: "{title}"
Company: {company} | Sector: {sector} | Filing Type: {category}

━━ LIVE MARKET DATA ━━
{market_ctx}

━━ STRICT RULES — MANDATORY ━━
1. This is ONLY a filing notification — the actual content is unknown
2. DO NOT invent numbers, EPS growth, target prices, or P/E multiples
3. DO NOT pretend to know what was discussed or decided
4. Analyse ONLY what filing TYPE typically means + the market data provided
5. Be honest that specific outcomes are not yet known

Respond ONLY with JSON:
{{"summary":"2-3 sentences — explain what this TYPE of filing typically signals, state clearly that specific outcomes are not yet disclosed","volume_signal":"1 sentence from actual volume data — does volume suggest anticipation?","price_setup":"1 sentence from 52w data — technical context","sector_view":"1 sentence macro context","sentiment":"bullish OR bearish OR neutral","impact":"high OR medium OR low","action":"BUY MORE OR HOLD OR WATCH OR REDUCE OR AVOID","horizon":"short_term OR medium_term OR long_term","reason":"Cautious actionable sentence — wait for filing details before acting"}}"""

def _parse(text):
    text = re.sub(r"```json\n?|```", "", text).strip()
    m = re.search(r"\{[\s\S]+?\}", text)
    if not m: return None
    try:
        r = json.loads(m.group())
        r["sentiment"] = r.get("sentiment","neutral").lower()
        r["impact"]    = r.get("impact","medium").lower()
        r["action"]    = r.get("action","WATCH").upper()
        r["horizon"]   = r.get("horizon","medium_term").lower()
        if r["sentiment"] not in ["bullish","bearish","neutral"]: r["sentiment"]="neutral"
        if r["impact"]    not in ["high","medium","low"]:         r["impact"]="medium"
        return r
    except: return None

def call_groq(prompt, importance):
    model = GROQ_MODEL_HIGH if importance == "HIGH" else GROQ_MODEL_STD
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={
                "model": model,
                "messages":[
                    {"role":"system","content":
                     "Expert Indian institutional equity analyst. "
                     "NEVER invent specific numbers not in the filing. "
                     "JSON only. No markdown."},
                    {"role":"user","content":prompt}
                ],
                "temperature":0.1,
                "max_tokens":500
            },
            timeout=30
        )
        if not r.ok:
            log.warning(f"Groq {r.status_code}: {r.text[:100]}")
            return None
        return _parse(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        log.warning(f"Groq: {e}")
        return None

def call_gemini(prompt):
    for model in ["gemini-2.0-flash","gemini-1.5-flash"]:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={GEMINI_API_KEY}",
                json={"contents":[{"parts":[{"text":prompt}]}],
                      "generationConfig":{"temperature":0.1,"maxOutputTokens":500}},
                timeout=25)
            if not r.ok: continue
            res = _parse(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            if res: return res
        except Exception as e:
            log.warning(f"Gemini {model}: {e}")
    return None

def ai_analyze(title, company, sector, cat_label, importance, nse_sym, filing_link, has_attachment):
    # 1. Fetch live market data
    q      = fetch_quote(nse_sym) if nse_sym else None
    mctx   = fmt_quote(q)

    # 2. Try to extract PDF content for richer analysis
    pdf_text = ""
    if has_attachment:
        extracted = extract_filing_text(filing_link)
        if extracted:
            pdf_text = extracted
            log.info(f"  PDF extracted: {len(pdf_text)} chars")

    # 3. Determine confidence and pick appropriate prompt
    confidence = get_filing_confidence(title, cat_label)
    if pdf_text or confidence == "HIGH":
        prompt = PROMPT_RICH.format(
            title=title, company=company, sector=sector, category=cat_label,
            pdf_text=pdf_text if pdf_text else "Not available — base analysis on filing type and market data only.",
            market_ctx=mctx
        )
    else:
        # Thin filing — use anti-hallucination prompt
        prompt = PROMPT_THIN.format(
            title=title, company=company, sector=sector, category=cat_label,
            market_ctx=mctx
        )
        confidence = "LOW"

    # 4. Call AI
    result = None
    if GROQ_API_KEY:   result = call_groq(prompt, importance)
    if not result and GEMINI_API_KEY: result = call_gemini(prompt)

    if result:
        result["_quote"]      = q
        result["_mctx"]       = mctx
        result["_confidence"] = confidence
        result["_has_pdf"]    = bool(pdf_text)
        log.info(f"  AI: {result['sentiment']} | {result['action']} | conf={confidence}")
    return result

# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE BUILDER
# ─────────────────────────────────────────────────────────────────────────────
SE = {"bullish":"🟢","bearish":"🔴","neutral":"🟡"}
IE = {"high":"🔥","medium":"⚡","low":"💧"}
AE = {"BUY MORE":"🚀","HOLD":"✋","WATCH":"👀","REDUCE":"⚠️","AVOID":"🚫"}
HZ = {"short_term":"⚡ Short-Term","medium_term":"📆 Medium-Term","long_term":"🏔 Long-Term"}
CF = {"HIGH":"🟢 HIGH — based on disclosed content",
      "MEDIUM":"🟡 MEDIUM — partial content available",
      "LOW":"🔴 LOW — filing content not yet disclosed"}

def now_ist():
    return datetime.now(IST).strftime("%d %b %Y · %I:%M %p IST")

def build_msg(stock, source, filing, cat_label, importance, ai):
    ce  = "📊" if stock["cat"] == "PORTFOLIO" else "👁"
    itg = {"HIGH":"🔴 HIGH","MEDIUM":"🟡 MEDIUM","LOW":"🟢 LOW"}.get(importance,"🟡")
    _, mkt_status = get_market_status()
    urg_tag, _   = get_urgency(filing["title"])

    L = ["━"*24,
         f"🏛 <b>{source} OFFICIAL FILING</b>",
         "━"*24,
         f"{ce} <b>{stock['cat']}</b>  ·  <code>{stock['ticker']}</code>",
         f"🏢 <b>{stock['name']}</b>  |  🏭 {stock['sector']}",
         f"🏷 {cat_label}  ·  {itg}",
         ""]

    ah = market_flag(mkt_status)
    if ah: L += [ah, ""]
    if urg_tag: L += [urg_tag, ""]

    L += [f"📄 <b>{filing['title']}</b>", ""]

    # P&L for portfolio stocks
    if stock["cat"] == "PORTFOLIO" and ai and ai.get("_quote"):
        pnl = fmt_pnl(stock, ai["_quote"].get("ltp"))
        if pnl: L += [pnl, ""]

    # Market data
    if ai and ai.get("_mctx") and "unavailable" not in ai["_mctx"]:
        L += ["📈 <b>Live Market Data</b>",
              f"<code>{ai['_mctx']}</code>", ""]

    if ai:
        conf = ai.get("_confidence","MEDIUM")
        pdf  = ai.get("_has_pdf", False)
        L += [
            f"🏦 <b>Institutional Analysis</b>",
            f"🔬 AI Confidence: {CF.get(conf,conf)}"
            + (" | 📎 Based on filing PDF" if pdf else ""),
            "─"*22,
            f"📝 {ai.get('summary','')}",
            "",
            f"📊 <b>Volume Signal:</b> {ai.get('volume_signal','')}",
            f"🎯 <b>Price Setup:</b> {ai.get('price_setup','')}",
            f"🌐 <b>Sector View:</b> {ai.get('sector_view','')}",
            "",
            f"{SE.get(ai['sentiment'],'🟡')} Sentiment: <b>{ai['sentiment'].capitalize()}</b>",
            f"{IE.get(ai['impact'],'⚡')} Impact: <b>{ai['impact'].capitalize()}</b>",
            f"{AE.get(ai['action'],'👀')} Signal: <b>{ai['action']}</b>",
            f"⏳ Horizon: <b>{HZ.get(ai['horizon'],ai['horizon'])}</b>",
            f"💡 {ai.get('reason','')}",
            "",
            "⚠️ <i>AI analysis is informational only. Verify before acting. Not financial advice.</i>",
            "",
        ]
    else:
        L += ["⚠️ <i>Add GROQ_API_KEY in Railway for AI analysis</i>",""]

    if filing.get("date"): L.append(f"📅 Filed: {filing['date']}")
    L += [f"🔗 <a href=\"{filing['link']}\">View Filing on {source}</a>",
          f"⏰ {now_ist()}"]
    return "\n".join(L)

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────
def send_tg(text, retries=3):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(text); return False
    for i in range(retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id":CHAT_ID,"text":text[:4000],
                      "parse_mode":"HTML","disable_web_page_preview":True},
                timeout=12)
            if r.ok: return True
            if r.status_code == 429:
                wait = r.json().get("parameters",{}).get("retry_after",30)
                time.sleep(wait); continue
            log.error(f"TG {r.status_code}: {r.text[:100]}")
            return False
        except Exception as e:
            log.error(f"TG: {e}"); time.sleep(5)
    return False

# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY DIGEST
# ─────────────────────────────────────────────────────────────────────────────
def maybe_weekly_digest(conn):
    now = datetime.now(IST)
    if now.weekday() != 0 or now.hour != 8: return
    wid = now.strftime("%Y-W%W")
    if conn.execute("SELECT 1 FROM weekly_digest_sent WHERE week_id=?",(wid,)).fetchone(): return
    rows = conn.execute(
        "SELECT ticker,title,source FROM sent_filings WHERE sent_at>? ORDER BY ticker,sent_at DESC",
        (int(time.time())-7*86400,)).fetchall()
    if not rows: return
    by = {}
    for ticker,title,source in rows:
        by.setdefault(ticker,[]).append(f"  [{source}] {title[:55]}")
    lines = ["━"*24,"📋 <b>WEEKLY FILING DIGEST</b>",
             f"Week ending {now.strftime('%d %b %Y')}","━"*24,
             f"Total: <b>{len(rows)}</b> filings across <b>{len(by)}</b> stocks",""]
    for t in sorted(by):
        evs = by[t]
        lines.append(f"<b>{t}</b> ({len(evs)} filing{'s' if len(evs)>1 else ''})")
        lines.extend(evs[:4])
        lines.append("")
    lines += ["━"*24,"Happy trading this week! 🚀"]
    send_tg("\n".join(lines))
    conn.execute("INSERT OR IGNORE INTO weekly_digest_sent VALUES (?,?)",(wid,int(time.time())))
    conn.commit()
    log.info("Weekly digest sent ✅")

# ─────────────────────────────────────────────────────────────────────────────
# PROCESS STOCK
# ─────────────────────────────────────────────────────────────────────────────
def process(stock, conn):
    sent = 0
    all_f = []
    if stock.get("nse"):
        all_f += [("NSE",f) for f in fetch_nse_filings(stock["nse"])]
        time.sleep(0.8)
    if stock.get("bse"):
        all_f += [("BSE",f) for f in fetch_bse_filings(stock["bse"])]
        all_f += [("BSE",f) for f in fetch_bse_ca(stock["bse"])]
        time.sleep(0.5)
    for source,f in all_f:
        if not is_recent(f.get("date","")): continue
        cat_label,importance = classify(f["title"],f.get("category",""))
        if cat_label is None: continue
        if is_dup(conn,stock,f["title"],source): continue
        if is_semantic_dup(conn,stock,f["title"],source): continue
        mark(conn,stock,f["title"],source)
        log.info(f"  [{source}][{stock['ticker']}][{importance}] {f['title'][:65]}")
        ai = ai_analyze(
            f["title"], stock["name"], stock["sector"], cat_label,
            importance, stock.get("nse"), f["link"],
            f.get("has_attachment", False)
        )
        msg = build_msg(stock,source,f,cat_label,importance,ai)
        if send_tg(msg):
            sent += 1
            time.sleep(1.5)
    return sent

# ─────────────────────────────────────────────────────────────────────────────
# CYCLE + STARTUP + MAIN
# ─────────────────────────────────────────────────────────────────────────────
def run_cycle(conn):
    maybe_weekly_digest(conn)
    log.info(f"━━ Cycle {datetime.now(IST).strftime('%H:%M:%S IST')} ━━")
    total = 0
    for stock in ALL_STOCKS:
        try: total += process(stock,conn)
        except Exception as e:
            log.error(f"{stock['ticker']}: {e}"); log_err(conn,str(e))
    log.info(f"━━ Done. {total} sent ━━\n")

def send_startup():
    ai = (f"✅ Groq (70B/HIGH · 8B/others) + anti-hallucination" if GROQ_API_KEY else
          f"✅ Gemini" if GEMINI_API_KEY else "❌ Add GROQ_API_KEY")
    msg = (
        f"{'━'*24}\n🚀 <b>StockPilot Bot v3.7</b>\n{'━'*24}\n"
        f"⏰ {datetime.now(IST).strftime('%d %b %Y · %I:%M %p IST')}\n\n"
        f"📊 Portfolio: {len(PORTFOLIO)} | 👁 Watchlist: {len(WATCHLIST)}\n\n"
        f"✅ <b>v3.7 improvements:</b>\n"
        f"  🛡 Anti-hallucination guardrails\n"
        f"  🔬 AI confidence scoring (HIGH/MED/LOW)\n"
        f"  📎 PDF content extraction for richer analysis\n"
        f"  ⚠️ Disclaimer on all AI alerts\n"
        f"  🧠 Separate prompts for rich vs thin filings\n\n"
        f"🤖 AI: {ai}\n🔄 Every {CHECK_INTERVAL//60} min\n{'━'*24}"
    )
    send_tg(msg)

def main():
    validate_config()
    log.info("StockPilot Bot v3.7 starting…")
    conn = init_db()
    nse.warm()
    send_startup()
    errs = 0
    while True:
        try:
            run_cycle(conn)
            errs = 0
        except KeyboardInterrupt:
            break
        except Exception as e:
            errs += 1
            log.error(f"Cycle #{errs}: {e}", exc_info=True)
            log_err(conn,str(e))
            if errs >= 5:
                send_tg(f"⚠️ 5 errors\nLast: {str(e)[:200]}")
                errs = 0
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
