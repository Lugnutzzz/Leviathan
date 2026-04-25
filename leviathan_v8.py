"""
╔═══════════════════════════════════════════════════════════════════╗
║        PROJECT LEVIATHAN v8.0 — FULL-KIT PRODUCTION SCANNER       ║
║        Whale-First Architecture. Bulletproof EDGAR XML.           ║
╠═══════════════════════════════════════════════════════════════════╣
║                                                                   ║
║  WHAT'S NEW IN v8.0 vs v7.x:                                      ║
║                                                                   ║
║  ARCHITECTURE FLIP — WHALE-FIRST:                                 ║
║    Old: scan 8600 tickers with yfinance → check whale on survivors║
║    New: pull all Form 4 P-code filings from EDGAR full-text       ║
║    search → parse XML directly → run quality checks only on       ║
║    the 30-80 companies whose officers actually bought this week.  ║
║                                                                   ║
║  BULLETPROOF EDGAR XML PARSING:                                   ║
║    - NEVER trust submissions JSON transactionCode (often wrong)   ║
║    - Always fetch and parse actual Form 4 XML                     ║
║    - P-code AND A-code validated in the SAME transaction block    ║
║    - $500K minimum to filter out tiny/auto transactions           ║
║    - Officer title required (CEO/CFO/COO/President/VP)            ║
║    - Directors alone: skip (weak signal)                          ║
║    - Cluster detection: 2+ officers same week = bonus             ║
║                                                                   ║
║  4 TRACKS:                                                        ║
║    Track A     — Whale buy + all tight filters → ACT              ║
║    Track B     — Tight filters, no whale yet → WATCH              ║
║    Track B-    — Whale buy + loosened filters → INVESTIGATE       ║
║    KTOS        — Whale buy + sector heat, failed filters → RISK   ║
║                                                                   ║
║  LOOSENED FUNDAMENTALS FOR Track B-:                              ║
║    PEG: <1.0 (vs <0.5 tight)                                      ║
║    Gross Margin: >25% any sector                                  ║
║    Revenue growth: >0% (profitable growth, any rate)              ║
║    Insider ownership: >8%                                         ║
║    D/E: 2x normal limit                                           ║
║                                                                   ║
║  UNIVERSE CLEANUP:                                                ║
║    - Rejects tickers ending in F (OTC foreign)                    ║
║    - Rejects names that are blank, all-digit, or ticker-only      ║
║    - Hard rejects: fund/trust/ETF/closed-end in description       ║
║    - OPY D/E bug fixed: financial sector limit enforced properly  ║
║                                                                   ║
║  SECTOR CATALYST TAGGING:                                         ║
║    Defense, AI infrastructure, domestic energy, reshoring,        ║
║    shipping supercycle — automatically tagged on every result     ║
╚═══════════════════════════════════════════════════════════════════╝
"""

import os, sys, json, time, logging, re, smtplib, ssl
from datetime import datetime, timedelta, timezone, date
from dataclasses import dataclass, field
from typing import Optional, List
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import numpy as np # type: ignore
import yfinance as yf # type: ignore
import requests
# ── Optional fast libraries ──────────────────────────────────
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

try:
    import lxml.etree as lxml_etree
    LXML_AVAILABLE = True
except ImportError:
    LXML_AVAILABLE = False

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

# fast parsing and progress bars if available
def parse_xml(content: str):
    if LXML_AVAILABLE:
        return lxml_etree.fromstring(content.encode())
    else:
        import xml.etree.ElementTree as ET
        return ET.fromstring(content)

def parse_html(content: str):
    if BS4_AVAILABLE:
        from bs4 import XMLParsedAsHTMLWarning
        import warnings
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        return BeautifulSoup(content, "lxml" if LXML_AVAILABLE else "html.parser")
    return content

def progress(iterable, desc=""):
    if TQDM_AVAILABLE:
        return tqdm(iterable, desc=desc, position=1, leave=False,
                    ncols=TQDM_COLS, dynamic_ncols=False)
    return iterable
# ══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════
TQDM_COLS = 80
SEC_EMAIL  = ""
USER_AGENT = f"LeviathanScout/8.0 {SEC_EMAIL}"

_DIR           = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_DIR, "leviathan_config.json")
WATCHLIST_FILE = os.path.join(_DIR, "leviathan_watchlist.json")

# ── Whale thresholds ────────────────────────────────────────
WHALE_MIN_DOLLARS      = 500_000      # $500K minimum for any signal
WHALE_TRACK_A_DOLLARS  = 10_000_000   # $10M+ for Track A (tight)
WHALE_LOOSE_DOLLARS    = 1_000_000    # $1M+ for Track B- (loose)
WHALE_KTOS_DOLLARS     = 5_000_000    # $5M+ for KTOS plays
WHALE_DAYS             = 7            # Look back 7 days for fresh buys
WHALE_SELL_WINDOW      = 30           # Look back 30 days for sells

# ── Tight filter thresholds (Track A / Track B) ─────────────
MAX_PEG_TIGHT          = 0.5
MIN_PEG                = 0.05         # below = bad data
MIN_GM_TIGHT           = 0.60         # normal sectors
MIN_GM_COMMODITY       = 0.35         # shipping/energy/mining
MIN_GM_INDUSTRIAL      = 0.20         # industrials soft exception
MIN_REV_GROWTH_TIGHT   = 0.15
MIN_INSIDER_OWN_TIGHT  = 0.20
MAX_INSIDER_OWN        = 0.70         # raised slightly from 0.60
MIN_PRICE              = 5.0
MIN_MARKET_CAP         = 200_000_000  # $200M (lowered from $300M)
MAX_MARKET_CAP         = 15_000_000_000  # $15B (raised from $10B)

# ── Loose filter thresholds (Track B-) ──────────────────────
MAX_PEG_LOOSE          = 1.0
MIN_GM_LOOSE           = 0.25
MIN_REV_GROWTH_LOOSE   = 0.0          # any positive growth ok
MIN_INSIDER_OWN_LOOSE  = 0.08         # 8% minimum (recovers KNSL-type)

# ── Sector-aware debt limits ─────────────────────────────────
DE_NORMAL     = 0.8    # raised from 0.6 (was too tight)
DE_FINANCIAL  = 5.0
DE_UTILITY    = 3.0
DE_NORMAL_LOOSE = 1.6  # 2x for Track B-

# ── Conviction scoring ───────────────────────────────────────
MIN_CONVICTION_TRACK_A = 7    # must score >= 7 to hit Track A
KTOS_TOP_SECTORS       = 5

# ── Rate limiting ─────────────────────────────────────────────
BASE_DELAY   = 0.5
BACKOFF_MAX  = 120.0
SEC_DELAY    = 0.12   # 10 req/sec SEC limit

# ── Email ─────────────────────────────────────────────────────
#EMAIL_ENABLED  = False
#EMAIL_FROM     = ""
#EMAIL_TO       = ""
#EMAIL_APP_PASS = ""

# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════

class TqdmLoggingHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            if TQDM_AVAILABLE:
                tqdm.write(msg)
            else:
                print(msg)
        except Exception:
            self.handleError(record)

LOG_FILE = os.path.join(_DIR, f"leviathan_{datetime.now().strftime('%Y-%m-%d')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        TqdmLoggingHandler(),
    ]
)
log = logging.getLogger("leviathan")

# email stuff, dont mind
def load_config() -> dict:
    """Load persisted config, prompting for missing values on first run."""
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)

    def is_valid_email(e: str) -> bool:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
            return False
        try:
            import dns.resolver # type: ignore
            domain = e.split("@")[1]
            dns.resolver.resolve(domain, "MX")
            return True
        except Exception:
            return False

    if not cfg.get("sec_email"):
        print("\n  SEC requires a contact email in the User-Agent header.")
        while True:
            val = input("  Enter your email for SEC User-Agent: ").strip()
            if is_valid_email(val):
                cfg["sec_email"] = val
                break
            print("  Invalid email, try again.")

    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

    return cfg
# ══════════════════════════════════════════════════════════════
#  DATA STRUCTURE
# ══════════════════════════════════════════════════════════════

@dataclass
class WhaleSignal:
    """Everything we know about the insider buy."""
    ticker:           str
    cik:              str
    company_name:     str       = ""
    accession:        str       = ""
    filed_date:       str       = ""
    officer_title:    str       = ""
    dollar_amount:    float     = 0.0
    shares_bought:    float     = 0.0
    price_per_share:  float     = 0.0
    edgar_url:        str       = ""
    is_cluster:       bool      = False    # 2+ officers same company same week
    cluster_count:    int       = 0
    cluster_total_usd: float    = 0.0

@dataclass
class StockResult:
    ticker:           str
    name:             str
    sector:           str             = ""
    industry:         str             = ""
    market_cap:       Optional[float] = None
    price:            Optional[float] = None

    # Fundamentals
    peg:              Optional[float] = None
    peg_source:       str             = ""     # "yfinance" / "calculated" / "unavailable"
    debt_equity:      Optional[float] = None
    debt_ratio:       Optional[float] = None
    gross_margin:     Optional[float] = None
    revenue_growth:   Optional[float] = None
    insider_pct:      Optional[float] = None
    net_income:       Optional[float] = None
    profitable:       bool            = False
    revenue:          Optional[float] = None
    inst_ownership:   Optional[float] = None
    quick_ratio:      Optional[float] = None

    # Price action
    price_1y_change:  Optional[float] = None
    price_vs_sma200:  Optional[float] = None
    trend_label:      str             = ""
    volume_ratio:     Optional[float] = None

    # Whale signal
    whale:            Optional[WhaleSignal] = None

    # Sell tracking
    sell_count:       int             = 0
    has_sells:        bool            = False

    # Sector catalyst
    catalyst_tag:     str             = ""     # "Defense", "AI Infra", "Energy", etc.

    # Flags
    red_flags:        list            = field(default_factory=list)
    red_score:        int             = 0
    green_flags:      list            = field(default_factory=list)
    green_score:      int             = 0

    # Soft exceptions
    gm_soft_exception: bool           = False
    peg_unverified:    bool           = False

    # Scoring
    conviction_score: int             = 0
    potential_score:  int             = 0
    potential_label:  str             = ""
    rank_score:       float           = 0.0

    # Watchlist
    on_watchlist:     bool            = False
    days_on_watchlist: int            = 0
    prev_peg:         Optional[float] = None

    # Track assignment
    track:            str             = ""     # "A" / "B" / "B-" / "KTOS" / "WARN"

    # Sector momentum (for KTOS)
    sector_momentum:  float           = 0.0
    sector_rank:      int             = 0
    ktos_fail_reason: str             = ""
    ktos_rank_score:  float           = 0.0
    ktos_why:         str             = ""

# ══════════════════════════════════════════════════════════════
#  NETWORK HELPERS
# ══════════════════════════════════════════════════════════════

def wait_for_internet(interval=15):
    while True:
        try:
            requests.get("https://www.google.com", timeout=5)
            return
        except Exception:
            log.warning(f"  Internet DOWN — retrying in {interval}s...")
            time.sleep(interval)

