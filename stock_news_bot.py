#!/usr/bin/env python3
"""
StockPilot NSE/BSE Filing Bot v3.6
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v3.6 — ALL LOOPHOLES FIXED:
  ✅ After-hours flag (🌙 impacts tomorrow's open)
  ✅ Ex-date urgency (🚨 URGENT < 3 days, ⏰ < 7 days)
  ✅ IZMO duplication fixed (NSE-symbol-level dedup)
  ✅ P&L context in every portfolio alert
  ✅ 70B model for HIGH importance, 8B for MEDIUM/LOW
  ✅ Weekly Monday digest (8 AM IST)
  ✅ Semantic dedup (same company, similar title, 30 min window)
  ✅ All Priority 1 + 2 improvements implemented
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

# Groq models — 70B for HIGH impact, 8B for others
GROQ_MODEL_HIGH = "llama-3.1-70b-versatile"
GROQ_MODEL_STD  = "llama-3.1-8b-instant"

def validate_config():
    missing = []
    if not TELEGRAM_TOKEN: missing.append("TELEGRAM_TOKEN")
    if not CHAT_ID:        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        log.error(f"Missing env vars: {', '.join(missing)}")
        sys.exit(1)
    if GROQ_API_KEY:
        log.info(f"Groq AI ready ✅ (70B for HIGH | 8B for others)")
    elif GEMINI_API_KEY:
        log.info("Gemini AI ready ✅")
    else:
        log.warning("No AI key — add GROQ_API_KEY for institutional analysis")

# ─────────────────────────────────────────────────────────────────────────────
# DATE UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
    "%d %b %Y", "%b %d, %Y",
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
# MARKET HOURS & AFTER-HOURS FLAG
# ─────────────────────────────────────────────────────────────────────────────
def get_market_status():
    """
    Returns (is_open: bool, label: str)
    label = LIVE | AFTER_HOURS | PRE_MARKET | WEEKEND
    """
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False, "WEEKEND"
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now < open_t:
        return False, "PRE_MARKET"
    if now > close_t:
        return False, "AFTER_HOURS"
    return True, "LIVE"

def market_flag(status_label):
    flags = {
        "LIVE":        "",
        "AFTER_HOURS": "\n🌙 <b>AFTER HOURS</b> — This filing will impact tomorrow's opening price",
        "PRE_MARKET":  "\n🌅 <b>PRE-MARKET</b> — Market opens in {mins} min — watch for gap up/down",
        "WEEKEND":     "\n📅 <b>WEEKEND FILING</b> — Will impact Monday's opening price",
    }
    label = flags.get(status_label, "")
    if status_label == "PRE_MARKET":
        now  = datetime.now(IST)
        open_t = now.replace(hour=9, minute=15, second=0)
        mins = max(0, int((open_t - now).total_seconds() / 60))
        label = label.format(mins=mins)
    return label

# ─────────────────────────────────────────────────────────────────────────────
# EX-DATE URGENCY DETECTOR
# ─────────────────────────────────────────────────────────────────────────────
def get_urgency(title):
    """
    Parses ex-date or record date from filing title.
    Returns (urgency_tag, days_left) or (None, None).
    """
    patterns = [
        r'ex.?date[:\s]+(\d{1,2}[-/\s]\w+[-/\s]\d{2,4})',
        r'ex.?date[:\s]+(\d{4}-\d{2}-\d{2})',
        r'record\s+date[:\s]+(\d{1,2}[-/\s]\w+[-/\s]\d{2,4})',
        r'(\d{1,2}[-/]\w{3}[-/]\d{4})',  # generic date in title
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            dt = parse_date(m.group(1))
            if dt:
                days_left = (dt.date() - datetime.now(IST).date()).days
                if days_left < 0: continue  # already passed
                if days_left <= 2:
                    return f"🚨 <b>URGENT — Ex-Date in {days_left} day{'s' if days_left != 1 else ''}!</b>", days_left
                if days_left <= 7:
                    return f"⏰ <b>UPCOMING — Ex-Date in {days_left} days</b>", days_left
    return None, None

# ─────────────────────────────────────────────────────────────────────────────
# STOCKS — with avg buy price + qty for P&L context
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
    # ── Original ──────────────────────────────────────────────────────────
    dict(ticker="JAINRESOUR", name="Jain Resource Recycl",   nse=None,          bse="533289", sector="Recycling",           cat="WATCHLIST"),
    dict(ticker="IREDA",      name="Indian Renewable Energy", nse="IREDA",       bse="544124", sector="Renewable Energy",    cat="WATCHLIST"),
    # NOTE: IZMO removed from watchlist — already in PORTFOLIO (same NSE symbol = duplicate alerts)
    dict(ticker="ONEGLOBAL",  name="One Global Service",      nse="ONEGLOBAL",   bse=None,     sector="Business Services",   cat="WATCHLIST"),
    dict(ticker="DOMS",       name="DOMS Industries",         nse="DOMS",        bse="544045", sector="Consumer Stationery", cat="WATCHLIST"),
    dict(ticker="LANCER",     name="Lancer Container",        nse=None,          bse="526807", sector="Packaging",           cat="WATCHLIST"),
    # ── Turnaround Stocks ─────────────────────────────────────────────────
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
    if any(s in combined for s in SKIP):
        return None, None
    for kw, (label, imp) in CATEGORIES.items():
        if kw.lower() in combined:
            return label, imp
    return "📢 Corporate Filing", "MEDIUM"

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS sent_filings (
        hash       TEXT PRIMARY KEY,
        ticker     TEXT,
        nse_sym    TEXT,
        title      TEXT,
        source     TEXT,
        sent_at    INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg TEXT, ts INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS weekly_digest_sent (
        week_id TEXT PRIMARY KEY,
        sent_at INTEGER
    )""")
    conn.execute("DELETE FROM sent_filings WHERE sent_at < ?",
                 (int(time.time()) - 30*86400,))
    conn.commit()
    log.info("Database ready ✅")
    return conn

