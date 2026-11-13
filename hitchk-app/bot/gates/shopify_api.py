"""Shopify API client for the bot.
Calls the Shopify Validator API at localhost:8081 (and 8082 round-robin).
"""
import os
import asyncio
import logging
import aiohttp
import json

logger = logging.getLogger("shopify_api")

SHOPIFY_API_URL = os.environ.get("SHOPIFY_API_URL", "http://127.0.0.1:8081").rstrip("/")
SHOPIFY_API_URL_2 = os.environ.get("SHOPIFY_API_URL_2", "http://127.0.0.1:8082").rstrip("/")
SHOPIFY_API_KEY = os.environ.get("SHOPIFY_API_KEY", "shopifyprod")
SHOPIFY_API_TIMEOUT = float(os.environ.get("SHOPIFY_API_TIMEOUT", "8"))

# Site errors that mean "try next store" (not a card verdict)
SITE_ERROR_PREFIXES = (
    "NO_PRODUCT", "CART_FAILED", "SITE_REQUIRES_LOGIN", "NO_SESSION_TOKEN",
    "NO_ATTEMPT_TOKEN", "NO_SHOPIFY_PAYMENTS_GATEWAY", "CHECKPOINTDENIED",
    "THROTTLED", "TIMEOUT", "SITE_ERROR", "PROXY_ERROR", "NO_SELLER_PROPOSAL",
    "GRAPHQL_ERROR", "NO_PAYMENT_REQUIRED", "CART_ERROR", "PRICE_OVER_MAX",
    "CAPTCHA_REQUIRED", "TOKENIZATION_FAILED", "SUBMIT_FAILED",
    "ORDER_CREATION_FAILED", "NEGOTIATE_FAILED", "PROCESSING_ERROR",
)

_api_rr = 0

def _get_next_api_url():
    """Round-robin between API 1 (8081) and API 2 (8082)."""
    global _api_rr
    _api_rr += 1
    if _api_rr % 2 == 0 and SHOPIFY_API_URL_2:
        return SHOPIFY_API_URL_2
    return SHOPIFY_API_URL


async def call_shopify_api(cc, mm, yy, cvv, site=None, proxy=None,
                             timeout=None, price_min=None, price_max=None,
                             **kwargs):
    """Single card check via Shopify Validator API. Returns dict response."""
    _timeout = float(timeout or SHOPIFY_API_TIMEOUT)
    _api_url = _get_next_api_url()

    params = {
        "cc": f"{cc}|{mm}|{yy}|{cvv}",
        "key": SHOPIFY_API_KEY,
    }
    if site:
        params["site"] = site
    if proxy:
        params["proxy"] = proxy
    if price_min is not None:
        params["min_price"] = str(price_min)
    if price_max is not None:
        params["max_price"] = str(price_max)

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                        f"{_api_url}/shopify",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=_timeout),
                        headers={"Accept": "application/json"}) as resp:
                    text = await resp.text()
            try:
                return json.loads(text)
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(1)
                    _api_url = _get_next_api_url()
                    continue
                return {"Response": "UNKNOWN", "Message": f"JSON error: {text[:50]}"}
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(1)
                _api_url = _get_next_api_url()
                continue
            return {"Response": "UNKNOWN", "Message": str(e)[:80]}
    return {"Response": "UNKNOWN", "Message": "all retries failed"}


async def call_shopify_api_mass(cards, site=None, proxy=None,
                                   price_min=None, price_max=None, timeout=None):
    """Mass card check - calls /shopify per card in parallel (20 concurrent).
    Returns list of dicts in same order as input cards."""
    cards = [str(c or "").strip() for c in (cards or []) if str(c or "").strip()]
    if not cards:
        return []

    _timeout = float(timeout or 15)
    _sem = asyncio.Semaphore(20)

    _params_base = {"key": SHOPIFY_API_KEY}
    if site:
        _params_base["site"] = site
    if proxy:
        _params_base["proxy"] = proxy
    if price_min is not None:
        _params_base["min_price"] = str(price_min)
    if price_max is not None:
        _params_base["max_price"] = str(price_max)

    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=_timeout),
        headers={"Accept": "application/json"})

    async def _check_one(card_str):
        for attempt in range(3):
            async with _sem:
                try:
                    _api_url = _get_next_api_url()
                    params = {**_params_base, "cc": card_str}
                    async with _session.get(
                            f"{_api_url}/shopify",
                            params=params) as resp:
                        text = await resp.text()
                    try:
                        return json.loads(text)
                    except Exception:
                        if attempt < 2:
                            await asyncio.sleep(1)
                            continue
                        return {"Response": "UNKNOWN", "Message": f"JSON error: {text[:50]}"}
                except Exception as e:
                    if attempt < 2:
                        await asyncio.sleep(1)
                        continue
                    return {"Response": "UNKNOWN", "Message": str(e)[:80]}
        return {"Response": "UNKNOWN", "Message": "all retries failed"}

    results = await asyncio.gather(*[_check_one(c) for c in cards])
    await _session.close()
    return list(results)