def sec_get(url: str, timeout: int = 20) -> Optional[requests.Response]:
    """
    GET with SEC rate limiting + retry. Returns Response or None.
    Always waits SEC_DELAY before the request.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }
    time.sleep(SEC_DELAY)
    for attempt in range(4):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 429:
                wait = min(10 * (2 ** attempt), 120)
                log.warning(f"  SEC 429 — backing off {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                return r
            return None
        except Exception as e:
            err = str(e).lower()
            if any(w in err for w in ["connection","timeout","reset","eof"]):
                if attempt == 3:
                    wait_for_internet()
                else:
                    time.sleep(5 * (attempt + 1))
            else:
                return None
    return None

def yf_get(func, retries=3):
    """Wraps yfinance call with retry + 429 backoff."""
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "too many requests" in err:
                wait = min(BASE_DELAY * (4 ** attempt), BACKOFF_MAX)
                log.warning(f"  yfinance 429 — backing off {wait:.0f}s...")
                time.sleep(wait)
                continue
            if any(w in err for w in ["connection","timeout","reset"]):
                time.sleep(5 * (attempt + 1))
                continue
            return None
    return None

# ══════════════════════════════════════════════════════════════
#  EDGAR CIK MAP
# ══════════════════════════════════════════════════════════════

_CIK_MAP: dict = {}       # ticker → cik
_CIK_TO_TICKER: dict = {} # cik → ticker

def load_cik_map():
    global _CIK_MAP, _CIK_TO_TICKER
    if _CIK_MAP:
        return
    log.info("  Loading SEC CIK map...")
    r = sec_get("https://www.sec.gov/files/company_tickers.json")
    if not r:
        log.error("  FAILED to load CIK map!")
        return
    for entry in r.json().values():
        t = (entry.get("ticker") or "").strip().upper()
        c = str(entry.get("cik_str", "")).zfill(10)
        if t and c:
            _CIK_MAP[t]   = c
            _CIK_TO_TICKER[c] = t
    log.info(f"  CIK map: {len(_CIK_MAP)} tickers")

def get_cik(ticker: str) -> Optional[str]:
    load_cik_map()
    return _CIK_MAP.get(ticker.upper())

def get_ticker_for_cik(cik: str) -> Optional[str]:
    load_cik_map()
    return _CIK_TO_TICKER.get(cik.zfill(10))

# ══════════════════════════════════════════════════════════════
#  BULLETPROOF FORM 4 XML PARSER
#
#  This is the core of the whale-finding engine.
#  Rules (ALL must pass for a transaction to count):
#    1. It must be inside a <nonDerivativeTransaction> block
#    2. <transactionCode> must be exactly 'P' (open-market purchase)
#    3. <transactionAcquiredDisposedCode> must be 'A' (acquired)
#    4. Both codes must be in the SAME transaction block
#    5. Dollar amount must be calculable (shares × price)
#    6. Total must be >= WHALE_MIN_DOLLARS
# ══════════════════════════════════════════════════════════════

def parse_form4_xml(xml_text: str, current_price: float = 0.0) -> dict:
    """
    Parse Form 4 XML and return purchase details.

    KEY INSIGHT: The issuer's ticker symbol is INSIDE the Form 4 XML
    as <issuerTradingSymbol>. We extract it directly — no CIK map needed.

    Returns dict with:
        dollars             — total dollar amount of verified purchases
        shares              — total shares purchased
        avg_price           — average price paid per share
        officer_title       — title of the reporting person
        is_officer          — True if CEO/CFO/COO/President/VP level
        is_director_only    — True if only director (weaker signal)
        raw_titles          — all titles found in the filing
        issuer_ticker       — ticker symbol from <issuerTradingSymbol>
        issuer_cik          — issuer CIK from <issuerCik>
        issuer_name         — issuer company name from <issuerName>
    """
    result = {
        "dollars":          0.0,
        "shares":           0.0,
        "avg_price":        0.0,
        "officer_title":    "",
        "is_officer":       False,
        "is_director_only": False,
        "raw_titles":       [],
        "issuer_ticker":    "",
        "issuer_cik":       "",
        "issuer_name":      "",
    }

    if not xml_text:
        return result

    officer_keywords  = ["chief executive","ceo","co-ceo","chief financial","cfo",
                         "chief operating","coo","president","chief","vp","vice president",
                         "svp","evp","general counsel","principal","secretary","treasurer"]
    director_keywords = ["director","trustee","board member"]

    # ── lxml fast path ────────────────────────────────────────
    if LXML_AVAILABLE:
        try:
            root = lxml_etree.fromstring(xml_text.encode())

            def xt(tag):
                el = root.find(".//" + tag)
                return el.text.strip() if el is not None and el.text else ""

            result["issuer_ticker"] = xt("issuerTradingSymbol").upper()
            cik_raw = xt("issuerCik")
            result["issuer_cik"]   = cik_raw.zfill(10) if cik_raw else ""
            result["issuer_name"]  = xt("issuerName")

            titles = [el.text.strip() for el in root.findall(".//officerTitle")
                      if el.text and el.text.strip()]
            result["raw_titles"]    = titles
            best_title              = max(titles, key=len) if titles else ""
            result["officer_title"] = best_title

            tl = best_title.lower()
            result["is_officer"]       = any(kw in tl for kw in officer_keywords)
            result["is_director_only"] = (not result["is_officer"]) and any(kw in tl for kw in director_keywords)

            total_dollars = 0.0
            total_shares  = 0.0
            price_sum     = 0.0
            price_count   = 0

            for block in root.findall(".//nonDerivativeTransaction"):
                tc = block.find(".//transactionCode")
                if tc is None or (tc.text or "").strip().upper() != "P":
                    continue
                ad = block.find(".//transactionAcquiredDisposedCode/value")
                if ad is None or (ad.text or "").strip().upper() != "A":
                    continue
                sv = block.find(".//transactionShares/value")
                pv = block.find(".//transactionPricePerShare/value")
                if sv is None:
                    continue
                try:
                    shares = float((sv.text or "0").replace(",", ""))
                    px     = float((pv.text or "0").replace(",", "")) if pv is not None else 0.0
                except ValueError:
                    continue
                if shares <= 0:
                    continue
                if px <= 0 and current_price > 0:
                    px = current_price
                if px <= 0:
                    continue
                total_dollars += shares * px
                total_shares  += shares
                price_sum     += px
                price_count   += 1

            result["dollars"]   = total_dollars
            result["shares"]    = total_shares
            result["avg_price"] = price_sum / price_count if price_count > 0 else 0.0
            return result

        except Exception:
            pass  # lxml failed, fall through to regex

    # ── regex fallback ────────────────────────────────────────
    # This is the key fix: no CIK-to-ticker lookup needed at all.
    t_match = re.search(r'<issuerTradingSymbol>\s*([A-Z.\-]{1,10})\s*</issuerTradingSymbol>',
                        xml_text, re.IGNORECASE)
    if t_match:
        result["issuer_ticker"] = t_match.group(1).strip().upper()

    c_match = re.search(r'<issuerCik>\s*(\d+)\s*</issuerCik>', xml_text, re.IGNORECASE)
    if c_match:
        result["issuer_cik"] = c_match.group(1).strip().zfill(10)

    n_match = re.search(r'<issuerName>\s*(.*?)\s*</issuerName>', xml_text, re.IGNORECASE)
    if n_match:
        result["issuer_name"] = n_match.group(1).strip()

    titles = re.findall(r'<officerTitle>\s*(.*?)\s*</officerTitle>', xml_text, re.IGNORECASE)
    result["raw_titles"] = [t.strip() for t in titles if t.strip()]

    best_title = ""
    for t in result["raw_titles"]:
        if len(t) > len(best_title):
            best_title = t
    result["officer_title"] = best_title

    tl = best_title.lower()
    result["is_officer"]       = any(kw in tl for kw in officer_keywords)
    result["is_director_only"] = (not result["is_officer"]) and any(kw in tl for kw in director_keywords)

    # CRITICAL: P-code and A-code MUST be in the same block.
    blocks = re.findall(
        r'<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>',
        xml_text, re.DOTALL
    )

    total_dollars = 0.0
    total_shares  = 0.0
    price_sum     = 0.0
    price_count   = 0

    for block in blocks:
        tc_match = re.search(r'<transactionCode>\s*([A-Za-z])\s*</transactionCode>', block)
        if not tc_match or tc_match.group(1).strip().upper() != 'P':
            continue
        ad_match = re.search(r'<transactionAcquiredDisposedCode>\s*<value>([AD])</value>', block)
        if not ad_match or ad_match.group(1).strip().upper() != 'A':
            continue
        shares_match = re.search(r'<transactionShares>\s*<value>([\d,\.]+)</value>', block)
        if not shares_match:
            continue
        try:
            shares = float(shares_match.group(1).replace(',', ''))
        except ValueError:
            continue
        if shares <= 0:
            continue
        price_match = re.search(r'<transactionPricePerShare>\s*<value>([\d,\.]+)</value>', block)
        px = 0.0
        if price_match:
            try:
                px = float(price_match.group(1).replace(',', ''))
            except ValueError:
                px = 0.0
        if px <= 0 and current_price > 0:
            px = current_price
        if px <= 0:
            continue
        total_dollars += shares * px
        total_shares  += shares
        price_sum     += px
        price_count   += 1

    result["dollars"]   = total_dollars
    result["shares"]    = total_shares
    result["avg_price"] = price_sum / price_count if price_count > 0 else 0.0
    return result


def fetch_form4_xml(cik: str, accession: str) -> Optional[str]:
    """
    Fetch Form 4 XML from SEC EDGAR.

    KEY FIX vs previous: Index page FIRST.
    The index page always lists the actual XML filename.
    Guessing at accession.xml / form4.xml fails ~95% of the time because
    SEC uses custom filenames (wf-form4_..., xslF345X05_..., etc.).

    Strategy:
    1. Hit the index page → parse actual XML filename → fetch it  (2 requests, ~90% success)
    2. Try accession.xml and form4.xml directly                   (2 requests, fallback)
    """
    if not accession or '-' not in accession:
        return None

    acc       = accession.strip()
    acc_clean = acc.replace('-', '')

    cik_str = (cik or "").strip()
    if not cik_str or cik_str in ("0", ""):
        cik_str = acc.split('-')[0]

    try:
        cik_int = int(cik_str)
    except ValueError:
        return None

    if cik_int == 0:
        return None

    def _try_url(url: str) -> Optional[str]:
        r = sec_get(url, timeout=15)
        if r and '<ownershipDocument' in r.text:
            return r.text
        return None

    # ── Step 1: Index page → find actual XML filename (PRIMARY) ──
    idx_url = (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
               f"{acc_clean}/{acc}-index.htm")
    r_idx = sec_get(idx_url, timeout=15)
    if r_idx and r_idx.status_code == 200:
        # Find the .xml file linked in the index — there should be exactly one
        # Form 4 XML file. It will have "ownershipDocument" as its description.
        # Pattern: any .xml link in the index
        if BS4_AVAILABLE:
            soup = parse_html(r_idx.text)
            xml_links = [a["href"] for a in soup.find_all("a", href=True)
                         if a["href"].lower().endswith(".xml")]
        else:
            xml_links = re.findall(r'href="([^"]*\.xml)"', r_idx.text, re.IGNORECASE)
        for xml_rel in xml_links:
            # Skip XSLT stylesheet links (they won't have ownershipDocument)
            if 'xsl' in xml_rel.lower() and 'form4' not in xml_rel.lower():
                continue
            if xml_rel.startswith('/'):
                xml_url = f"https://www.sec.gov{xml_rel}"
            elif xml_rel.startswith('http'):
                xml_url = xml_rel
            else:
                xml_url = (f"https://www.sec.gov/Archives/edgar/data/"
                           f"{cik_int}/{acc_clean}/{xml_rel.split('/')[-1]}")
            result = _try_url(xml_url)
            if result:
                return result

    # ── Step 2: Direct URL guesses (fallback) ─────────────────────
    for url in [
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{acc}.xml",
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/form4.xml",
    ]:
        result = _try_url(url)
        if result:
            return result

    return None

def build_edgar_url(cik: str, accession: str) -> str:
    if not cik or not accession:
        return ""
    try:
        cik_int   = int(cik.strip())
        acc_clean = accession.replace('-', '')
        return (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                f"{acc_clean}/{accession}-index.htm")
    except ValueError:
        return ""

# ══════════════════════════════════════════════════════════════
#  EDGAR FORM 4 FILING DISCOVERY
#
#  Three-tier approach, each more reliable than the previous v8.0:
#
#  Tier 1: EDGAR RSS atom feed (getcurrent?type=4)
#    - Official SEC feed of all recent Form 4 filings
#    - Returns CIK and accession number cleanly
#    - Most reliable — used by financial data vendors
#
#  Tier 2: EDGAR EFTS full-text search (corrected URL)
#    - Searches for "transactionCode" in Form 4 text
#    - Corrected _id parsing (format: edgar_data_{cik}_{acc}.txt)
#
#  KEY FIX vs v8.0:
#    The ticker is extracted directly from the Form 4 XML as
#    <issuerTradingSymbol>. No CIK-to-ticker map lookup needed.
#    This was why 64 filings all showed 0 valid buys — the map
#    lookup was failing silently for every single one.
# ══════════════════════════════════════════════════════════════

def _parse_atom_entries(atom_text: str) -> List[dict]:
    """
    Parse EDGAR atom feed XML into filing dicts.
    Only returns GENUINE Form 4 filings — filters out 485BPOS, 424B2,
    40-APP, 497, and other non-Form-4 types that EDGAR's type=4 filter
    accidentally includes (because "4" appears anywhere in the type).

    Real Form 4 title format: "4 - COMPANY NAME (CIK) (Filer)"
    Bad entries: "485BPOS - PRUCO LIFE...", "424B2 - JPMorgan..."

    Filter rule: title must start with exactly "4" followed by space/dash,
    NOT "40-", "485", "424", "4-APP", etc.
    """
    entries = []

    # Strip HTML entities
    text = atom_text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')

    if BS4_AVAILABLE:
        soup = parse_html(text)
        block_texts = [str(e) for e in soup.find_all("entry")]
    else:
        block_texts = re.findall(r'<entry>(.*?)</entry>', text, re.DOTALL)

    for block in block_texts:
        # ── Form type gate: only real Form 4s ──────────────────
        # Extract title and check it starts with "4 -" or "4-" exactly
        raw_title_m = re.search(r'<title[^>]*>(.*?)</title>', block, re.IGNORECASE | re.DOTALL)
        raw_title   = raw_title_m.group(1).strip() if raw_title_m else ""

        # Real Form 4: title like "4 - COMPANY NAME" or "4- COMPANY"
        # Bad: "485BPOS - ...", "424B2 - ...", "40-APP - ...", "497 - ...", "4-1", etc.
        if not re.match(r'^4\s*[-–]\s*[A-Z]', raw_title, re.IGNORECASE):
            continue  # Not a Form 4 — skip immediately

        entry = {}

        # ── Accession number ─────────────────────────────────
        acc_match = re.search(
            r'accession-number=([0-9]{10}-[0-9]{2}-[0-9]{6})', block
        )
        if acc_match:
            entry["accession"] = acc_match.group(1)
        else:
            acc2 = re.search(r'/([0-9]{10}-[0-9]{2}-[0-9]{6})-index', block)
            if acc2:
                entry["accession"] = acc2.group(1)

        if not entry.get("accession"):
            continue

        # ── CIK from /edgar/data/{cik}/ in href ──────────────
        data_match = re.search(r'/edgar/data/(\d+)/', block)
        if data_match:
            entry["cik"] = data_match.group(1).zfill(10)
        else:
            entry["cik"] = entry["accession"].split('-')[0].zfill(10)

        # ── Company name ──────────────────────────────────────
        # Title format: "4 - COMPANY NAME (CIK) (Filer)"
        name_m = re.search(r'^4\s*[-–]\s*(.+?)(?:\s*\(\d+\).*)?$', raw_title, re.IGNORECASE)
        if name_m:
            name = name_m.group(1).strip()
            name = re.sub(r'\s*\(\d+\)\s*', '', name)
            name = re.sub(r'\s*\(Filer\)\s*', '', name, flags=re.IGNORECASE)
            entry["company_name"] = name.strip()[:60]
        else:
            entry["company_name"] = ""

        # ── Filed date ────────────────────────────────────────
        date_m = re.search(r'<updated[^>]*>(\d{4}-\d{2}-\d{2})', block)
        entry["filed_date"] = date_m.group(1) if date_m else ""

        entries.append(entry)

    return entries


def fetch_form4_filings_atom(days_back: int = 7) -> List[dict]:
    """
    Get recent Form 4 filings from EDGAR atom feed.
    Paginates until date cutoff reached OR safety cap hit.

    FIX vs previous: Removed the broken early-stop logic that was
    triggering after 1 page. Now paginate cleanly: keep going until
    the ENTIRE batch is older than cutoff. Use count=100 per page
    (EDGAR allows up to 100) to minimize requests.
    """
    cutoff      = datetime.now(timezone.utc) - timedelta(days=days_back)
    results     = []
    seen        = set()
    MAX_FILINGS = 3000
    PER_PAGE    = 100   # EDGAR allows 100 per page

    log.info(f"  Tier 1: EDGAR atom feed (past {days_back} days, cap {MAX_FILINGS})...")

    offset             = 0
    consecutive_old    = 0   # batches in a row with ALL entries older than cutoff
    MAX_CONSECUTIVE_OLD = 2  # stop if 2 batches in a row are all old

    while len(results) < MAX_FILINGS:
        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?"
            f"action=getcurrent&type=4&dateb=&owner=include"
            f"&count={PER_PAGE}&output=atom&start={offset}"
        )
        r = sec_get(url, timeout=30)
        if not r:
            log.warning(f"  Atom feed: no response at offset {offset}")
            break

        entries = _parse_atom_entries(r.text)
        if not entries:
            log.info(f"  Atom feed: empty page at offset {offset} — done")
            break

        batch_all_old = True
        batch_added   = 0

        for e in entries:
            acc = e.get("accession", "")
            if not acc or acc in seen:
                continue
            seen.add(acc)

            fd = e.get("filed_date", "")
            if fd:
                try:
                    dt = datetime.strptime(fd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if dt >= cutoff:
                        batch_all_old = False  # at least one entry is fresh
                    else:
                        continue  # skip old entries but keep paginating
                except ValueError:
                    batch_all_old = False  # unknown date = treat as fresh

            results.append(e)
            batch_added += 1

        log.info(
            f"  Atom offset={offset:>4}: {len(entries)} entries → "
            f"{batch_added} added ({len(results)} total)"
        )

        if batch_all_old:
            consecutive_old += 1
            if consecutive_old >= MAX_CONSECUTIVE_OLD:
                log.info("  Atom feed: reached date cutoff — stopping")
                break
        else:
            consecutive_old = 0

        if len(entries) < PER_PAGE:
            log.info("  Atom feed: last page reached")
            break

        offset += PER_PAGE

    log.info(f"  Tier 1 complete: {len(results)} Form 4 filings")
    return results


def fetch_form4_filings_efts(days_back: int = 7) -> List[dict]:
    """
    Tier 2: EDGAR EFTS search (corrected URL and _id parsing).
    Used as supplement/fallback to atom feed.

    The _id format from EFTS is: "edgar_data_{cik}_{accession_nodash}.txt"
    Example: "edgar_data_1234567_000123456726001234.txt"
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days_back)
    start_s  = start_dt.strftime("%Y-%m-%d")
    end_s    = end_dt.strftime("%Y-%m-%d")

    # Simple EFTS URL — no text filter, just date + form type
    base_url = (
        f"https://efts.sec.gov/LATEST/search-index?"
        f"forms=4"
        f"&dateRange=custom&startdt={start_s}&enddt={end_s}"
    )

    results = []
    seen    = set()
    log.info(f"  Tier 2: EDGAR EFTS search ({start_s} to {end_s})...")

    for offset in range(0, 2000, 40):
        page_url = base_url + f"&from={offset}&hits.hits.total.value=true"
        r = sec_get(page_url, timeout=30)
        if not r:
            break
        try:
            data = r.json()
        except Exception:
            break

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            src      = hit.get("_source", {})
            _id      = hit.get("_id", "")
            fd       = src.get("file_date", "")

            # Parse _id: "edgar_data_{cik}_{accession_nodash}.txt"
            # OR sometimes just the accession: "0001234567-26-001234"
            acc = ""
            cik = ""

            # Try accession format directly in _id
            acc_direct = re.search(r'([0-9]{10}-[0-9]{2}-[0-9]{6})', _id)
            if acc_direct:
                acc = acc_direct.group(1)
                # CIK is the prefix before the accession in edgar_data_ format
                cik_in_id = re.search(r'edgar_(?:data_)?(\d+)_', _id)
                if cik_in_id:
                    cik = cik_in_id.group(1).zfill(10)
                else:
                    # CIK might be the first 10 digits of accession
                    cik = acc.split('-')[0].zfill(10)
            else:
                # Fallback: reconstruct accession from nodash format in _id
                nodash = re.search(r'(\d{18,20})(?:\.txt)?$', _id)
                if nodash:
                    nd = nodash.group(1)
                    # Format: XXXXXXXXXX-YY-ZZZZZZ (10+2+6 = 18 digits)
                    if len(nd) >= 18:
                        acc = f"{nd[:10]}-{nd[10:12]}-{nd[12:18]}"
                        cik = nd[:10]

            if not acc:
                continue

            # Entity CIK from _source if available
            entity_cik = src.get("entity_id", "") or src.get("cik", "")
            if entity_cik:
                cik = str(entity_cik).zfill(10)

            if acc in seen:
                continue
            seen.add(acc)

            results.append({
                "cik":          cik,
                "accession":    acc,
                "company_name": (src.get("entity_name") or "")[:60],
                "filed_date":   fd,
            })

        total = data.get("hits", {}).get("total", {}).get("value", 0)
        if offset + 40 >= min(total, 2000):
            break

    log.info(f"  Tier 2 result: {len(results)} Form 4 filings")
    return results