def _h(nse_sym_or_ticker, title, source):
    """
    FIX: Use NSE symbol (not ticker) as dedup key.
    IZMO (portfolio) and IZMOWATCH (watchlist) share nse="IZMO"
    so the second one is correctly skipped.
    """
    key = f"{source}:{nse_sym_or_ticker}:{title.strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()

def is_dup(conn, stock, title, source):
    dedup_key = stock.get("nse") or stock["ticker"]
    return conn.execute(
        "SELECT 1 FROM sent_filings WHERE hash=?",
        (_h(dedup_key, title, source),)
    ).fetchone() is not None

def mark(conn, stock, title, source):
    dedup_key = stock.get("nse") or stock["ticker"]
    conn.execute(
        "INSERT OR IGNORE INTO sent_filings VALUES (?,?,?,?,?,?)",
        (_h(dedup_key, title, source),
         stock["ticker"], dedup_key, title[:200],
         source, int(time.time()))
    )
    conn.commit()

def log_err(conn, msg):
    conn.execute("INSERT INTO errors VALUES (NULL,?,?)",
                 (msg[:500], int(time.time())))
    conn.commit()

def is_semantic_dup(conn, stock, title, source):
    """
    Checks if same company filed something very similar in last 30 min
    from the OTHER exchange (NSE vs BSE cross-dedup).
    Prevents double-alerts for same board meeting on both exchanges.
    """
    dedup_key = stock.get("nse") or stock["ticker"]
    cutoff = int(time.time()) - 1800
    recent = conn.execute(
        "SELECT title FROM sent_filings WHERE nse_sym=? AND sent_at>? AND source!=?",
        (dedup_key, cutoff, source)
    ).fetchall()
    if not recent: return False
    title_words = set(re.findall(r'\w+', title.lower()))
    for (rt,) in recent:
        rt_words = set(re.findall(r'\w+', rt.lower()))
        if not title_words: continue
        overlap = len(title_words & rt_words) / len(title_words)
        if overlap > 0.75:
            log.debug(f"  Semantic dup skipped ({overlap:.0%} overlap): {title[:50]}")
            return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# NSE SESSION
