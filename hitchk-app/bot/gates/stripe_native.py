"""The checkout, in a real browser.

Stripe's own script puts up two captchas, and they are not the same
one.  The first belongs to the page and it gives an answer by itself.
The second belongs to the payment intent, it puts up a box that has to
be ticked, and it is the one the payment intent is waiting for.

When the card is put into the page by hand, stripe's own script does
all of it: it puts up the captcha, it hands the answer over, and it
takes the card through the bank.  That is the whole way round, and it
is the only way round that the bank believes.

So the card is put into the page by hand here, the box is ticked when
it shows, and the card is read from stripe's own frame, which says
which intent it is about.
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import time
from urllib.parse import unquote

logger = logging.getLogger("stripe_native")

# How long the page is given to finish.  The captcha is about ten
# seconds, the bank is given the rest.
THE_TIME_TO_WAIT = 100

# The things that are put into the page.
THE_COUNTRY = "US"
THE_CITIES = [
    ("Portland", "OR", "97201"), ("Salem", "OR", "97301"), ("Eugene", "OR", "97401"),
    ("Bend", "OR", "97701"), ("Billings", "MT", "59101"), ("Helena", "MT", "59601"),
    ("Missoula", "MT", "59801"), ("Wilmington", "DE", "19801"), ("Dover", "DE", "19901"),
    ("Manchester", "NH", "03101"), ("Concord", "NH", "03301"), ("Nashua", "NH", "03060"),
    ("Anchorage", "AK", "99501"), ("Fairbanks", "AK", "99701"), ("Juneau", "AK", "99801"),
]
THE_STREETS = [
    ("Main St", "Oak Ave", "Elm St", "Park Blvd", "Cedar Ln", "Maple Dr",
     "Washington St", "Lake Ave", "Hill Rd", "Valley Way", "Pine St", "River Rd"),
]


def _name():
    first = random.choice(["James", "Mary", "Robert", "Patricia", "John", "Jennifer",
                           "Michael", "Linda", "David", "Elizabeth", "William", "Barbara",
                           "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah"])
    last = random.choice(["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
                           "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez",
                           "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore"])
    return first, last


def _street():
    number = random.randint(10, 9999)
    name = random.choice(THE_STREETS)
    return "%d %s" % (number, name)


def the_key_of(link):
    """The key that goes with the link, out of the part after the #.

    The part after the # holds the key, turned a little so it cannot be
    read by accident.
    """
    try:
        if "#" not in link:
            return ""
        raw = unquote(link.split("#", 1)[1])
        padded = raw + "=" * (-len(raw) % 4)
        turned = base64.b64decode(padded)
        text = "".join(chr(b ^ 5) for b in turned)
        return (json.loads(text) or {}).get("apiKey") or ""
    except Exception:
        return ""


# The country is a list that stripe keeps in a box of its own, and it is
# closed until the page says so.  It is set from the script, and the
# page is told, so it never opens over the form.
SET_THE_COUNTRY = """
(() => {
    const s = document.getElementById('billingCountry');
    if (!s) { return 'no country'; }
    s.disabled = false;
    s.value = '%s';
    s.dispatchEvent(new Event('change', {bubbles: true}));
    s.dispatchEvent(new Event('input', {bubbles: true}));
    return s.value;
})()
"""

# The other boxes of the form are set the same way, so the page knows
# what is in them.
SET_A_BOX = """
((name, value) => {
    const el = document.querySelector('input[name="' + name + '"]');
    if (!el) { return 'none'; }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
    return el.value;
})
"""

OPEN_THE_CARD_PART = """
(() => {
    const b = document.querySelector('[data-testid="card-accordion-item-button"]');
    if (!b) { return 'no button'; }
    b.scrollIntoView({block: 'center'});
    b.click();
    return 'clicked';
})()
"""

PRESS_PAY = """
(() => {
    const b = document.querySelector('[data-testid="hosted-payment-submit-button"]')
           || document.querySelector('button[type="submit"]');
    if (!b) { return 'no button'; }
    if (b.disabled) { return 'the button is closed'; }
    b.scrollIntoView({block: 'center'});
    b.click();
    return 'clicked';
})()
"""

# The box that hCaptcha puts up when it wants to be sure.  It is in a
# frame of hCaptcha's own, and the frame says which kind it is.
TICK_THE_BOX = """
() => {
    const el = document.querySelector('#checkbox');
    if (!el) { return 'no box'; }
    try { el.click(); return 'ticked'; } catch (e) { return 'the tick failed: ' + e; }
}
"""

# Stripe's own frame for the challenge of the intent.  Its address says
# which intent it is about, and it holds the key and the request data
# of the challenge.
THE_INTENT_FRAME = "hcaptcha-inner"

# The browser of the checkout has a profile of its own.  The browser
# that answers the captcha has one too, and two browsers cannot hold
# the same one at the same time.
THE_PROFILE = "/tmp/lc_checkout_profile"


def _the_profile():
    """The profile of the checkout's own browser."""
    path = THE_PROFILE
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        return "/tmp"
    return path