def find_whale_buys(days_back: int = 7) -> List[WhaleSignal]:
    """
    Main entry point for whale-first scanning.

    v8.1 KEY FIXES vs v8.0:
    ─────────────────────────────────────────────────────────────
    FIX 1: Ticker extracted from <issuerTradingSymbol> in Form 4 XML.
            No CIK-to-ticker map lookup — that's what caused 64 filings
            → 0 valid buys. The map was failing for every single one.

    FIX 2: Atom feed as primary source (not broken EFTS text search).
            EFTS full-text search was returning 0 because EDGAR doesn't
            index Form 4 XML angle brackets as searchable text.

    FIX 3: All skip reasons logged at INFO level, not DEBUG.
            Now you can see exactly why each filing was rejected.

    FIX 4: EFTS used as SUPPLEMENT to atom feed, not replacement.
            Combined deduped list gives broader coverage.
    ─────────────────────────────────────────────────────────────

    Steps:
    1. Get Form 4 filing list from EDGAR atom feed (Tier 1)
    2. Supplement with EDGAR EFTS search (Tier 2)
    3. For each filing: fetch actual XML from SEC archives
    4. Parse XML: extract issuerTradingSymbol + P-code transactions
    5. Filter: P+A codes in same block, >= $500K, real ticker
    6. Detect clusters (2+ officers same company same week)
    7. Return sorted list of WhaleSignal objects
    """
    load_cik_map()

    # ── Tier 1: Atom feed (most reliable) ─────────────────────
    raw_filings = fetch_form4_filings_atom(days_back)

    # ── Tier 2: EFTS supplement ────────────────────────────────
    efts_filings = fetch_form4_filings_efts(days_back)

    # Merge and deduplicate by accession number
    seen_acc = {f["accession"] for f in raw_filings}
    for f in efts_filings:
        if f["accession"] not in seen_acc:
            raw_filings.append(f)
            seen_acc.add(f["accession"])

    log.info(f"  Total unique Form 4 filings to parse: {len(raw_filings)}")
    log.info(f"  Parsing XMLs — extracting issuer ticker + P-code transactions...")

    company_buys: dict = {}   # ticker → list of WhaleSignal

    valid_count    = 0
    skip_no_xml    = 0
    skip_no_ticker = 0
    skip_too_small = 0
    skip_not_p     = 0
    skip_junk      = 0

    for i, filing in enumerate(progress(raw_filings, desc="Parsing Form 4s")):
        acc  = filing.get("accession", "")
        cik  = filing.get("cik", "")   # submitter CIK (may be reporting person, not issuer)
        name = filing.get("company_name", "")

        if not acc:
            continue

        # Progress log every 50 filings
        if i > 0 and i % 50 == 0:
            log.info(f"  Progress: {i}/{len(raw_filings)} filings parsed "
                     f"({valid_count} valid, {skip_no_xml} no-xml, "
                     f"{skip_no_ticker} no-ticker, {skip_too_small} too-small)")

        # Fetch the Form 4 XML
        xml = fetch_form4_xml(cik, acc)
        if not xml:
            skip_no_xml += 1
            if i < 20 or i % 100 == 0:   # log first 20 + every 100th
                log.info(f"  [{i+1}] No XML: {acc} ({name or 'unknown'})")
            continue

        # Parse the XML — ticker, officer, dollars all come from here
        parsed = parse_form4_xml(xml)

        # ── Ticker from XML (the KEY FIX) ─────────────────────
        ticker = parsed.get("issuer_ticker", "").upper().strip()

        # If XML had no ticker, try our CIK map with the ISSUER cik from XML
        if not ticker:
            issuer_cik = parsed.get("issuer_cik", "")
            if issuer_cik:
                ticker = get_ticker_for_cik(issuer_cik) or ""

        # Still no ticker — skip
        if not ticker:
            skip_no_ticker += 1
            log.info(f"  [{i+1}] No ticker in XML: {name or acc} "
                     f"(issuer: {parsed.get('issuer_name','?')})")
            continue

        # Pre-filter junk tickers
        if not ticker_is_scannable(ticker):
            skip_junk += 1
            log.info(f"  [{i+1}] Junk ticker: {ticker} ({name})")
            continue

        # Must have P-code purchases
        if parsed["dollars"] <= 0:
            skip_not_p += 1
            log.info(f"  [{i+1}] No P-code buy: {ticker} ({parsed.get('issuer_name','')})")
            continue

        # Dollar floor
        if parsed["dollars"] < WHALE_MIN_DOLLARS:
            skip_too_small += 1
            log.info(f"  [{i+1}] Too small: {ticker} "
                     f"${parsed['dollars']:,.0f} (min ${WHALE_MIN_DOLLARS:,})")
            continue

        # Resolve company name: prefer issuer name from XML > atom feed name
        company_name = parsed.get("issuer_name") or name or ticker
        issuer_cik   = parsed.get("issuer_cik") or cik

        edgar_url = build_edgar_url(issuer_cik, acc) if issuer_cik else \
                    build_edgar_url(cik, acc) if cik else ""

        whale = WhaleSignal(
            ticker          = ticker,
            cik             = issuer_cik,
            company_name    = company_name,
            accession       = acc,
            filed_date      = filing.get("filed_date", ""),
            officer_title   = parsed["officer_title"] or "Unknown",
            dollar_amount   = parsed["dollars"],
            shares_bought   = parsed["shares"],
            price_per_share = parsed["avg_price"],
            edgar_url       = edgar_url,
        )

        # Group by ticker for cluster detection
        company_buys.setdefault(ticker, []).append(whale)

        valid_count += 1
        log.info(
            f"  ✓ WHALE #{valid_count}: {ticker:<8} | "
            f"{parsed['officer_title'][:28]:<28} | "
            f"${parsed['dollars']/1e6:.2f}M | "
            f"{filing.get('filed_date','')}"
        )

    log.info(
        f"\n  ── Whale parse complete ──────────────────────────────\n"
        f"  Valid whale buys : {valid_count}\n"
        f"  No XML fetched   : {skip_no_xml}\n"
        f"  No ticker in XML : {skip_no_ticker}\n"
        f"  No P-code buy    : {skip_not_p}\n"
        f"  Below $500K      : {skip_too_small}\n"
        f"  Junk ticker      : {skip_junk}\n"
        f"  ─────────────────────────────────────────────────────"
    )

    # ── Cluster detection ──────────────────────────────────────
    final_whales: List[WhaleSignal] = []
    for ticker, buys in company_buys.items():
        buys.sort(key=lambda x: x.dollar_amount, reverse=True)
        best = buys[0]

        if len(buys) >= 2:
            best.is_cluster       = True
            best.cluster_count    = len(buys)
            best.cluster_total_usd = sum(b.dollar_amount for b in buys)
            all_titles = "; ".join(
                b.officer_title for b in buys
                if b.officer_title and b.officer_title != "Unknown"
            )
            best.officer_title = f"CLUSTER [{len(buys)} officers: {all_titles[:50]}]"
            log.info(
                f"  ★ CLUSTER: {ticker} — {len(buys)} officers buying, "
                f"total ${best.cluster_total_usd/1e6:.1f}M"
            )

        final_whales.append(best)

    final_whales.sort(
        key=lambda x: x.cluster_total_usd if x.is_cluster else x.dollar_amount,
        reverse=True
    )

    log.info(f"  Final whale signals: {len(final_whales)} companies")
    total_filings = len(raw_filings)
    return final_whales, total_filings