# ─────────────────────────────────────────────────────────────────────────────
class NSESession:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"),
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://www.nseindia.com/",
        })
        self.warmed = False
        self._last = 0

    def warm(self):
        try:
            self.s.get("https://www.nseindia.com/", timeout=15)
            time.sleep(2)
            self.s.get("https://www.nseindia.com/companies-listing/"
                       "corporate-filings-announcements", timeout=12)
            time.sleep(1)
            self.warmed = True
            self._last = time.time()
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
# NSE LIVE QUOTE (price + volume for AI context + P&L)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_quote(nse_sym):
    if not nse_sym: return None
    r = nse.get(f"https://www.nseindia.com/api/quote-equity?symbol={nse_sym}")
    if not r or not r.ok: return None
    try:
        d   = r.json()
        pi  = d.get("priceInfo", {})
        ti  = d.get("tradedInfo", {})
        whl = pi.get("weekHighLow", {})
        ltp       = pi.get("lastPrice")
        chg_pct   = pi.get("pChange")
        w52_hi    = whl.get("max")
        w52_lo    = whl.get("min")
        vol_today = ti.get("totalTradedVolume")
        avg_vol   = ti.get("tottrdqty")
        range_pct = None
        vol_ratio = None
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

def fmt_market_ctx(q):
    if not q: return "Live data unavailable"
    ltp  = f"₹{q['ltp']}"      if q.get("ltp")     else "N/A"
    chg  = f"{q['chg_pct']}%"  if q.get("chg_pct") else "N/A"
    hi52 = f"₹{q['w52_hi']}"   if q.get("w52_hi")  else "N/A"
    lo52 = f"₹{q['w52_lo']}"   if q.get("w52_lo")  else "N/A"

    if q.get("range_pct") is not None:
        rp = q["range_pct"]
        zone = "near 52w HIGH — extended" if rp > 70 else \
               "near 52w LOW — value zone" if rp < 30 else "mid-range"
        rng = f"{rp}% above 52w low ({zone})"
    else:
        rng = "N/A"

    if q.get("vol_ratio") is not None:
        vr = q["vol_ratio"]
        vsig = "HIGH — possible institutional activity" if vr > 150 else \
               "LOW — limited institutional interest" if vr < 50 else "normal"
        vol = f"{int(q['vol_today']):,} shares ({vr}% of annual avg — {vsig})"
    else:
        vol = f"{q.get('vol_today','N/A')} shares"

    return (f"Price: {ltp} ({chg} today)\n"
            f"52w Range: {lo52} → {hi52} | Position: {rng}\n"
            f"Volume: {vol}")

def fmt_pnl(stock, ltp):
    """Calculate and format P&L for portfolio stocks."""
    if not stock.get("avg") or not stock.get("qty") or not ltp:
        return None
    avg = stock["avg"]; qty = stock["qty"]
    invested = qty * avg
    current  = qty * float(ltp)
    pnl      = current - invested
    pnl_pct  = (pnl / invested) * 100
    arrow    = "📈" if pnl >= 0 else "📉"
    sign     = "+" if pnl >= 0 else ""
    return (f"{arrow} <b>Your Position:</b> {qty} shares @ ₹{avg:,.2f} avg\n"
            f"   Invested: ₹{invested:,.0f} → Now: ₹{current:,.0f}\n"
            f"   P&amp;L: <b>{sign}₹{pnl:,.0f} ({sign}{pnl_pct:.1f}%)</b>")

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
                date=(a.get("sort_date") or a.get("an_dt") or "")
            ))
        return out
    except Exception as e:
        log.debug(f"NSE filings {sym}: {e}")
        return []

def fetch_bse_filings(code):
    if not code: return []
    out = []
    for dur in ["D", "W"]:
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
                    date=(a.get("NEWS_DT") or a.get("DTIME") or "")
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
                category="Corporate Action", date=ex
            ))
        return out
    except Exception as e:
        log.debug(f"BSE CA {code}: {e}")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# AI — Institutional prompt | 70B for HIGH | 8B for others
