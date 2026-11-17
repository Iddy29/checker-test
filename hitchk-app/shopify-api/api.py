#!/usr/bin/env python3
"""Shopify Card Checker API - Railway-style simple implementation.

Uses shopify_checker.py (312 lines) which reaches the bank.
No captcha, no complex headers, just clean aiohttp requests.
"""
import os, sys, json, time, asyncio, logging, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from typing import Optional, List
from pydantic import BaseModel

from shopify_checker import shopify_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shopify-api")

app = FastAPI(title="Shopify Card Checker API")

# ── Config ────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", "8081"))
VALID_API_KEYS = set(k.strip() for k in os.environ.get("API_KEYS", "oozaruhshop,shopifyprod").split(",") if k.strip())

# Load sites from site.txt if available
def _load_sites():
    sites = []
    try:
        with open(os.path.join(os.path.dirname(__file__), "site.txt")) as f:
            sites = [l.strip() for l in f if l.strip() and "." in l]
    except Exception:
        pass
    return sites

SITE_LIST = _load_sites()
logger.info(f"Loaded {len(SITE_LIST)} sites from site.txt")

# ── Response mapper ───────────────────────────────────────────────────────
def _map_result(r, cc_str, site, proxy_used):
    """Map shopify_check output to bot-expected format."""
    verdict = r.get("verdict", "UNTESTED")
    message = r.get("message", "")
    code = r.get("code", "")
    amount = r.get("amount", "N/A")
    gate = r.get("gate", "Shopify Payments")
    r_site = r.get("site", site or "N/A")
    r_time = r.get("time", "0.0s")

    # Determine status
    charged = verdict == "APPROVED_CHARGED"
    approved = "APPROVED" in verdict
    three_ds = verdict == "APPROVED_3DS"

    # Response code for bot
    if charged:
        resp = "ORDER_CREATED"
    elif three_ds:
        resp = "3DS_REQUIRED"
    elif "LIVE" in verdict:
        resp = code.upper() or "INSUFFICIENT_FUNDS"
    elif "DEAD" in verdict:
        resp = code.upper() or "CARD_EXPIRED"
    elif verdict == "DECLINED":
        resp = code.upper() or "CARD_DECLINED"
    elif verdict == "UNTESTED":
        resp = code.upper() or "UNTESTED"
    else:
        resp = verdict

    # Bot status
    if charged or approved:
        status = "approved" if not three_ds else "approved_3ds"
    elif resp == "UNTESTED" or "SITE_ERROR" in resp or "NO_PRODUCT" in resp:
        status = "error"
    else:
        status = "declined"

    return {
        "Response": resp,
        "Message": message,
        "Gateway": gate,
        "Price": str(amount),
        "Site": r_site,
        "Charged": "True" if charged else "False",
        "Approved": "True" if approved else "False",
        "Time": r_time,
        "Product": "",
        "Proxy": proxy_used or "not used",
        "code": code,
        "decline_code": code,
        "Retry": False,
        "BankReached": verdict not in ("UNTESTED",),
        "status": status,
        "verdict": verdict,
        "elapsed": float(r_time.rstrip("s")) if r_time.rstrip("s").replace(".", "", 1).isdigit() else 0.0,
    }


def _check_key(key):
    if VALID_API_KEYS and (not key or key not in VALID_API_KEYS):
        return False
    return True