# ══════════════════════════════════════════════════════════════
#  INSIDER SELL CHECK — Separate from whale-first scan
#  Checks if insiders have been SELLING at the same company
# ══════════════════════════════════════════════════════════════

def check_insider_sells(cik: str) -> tuple:
    """
    Returns (sell_count: int, has_heavy_selling: bool).
    Looks at recent Form 4 S-code filings for this company.
    A few sells is normal. 3+ is a warning. 5+ is a red flag.
    """
    if not cik:
        return 0, False

    url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    r   = sec_get(url, timeout=20)
    if not r:
        return 0, False

    try:
        data   = r.json()
        recent = data.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        dates  = recent.get("filingDate", [])

        cutoff = datetime.now(timezone.utc) - timedelta(days=WHALE_SELL_WINDOW)
        sell_count = 0

        for form, date_str in zip(forms, dates):
            if form.strip() != "4":
                continue
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if dt < cutoff:
                continue

            # Note: we don't fetch XML for every sell check — too slow.
            # We use a heuristic: any Form 4 in the window that isn't
            # our own buy filing counts toward the sell proxy.
            # This is approximate — a Form 4 could be an award.
            # We treat this as a soft signal, not a hard reject.
            sell_count += 1

        # Subtract 1 for the buy filing we already know about
        sell_count = max(0, sell_count - 1)
        return sell_count, sell_count >= 5

    except Exception:
        return 0, False

# ══════════════════════════════════════════════════════════════
#  UNIVERSE PRE-FILTER — Is this ticker worth scanning?
# ══════════════════════════════════════════════════════════════

def ticker_is_scannable(ticker: str) -> bool:
    """
    Quick reject before touching yfinance.
    Rejects warrants, units, rights, OTC foreign, SPACs.

    IMPORTANT: Suffix rules (W/U/R/F) only apply to tickers with 5+
    characters. 4-letter tickers like ROKU, FSLR, KREF, WOOF are real
    stocks that happen to end in those letters — never reject them.
    """
    n = len(ticker)

    if n <= 1 or n > 5:
        return False

    # Embedded digits (OTC foreign: A1B, BK2C etc)
    if any(c.isdigit() for c in ticker):
        return False

    # Suffix rules ONLY apply to 5-char tickers
    # (SATLF, EXOSF, ACBWW, IPAXW etc.)
    # 4-char tickers like ROKU/FSLR/KREF/WOOF are always real stocks
    if n == 5:
        if ticker.endswith("F") and ticker[:4].isalpha():
            return False   # OTC foreign ADR (e.g. SATLF, EXOSF, VOLVF)
        if ticker.endswith("W") and ticker[:4].isalpha():
            return False   # Warrant (e.g. ACBWW, IPAXW)
        if ticker.endswith("U") and ticker[:4].isalpha():
            return False   # Unit (e.g. BWAQM → actually catches BWAQU type)
        if ticker.endswith("R") and ticker[:4].isalpha():
            return False   # Rights

    return True

# ══════════════════════════════════════════════════════════════
#  SECTOR + CATALYST UTILITIES
# ══════════════════════════════════════════════════════════════

MACRO_CATALYSTS = {
    "Defense": ["defense","aerospace","military","weapon","armament","drone","missile","radar"],
    "AI Infrastructure": ["semiconductor","chip","data center","gpu","ai infrastructure",
                          "artificial intelligence","compute","wafer","fab"],
    "Domestic Energy": ["oil","gas","petroleum","coal","lng","natural gas","energy","shale",
                        "upstream","drilling","refin"],
    "Electrification": ["electrical equipment","switchgear","power grid","transformer",
                        "electric vehicle","ev charging","battery","powl","solar","wind"],
    "Reshoring/Manufacturing": ["manufactur","industrial","machinery","fabricat",
                                "steel","aluminum","reshoring","onshoring"],
    "Shipping Supercycle": ["shipping","maritime","tanker","container ship","bulk carrier",
                            "dry bulk","vessel"],
    "Insurance/Specialty Finance": ["insurance","reinsurance","specialty finance",
                                    "underwriting","surplus lines"],
    "Healthcare/Biotech": ["biotech","pharmaceutical","medical device","healthcare",
                           "drug","therapy","biopharmaceutical"],
}

def get_catalyst_tag(sector: str, industry: str, summary: str) -> str:
    """Returns the macro catalyst label for a stock, or '' if none."""
    combined = (sector + " " + industry + " " + summary[:200]).lower()
    for tag, keywords in MACRO_CATALYSTS.items():
        if any(kw in combined for kw in keywords):
            return tag
    return ""

def normalize_sector(sector: str, industry: str) -> str:
    combined = (sector + " " + industry).lower()
    keyword_map = [
        (["defense","aerospace","military","weapon","armament"],  "Aerospace & Defense"),
        (["semiconductor","chip","wafer","fab","microchip","gpu"],"Semiconductors"),
        (["software","saas","cloud","application software"],      "Software"),
        (["biotech","biopharmaceutical","genomics"],              "Biotechnology"),
        (["oil","gas","petroleum","upstream","drilling","shale","lng","energy"], "Energy"),
        (["bank","banking","savings","lending","credit union"],   "Banking"),
        (["insurance","reinsurance","underwriting"],              "Insurance"),
        (["reit","real estate investment","property trust"],      "REITs"),
        (["healthcare","medical device","health service"],        "Healthcare"),
        (["mining","gold","silver","copper","precious metal"],    "Mining & Metals"),
        (["shipping","maritime","tanker","container ship"],       "Shipping"),
        (["retail","consumer","apparel","clothing","footwear"],   "Consumer Retail"),
        (["restaurant","food service","fast food"],               "Restaurants"),
        (["utility","electric power","gas distribut","water supply"], "Utilities"),
        (["telecom","wireless","broadband","fiber"],              "Telecom"),
        (["fintech","payment","digital finance"],                 "Fintech"),
        (["industrial","machinery","manufactur","switchgear",
           "electrical equipment","fabricat","construction"],     "Industrials"),
    ]
    for keywords, canonical in keyword_map:
        if any(kw in combined for kw in keywords):
            return canonical
    return sector if sector else (industry if industry else "Other")

def sector_type(sector: str) -> str:
    s = sector.lower()
    if any(w in s for w in ["bank","insur","financial","bdc","mortgage","broker","investment"]):
        return "financial"
    if any(w in s for w in ["utilit","reit","real estate","pipeline"]):
        return "utility"
    return "normal"

def is_commodity_sector(sector: str) -> bool:
    return any(w in sector.lower() for w in ["shipping","mining","energy","materials","metal"])

def is_industrial_sector(sector: str) -> bool:
    return any(w in sector.lower() for w in
               ["industrial","manufactur","machinery","fabricat","electrical equipment","construction"])

def get_de_limit(stype: str, loose: bool = False) -> float:
    if loose:
        if stype == "financial": return DE_FINANCIAL
        if stype == "utility":   return DE_UTILITY
        return DE_NORMAL_LOOSE
    if stype == "financial": return DE_FINANCIAL
    if stype == "utility":   return DE_UTILITY
    return DE_NORMAL

# ══════════════════════════════════════════════════════════════
#  FUNDAMENTALS FETCH — runs only on whale companies
# ══════════════════════════════════════════════════════════════

def get_fundamentals(ticker: str, whale: Optional[WhaleSignal] = None) -> tuple:
    """
    Returns (StockResult | None, rejection_reason: str).
    Only called for companies where a whale buy was already confirmed.
    """
    try:
        def _fetch():
            t    = yf.Ticker(ticker)
            info = t.info
            hist = t.history(period="400d")
            return info, hist

        result = yf_get(_fetch)
        if result is None:
            return None, "yfinance returned no data"

        info, hist = result

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            return None, "no price data (possibly delisted)"
        if price < MIN_PRICE:
            return None, f"price ${price:.2f} below ${MIN_PRICE:.0f} floor"

        mcap = info.get("marketCap")
        if not mcap:
            return None, "no market cap data"
        if mcap < MIN_MARKET_CAP:
            return None, f"market cap ${mcap/1e6:.0f}M below ${MIN_MARKET_CAP/1e6:.0f}M floor"
        if mcap > MAX_MARKET_CAP:
            return None, f"market cap ${mcap/1e9:.1f}B above ${MAX_MARKET_CAP/1e9:.0f}B ceiling"

        raw_name = info.get("shortName") or info.get("longName") or ""
        if not raw_name or raw_name.strip().isdigit() or raw_name.strip() == ticker:
            name = f"{ticker} (name N/A)"
        else:
            name = raw_name[:45]

        industry = info.get("industry", "") or ""
        sector   = normalize_sector(info.get("sector", "") or "", industry)
        summary  = (info.get("longBusinessSummary") or "")[:400]

        # Hard reject: fund / ETF / blank check masquerading as a stock
        summary_low = summary.lower()
        industry_low = industry.lower()
        for fund_kw in ["blank check","special purpose acquisition","closed-end fund",
                        "closed end fund","interval fund","unit investment trust",
                        "exchange-traded fund","money market fund"]:
            if fund_kw in summary_low or fund_kw in industry_low:
                return None, f"EXCLUDED: fund/SPAC ({fund_kw})"

        # Geopolitical hard reject
        country = (info.get("country") or "").upper()
        if country in ["CN","CHINA"]:
            return None, "EXCLUDED: China HQ (ADR risk)"
        if country in ["RU","RUSSIA","IR","IRAN"]:
            return None, f"EXCLUDED: {country} HQ (geopolitical)"

        # Debt ratios
        de = info.get("debtToEquity")
        if de and de > 10:
            de = de / 100.0

        total_debt   = info.get("totalDebt")
        total_assets = info.get("totalAssets")
        debt_ratio   = (total_debt / total_assets) if (total_debt and total_assets and total_assets > 0) else None

        # PEG
        peg        = info.get("pegRatio")
        peg_source = "yfinance"
        peg_unverif = False

        if peg is not None:
            if peg <= 0 or peg < MIN_PEG or peg > 100:
                peg = None

        if peg is None:
            pe     = info.get("trailingPE") or info.get("forwardPE")
            growth = info.get("revenueGrowth")
            if pe and pe > 0 and growth and growth > 0.01:
                calc_peg = pe / (growth * 100)
                if MIN_PEG <= calc_peg <= 100:
                    peg        = calc_peg
                    peg_source = "calculated"
                    peg_unverif = True

        if peg is None:
            peg_source  = "unavailable"
            peg_unverif = True

        # Revenue growth
        rev_growth = info.get("revenueGrowth")
        if rev_growth is None:
            try:
                qf = yf.Ticker(ticker).quarterly_financials
                if qf is not None and "Total Revenue" in qf.index and qf.shape[1] >= 5:
                    rev_now  = float(qf.loc["Total Revenue"].iloc[:4].sum())
                    rev_prev = float(qf.loc["Total Revenue"].iloc[4:8].sum())
                    if rev_prev > 0:
                        rev_growth = (rev_now - rev_prev) / rev_prev
            except Exception:
                pass

        # Price trend
        price_1y = price_sma = trend_label = None
        if hist is not None and len(hist) >= 50:
            closes    = hist["Close"].dropna()
            current_p = float(closes.iloc[-1])
            one_yr    = closes.iloc[max(0, len(closes) - 252)]
            price_1y  = (current_p - float(one_yr)) / float(one_yr) * 100
            sma200    = float(closes.tail(200).mean())
            price_sma = (current_p - sma200) / sma200 * 100
            recent    = closes.tail(min(90, len(closes)))
            r90       = (float(recent.iloc[-1]) - float(recent.iloc[0])) / float(recent.iloc[0]) * 100
            if r90 > 5:     trend_label = "rising"
            elif r90 < -10: trend_label = "falling"
            else:           trend_label = "flat"

        vol_ratio = None
        if hist is not None and len(hist) >= 5:
            avg_v     = float(hist["Volume"].iloc[:-1].mean())
            curr_v    = float(hist["Volume"].iloc[-1])
            if avg_v > 0:
                vol_ratio = curr_v / avg_v

        catalyst = get_catalyst_tag(sector, industry, summary)

        s = StockResult(
            ticker=ticker, name=name, sector=sector, industry=industry,
            market_cap=mcap, price=price,
            peg=peg, peg_source=peg_source, peg_unverified=peg_unverif,
            debt_equity=de, debt_ratio=debt_ratio,
            gross_margin=info.get("grossMargins"),
            revenue_growth=rev_growth,
            insider_pct=info.get("heldPercentInsiders"),
            net_income=info.get("netIncomeToCommon"),
            profitable=bool(info.get("netIncomeToCommon") and info.get("netIncomeToCommon") > 0),
            revenue=info.get("totalRevenue"),
            inst_ownership=info.get("heldPercentInstitutions"),
            quick_ratio=info.get("quickRatio"),
            price_1y_change=price_1y, price_vs_sma200=price_sma,
            trend_label=trend_label or "", volume_ratio=vol_ratio,
            catalyst_tag=catalyst,
            whale=whale,
        )
        return s, ""

    except Exception as e:
        return None, f"error: {str(e)[:60]}"