def _clear_the_lock():
    """Take the lock of a browser that died away."""
    try:
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            full = os.path.join(_the_profile(), name)
            if os.path.islink(full) or os.path.exists(full):
                try:
                    os.unlink(full)
                except OSError:
                    pass
    except Exception:
        pass


def _the_intent_in_the_frames(page):
    """The intent stripe's own frame is about, if it is there.

    The frame's address carries the intent, its secret, and the address
    the answer is handed to.
    """
    for frame in page.frames:
        url = frame.url or ""
        if THE_INTENT_FRAME not in url:
            continue
        tail = url.split("#", 1)[-1]
        found = {}
        for part in tail.split("&"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            found[name] = unquote(value)
        intent_id = found.get("intentId") or ""
        if not intent_id:
            continue
        verify = found.get("verifyUrl") or ""
        if verify and not verify.startswith("/"):
            verify = "/" + verify
        return {
            "intent_id": intent_id,
            "client_secret": found.get("clientSecret") or "",
            "site_key": found.get("sitekey") or "",
            "rqdata": found.get("rqdata") or "",
            "verify_url": verify,
        }
    return None


def _classify(intent):
    """What stripe's answer says about the card."""
    status = intent.get("status") or ""
    if status == "succeeded":
        return "charged", "Charged"
    if status == "requires_capture":
        return "charged", "Authorized"
    if status == "processing":
        return "approved", "Processing"
    if status == "requires_action":
        action = intent.get("next_action") or {}
        kind = action.get("type") or ""
        if not kind:
            kind = (action.get("use_stripe_sdk") or {}).get("type") or ""
        if "three_d_secure" in kind or "3ds" in kind:
            return "approved_3ds", "3DS required"
        return "live", "3DS required"
    error = intent.get("last_payment_error") or intent.get("last_setup_error") or {}
    if error:
        code = error.get("code") or ""
        decline = error.get("decline_code") or ""
        message = str(error.get("message") or "")[:90]
        if code == "card_declined" and decline:
            return "declined", decline
        if code == "card_declined":
            return "declined", "card_decline"
        if code == "expired_card":
            return "declined", "expired_card"
        if code == "incorrect_cvc":
            return "declined", "incorrect_cvc"
        if code == "processing_error":
            return "declined", "processing_error"
        if code == "payment_intent_authentication_failure":
            return "declined", "3DS authentication failed"
        if "captcha" in message.lower():
            return "error", "hCaptcha Challenge Required"
        return "declined", (decline or code or message or "card declined")[:60]
    if status == "canceled":
        return "declined", "Payment canceled"
    if status == "requires_payment_method":
        return "declined", "Payment method failed"
    return "error", ("Payment failed (%s)" % (status or "unknown"))[:60]


def _card_info(intent):
    """What is known about the card, for the record."""
    method = intent.get("payment_method") or {}
    if isinstance(method, str):
        method = {}
    card = method.get("card") or {}
    if not card:
        return ""
    brand = card.get("brand") or ""
    last = card.get("last4") or ""
    if not brand and not last:
        return ""
    return "%s %s" % (brand.upper(), last).strip()


async def _read_the_intent(client, pk, intent_id, client_secret):
    """The intent, read from stripe."""
    try:
        r = await client.get(
            "https://api.stripe.com/v1/payment_intents/%s" % intent_id,
            params={"key": pk, "client_secret": client_secret},
            headers={"origin": "https://checkout.stripe.com",
                     "referer": "https://checkout.stripe.com/"},
            timeout=12)
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.info("the intent could not be read: %s", e)
        return {}


def _the_intent_in_the_wire(seen):
    """The intent out of what stripe said on the wire.

    Stripe's own script says what happened to the intent in its answer
    to the confirm.  The answer is the truth about the card.
    """
    best = None
    for status, url, body in seen:
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        intent = data.get("payment_intent") or {}
        if not intent and data.get("object") in ("payment_intent", "setup_intent"):
            intent = data
        if intent:
            best = intent
    return best


async def native_check(cc, mm, yy, cvv, checkout_url, proxy=None, timeout=None):
    """The card in the real checkout, in a real browser.

    Returns the same thing the other gates return: what happened to the
    card, what stripe said, what is known about the card, and how long
    it took.
    """
    started = time.time()
    seen = []
    try:
        from local_hcaptcha_solver import (PAGE_OPTIONS, STEALTH,
                                           _playwright_proxy,
                                           apply_browser_identity, client_hints_metadata)
    except Exception as e:
        logger.info("the browser helpers are missing: %s", e)
        return "error", "The browser is not available", "", 0.0

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger.info("playwright is missing: %s", e)
        return "error", "The browser is not available", "", 0.0

    from browser_identity import BrowserClient

    launch = {
        "headless": not bool(os.environ.get("DISPLAY")),
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                 "--disable-blink-features=AutomationControlled",
                 "--window-size=1366,900"],
    }
    pw_proxy = _playwright_proxy(proxy)
    if pw_proxy:
        launch["proxy"] = pw_proxy

    pk = the_key_of(checkout_url)
    client_kwargs = {"timeout": 15, "follow_redirects": True, "proxy": None}
    if proxy and proxy != "NONE":
        client_kwargs["proxy"] = proxy

    pw = None
    ctx = None
    try:
        pw = await async_playwright().start()
        try:
            ctx = await pw.chromium.launch_persistent_context(
                _the_profile(), channel="chrome", bypass_csp=True,
                **PAGE_OPTIONS, **launch)
        except Exception:
            _clear_the_lock()
            browser = await pw.chromium.launch(**launch)
            ctx = await browser.new_context(bypass_csp=True, **PAGE_OPTIONS)
        await ctx.add_init_script(STEALTH)

        page = await ctx.new_page()
        if apply_browser_identity is not None:
            await apply_browser_identity(page, client_hints_metadata())

        async def _on_response(response):
            url = response.url or ""
            if "api.stripe.com" not in url:
                return
            body = ""
            try:
                body = await response.text()
            except Exception:
                pass
            seen.append((response.status, url, body))

        page.on("response", _on_response)

        logger.info("the checkout is opened: %s", checkout_url[:90])
        await page.goto(checkout_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)

        # a page that is already finished says so, and there is nothing
        # to be done there
        try:
            words = " ".join((await page.evaluate(
                "() => (document.body ? document.body.innerText : '')")).split())
        except Exception:
            words = ""
        if "all done here" in words.lower() or "timed out" in words.lower():
            logger.info("the session is finished: %s", words[:120])
            return "error", "The session is finished", "", time.time() - started

        for _ in range(4):
            try:
                await page.evaluate(OPEN_THE_CARD_PART)
            except Exception:
                pass
            try:
                await page.locator('input[name="cardNumber"]').first.wait_for(
                    state="visible", timeout=8000)
                break
            except Exception:
                await page.wait_for_timeout(1500)
        else:
            logger.info("the card part of the page never came: %s", words[:200])
            return "error", "The page did not open", "", time.time() - started

        first, last = _name()
        city, state, postal = random.choice(THE_CITIES)
        try:
            await page.evaluate(SET_THE_COUNTRY % THE_COUNTRY)
        except Exception:
            pass
        for name, value in (("billingName", "%s %s" % (first, last)),
                            ("billingAddressLine1", _street()),
                            ("billingLocality", city),
                            ("billingPostalCode", postal)):
            try:
                await page.evaluate(SET_A_BOX, [name, value])
            except Exception:
                pass

        for name, value in (("cardNumber", cc),
                            ("cardExpiry", "%s/%s" % (mm.zfill(2), yy[-2:])),
                            ("cardCvc", cvv)):
            try:
                field = page.locator('input[name="%s"]' % name)
                await field.first.click(timeout=6000)
                await field.first.type(value, delay=30, timeout=12000)
            except Exception as e:
                logger.info("the %s box did not take the answer: %s", name, e)
                return "error", "The card could not be put in", "", time.time() - started

        try:
            said = await page.evaluate(PRESS_PAY)
        except Exception:
            said = "the press failed"
        logger.info("the pay button: %s", said)

        # the box of the captcha is ticked when it shows, and the intent
        # is read from stripe's own frame, which says which one it is
        deadline = time.time() + (timeout or THE_TIME_TO_WAIT)
        ticked = 0
        intent = None
        async with BrowserClient(**client_kwargs) as client:
            while time.time() < deadline:
                await page.wait_for_timeout(800)
                for frame in page.frames:
                    url = frame.url or ""
                    if "hcaptcha.com/captcha" not in url or "frame=checkbox" not in url:
                        continue
                    try:
                        did = await frame.evaluate(TICK_THE_BOX)
                        if did == "ticked":
                            ticked += 1
                            logger.info("the captcha's box was ticked (%d)", ticked)
                    except Exception:
                        pass

                if intent is None:
                    intent = _the_intent_in_the_frames(page)
                    if intent:
                        logger.info("stripe's frame is about %s", intent["intent_id"])
                if intent:
                    read = await _read_the_intent(client, pk, intent["intent_id"],
                                                  intent["client_secret"])
                    if read:
                        intent = dict(intent)
                        intent["read"] = read
                        state = read.get("status") or ""
                        if state not in ("requires_action", "requires_source_action"):
                            break
                else:
                    # stripe's own answer says what happened, and it
                    # says it as soon as it knows
                    from_the_wire = _the_intent_in_the_wire(seen)
                    if from_the_wire:
                        state = from_the_wire.get("status") or ""
                        if state not in ("requires_action", "requires_source_action"):
                            intent = {"intent_id": "", "client_secret": "",
                                      "read": from_the_wire}
                            break

            if not intent:
                logger.info("stripe never said which intent it was about")
                from_the_wire = _the_intent_in_the_wire(seen)
                if from_the_wire:
                    logger.info("the intent came off the wire: %s", from_the_wire.get("status"))
                    verdict, message = _classify(from_the_wire)
                    return verdict, message, _card_info(from_the_wire), time.time() - started
                try:
                    words = " ".join((await page.evaluate(
                        "() => (document.body ? document.body.innerText : '')")).split())
                    logger.info("the page says: %s", words[:400])
                except Exception:
                    pass
                return "error", "The page did not answer", "", time.time() - started

            read = intent.get("read") or {}
            if not read and intent.get("intent_id"):
                read = await _read_the_intent(client, pk, intent["intent_id"],
                                              intent["client_secret"])
            logger.info("stripe said: %s", read.get("status"))
            verdict, message = _classify(read)
            info = _card_info(read)
            logger.info("the verdict: %s | %s", verdict, message)
            return verdict, message, info, time.time() - started
    except Exception as e:
        logger.warning("the checkout in the browser failed: %s", e)
        return "error", str(e)[:60], "", time.time() - started
    finally:
        try:
            if ctx is not None:
                await ctx.close()
        except Exception:
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass
