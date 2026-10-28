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
)

# An expired card is a dead card, not a live one -- the validator hands back
# Shopify's BASE_EXPIRED code, which must not be read as approval.
EXPIRED_MARKERS = (
    "BASE_EXPIRED", "CARD_EXPIRED", "EXPIRED_CARD", "INVALID_EXPIRY",
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
    (("INVALID_NUMBER", "INVALID_CARD", "INCORRECT_NUMBER", "BAD_NUMBER",
      "NO_SUCH_ISSUER"), "The card number is not valid"),
    (("LOST", "STOLEN", "PICKUP", "RESTRICTED", "REVOCATION",
      "CALL_ISSUER", "REFER_TO_ISSUER"), "The issuer refused the card"),
    (("3DS_REQUIRED", "THREE_D_SECURE", "SCA_REQUIRED"),
     "The card needs a 3D Secure check"),
    (("CAPTCHA_REQUIRED", "CAPTCHA", "CHALLENGE_REQUIRED"),
     "The store asked for a captcha"),
    (("THROTTLED", "RATE_LIMIT"), "The store is rate limiting us"),
    (("NO_PRODUCT", "OUT_OF_STOCK", "INVENTORY"),
     "The store has no product to buy"),
)


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
    params = {
        "site": site or "",
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

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"{SHOPIFY_API_URL}/shopify",
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                body = await resp.json(content_type=None)
    except Exception as e:
        logger.warning("shopify api failed: %s", str(e)[:120])
        return {
            "status": "error",
            "response": f"API error: {type(e).__name__}: {str(e)[:100]}",
            "gateway": "Shopify Payments",
            "amount": None,
            "site": site,
            "elapsed": round(time.time() - t0, 2),
            "charged": False,
            "extra": None,
        }

    resp_text = str(body.get("Response", "") or "").strip()
    message = str(body.get("Message", "") or resp_text).strip()
    elapsed = round(time.time() - t0, 2)

    is_site_error = any(resp_text.startswith(k) for k in SITE_ERROR_PREFIXES)
    # The code can sit in the response or in the message, so both are read.
    is_expired = any(k in (resp_text + " " + message).upper() for k in EXPIRED_MARKERS)
    charged = str(body.get("Charged", "False")).lower() == "true"
    approved = str(body.get("Approved", "False")).lower() == "true"

    if is_site_error:
        status = "error"
    elif is_expired:
        # The store says the card is expired.  That is a dead card, and it
        # stays a dead card even when the validator's own flag says approved.
        status = "declined"
    elif charged or approved:
        status = "approved"
    else:
        status = "declined"

    explanation = _reason_for(resp_text + " " + message)
    if not explanation and status == "declined":
        explanation = "The issuer declined the card"

    return {
        "status": status,
        "response": message or resp_text or "Unknown",
        "gateway": body.get("Gateway", "Shopify Payments"),
        "amount": body.get("Price"),
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


async def shopify_api_check_1_2(cc, mm, yy, cvv, site=None, proxy=None, user_id=None,
                                **kwargs):
    """Shopify, but the store sells an item worth $1 to $2.

    The validator picks the cheapest product inside the band, so the
    order is worth $1 to $2 instead of the $0.25 sticker a plain
    /shp buys.
    """
    kwargs["price_min"] = SHOPIFY_MIN_PRICE
    kwargs["price_max"] = SHOPIFY_MAX_PRICE
    return await shopify_api_check(cc, mm, yy, cvv, site=site, proxy=proxy,
                                    user_id=user_id, **kwargs)


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

    response = str(result.get("response", "") or "Unknown").strip()
    message = response

    if result.get("status") == "error":
        return message

    if any(k in response.upper() for k in EXPIRED_MARKERS):
        return f"Declined - {message}"

    if result.get("status") == "approved":
        return f"Approved - {message}"

    price = result.get("amount")
    if price and str(price) != "N/A":
        return f"{message} | ${price}"

    return message or response
