"""The checkout, in a real browser.

Stripe's own script puts up two captchas, and they are not the same
one.  The first belongs to the page and it gives an answer by itself.
The second belongs to the payment intent, it puts up a box that has to
be ticked, and it is the one the payment intent is waiting for.

When the card is put into the page by hand, stripe's own script does
all of it: it puts up the captcha, it ticks nothing and waits, it
hands the answer over, and it takes the card through the bank.  That is
the whole way round, and it is the only way round that the bank
believes.

So the card is put into the page by hand here, and the answer is read
off the wire.
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
import time

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


def _email(first, last):
    return ("%s.%s%d" % (first.lower(), last.lower(), random.randint(10, 9999))
            + "@gmail.com")


def _street():
    number = random.randint(10, 9999)
    name = random.choice(THE_STREETS)
    return "%d %s" % (number, name)


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


def _is_the_box_frame(url):
    return "hcaptcha.com/captcha" in (url or "") and "frame=checkbox" in (url or "")


class _Answers:
    """What stripe said on the wire."""

    def __init__(self):
        self.list = []

    async def take(self, response):
        url = response.url or ""
        if "api.stripe.com" not in url:
            return
        if not any(k in url for k in ("payment_intents", "setup_intents",
                                      "verify_challenge", "confirm")):
            return
        body = ""
        try:
            body = await response.text()
        except Exception:
            pass
        self.list.append((response.status, url, body))

    def the_intents(self):
        """Every answer that carried an intent, the newest last."""
        out = []
        for status, url, body in self.list:
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
                out.append((status, url, intent))
        return out


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


async def native_check(cc, mm, yy, cvv, checkout_url, proxy=None, timeout=None):
    """The card in the real checkout, in a real browser.

    Returns the same thing the other gates return: what happened to the
    card, what stripe said, what is known about the card, and how long
    it took.
    """
    started = time.time()
    try:
        from local_hcaptcha_solver import (PAGE_OPTIONS, STEALTH, _profile_dir,
                                           _clear_the_lock, _playwright_proxy,
                                           apply_browser_identity, client_hints_metadata)
    except Exception as e:
        logger.info("the browser helpers are missing: %s", e)
        return "error", "The browser is not available", "", 0.0

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger.info("playwright is missing: %s", e)
        return "error", "The browser is not available", "", 0.0

    launch = {
        "headless": not bool(os.environ.get("DISPLAY")),
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                 "--disable-blink-features=AutomationControlled",
                 "--window-size=1366,900"],
    }
    pw_proxy = _playwright_proxy(proxy)
    if pw_proxy:
        launch["proxy"] = pw_proxy

    answers = _Answers()
    pw = None
    ctx = None
    try:
        pw = await async_playwright().start()
        try:
            ctx = await pw.chromium.launch_persistent_context(
                _profile_dir(), channel="chrome", bypass_csp=True,
                **PAGE_OPTIONS, **launch)
        except Exception:
            _clear_the_lock()
            browser = await pw.chromium.launch(**launch)
            ctx = await browser.new_context(bypass_csp=True, **PAGE_OPTIONS)
        await ctx.add_init_script(STEALTH)

        page = await ctx.new_page()
        if apply_browser_identity is not None:
            await apply_browser_identity(page, client_hints_metadata())
        page.on("response", answers.take)

        await page.goto(checkout_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)

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
            logger.info("the card part of the page never came")
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

        deadline = time.time() + (timeout or THE_TIME_TO_WAIT)
        ticked = 0
        while time.time() < deadline:
            await page.wait_for_timeout(900)
            for frame in page.frames:
                if not _is_the_box_frame(frame.url):
                    continue
                try:
                    did = await frame.evaluate(TICK_THE_BOX)
                    if did == "ticked":
                        ticked += 1
                        logger.info("the captcha's box was ticked (%d)", ticked)
                except Exception:
                    pass
            intents = answers.the_intents()
            if intents:
                status, url, intent = intents[-1]
                if intent.get("status") not in ("requires_action", "requires_source_action"):
                    break

        intents = answers.the_intents()
        if not intents:
            logger.info("stripe said nothing about the card")
            return "error", "The page did not answer", "", time.time() - started

        status, url, intent = intents[-1]
        logger.info("stripe said: %s | %s", intent.get("status"), url.split("?")[0][-40:])
        verdict, message = _classify(intent)
        info = _card_info(intent)
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