def _get_sites(site_param):
    """Get sites to try - from param, site.txt, or built-in list."""
    if site_param:
        sites = [s.strip().replace("https://", "").replace("http://", "").rstrip("/") for s in site_param.split(",") if s.strip()]
        if sites:
            return sites
    # Use site.txt if available, otherwise built-in
    if SITE_LIST:
        return SITE_LIST.copy()
    from shopify_checker import SHOPIFY_SITES
    return SHOPIFY_SITES.copy()


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status": "running",
        "service": "Shopify Card Checker API (Railway-style)",
        "endpoints": {
            "GET /shopify": "Card checker (params: site, cc, proxy, key)",
            "GET /shopii": "Card checker - Railway format",
            "POST /shopify/mass": "Mass card checker",
            "GET /health": "Health check",
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Shopify Card Checker API"}


@app.get("/status")
async def status():
    return {
        "status": "online",
        "sites": len(SITE_LIST),
        "keys_configured": len(VALID_API_KEYS),
        "verdicts": ["APPROVED_CHARGED", "APPROVED_LIVE", "APPROVED_3DS", "DECLINED", "DECLINED_DEAD", "UNTESTED"],
    }


@app.get("/sites")
async def get_sites():
    return {"count": len(SITE_LIST), "sites": SITE_LIST[:50]}


@app.get("/shopify")
async def shopify_get(
    site: Optional[str] = Query(None),
    cc: Optional[str] = Query(None),
    proxy: Optional[str] = Query(None),
    key: Optional[str] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
):
    """Shopify card checker - bot format."""
    if not _check_key(key):
        return JSONResponse({"error": "Invalid or missing API key", "Response": "API_AUTH_ERROR"}, status_code=401)

    if not cc:
        return {"Response": "ERROR", "Message": "cc parameter required", "CC": cc or "", "Price": "N/A", "Gateway": "Shopify Payments", "Site": site or "N/A", "Charged": "False", "Approved": "False", "Time": "0.0s", "status": "error"}

    parts = cc.split("|")
    if len(parts) < 4:
        return {"Response": "ERROR", "Message": "Invalid format. Use: number|mm|yy|cvv", "CC": cc, "Price": "N/A", "Gateway": "Shopify Payments", "Site": site or "N/A", "Charged": "False", "Approved": "False", "Time": "0.0s", "status": "error"}

    card_num, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]

    # Get sites to try
    sites = _get_sites(site)

    # Try each site
    start = time.time()
    proxy_url = proxy.strip() if proxy and proxy.strip() else None

    result = None
    for s in sites[:5]:
        try:
            r = await asyncio.wait_for(
                shopify_check(card_num, mm, yy, cvv, site=s, proxy=proxy_url),
                timeout=30
            )
            if r and r.get("verdict") != "UNTESTED":
                result = r
                break
            if r:
                result = r
        except asyncio.TimeoutError:
            logger.info(f"Timeout on {s}")
            continue
        except Exception as e:
            logger.info(f"Error on {s}: {str(e)[:80]}")
            continue

    if not result:
        elapsed = round(time.time() - start, 2)
        return {
            "Response": "UNTESTED",
            "Message": "All sites failed",
            "Gateway": "Shopify Payments",
            "Price": "N/A",
            "Site": site or "N/A",
            "Charged": "False",
            "Approved": "False",
            "Time": f"{elapsed}s",
            "Product": "",
            "Proxy": proxy_url or "not used",
            "code": "ALL_FAILED",
            "decline_code": "",
            "Retry": False,
            "BankReached": False,
            "status": "error",
            "verdict": "UNTESTED",
            "elapsed": elapsed,
        }

    return _map_result(result, cc, site, proxy_url)


@app.get("/shopii")
async def shopii(
    site: Optional[str] = Query(None),
    cc: Optional[str] = Query(None),
    proxy: Optional[str] = Query(None),
    key: Optional[str] = Query(None),
):
    """Railway-format endpoint."""
    if not _check_key(key):
        return JSONResponse({"error": "Invalid or missing API key", "Response": "API_AUTH_ERROR"}, status_code=401)

    if not cc:
        return {"CC": "", "Currency": "USD", "Gateway": "unknown", "Price": "0.00", "Proxy": "not used", "Response": "ERROR", "Status": "false"}

    parts = cc.split("|")
    if len(parts) < 4:
        return {"CC": cc, "Currency": "USD", "Gateway": "unknown", "Price": "0.00", "Proxy": "not used", "Response": "INVALID_FORMAT", "Status": "false"}

    card_num, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    sites = _get_sites(site)
    proxy_url = proxy.strip() if proxy and proxy.strip() else None

    result = None
    for s in sites[:5]:
        try:
            r = await asyncio.wait_for(
                shopify_check(card_num, mm, yy, cvv, site=s, proxy=proxy_url),
                timeout=30
            )
            if r and r.get("verdict") != "UNTESTED":
                result = r
                break
            if r:
                result = r
        except (asyncio.TimeoutError, Exception):
            continue

    if not result:
        return {"CC": cc, "Currency": "USD", "Gateway": "unknown", "Price": "0.00", "Proxy": proxy_url or "not used", "Response": "UNTESTED", "Status": "false"}

    verdict = result.get("verdict", "UNTESTED")
    return {
        "CC": cc,
        "Currency": "USD",
        "Gateway": result.get("gate", "shopify_payments"),
        "Price": str(result.get("amount", "0.00")),
        "Proxy": proxy_url or "not used",
        "Response": result.get("code", verdict).upper(),
        "Status": "true" if "APPROVED" in verdict else "false",
    }