# ─────────────────────────────────────────────────────────────────────────────
INST_PROMPT = """You are a senior equity research analyst at a top Indian institutional fund.

━━ FILING ━━
"{title}"
Company: {company} | Sector: {sector} | Type: {category}

━━ LIVE MARKET DATA ━━
{market_context}

━━ TASK ━━
Analyze like a fund manager. Cover:
1. Fundamental impact (revenue/margins/EPS effect)
2. Re-rating trigger? (P/E expansion or compression)
3. What would FIIs/DIIs do — accumulate, hold, or trim?
4. Volume vs annual average — smart money signal?
5. Price position in 52w range — breakout catalyst or risk?

Respond ONLY with JSON (no markdown, no backticks):
{{"summary":"3-4 sentences institutional analysis — fundamental impact, re-rating thesis, FII/DII likely action","volume_signal":"1 sentence — volume vs annual average and what it implies about institutional interest","price_setup":"1 sentence — 52w range position and whether this filing is a breakout catalyst","sector_view":"1 sentence — comparison to sector peers or macro trend","sentiment":"bullish OR bearish OR neutral","impact":"high OR medium OR low","action":"BUY MORE OR HOLD OR WATCH OR REDUCE OR AVOID","horizon":"short_term OR medium_term OR long_term","reason":"One specific actionable sentence with price levels or % where possible"}}"""

def _parse_ai(text):
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

def groq_analyze(title, company, sector, category, mctx, importance):
    if not GROQ_API_KEY: return None
    # Use 70B for HIGH importance filings — much better analysis
    model = GROQ_MODEL_HIGH if importance == "HIGH" else GROQ_MODEL_STD
    prompt = INST_PROMPT.format(title=title, company=company, sector=sector,
                                 category=category, market_context=mctx)
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role":"system","content":"Expert Indian institutional stock analyst. JSON only. No markdown."},
                    {"role":"user","content":prompt}
                ],
                "temperature": 0.2,
                "max_tokens": 500
            },
            timeout=30
        )
        if not r.ok:
            log.warning(f"Groq({model}) {r.status_code}: {r.text[:150]}")
            return None
        content = r.json()["choices"][0]["message"]["content"]
        result  = _parse_ai(content)
        if result:
            log.info(f"  AI({model.split('-')[-1]}): {result['sentiment']} | {result['action']}")
        return result
    except Exception as e:
        log.warning(f"Groq: {e}")
        return None

def gemini_analyze(title, company, sector, category, mctx, importance):
    if not GEMINI_API_KEY: return None
    prompt = INST_PROMPT.format(title=title, company=company, sector=sector,
                                 category=category, market_context=mctx)
    for model in ["gemini-2.0-flash","gemini-1.5-flash"]:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={GEMINI_API_KEY}",
                json={"contents":[{"parts":[{"text":prompt}]}],
                      "generationConfig":{"temperature":0.2,"maxOutputTokens":500}},
                timeout=25)
            if not r.ok: continue
            text   = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            result = _parse_ai(text)
            if result: return result
        except Exception as e:
            log.warning(f"Gemini {model}: {e}")
    return None

def ai_analyze(title, company, sector, category, nse_sym, importance):
    q    = fetch_quote(nse_sym) if nse_sym else None
    mctx = fmt_market_ctx(q)
    res  = None
    if GROQ_API_KEY:
        res = groq_analyze(title, company, sector, category, mctx, importance)
    if not res and GEMINI_API_KEY:
        res = gemini_analyze(title, company, sector, category, mctx, importance)
    if res:
        res["_quote"]  = q
        res["_mctx"]   = mctx
    return res

# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE BUILDER — full institutional format
# ─────────────────────────────────────────────────────────────────────────────
SE = {"bullish":"🟢","bearish":"🔴","neutral":"🟡"}
IE = {"high":"🔥","medium":"⚡","low":"💧"}
AE = {"BUY MORE":"🚀","HOLD":"✋","WATCH":"👀","REDUCE":"⚠️","AVOID":"🚫"}
HZ = {"short_term":"⚡ Short-Term","medium_term":"📆 Medium-Term","long_term":"🏔 Long-Term"}

def now_ist():
    return datetime.now(IST).strftime("%d %b %Y · %I:%M %p IST")

