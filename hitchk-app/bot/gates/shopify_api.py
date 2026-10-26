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
)

# An expired card is a dead card, not a live one -- the validator hands back
# Shopify's BASE_EXPIRED code, which must not be read as approval.
EXPIRED_MARKERS = (
    "BASE_EXPIRED", "CARD_EXPIRED", "EXPIRED_CARD",
    "PAYMENTS_CREDIT_CARD_BASE_EXPIRED",
)


async def call_shopify_api(cc, mm, yy, cvv, site=None, proxy=None, timeout=120):
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
    is_expired = any(k in resp_text.upper() for k in EXPIRED_MARKERS)
    charged = str(body.get("Charged", "False")).lower() == "true"
    approved = str(body.get("Approved", "False")).lower() == "true"

    if is_site_error:
        status = "error"
    elif charged or approved:
        status = "approved"
    else:
        status = "declined"

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