# ── Mass endpoint for /mtxt ───────────────────────────────────────────────

class MassRequest(BaseModel):
    cards: List[str] = []
    site: Optional[str] = None
    proxy: Optional[str] = None
    key: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None


@app.post("/shopify/mass")
async def shopify_mass(req: MassRequest):
    """Mass card checker - process multiple cards in parallel."""
    if not _check_key(req.key):
        return JSONResponse({"error": "Invalid or missing API key", "Response": "API_AUTH_ERROR"}, status_code=401)

    cards = [c.strip() for c in req.cards if c.strip()]
    if not cards:
        return {"results": [], "error": "No cards provided"}

    sites = _get_sites(req.site)
    proxy_url = req.proxy.strip() if req.proxy and req.proxy.strip() else None

    sem = asyncio.Semaphore(10)  # Max 10 concurrent checks

    async def _check_one(card_str):
        async with sem:
            parts = card_str.split("|")
            if len(parts) < 4:
                return {"Response": "ERROR", "Message": "Invalid format", "CC": card_str, "Price": "N/A", "Gateway": "Shopify Payments", "Charged": "False", "Approved": "False", "Time": "0.0s", "status": "error"}

            card_num, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]

            # Pick a random site for each card
            s = random.choice(sites) if sites else None
            if not s:
                return {"Response": "NO_SITES", "Message": "No sites available", "CC": card_str, "Price": "N/A", "Gateway": "Shopify Payments", "Charged": "False", "Approved": "False", "Time": "0.0s", "status": "error"}

            try:
                r = await asyncio.wait_for(
                    shopify_check(card_num, mm, yy, cvv, site=s, proxy=proxy_url),
                    timeout=30
                )
                if r:
                    return _map_result(r, card_str, s, proxy_url)
            except (asyncio.TimeoutError, Exception) as e:
                logger.info(f"Mass error {card_str[:6]}xx: {str(e)[:60]}")

            return {"Response": "UNTESTED", "Message": "Check failed", "CC": card_str, "Price": "N/A", "Gateway": "Shopify Payments", "Charged": "False", "Approved": "False", "Time": "0.0s", "status": "error"}

    tasks = [asyncio.create_task(_check_one(c)) for c in cards]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({"Response": "ERROR", "Message": str(r)[:100], "CC": cards[i], "Price": "N/A", "Gateway": "Shopify Payments", "Charged": "False", "Approved": "False", "Time": "0.0s", "status": "error"})
        else:
            out.append(r)

    return {"results": out}


# ── POST /check for backward compat ───────────────────────────────────────

class CheckRequest(BaseModel):
    cc: str
    mm: str
    yy: str
    cvv: str
    site: Optional[str] = None
    sites: Optional[List[str]] = None
    proxy: Optional[str] = None


@app.post("/check")
async def check_post(req: CheckRequest):
    if len(req.yy) == 4:
        req.yy = req.yy[2:]
    req.mm = req.mm.zfill(2)

    sites = []
    if req.site:
        sites = [req.site.replace("https://", "").replace("http://", "").rstrip("/")]
    elif req.sites:
        sites = [s.replace("https://", "").replace("http://", "").rstrip("/") for s in req.sites if s]

    if not sites:
        sites = _get_sites(None)

    proxy_url = req.proxy.strip() if req.proxy and req.proxy.strip() else None
    start = time.time()

    result = None
    for s in sites[:5]:
        try:
            r = await asyncio.wait_for(
                shopify_check(req.cc, req.mm, req.yy, req.cvv, site=s, proxy=proxy_url),
                timeout=30
            )
            if r and r.get("verdict") != "UNTESTED":
                result = r
                break
            if r:
                result = r
        except (asyncio.TimeoutError, Exception):
            continue

    elapsed = round(time.time() - start, 2)

    if not result:
        return {"status": "error", "response": "UNTESTED", "gateway": "Shopify Payments", "amount": None, "site": req.site, "elapsed": elapsed, "verdict": "UNTESTED", "code": "ALL_FAILED", "iso": "", "meaning": "All sites failed"}

    mapped = _map_result(result, f"{req.cc}|{req.mm}|{req.yy}|{req.cvv}", req.site, proxy_url)
    mapped["elapsed"] = elapsed
    return mapped


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