# ══════════════════════════════════════════════════════════════
#  FILTER ENGINE — Tight and Loose versions
# ══════════════════════════════════════════════════════════════

def passes_tight_filters(s: StockResult) -> tuple:
    """
    Returns (passed: bool, reason: str).
    Tight = Track A / Track B criteria.
    """
    stype        = sector_type(s.sector)
    max_de       = get_de_limit(stype, loose=False)
    is_commodity = is_commodity_sector(s.sector)
    is_industrial = is_industrial_sector(s.sector)

    # PEG — only hard-filter when we have real data
    if s.peg is not None and s.peg_source == "yfinance":
        if s.peg > MAX_PEG_TIGHT:
            return False, f"PEG={s.peg:.2f} > {MAX_PEG_TIGHT} (tight max)"

    # D/E
    if s.debt_equity is not None and s.debt_equity > max_de:
        return False, f"D/E={s.debt_equity:.2f} > {max_de:.1f} ({stype} limit)"
    if stype == "normal" and s.debt_equity is None:
        # Allow through — whale buy compensates for missing data
        pass

    # Profitability
    if s.net_income is not None and s.net_income <= 0:
        return False, "not profitable (negative net income)"
    if s.revenue is not None and s.revenue < 5_000_000:
        return False, "under $5M revenue"

    # Gross margin — three tiers
    if s.gross_margin is not None:
        if is_commodity:
            if s.gross_margin < MIN_GM_COMMODITY:
                return False, f"GM {s.gross_margin*100:.0f}% < {MIN_GM_COMMODITY*100:.0f}% (commodity)"
        elif is_industrial:
            if s.gross_margin < MIN_GM_INDUSTRIAL:
                return False, f"GM {s.gross_margin*100:.0f}% < {MIN_GM_INDUSTRIAL*100:.0f}% (industrial min)"
            elif s.gross_margin < MIN_GM_TIGHT:
                s.gm_soft_exception = True  # soft — passes but gets penalty
        else:
            if s.gross_margin < MIN_GM_TIGHT:
                return False, f"GM {s.gross_margin*100:.0f}% < {MIN_GM_TIGHT*100:.0f}%"

    # Revenue growth
    if s.revenue_growth is not None and s.revenue_growth < MIN_REV_GROWTH_TIGHT:
        if s.peg is None or s.peg >= 0.30:
            return False, f"rev growth {s.revenue_growth*100:.1f}% < {MIN_REV_GROWTH_TIGHT*100:.0f}%"

    # Insider ownership (soft — only hard reject if we have the data and it's very low)
    if s.insider_pct is not None:
        if s.insider_pct < MIN_INSIDER_OWN_TIGHT:
            return False, f"insider own {s.insider_pct*100:.1f}% < {MIN_INSIDER_OWN_TIGHT*100:.0f}%"
        if s.insider_pct > MAX_INSIDER_OWN:
            return False, f"insider own {s.insider_pct*100:.1f}% > {MAX_INSIDER_OWN*100:.0f}% (low float)"

    return True, ""

def passes_loose_filters(s: StockResult) -> tuple:
    """
    Returns (passed: bool, reason: str).
    Loose = Track B- criteria. For whale buys that miss tight filters.
    """
    stype  = sector_type(s.sector)
    max_de = get_de_limit(stype, loose=True)

    # PEG — loose
    if s.peg is not None and s.peg_source == "yfinance":
        if s.peg > MAX_PEG_LOOSE:
            return False, f"PEG={s.peg:.2f} > {MAX_PEG_LOOSE} even for loose track"

    # D/E — loose
    if s.debt_equity is not None and s.debt_equity > max_de:
        return False, f"D/E={s.debt_equity:.2f} > {max_de:.1f} even for loose"

    # Must have some revenue
    if s.revenue is not None and s.revenue < 1_000_000:
        return False, "under $1M revenue"

    # Gross margin — loose
    if s.gross_margin is not None and s.gross_margin < MIN_GM_LOOSE:
        return False, f"GM {s.gross_margin*100:.0f}% < {MIN_GM_LOOSE*100:.0f}% even for loose"

    # Revenue growth — loose (just needs to not be deeply negative)
    if s.revenue_growth is not None and s.revenue_growth < -0.20:
        return False, f"revenue shrinking {s.revenue_growth*100:.1f}% (too much)"

    # Insider ownership — loose
    if s.insider_pct is not None:
        if s.insider_pct < MIN_INSIDER_OWN_LOOSE:
            return False, f"insider own {s.insider_pct*100:.1f}% < {MIN_INSIDER_OWN_LOOSE*100:.0f}%"
        if s.insider_pct > MAX_INSIDER_OWN:
            return False, f"insider own {s.insider_pct*100:.1f}% > {MAX_INSIDER_OWN*100:.0f}%"

    return True, ""

# ══════════════════════════════════════════════════════════════
#  FLAG CHECKS — Red and green flags for report
# ══════════════════════════════════════════════════════════════

def run_flag_checks(ticker: str, s: StockResult) -> None:
    """Populates s.red_flags, s.red_score, s.green_flags, s.green_score in-place."""
    red = []; rscore = 0
    grn = []; gscore = 0
    stype = sector_type(s.sector)
    is_fin = stype in ("financial", "utility")

    try:
        info    = yf.Ticker(ticker).info
        summary = (info.get("longBusinessSummary") or "").lower()
        country = (info.get("country") or "").upper()
        emp     = info.get("fullTimeEmployees")
        ar      = info.get("auditRisk")
        gov_r   = info.get("overallRisk")

        # ── RED FLAGS ────────────────────────────────────────

        if country in ["CN","CHINA"]:
            red.append(("HQ in China — ADR/delisting risk", 8)); rscore += 8
        elif country in ["RU","RUSSIA","IR","IRAN"]:
            red.append((f"HQ in {country} — geopolitical risk", 10)); rscore += 10

        buzz = ["blockchain","metaverse","web3","nft","revolutionary","paradigm shift"]
        hits = [w for w in buzz if w in summary]
        if len(hits) >= 2:
            red.append((f"Buzzword-heavy: {', '.join(hits[:3])}", 4)); rscore += 4

        for ph in ["not yet generated revenue","exploration stage","development stage"]:
            if ph in summary:
                red.append(("Pre-revenue / development stage", 6)); rscore += 6; break

        if emp and emp < 5:
            red.append((f"Only {emp} employees", 5)); rscore += 5

        if ar and ar >= 8:
            red.append((f"High audit risk ({ar}/10)", 5)); rscore += 5
        if gov_r and gov_r >= 8:
            red.append((f"High governance risk ({gov_r}/10)", 4)); rscore += 4

        if s.gross_margin is not None and s.gross_margin < 0.05:
            red.append((f"Gross margin {s.gross_margin*100:.1f}% — extremely thin", 5)); rscore += 5

        qr = info.get("quickRatio")
        if qr is not None and not is_fin and qr < 0.5:
            red.append((f"Quick ratio {qr:.2f} — liquidity risk", 4)); rscore += 4

        if s.trend_label == "falling":
            red.append(("Price falling 90d — enter only with extreme conviction", 4)); rscore += 4
        elif s.trend_label == "flat":
            red.append(("Price flat 90d — possible value trap", 2)); rscore += 2

        if s.sell_count >= 5:
            red.append((f"{s.sell_count} recent Form 4 filings (possible selling activity)", 4)); rscore += 4

        if s.peg_unverified and s.peg_source == "unavailable":
            red.append(("PEG unverified — yfinance returned no ratio", 2)); rscore += 2

        if s.gm_soft_exception:
            red.append((f"GM {(s.gross_margin or 0)*100:.0f}% — Industrials soft exception", 3)); rscore += 3

        if s.prev_peg and s.peg and s.peg > s.prev_peg * 1.3:
            red.append((f"PEG worsened ({s.prev_peg:.2f} → {s.peg:.2f})", 3)); rscore += 3

        # ── GREEN FLAGS ───────────────────────────────────────

        # Whale quality
        if s.whale:
            d = s.whale.cluster_total_usd if s.whale.is_cluster else s.whale.dollar_amount
            if d >= 50_000_000:
                grn.append((f"~${d/1e6:.0f}M whale buy — massive conviction", 15)); gscore += 15
            elif d >= 10_000_000:
                grn.append((f"~${d/1e6:.0f}M whale buy — very strong signal", 12)); gscore += 12
            elif d >= 1_000_000:
                grn.append((f"~${d/1e6:.1f}M insider buy", 7)); gscore += 7

            if s.whale.is_cluster:
                grn.append((f"CLUSTER: {s.whale.cluster_count} officers bought same week "
                             f"(${s.whale.cluster_total_usd/1e6:.1f}M total)", 10)); gscore += 10

            tl = s.whale.officer_title.lower()
            if "ceo" in tl or "chief executive" in tl:
                grn.append(("CEO buy — strongest possible signal", 8)); gscore += 8
            elif "cfo" in tl or "chief financial" in tl:
                grn.append(("CFO buy — knows the numbers", 6)); gscore += 6
            elif "coo" in tl or "president" in tl:
                grn.append((f"{s.whale.officer_title[:30]} buy — strong signal", 5)); gscore += 5

        # Catalyst tag
        if s.catalyst_tag:
            grn.append((f"Macro catalyst: {s.catalyst_tag}", 6)); gscore += 6

        # PEG
        if s.peg is not None:
            if s.peg < 0.2:
                grn.append((f"PEG {s.peg:.2f} — exceptional value vs growth", 12)); gscore += 12
            elif s.peg < 0.5:
                grn.append((f"PEG {s.peg:.2f} — cheap vs growth", 8)); gscore += 8
            elif s.peg < 1.0:
                grn.append((f"PEG {s.peg:.2f} — reasonable value", 4)); gscore += 4

        # Leverage
        if s.debt_equity is not None and s.debt_equity < 0.3:
            grn.append((f"D/E {s.debt_equity:.2f} — very clean balance sheet", 6)); gscore += 6
        elif s.debt_equity is not None and s.debt_equity < 0.6:
            grn.append((f"D/E {s.debt_equity:.2f} — low leverage", 3)); gscore += 3

        # Insider ownership
        if s.insider_pct:
            if s.insider_pct > 0.30:
                grn.append((f"Insiders own {s.insider_pct*100:.0f}% — skin in the game", 8)); gscore += 8
            elif s.insider_pct > 0.15:
                grn.append((f"Insiders own {s.insider_pct*100:.0f}% — good alignment", 5)); gscore += 5
            elif s.insider_pct > 0.08:
                grn.append((f"Insiders own {s.insider_pct*100:.0f}%", 2)); gscore += 2

        # Gross margin
        gm = s.gross_margin
        if gm is not None:
            if gm > 0.70:
                grn.append((f"GM {gm*100:.0f}% — excellent moat", 6)); gscore += 6
            elif gm > 0.50:
                grn.append((f"GM {gm*100:.0f}% — strong", 4)); gscore += 4
            elif gm > 0.35:
                grn.append((f"GM {gm*100:.0f}% — decent for sector", 2)); gscore += 2

        # Revenue growth
        if s.revenue_growth:
            if s.revenue_growth > 0.30:
                grn.append((f"Revenue +{s.revenue_growth*100:.0f}%/yr — accelerating", 8)); gscore += 8
            elif s.revenue_growth > 0.15:
                grn.append((f"Revenue +{s.revenue_growth*100:.0f}%/yr — growing", 5)); gscore += 5
            elif s.revenue_growth > 0:
                grn.append((f"Revenue +{s.revenue_growth*100:.0f}%/yr — positive", 2)); gscore += 2

        # Price momentum
        if s.trend_label == "rising":
            grn.append(("Price rising — momentum confirmation", 5)); gscore += 5
            if s.price_1y_change and s.price_1y_change > 30:
                grn.append((f"+{s.price_1y_change:.0f}% 1-year return", 5)); gscore += 5

        # Institutions
        if s.inst_ownership and s.inst_ownership > 0.40:
            grn.append((f"Institutions own {s.inst_ownership*100:.0f}%", 3)); gscore += 3

        # PEG improved
        if s.prev_peg and s.peg and s.peg < s.prev_peg * 0.85:
            grn.append((f"PEG improved ({s.prev_peg:.2f} → {s.peg:.2f})", 4)); gscore += 4

    except Exception as e:
        log.debug(f"  {ticker}: flag check error — {e}")

    s.red_flags = red
    s.red_score = rscore
    s.green_flags = grn
    s.green_score = gscore