def build_msg(stock, source, filing, cat_label, importance, ai):
    ce  = "📊" if stock["cat"] == "PORTFOLIO" else "👁"
    itg = {"HIGH":"🔴 HIGH","MEDIUM":"🟡 MEDIUM","LOW":"🟢 LOW"}.get(importance,"🟡")
    _, mkt_status = get_market_status()
    after_hours   = market_flag(mkt_status)
    urgency_tag, days_left = get_urgency(filing["title"])

    L = ["━"*24,
         f"🏛 <b>{source} OFFICIAL FILING</b>",
         "━"*24,
         f"{ce} <b>{stock['cat']}</b>  ·  <code>{stock['ticker']}</code>",
         f"🏢 <b>{stock['name']}</b>  |  🏭 {stock['sector']}",
         f"🏷 {cat_label}  ·  {itg}",
         ""]

    # After-hours flag
    if after_hours:
        L.append(after_hours)
        L.append("")

    # Urgency flag
    if urgency_tag:
        L.append(urgency_tag)
        L.append("")

    L += [f"📄 <b>{filing['title']}</b>", ""]

    # P&L context for portfolio stocks
    if stock["cat"] == "PORTFOLIO" and ai and ai.get("_quote"):
        pnl_line = fmt_pnl(stock, ai["_quote"].get("ltp"))
        if pnl_line:
            L.append(pnl_line)
            L.append("")

    # Market context
    if ai and ai.get("_mctx") and ai["_mctx"] != "Live data unavailable":
        L += ["📈 <b>Live Market Data</b>",
              f"<code>{ai['_mctx']}</code>", ""]

    # Institutional AI analysis
    if ai:
        L += ["🏦 <b>Institutional Analysis</b>",
              "─"*22,
              f"📝 {ai.get('summary','')}",
              "",
              f"📊 <b>Volume Signal:</b> {ai.get('volume_signal','')}",
              f"🎯 <b>Price Setup:</b> {ai.get('price_setup','')}",
              f"🌐 <b>Sector View:</b> {ai.get('sector_view','')}",
              "",
              f"{SE.get(ai['sentiment'],'🟡')} Sentiment: <b>{ai['sentiment'].capitalize()}</b>",
              f"{IE.get(ai['impact'],'⚡')} Market Impact: <b>{ai['impact'].capitalize()}</b>",
              f"{AE.get(ai['action'],'👀')} Action Signal: <b>{ai['action']}</b>",
              f"⏳ Horizon: <b>{HZ.get(ai['horizon'], ai['horizon'])}</b>",
              f"💡 {ai.get('reason','')}",
              ""]
    else:
        L += ["⚠️ <i>Add GROQ_API_KEY in Railway for AI analysis</i>", ""]

    if filing.get("date"):
        L.append(f"📅 Filed: {filing['date']}")
    L += [f"🔗 <a href=\"{filing['link']}\">View on {source}</a>",
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
                json={"chat_id":CHAT_ID, "text":text[:4000],
                      "parse_mode":"HTML", "disable_web_page_preview":True},
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
# WEEKLY DIGEST — every Monday 8 AM IST
# ─────────────────────────────────────────────────────────────────────────────
def maybe_send_weekly_digest(conn):
    now = datetime.now(IST)
    if now.weekday() != 0 or now.hour != 8: return  # Monday 8 AM only
    week_id = now.strftime("%Y-W%W")
    if conn.execute("SELECT 1 FROM weekly_digest_sent WHERE week_id=?",
                    (week_id,)).fetchone(): return

    week_ago = int(time.time()) - 7 * 86400
    rows = conn.execute(
        "SELECT ticker, title, source FROM sent_filings WHERE sent_at > ? ORDER BY ticker, sent_at DESC",
        (week_ago,)
    ).fetchall()

    if not rows:
        return

    by_ticker = {}
    for ticker, title, source in rows:
        by_ticker.setdefault(ticker, []).append(f"  [{source}] {title[:55]}")

    lines = [
        "━"*24,
        "📋 <b>WEEKLY FILING DIGEST</b>",
        f"Week ending {now.strftime('%d %b %Y')}",
        "━"*24,
        f"Total filings this week: <b>{len(rows)}</b>",
        f"Stocks with activity: <b>{len(by_ticker)}</b>",
        "",
    ]
    for ticker in sorted(by_ticker.keys()):
        events = by_ticker[ticker]
        lines.append(f"<b>{ticker}</b> ({len(events)} filing{'s' if len(events)>1 else ''})")
        lines.extend(events[:4])
        lines.append("")

    lines += ["━"*24, "Have a great trading week! 🚀"]
    send_tg("\n".join(lines))
    conn.execute("INSERT OR IGNORE INTO weekly_digest_sent VALUES (?,?)",
                 (week_id, int(time.time())))
    conn.commit()
    log.info("Weekly digest sent ✅")

# ─────────────────────────────────────────────────────────────────────────────
# PROCESS STOCK
# ─────────────────────────────────────────────────────────────────────────────
def process(stock, conn):
    sent = 0
    all_f = []
    if stock.get("nse"):
        all_f += [("NSE", f) for f in fetch_nse_filings(stock["nse"])]
        time.sleep(0.8)
    if stock.get("bse"):
        all_f += [("BSE", f) for f in fetch_bse_filings(stock["bse"])]
        all_f += [("BSE", f) for f in fetch_bse_ca(stock["bse"])]
        time.sleep(0.5)

    for source, f in all_f:
        if not is_recent(f.get("date","")): continue
        cat_label, importance = classify(f["title"], f.get("category",""))
        if cat_label is None: continue
        if is_dup(conn, stock, f["title"], source): continue
        if is_semantic_dup(conn, stock, f["title"], source): continue
        mark(conn, stock, f["title"], source)
        log.info(f"  [{source}][{stock['ticker']}][{importance}] {f['title'][:65]}")
        ai  = ai_analyze(f["title"], stock["name"], stock["sector"],
                         cat_label, stock.get("nse"), importance)
        msg = build_msg(stock, source, f, cat_label, importance, ai)
        if send_tg(msg):
            sent += 1
            time.sleep(1.5)
    return sent

# ─────────────────────────────────────────────────────────────────────────────
# CYCLE
# ─────────────────────────────────────────────────────────────────────────────
def run_cycle(conn):
    maybe_send_weekly_digest(conn)
    log.info(f"━━ Cycle {datetime.now(IST).strftime('%H:%M:%S IST')} ━━")
    total = 0
    for stock in ALL_STOCKS:
        try:
            total += process(stock, conn)
        except Exception as e:
            log.error(f"{stock['ticker']}: {e}")
            log_err(conn, str(e))
    log.info(f"━━ Done. {total} sent ━━\n")

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────
def send_startup():
    ai_info = (
        f"✅ Groq (70B for HIGH / 8B for others)" if GROQ_API_KEY else
        f"✅ Gemini (fallback)" if GEMINI_API_KEY else
        "❌ No AI key — add GROQ_API_KEY"
    )
    msg = (
        f"{'━'*24}\n"
        f"🚀 <b>StockPilot Bot v3.6</b>\n"
        f"{'━'*24}\n"
        f"⏰ {datetime.now(IST).strftime('%d %b %Y · %I:%M %p IST')}\n\n"
        f"📊 Portfolio: {len(PORTFOLIO)} stocks (with avg buy price)\n"
        f"👁 Watchlist: {len(WATCHLIST)} stocks (incl. 18 turnaround)\n\n"
        f"<b>✅ v3.6 loopholes fixed:</b>\n"
        f"  🌙 After-hours filing flag\n"
        f"  🚨 Ex-date urgency (URGENT/UPCOMING)\n"
        f"  📈 P&amp;L context in every portfolio alert\n"
        f"  🏦 70B model for HIGH priority filings\n"
        f"  📋 Weekly digest every Monday 8 AM\n"
        f"  🔄 Smart semantic dedup (NSE+BSE cross)\n"
        f"  ✂️ IZMO watchlist duplicate removed\n\n"
        f"🤖 AI: {ai_info}\n"
        f"🔄 Every {CHECK_INTERVAL//60} min\n"
        f"{'━'*24}"
    )
    send_tg(msg)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    validate_config()
    log.info("StockPilot Bot v3.6 starting…")
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
            log_err(conn, str(e))
            if errs >= 5:
                send_tg(f"⚠️ 5 errors\nLast: {str(e)[:200]}")
                errs = 0
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