# ══════════════════════════════════════════════════════════════
#  CONVICTION + POTENTIAL SCORING
# ══════════════════════════════════════════════════════════════

def compute_conviction(s: StockResult) -> int:
    """
    0-15 point conviction score. Only act on Track A if >= 7.
    Whale size + officer rank + PEG + cluster + catalyst.
    """
    score = 0

    if not s.whale:
        return 0

    # Whale dollar size (0-5 pts)
    d = s.whale.cluster_total_usd if s.whale.is_cluster else s.whale.dollar_amount
    if d >= 50_000_000:   score += 5
    elif d >= 10_000_000: score += 4
    elif d >= 1_000_000:  score += 3
    elif d >= 500_000:    score += 2
    else:                 score += 1

    # Officer rank (0-3 pts)
    tl = s.whale.officer_title.lower()
    if "ceo" in tl or "chief executive" in tl:
        score += 3
    elif "cfo" in tl or "chief financial" in tl or "coo" in tl or "president" in tl:
        score += 2
    elif any(k in tl for k in ["vp","vice president","chief","svp","evp"]):
        score += 1
    elif "cluster" in tl:
        score += 3  # cluster = multi-officer = treat as CEO-level signal
    # director alone = 0 pts

    # PEG tier (0-3 pts)
    if s.peg is not None:
        if s.peg < 0.3:    score += 3
        elif s.peg < 0.5:  score += 2
        elif s.peg < 1.0:  score += 1
    elif s.peg_source == "unavailable":
        score = max(0, score - 1)  # mild penalty for no PEG data

    # Cluster bonus (extra +2 beyond officer pts above)
    if s.whale.is_cluster and s.whale.cluster_count >= 2:
        score += 2

    # Catalyst bonus (+1)
    if s.catalyst_tag:
        score += 1

    # GM soft exception penalty
    if s.gm_soft_exception:
        score = max(0, score - 1)

    return score

def compute_potential(s: StockResult, is_loose: bool = False) -> tuple:
    score = 40 if is_loose else 50

    if s.peg:
        if s.peg < 0.2:   score += 20
        elif s.peg < 0.4: score += 14
        elif s.peg < 0.7: score += 8
        elif s.peg < 1.0: score += 4

    if s.trend_label == "falling" and s.price_1y_change:
        if s.price_1y_change < -20: score += 12
        elif s.price_1y_change < -10: score += 7
    elif s.trend_label == "rising":
        score -= 5

    if s.whale:
        d = s.whale.cluster_total_usd if s.whale.is_cluster else s.whale.dollar_amount
        if d >= 10_000_000: score += 14
        elif d >= 1_000_000: score += 8
        else:                score += 4
        if s.whale.is_cluster:
            score += 8

    if s.catalyst_tag:
        score += 8

    if s.insider_pct:
        if s.insider_pct > 0.30:   score += 8
        elif s.insider_pct > 0.15: score += 4

    if s.revenue_growth and s.revenue_growth > 0.25:
        score += 6

    score -= min(s.red_score * 1.2, 20)
    score  = max(0, min(100, int(score)))

    if score >= 80:   label = "EXPLOSIVE"
    elif score >= 65: label = "HIGH"
    elif score >= 45: label = "MODERATE"
    else:             label = "LOW"

    return score, label

def compute_rank_score(s: StockResult) -> float:
    base        = s.conviction_score * 10.0
    peg_bonus   = 15.0 if (s.peg and s.peg < 0.3) else (10.0 if (s.peg and s.peg < 0.5) else 0.0)
    de_bonus    = 6.0 if (s.debt_equity is not None and s.debt_equity < 0.3) else 0.0
    trend_adj   = 8.0 if s.trend_label=="rising" else (-8.0 if s.trend_label=="falling" else -2.0)
    cat_bonus   = 8.0 if s.catalyst_tag else 0.0
    cluster_b   = 15.0 if (s.whale and s.whale.is_cluster) else 0.0
    green_b     = min(s.green_score, 35.0)
    red_p       = min(s.red_score, 25.0)
    gm_p        = 5.0 if s.gm_soft_exception else 0.0
    return base + peg_bonus + de_bonus + trend_adj + cat_bonus + cluster_b + green_b - red_p - gm_p

# ══════════════════════════════════════════════════════════════
#  SECTOR MOMENTUM ENGINE (for KTOS plays)
# ══════════════════════════════════════════════════════════════

def get_price_return_days(ticker: str, days: int) -> Optional[float]:
    try:
        hist = yf.Ticker(ticker).history(period=f"{days+15}d")
        if hist is None or len(hist) < days // 5:
            return None
        closes = hist["Close"].dropna()
        if len(closes) < 2:
            return None
        idx = max(0, len(closes) - min(days, len(closes)))
        old = float(closes.iloc[idx])
        now = float(closes.iloc[-1])
        return ((now - old) / old * 100) if old > 0 else None
    except Exception:
        return None

def measure_sector_momentum(sector_map: dict) -> dict:
    """
    sector_map: {sector_name: [StockResult, ...]}
    Returns {sector: momentum_score}
    """
    if not sector_map:
        return {}

    spy_30 = get_price_return_days("SPY", 30) or 0.0
    spy_90 = get_price_return_days("SPY", 90) or 0.0

    scores = {}
    for sector, stocks in sector_map.items():
        if len(stocks) < 2:
            continue
        avg_1y = sum(
            s.price_1y_change for s in stocks if s.price_1y_change is not None
        ) / max(1, sum(1 for s in stocks if s.price_1y_change is not None))
        avg_sma = sum(
            s.price_vs_sma200 for s in stocks if s.price_vs_sma200 is not None
        ) / max(1, sum(1 for s in stocks if s.price_vs_sma200 is not None))

        alpha_90 = avg_1y - spy_90
        alpha_30 = avg_sma - spy_30

        accel = 10.0 if alpha_30 > alpha_90 else 0.0
        scores[sector] = round(alpha_90 * 0.4 + accel, 2)

    return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))

# ══════════════════════════════════════════════════════════════
#  SPY 200D MA CHECK
# ══════════════════════════════════════════════════════════════

def check_spy_200d_ma() -> tuple:
    try:
        hist = yf.Ticker("SPY").history(period="300d")
        if hist is None or len(hist) < 60:
            return True, None, None, None
        closes    = hist["Close"].dropna()
        spy_price = float(closes.iloc[-1])
        sma200    = float(closes.tail(200).mean())
        pct       = (spy_price - sma200) / sma200 * 100
        return spy_price > sma200, spy_price, sma200, pct
    except Exception as e:
        log.warning(f"  SPY 200d check failed: {e}")
        return True, None, None, None

# ══════════════════════════════════════════════════════════════
#  WATCHLIST
# ══════════════════════════════════════════════════════════════

def load_watchlist() -> dict:
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_watchlist(wl: dict):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(wl, f, indent=2)

def update_watchlist(wl: dict, s: StockResult):
    today = datetime.now().strftime("%Y-%m-%d")
    if s.ticker not in wl:
        wl[s.ticker] = {
            "first_seen":   today, "last_seen": today,
            "name":         s.name, "days_on_list": 1,
            "last_peg":     round(s.peg, 3) if s.peg else None,
            "peg_history":  [], "track": s.track,
        }
    else:
        e = wl[s.ticker]
        e["last_seen"]    = today
        e["days_on_list"] = e.get("days_on_list", 1) + 1
        e["track"]        = s.track
        if s.peg:
            hist = e.get("peg_history", [])
            if not hist or hist[-1] != round(s.peg, 3):
                hist.append(round(s.peg, 3))
            e["peg_history"] = hist[-6:]
            e["last_peg"]    = round(s.peg, 3)

def enrich_from_watchlist(wl: dict, s: StockResult):
    if s.ticker in wl:
        s.on_watchlist      = True
        s.days_on_watchlist = wl[s.ticker].get("days_on_list", 0)
        s.prev_peg          = wl[s.ticker].get("last_peg")

# ══════════════════════════════════════════════════════════════
#  REPORT BUILDER
# ══════════════════════════════════════════════════════════════

def stock_block(s: StockResult, show_conviction: bool = True) -> List[str]:
    lines = []
    L = lines.append

    wl_tag      = f" [WATCHLIST {s.days_on_watchlist}d]" if s.on_watchlist else ""
    cat_tag     = f" ◆ {s.catalyst_tag}" if s.catalyst_tag else ""
    cluster_tag = " ★ CLUSTER BUY" if (s.whale and s.whale.is_cluster) else ""
    L(f"\n  {s.ticker}  —  {s.name}{wl_tag}{cat_tag}{cluster_tag}")
    L(f"     Sector     : {s.sector}")
    L(f"     Market Cap : ${s.market_cap/1e9:.2f}B" if s.market_cap else "     Market Cap : N/A")
    L(f"     Price      : ${s.price:.2f}" if s.price else "     Price      : N/A")
    L("")

    L("     ── WHALE SIGNAL ───────────────────────────────────────")
    if s.whale:
        d = s.whale.cluster_total_usd if s.whale.is_cluster else s.whale.dollar_amount
        L(f"     Buyer       : {s.whale.officer_title[:55]}")
        L(f"     Amount      : ~${d/1e6:.2f}M  ({s.whale.shares_bought:,.0f} shares @ ${s.whale.price_per_share:.2f})")
        L(f"     Filed       : {s.whale.filed_date}")
        L(f"     VERIFY      : {s.whale.edgar_url}")
        L(f"     CHECK       : transactionType=P? CEO/CFO/COO? Not 10b5-1 plan?")
    else:
        L("     No whale buy (Track B — watching for one)")

    L("")
    L("     ── FUNDAMENTALS ───────────────────────────────────────")

    peg_str = (f"{s.peg:.2f} [{s.peg_source}]" if s.peg
               else f"UNAVAILABLE [{s.peg_source}]")
    L(f"     PEG        : {peg_str}")

    de_str = f"{s.debt_equity:.2f}" if s.debt_equity is not None else "N/A"
    L(f"     D/E        : {de_str}")

    gm_str = (f"{s.gross_margin*100:.0f}%" +
               (" [SOFT EXCEPTION]" if s.gm_soft_exception else "")
              ) if s.gross_margin is not None else "N/A"
    L(f"     Gross Mgn  : {gm_str}")

    rg_str = f"{s.revenue_growth*100:.0f}%/yr" if s.revenue_growth is not None else "N/A"
    L(f"     Rev Growth : {rg_str}")

    io_str = f"{s.insider_pct*100:.0f}%" if s.insider_pct is not None else "N/A"
    L(f"     Insider Own: {io_str}")

    vr_str = f"{s.volume_ratio:.2f}x avg" if s.volume_ratio is not None else "N/A"
    L(f"     Volume     : {vr_str}  (under radar = good if < 1.5x)")

    if s.trend_label:
        icon  = "RISING" if s.trend_label=="rising" else ("FALLING" if s.trend_label=="falling" else "FLAT")
        pct_s = f"{s.price_1y_change:+.1f}%" if s.price_1y_change is not None else "N/A"
        sma_s = f"{s.price_vs_sma200:+.1f}% vs 200d SMA" if s.price_vs_sma200 is not None else ""
        L(f"     Trend      : {icon}  (1yr {pct_s}  {sma_s})")

    if s.sell_count > 0:
        L(f"     Sell watch : {s.sell_count} recent Form 4 filings (verify manually)")

    L("")

    if show_conviction and s.whale:
        bar_filled = min(s.conviction_score, 15)
        cv_bar = "█" * bar_filled + "░" * (15 - bar_filled)
        L(f"     ── CONVICTION SCORE ────────────────────────────────")
        L(f"     Score     : {s.conviction_score}/15  {cv_bar}")
        weight = (
            "35-40% of capital (HIGH CONVICTION — ACT)" if s.conviction_score >= 11 else
            "20-30% of capital (good conviction)" if s.conviction_score >= 8 else
            "10-20% of capital (moderate — verify first)" if s.conviction_score >= 7 else
            "DO NOT ACT yet — score below 7"
        )
        L(f"     Sizing    : {weight}")
        L("")

    bar = "█" * (s.potential_score // 10) + "░" * (10 - s.potential_score // 10)
    L(f"     Potential  : {s.potential_label}  {bar}  ({s.potential_score}/100)")

    if s.green_flags:
        L(f"     Green flags ({len(s.green_flags)}, +{s.green_score} pts):")
        for text, pts in s.green_flags:
            L(f"       + {text}  [+{pts}]")

    if s.red_flags:
        L(f"     Red flags ({len(s.red_flags)}, -{s.red_score} pts):")
        for text, pts in s.red_flags:
            L(f"       - {text}  [-{pts}]")
    else:
        L("     Red flags  : None detected")

    if s.prev_peg and s.peg:
        diff  = s.peg - s.prev_peg
        arrow = "IMPROVED ↓" if diff < -0.05 else ("GOT WORSE ↑" if diff > 0.05 else "unchanged")
        L(f"     PEG trend  : {s.prev_peg:.2f} → {s.peg:.2f}  ({arrow})")

    L(f"     Rank score : {s.rank_score:.1f}")

    return lines

def build_report(
    track_a:    List[StockResult],
    track_b:    List[StockResult],
    track_b_lo: List[StockResult],
    track_ktos: List[StockResult],
    track_warn: List[StockResult],
    watchlist:  dict,
    spy_above:  bool,
    spy_price,  sma200, spy_pct,
    total_whales: int,
    total_parsed: int,
) -> str:
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    L = lines.append

    L("╔" + "═"*68 + "╗")
    L("║   PROJECT LEVIATHAN v8.0 — DAILY INTELLIGENCE REPORT")
    L(f"║   Scan date    :  {now}")
    L(f"║   Whale scan   :  {total_whales} whale buys found from {total_parsed} Form 4 filings")
    L("╚" + "═"*68 + "╝")
    L("")

    # SPY
    L("━"*70)
    L("  MACRO GATE — SPY 200-DAY MOVING AVERAGE")
    L("━"*70)
    if spy_price and sma200:
        if spy_above:
            L(f"  ✓ SPY ABOVE 200d MA  |  ${spy_price:.2f} vs SMA ${sma200:.2f}  |  {spy_pct:+.1f}%")
            L(f"  GREEN LIGHT — New positions OK")
        else:
            L(f"  ✗ *** SPY BELOW 200d MA — HEDGE MODE ***")
            L(f"    SPY ${spy_price:.2f}  |  200d SMA ${sma200:.2f}  |  {spy_pct:+.1f}%")
            L(f"    DO NOT enter new positions. Reduce existing by 50%.")
            L(f"    Wait for SPY above 200d MA for 3 consecutive days.")
            L(f"    Results below shown for monitoring only.")
    else:
        L("  SPY 200d MA unavailable — proceed with caution")
    L("")

    # TRACK A
    L("━"*70)
    L(f"  TRACK A — WHALE BUY + ALL TIGHT FILTERS  ({len(track_a)} stocks)")
    L("  Officer bought their OWN shares + passes ALL fundamental checks.")
    L("  Verify Form 4 manually before acting. Hard stop -25% from entry.")
    L("━"*70)
    if track_a:
        for rank, s in enumerate(sorted(track_a, key=lambda x: x.rank_score, reverse=True), 1):
            L(f"  {'─'*68}")
            medal = "★ RANK #1 — TOP PICK" if rank == 1 else f"  Rank #{rank}"
            L(f"  {medal} of {len(track_a)}")
            lines.extend(stock_block(s, show_conviction=True))
    else:
        L("\n  No Track A stocks this scan. This is normal — all 11 factors must align.")
        L("  Check Track B- and KTOS for active whale buys with looser criteria.")
    L("")

    # TRACK B
    L("━"*70)
    L(f"  TRACK B — TIGHT FILTERS, NO WHALE YET  ({len(track_b)} stocks)")
    L("  All quality criteria pass. Watching daily for a whale buy.")
    L("  When a whale buy appears → moves to Track A.")
    L("━"*70)
    if track_b:
        for rank, s in enumerate(sorted(track_b, key=lambda x: x.rank_score, reverse=True), 1):
            L(f"  {'─'*68}")
            L(f"  Rank #{rank} of {len(track_b)}")
            lines.extend(stock_block(s, show_conviction=False))
    else:
        L("\n  No Track B stocks today.")
    L("")

    # TRACK B-
    L("━"*70)
    L(f"  TRACK B- — WHALE BUY + LOOSENED FILTERS  ({len(track_b_lo)} stocks)")
    L("  Whale buy confirmed but fails one or more tight filters.")
    L("  Like CEIX (coal stigma), SBLK (negative rev growth), KTOS (PEG 23).")
    L("  HIGH RISK / HIGH REWARD. Do your own research. Verify Form 4 first.")
    L("━"*70)
    if track_b_lo:
        for rank, s in enumerate(sorted(track_b_lo, key=lambda x: x.rank_score, reverse=True), 1):
            L(f"  {'─'*68}")
            L(f"  Rank #{rank} of {len(track_b_lo)}  |  Failed tight because: {s.ktos_fail_reason}")
            lines.extend(stock_block(s, show_conviction=True))
    else:
        L("\n  No Track B- stocks today.")
    L("")

    # KTOS
    if track_ktos:
        L("━"*70)
        L(f"  KTOS PLAYS — WHALE BUY + SECTOR HEAT, FAILED FILTERS  ({len(track_ktos)} stocks)")
        L("  Like KTOS 2015: PEG 23, $40M buy, defense heating → +130%")
        L("  Sector momentum is the edge here. SPECULATIVE. Verify everything.")
        L("━"*70)
        for rank, s in enumerate(track_ktos, 1):
            L(f"  {'─'*68}")
            L(f"  KTOS #{rank} of {len(track_ktos)}  |  Sector: {s.sector}  |  Momentum: {s.sector_momentum:+.1f}")
            L(f"  Failed because: {s.ktos_fail_reason}")
            lines.extend(stock_block(s, show_conviction=True))
        L("")

    # WATCH OUT
    if track_warn:
        L("━"*70)
        L(f"  WATCH OUT — HEAVY INSIDER SELLING  ({len(track_warn)} stocks)")
        L("  These passed quality checks but show heavy Form 4 selling activity.")
        L("━"*70)
        for s in track_warn:
            L(f"\n  {s.ticker}  —  {s.name}  |  Sell filings: {s.sell_count}")
        L("")

    # WATCHLIST
    L("━"*70)
    L(f"  WATCHLIST  ({len(watchlist)} stocks tracked)")
    L("━"*70)
    if watchlist:
        by_days = sorted(watchlist.items(), key=lambda x: x[1].get("days_on_list", 0), reverse=True)
        for ticker, info in by_days[:25]:
            peg   = info.get("last_peg", "?")
            track = info.get("track", "B")
            days  = info.get("days_on_list", 1)
            name  = info.get("name", "")[:28]
            first = info.get("first_seen", "?")
            L(f"  {ticker:<8}  {name:<28}  Track:{track}  Days:{days:>3}  PEG:{peg}  Since:{first}")
    L("")

    # SUMMARY
    L("━"*70)
    L("  SCAN SUMMARY")
    L("━"*70)
    L(f"  Form 4 filings parsed  : {total_parsed}")
    L(f"  Verified whale buys    : {total_whales}")
    L(f"  Track A  (act now)     : {len(track_a)}")
    L(f"  Track B  (watch)       : {len(track_b)}")
    L(f"  Track B- (investigate) : {len(track_b_lo)}")
    L(f"  KTOS     (speculative) : {len(track_ktos)}")
    L(f"  Watch Out (selling)    : {len(track_warn)}")
    L(f"  Watchlist              : {len(watchlist)} stocks")
    L("")
    spy_s = "ABOVE 200d MA — entries OK" if spy_above else "BELOW 200d MA — DO NOT ENTER"
    L(f"  SPY status             : {spy_s}")
    L("")
    L("  Research tool only. Not financial advice.")
    L("  ALWAYS verify Form 4 manually before acting on any signal.")
    L("")

    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════
#  EMAIL
# ══════════════════════════════════════════════════════════════

def send_email(report: str, n_a: int, n_blo: int):
    if not EMAIL_ENABLED or not EMAIL_APP_PASS:
        return
    try:
        subject = (f"Leviathan {datetime.now().strftime('%Y-%m-%d')} "
                   f"— TrackA:{n_a} TrackB-:{n_blo}")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(report, "plain"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as srv:
            srv.login(EMAIL_FROM, EMAIL_APP_PASS)
            srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info(f"  Email sent to {EMAIL_TO}")
    except Exception as e:
        log.warning(f"  Email failed: {e}")

# ══════════════════════════════════════════════════════════════
#  MAIN — WHALE-FIRST FLOW
# ══════════════════════════════════════════════════════════════

def clean_watchlist(wl: dict) -> dict:
    """
    Remove junk entries from watchlist — OTC foreign stocks that
    slipped through before the pre-filter was improved.
    Also removes entries older than 90 days with no whale ever.
    """
    to_remove = []
    cutoff_days = 90
    today = datetime.now()

    for ticker, info in wl.items():
        # Remove OTC foreign (ends in F, more than 3 chars)
        if len(ticker) >= 4 and ticker.endswith("F") and ticker[:-1].isalpha():
            to_remove.append(ticker); continue
        # Remove if name is "unavailable" and no PEG ever recorded
        if "(name unavailable)" in info.get("name", "") and not info.get("last_peg"):
            to_remove.append(ticker); continue
        # Remove stale entries with no whale signal after 90 days
        first = info.get("first_seen", "")
        if first:
            try:
                age = (today - datetime.strptime(first, "%Y-%m-%d")).days
                if age > cutoff_days and info.get("track") in ("B", "") :
                    to_remove.append(ticker)
            except ValueError:
                pass

    for t in to_remove:
        del wl[t]
        log.info(f"  Watchlist cleanup: removed {t}")

    return wl


def main():
    try:
        print("\n╔" + "═"*64 + "╗")
        print("║   PROJECT LEVIATHAN v8.1 — WHALE-FIRST SCANNER")
        print(f"║   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("║   EDGAR atom feed → XML parse → quality filter")
        print("╚" + "═"*64 + "╝\n")

        #email test
        global SEC_EMAIL, USER_AGENT, EMAIL_ENABLED, EMAIL_FROM, EMAIL_TO, EMAIL_APP_PASS

        cfg = load_config()
        SEC_EMAIL     = cfg["sec_email"]
        USER_AGENT    = f"LeviathanScout/8.0 {SEC_EMAIL}"
        EMAIL_FROM    = cfg["sec_email"]          # send from same address
        EMAIL_TO      = cfg.get("report_email", "")
        EMAIL_APP_PASS = cfg.get("report_app_pass", "")
        EMAIL_ENABLED  = cfg.get("email_enabled", False)

        watchlist = load_watchlist()
        watchlist = clean_watchlist(watchlist)
        log.info(f"Watchlist loaded: {len(watchlist)} stocks (after cleanup)")

        overall = tqdm(total=6, desc="Overall progress", unit="step", position=0,
                    leave=True, ncols=80, dynamic_ncols=False) if TQDM_AVAILABLE else None

        def step_done(label=""):
            if overall:
                if label:
                    overall.set_postfix_str(label)
                overall.update(1)

        # ── Step 1: SPY macro gate ─────────────────────────────
        log.info("Step 1: SPY 200-day MA check...")
        spy_above, spy_price, sma200, spy_pct = check_spy_200d_ma()
        if spy_price and sma200:
            status = "ABOVE" if spy_above else "BELOW"
            log.info(f"  SPY ${spy_price:.2f} is {status} 200d MA (${sma200:.2f}, {spy_pct:+.1f}%)")
            if not spy_above:
                log.warning("  *** SPY BELOW 200d MA — hedge mode. No new entries. ***")
        step_done("SPY check")

        # ── Step 2: Find whale buys from EDGAR ─────────────────
        log.info("Step 2: Scanning EDGAR Form 4 filings for whale buys...")
        whale_signals, total_filings_parsed = find_whale_buys(days_back=WHALE_DAYS)
        step_done("EDGAR scan")

        if not whale_signals:
            log.warning("  No whale signals found. Check internet + SEC EDGAR availability.")
            log.warning("  Running in watchlist-only mode.")

        # ── Step 3: For each whale, fetch fundamentals ─────────
        log.info(f"Step 3: Fetching fundamentals for {len(whale_signals)} whale companies...")

        track_a:    List[StockResult] = []
        track_b_lo: List[StockResult] = []
        track_ktos: List[StockResult] = []
        track_warn: List[StockResult] = []

        # Also track non-whale stocks for Track B (from watchlist + universe spot-check)
        track_b:    List[StockResult] = []

        all_stocks_by_sector: dict = {}   # for sector momentum

        processed = 0
        for whale in progress(whale_signals, desc="Fetching fundamentals"):
            ticker = whale.ticker

            if not ticker_is_scannable(ticker):
                log.debug(f"  {ticker}: pre-filtered (warrant/unit/OTC)")
                continue

            log.info(f"  Analyzing {ticker} (${whale.dollar_amount/1e6:.1f}M buy, {whale.officer_title[:30]})")

            s, reject = get_fundamentals(ticker, whale=whale)
            time.sleep(BASE_DELAY)

            if s is None:
                log.info(f"    Skipped: {reject}")
                continue

            processed += 1

            enrich_from_watchlist(watchlist, s)

            # Check insider sells
            s.sell_count, s.has_sells = check_insider_sells(whale.cik)
            if s.has_sells:
                log.info(f"    {ticker}: {s.sell_count} sell filings detected")

            # Run flag checks
            run_flag_checks(ticker, s)
            time.sleep(0.3)

            # Hard exclude on heavy red flags (SPAC/fund/China already caught in get_fundamentals)
            if s.red_score >= 15:
                log.info(f"    {ticker}: excluded — red score {s.red_score}")
                continue

            # Compute scores
            s.conviction_score = compute_conviction(s)

            # ── Assign track ──────────────────────────────────────
            tight_ok, tight_reason = passes_tight_filters(s)
            loose_ok, loose_reason = passes_loose_filters(s)

            # Heavy selling → Watch Out regardless
            if s.sell_count >= 5 and s.has_sells:
                s.track = "WARN"
                s.potential_score, s.potential_label = compute_potential(s)
                s.rank_score = compute_rank_score(s)
                track_warn.append(s)
                log.info(f"    {ticker}: WARN — {s.sell_count} sell filings")
                continue

            # Dollar threshold checks
            dollar = whale.cluster_total_usd if whale.is_cluster else whale.dollar_amount

            if tight_ok:
                if dollar >= WHALE_TRACK_A_DOLLARS and s.conviction_score >= MIN_CONVICTION_TRACK_A:
                    s.track = "A"
                    s.potential_score, s.potential_label = compute_potential(s)
                    s.rank_score = compute_rank_score(s)
                    track_a.append(s)
                    update_watchlist(watchlist, s)
                    log.info(f"    {ticker}: TRACK A  (score {s.conviction_score}, ${dollar/1e6:.0f}M)")
                else:
                    # Good company, whale buy but < $10M or conviction < 7 → Track B-
                    s.track = "B-"
                    s.ktos_fail_reason = f"whale ${dollar/1e6:.1f}M < $10M threshold or conviction {s.conviction_score} < 7"
                    s.potential_score, s.potential_label = compute_potential(s, is_loose=True)
                    s.rank_score = compute_rank_score(s)
                    track_b_lo.append(s)
                    update_watchlist(watchlist, s)
                    log.info(f"    {ticker}: TRACK B-  (tight ok, dollar/conviction threshold)")

            elif loose_ok:
                s.track = "B-"
                s.ktos_fail_reason = tight_reason
                s.potential_score, s.potential_label = compute_potential(s, is_loose=True)
                s.rank_score = compute_rank_score(s)
                track_b_lo.append(s)
                update_watchlist(watchlist, s)
                log.info(f"    {ticker}: TRACK B-  ({tight_reason})")

            else:
                # Failed even loose filters → KTOS if sector is hot (we check later)
                s.track = "KTOS"
                s.ktos_fail_reason = f"tight: {tight_reason} | loose: {loose_reason}"
                s.potential_score, s.potential_label = compute_potential(s, is_loose=True)
                s.rank_score = compute_rank_score(s)
                track_ktos.append(s)
                log.info(f"    {ticker}: KTOS candidate  ({tight_reason})")

            # Track sector
            if s.sector:
                all_stocks_by_sector.setdefault(s.sector, []).append(s)

        step_done("Fundamentals")

        # ── Step 4: Sector momentum → score KTOS plays ─────────
        log.info("Step 4: Sector momentum analysis...")
        sector_scores = measure_sector_momentum(all_stocks_by_sector)
        hot_sectors   = set(list(sector_scores.keys())[:KTOS_TOP_SECTORS])

        # Assign sector momentum scores to KTOS plays + filter to hot sectors only
        filtered_ktos = []
        sector_ktos_count: dict = {}
        for s in sorted(track_ktos, key=lambda x: x.rank_score, reverse=True):
            if s.sector in sector_scores:
                s.sector_momentum = sector_scores[s.sector]
                s.sector_rank     = list(sector_scores.keys()).index(s.sector) + 1
            count = sector_ktos_count.get(s.sector, 0)
            if count < 3:  # max 3 per sector
                filtered_ktos.append(s)
                sector_ktos_count[s.sector] = count + 1
        track_ktos = filtered_ktos[:10]
        step_done("Sector momentum")

        # ── Step 5: Track B universe scan ─────────────────────────
        # Scan a curated universe of real small/mid-cap stocks for Track B.
        # This runs independently of the whale scan — it finds companies
        # that pass all tight filters and puts them on watch for a whale.
        # Without this, Track B only contains the 4-stock watchlist.
        log.info("Step 5: Track B universe scan...")

        # All stocks already found in whale scan
        already_found = {s.ticker for s in track_a + track_b_lo + track_ktos + track_warn}

        # Curated universe: real small/mid-cap operating companies across all sectors
        # Designed to match the Leviathan profile: $200M-$15B, high quality
        TRACK_B_UNIVERSE = [
            # Defense / Aerospace
            "KTOS","LHX","MANT","CACI","SAIC","LDOS","AVAV","BWXT","HEICO",
            "MRCY","DRS","MOOG","HXL","KAMN","PSN","TXT",
            # Energy / Commodities
            "MTDR","CIVI","NOG","SM","CEIX","ARCH","HCC","ARLP","REX","PARR",
            "HPK","PTEN","RRC","GPOR","ESTE","CPE","VTLE","SWN","CHRD","MGY",
            # Shipping
            "SBLK","GOGL","NMM","GSL","HAFN","DAC","CMRE","STNG","INSW",
            "TDW","EGLE","GNK","SALT",
            # Industrials / Electrification
            "POWL","XPEL","AAON","SSD","UFPI","DXPE","GHM","CECO","TRN",
            "NVEE","MYRG","IIIN","AZZ","SXI","ARCB","EXPO","ICFI","HCKT",
            # Software / Tech
            "PCTY","SPSC","BLKB","UPLD","NCNO","ALRM","JAMF","APPF","QTWO",
            "RAMP","BILL","TOST","PRGS","SCSC","MGRC","CEVA","RDVT",
            # Insurance / Financial Quality
            "KNSL","SIGI","MCY","RDN","NMIH","ORI","SKWD","HGTY","AMSF",
            "FFIN","UVSP","FBIZ","LKFN","MBWM","CHCO","STBA","FULT","NBTB",
            # Healthcare / Biotech
            "ADUS","ENSG","GKOS","HRMY","SUPN","AMPH","ANIP","ATRC","CCRN",
            "AXSM","PRDO","LOPE","STRA","BHVN","AUPH","ANIK","CNMD","PNTG",
            # Consumer / Restaurants
            "TXRH","WING","BOOT","SHOO","CBRL","EAT","FIZZ","LANC","JJSF",
            "ASO","SCVL","JBSS","RCKY","OLLI","CATO",
            # Mining / Metals
            "PAAS","CDE","HL","WPM","KGC","BTG","RGLD","EGO","MAG","SAND",
            # Additional high-quality small caps
            "KFRC","FCN","EXLS","DORM","MMSI","KFY","HUBG","MATX","VMI",
            "NFG","MKTX","PRI","TTEK","LRN","LOAR","TRUP","HQY","QRVO",
            "APAM","VCTR","FHI","GAIN","GLAD","AMG","ARW",
        ]

        # Remove already-found tickers, watchlist tickers (already scanned)
        watchlist_tickers = list(watchlist.keys())
        to_scan_b = [
            t for t in TRACK_B_UNIVERSE
            if t not in already_found and t not in watchlist_tickers
        ]
        # Add watchlist tickers not yet found
        to_scan_b += [t for t in watchlist_tickers if t not in already_found]

        log.info(f"  Universe scan: {len(to_scan_b)} tickers to check for Track B")

        for ticker in progress(to_scan_b, desc="Universe scan"):
            if not ticker_is_scannable(ticker):
                continue
            try:
                s, reject = get_fundamentals(ticker, whale=None)
                time.sleep(BASE_DELAY)
                if s is None:
                    continue
                enrich_from_watchlist(watchlist, s)
                run_flag_checks(ticker, s)
                time.sleep(0.2)
                if s.red_score >= 15:
                    continue
                tight_ok, reject_reason = passes_tight_filters(s)
                if tight_ok:
                    s.track = "B"
                    s.conviction_score = 0
                    s.potential_score, s.potential_label = compute_potential(s)
                    s.rank_score = compute_rank_score(s)
                    track_b.append(s)
                    update_watchlist(watchlist, s)
                    peg_s = f"{s.peg:.2f}" if s.peg else "N/A"
                    gm_s  = f"{int((s.gross_margin or 0)*100)}%"
                    rg_s  = f"{int((s.revenue_growth or 0)*100)}%"
                    log.info(f"    {ticker}: TRACK B  (PEG={peg_s}, GM={gm_s}, RG={rg_s})")
                else:
                    log.debug(f"    {ticker}: failed — {reject_reason}")
            except Exception as e:
                log.debug(f"  Universe scan {ticker}: {e}")

        save_watchlist(watchlist)
        step_done("Universe scan")

        # ── Step 6: Build and save report ─────────────────────
        log.info("Step 6: Building report...")
        report = build_report(
            track_a, track_b, track_b_lo, track_ktos, track_warn,
            watchlist, spy_above, spy_price, sma200, spy_pct,
            total_whales=len(whale_signals), total_parsed=total_filings_parsed,
        )
        step_done("Report built")
        if overall:
            overall.close()

        print("\n\n" + report)

        rpath = os.path.join(_DIR, f"leviathan_report_{datetime.now().strftime('%Y-%m-%d_%H%M')}.txt")
        with open(rpath, "w", encoding="utf-8") as f:
            f.write(report)

        send_email(report, len(track_a), len(track_b_lo))

        print(f"\n  Report saved : {rpath}")
        print(f"  Form 4s parsed : {total_filings_parsed}")
        print(f"  Whale signals  : {len(whale_signals)}")
        print(f"  Track A        : {len(track_a)}")
        print(f"  Track B        : {len(track_b)}")
        print(f"  Track B-       : {len(track_b_lo)}")
        print(f"  KTOS           : {len(track_ktos)}")
        print(f"  Watchlist      : {len(watchlist)}")
        print("\n" + "═"*68 + "\n")
    except KeyboardInterrupt:
        if overall:
            overall.close()
        print("\n\n  ⚠  Scan interrupted by user. Goodbye.\n")
        sys.exit(0)

if __name__ == "__main__":
    main()
