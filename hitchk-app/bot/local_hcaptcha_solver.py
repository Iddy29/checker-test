"""A local captcha solver -- a real browser gets the token.

No outside service is used.  hCaptcha and Turnstile put the token in a
hidden field and reCAPTCHA v3 hands it to a javascript callback, so a
real browser can read it out.  That costs a browser start, but no key,
no credit and nothing to leak.
"""
import asyncio
import logging
import os
import re
import time

logger = logging.getLogger("local_captcha")

# The user agent is never written by hand: a browser that claims a
# different version than it really is gives itself away, and hCaptcha
# reads that.  So the browser keeps its own.
UA = None

# the small things that give a browser away
STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const q = navigator.permissions && navigator.permissions.query;
if (q) { navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : q.call(navigator.permissions, p)); }
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
"""

# the token the page hands over
TOKEN_JS = """
() => {
    const el = document.querySelector(
        'textarea[name="h-captcha-response"], input[name="cf-turnstile-response"]');
    if (el && el.value) return el.value;
    const t = window.__captchaToken;
    if (!t) return "";
    // the enterprise captcha hands over an object with the token and
    // the ekey in it
    if (typeof t === 'object') return t.response || "";
    return t;
}
"""

# the ekey that comes with the enterprise token
EKEY_JS = """
() => {
    const e = window.__captchaEkey;
    if (!e) return "";
    if (typeof e === 'object') return e.ekey || "";
    return e;
}
"""

# the last ekey the solver got, for the callers that need it
LAST_EKEY = ""

# reCAPTCHA v3 hands the token to a callback, so catch it on the way
RECAPTCHA_HOOK = """
(function () {
    window.__captchaToken = "";
    const install = () => {
        try {
            if (window.grecaptcha && window.grecaptcha.execute && !window.grecaptcha.__patched) {
                const orig = window.grecaptcha.execute.bind(window.grecaptcha);
                window.grecaptcha.execute = function (...args) {
                    return orig(...args).then((tok) => {
                        window.__captchaToken = tok;
                        return tok;
                    });
                };
                window.grecaptcha.__patched = true;
            }
        } catch (e) {}
    };
    install();
    const timer = setInterval(install, 200);
    setTimeout(() => clearInterval(timer), 40000);
})();
"""

# the hCaptcha widget, for a page that does not put one up itself.
# The second argument is the rqdata, which Stripe and other enterprise
# users hand over: it needs the enterprise script.
HCAPTCHA_WIDGET = """
(args) => {
    const sitekey = args[0], rqdata = args[1] || '';
    const keep = (t) => {
        // the enterprise captcha hands over an object with the token
        // and the ekey
        if (t && typeof t === 'object') {
            window.__captchaToken = t.response || '';
            window.__captchaEkey = t.ekey || '';
        } else {
            window.__captchaToken = t || '';
        }
    };
    try {
        if (!window.hcaptcha) {
            if (!document.getElementById('__lc_api')) {
                const s = document.createElement('script');
                s.id = '__lc_api';
                s.src = rqdata
                    ? 'https://js.hcaptcha.com/1/api.js?render=explicit&enterprise.js'
                    : 'https://js.hcaptcha.com/1/api.js?render=explicit';
                s.async = true;
                s.defer = true;
                document.head.appendChild(s);
            }
            return 'loading';
        }
        if (window.__lc_widget) {
            // the widget is up, so let it go
            if (!window.__lc_started) {
                window.__lc_started = true;
                try {
                    // the async flag is what makes execute hand back a
                    // promise, and without it the result is lost
                    const p = window.hcaptcha.execute(window.__lc_widget, {async: true});
                    if (p && p.then) {
                        p.then(keep).catch((e) => { window.__lc_error = 'execute: ' + e; });
                    }
                } catch (e) { window.__lc_error = 'execute threw: ' + e; }
            }
            return 'the widget is up';
        }
        const box = document.createElement('div');
        box.className = 'h-captcha';
        box.dataset.sitekey = sitekey;
        box.style.position = 'fixed';
        box.style.left = '10px';
        box.style.top = '10px';
        box.style.zIndex = '2147483647';
        document.body.appendChild(box);
        const opts = {
            sitekey: sitekey,
            callback: keep,
            'error-callback': (e) => { window.__lc_error = String(e); }
        };
        if (rqdata) {
            // the enterprise captcha is the invisible one, the way
            // stripe does it
            opts.size = 'invisible';
            opts.rqdata = rqdata;
        }
        try {
            window.__lc_widget = window.hcaptcha.render(box, opts);
        } catch (e) {
            window.__lc_widget = null;
            window.__lc_error = 'the render failed: ' + e;
            return 'the render failed: ' + e;
        }
        window.__lc_started = true;
        try {
            const p = window.hcaptcha.execute(window.__lc_widget, {async: true});
            if (p && p.then) {
                p.then(keep).catch((e) => { window.__lc_error = 'execute: ' + e; });
            }
        } catch (e) { window.__lc_error = 'execute threw: ' + e; }
        return 'the widget is up';
    } catch (e) {
        return 'outer ' + e;
    }
}
"""

# the same for reCAPTCHA v3, which is never a widget
RECAPTCHA_WIDGET = """
(args) => {
    const sitekey = args[0];
    try {
        if (!window.grecaptcha) {
            if (!document.getElementById('__lc_grec_script')) {
                const s = document.createElement('script');
                s.id = '__lc_grec_script';
                s.src = 'https://www.google.com/recaptcha/api.js?render=' + sitekey;
                s.async = true;
                s.defer = true;
                document.head.appendChild(s);
            }
            return false;
        }
        if (window.__lc_grec_done) return true;
        window.__lc_grec_done = true;
        window.grecaptcha.ready(() => {
            window.grecaptcha.execute(sitekey, {}).then((tok) => {
                window.__captchaToken = tok;
            }).catch(() => {});
        });
        return true;
    } catch (e) {
        return false;
    }
}
"""


# The browser is kept open between the calls: a hitter runs many
# cards through one session, and starting a browser every time is slow.
# The playwright session has to stay open as well, because the browser
# lives inside it and dies with it.
_BROWSER = None
_PW = None
_PW_LOOP = None
_BROWSER_AT = 0.0
_BROWSER_IDLE = 180.0
_BROWSER_PROXY = None


async def _get_browser(launch):
    """The browser, opened once and kept for a while.

    The address the browser goes out through is part of the browser, so
    a new address means a new browser.
    """
    global _BROWSER, _PW, _PW_LOOP, _BROWSER_AT, _BROWSER_PROXY
    wanted = launch.get("proxy")
    wanted = wanted.get("server") if isinstance(wanted, dict) else wanted
    if _BROWSER is not None:
        if wanted == _BROWSER_PROXY:
            _BROWSER_AT = time.time()
            return _BROWSER
        # the address changed, so the browser goes too
        try:
            await _BROWSER.close()
        except Exception:
            pass
        _BROWSER = None
    if _PW is None:
        from playwright.async_api import async_playwright
        _PW = await async_playwright().start()
        _PW_LOOP = asyncio.get_event_loop()
    ctx = None
    try:
        ctx = await _PW.chromium.launch_persistent_context(
            _profile_dir(), channel="chrome", bypass_csp=True, **launch)
    except Exception as e:
        logger.debug("the saved profile browser did not start: %s", e)
        browser = await _PW.chromium.launch(**launch)
        ctx = await browser.new_context(
            bypass_csp=True,
            locale="en-US", timezone_id="America/Chicago",
            viewport={"width": 1366, "height": 900})
    _BROWSER = ctx
    _BROWSER_AT = time.time()
    _BROWSER_PROXY = wanted
    return ctx


async def _close_browser_if_idle():
    """Close the browser when it has been sitting still for a while."""
    global _BROWSER, _PW
    if _BROWSER is None:
        return
    if (time.time() - _BROWSER_AT) < _BROWSER_IDLE:
        return
    try:
        await _BROWSER.close()
    except Exception:
        pass
    _BROWSER = None
    if _PW is not None:
        try:
            await _PW.stop()
        except Exception:
            pass
        _PW = None


def _profile_dir():
    """A saved browser profile, so the browser looks used before."""
    path = os.path.join("/tmp", "lc_browser_profile")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        return "/tmp"
    return path


# The proxies that are worth trying, and the file they are in.
PROXY_FILE = "/root/hitchecker/bot/user_proxies.json"
_bad_proxies = set()


def _working_proxies():
    """The proxies that answered the last time they were tried.

    hCaptcha gives up on an address that has failed, so a proxy that
    does not work is not asked again.
    """
    global _bad_proxies
    try:
        import json
        with open(PROXY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("proxies"), list):
                out.extend(v["proxies"])
            elif isinstance(v, list):
                out.extend(v)
    elif isinstance(data, list):
        out = list(data)
    return [p for p in out
            if isinstance(p, str) and p.count(":") >= 3 and p not in _bad_proxies]


def _playwright_proxy(proxy):
    """The proxy for the browser.

    The proxy list uses "host:port:user:pass", and a plain
    "http://host:port" works as well.
    """
    if not proxy or proxy == "NONE":
        return None
    try:
        proxy = str(proxy).strip()
        scheme = "http"
        if "://" in proxy:
            scheme, proxy = proxy.split("://", 1)
        if "@" in proxy:
            creds, hostport = proxy.rsplit("@", 1)
            user, _, pw = creds.partition(":")
            return {"server": f"{scheme}://{hostport}",
                    "username": user, "password": pw}
        parts = proxy.split(":")
        if len(parts) == 4:
            host, port, user, pw = parts
            return {"server": f"{scheme}://{host}:{port}",
                    "username": user, "password": pw}
        if len(parts) == 2:
            return {"server": f"{scheme}://{proxy}"}
        return {"server": f"{scheme}://{proxy}"}
    except Exception:
        return None


# The picture challenge is recognised by the words it uses: "tap on the
# buildings", "click on the images" and so on.  The empty placeholders
# are always in the page, so counting them says nothing at all, and the
# "please try again / verify" prompt is not a picture either.
_IMAGE_WORDS = ("tap on", "click on", "select all", "choose all",
                "find things", "find the", "things you would",
                "press enter", "images of", "which of the",
                "two groups with the same", "one group with the same",
                "objects that", "people that", "animals that",
                "vehicles that", "food and", "hot and cold",
                "usually spray", "commonly have")


async def _image_challenge_up(page):
    """Has the picture challenge come up?

    The picture challenge is not worth solving: it needs a person or a
    model that can see.  So when it shows up the solver stops at once
    instead of sitting there until the time runs out.
    """
    for frame in page.frames:
        if "hcaptcha" not in frame.url and "frame=challenge" not in frame.url:
            continue
        try:
            text = await frame.locator("body").inner_text(timeout=1500)
        except Exception:
            text = ""
        low = (text or "").lower()
        for word in _IMAGE_WORDS:
            if word in low:
                return True
    return False


async def _what_it_showed(page):
    """What the captcha put on the page, in words.

    The frames of the captcha are the only way to tell a picture
    challenge from a box that was never there, so the words are written
    down when the run ends.
    """
    parts = []
    for frame in page.frames:
        if "hcaptcha" not in frame.url and "challenge" not in frame.url:
            continue
        try:
            text = (await frame.locator("body").inner_text(timeout=1200)) or ""
        except Exception:
            text = ""
        text = " ".join(text.split())
        if not text or text in ("I am human Privacy - Terms", "Verify EN"):
            continue
        if text not in parts:
            parts.append(text[:150])
    return " / ".join(parts[:3])


async def _click_the_verify(page):
    """Click the Verify button.

    The enterprise captcha puts up a "please try again" and a Verify
    button in the challenge frame.  Clicking that is what asks for the
    answer, and without it nothing comes back.
    """
    for frame in page.frames:
        if "hcaptcha" not in frame.url:
            continue
        for sel in ("text=/^verify$/i", "text='Verify'", "[class*='verify']",
                    "[id*='verify']", "a:has-text('Verify')",
                    "div:has-text('Verify')", "span:has-text('Verify')"):
            try:
                el = frame.locator(sel).first
                if await el.count():
                    await el.click(timeout=4000)
                    return True
            except Exception:
                continue
    return False


async def _click_the_box(page):
    """Click the captcha checkbox, the way a visitor would.

    hCaptcha shows a checkbox before it hands the token over, and
    without the click it never gives one.
    """
    for frame in page.frames:
        try:
            box = frame.locator("#checkbox")
            if await box.count():
                el = box.first
                spot = await el.bounding_box()
                if spot:
                    x = spot["x"] + spot["width"] / 2
                    y = spot["y"] + spot["height"] / 2
                    await page.mouse.move(x - 70, y - 50, steps=14)
                    await page.wait_for_timeout(140)
                    await page.mouse.move(x, y, steps=9)
                    await page.wait_for_timeout(110)
                    await page.mouse.down()
                    await page.wait_for_timeout(80)
                    await page.mouse.up()
                else:
                    await el.click(timeout=3000)
                return True
        except Exception:
            continue
    try:
        box = page.locator("input[type=checkbox]")
        if await box.count():
            await box.first.click(timeout=3000)
            return True
    except Exception:
        pass
    return False


# what the last run ran into, so the log says which one it was
LAST_REASON = ""


async def _solve_in_browser(page_url, timeout=60, proxy=None, extra_js=None,
                            widget_js=None, widget_arg=None, widget_arg2=None):
    """Open the page in a real browser and read the token out of it."""
    global LAST_REASON
    LAST_REASON = ""
    launch = {
        "headless": not bool(os.environ.get("DISPLAY")),
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                 "--disable-blink-features=AutomationControlled",
                 "--window-size=1366,900"],
    }
    pw_proxy = _playwright_proxy(proxy)
    if pw_proxy:
        launch["proxy"] = pw_proxy

    if True:
        try:
            await _close_browser_if_idle()
            ctx = await _get_browser(launch)
            # The real chrome is harder to tell apart than the bundled
            # one, and a saved profile looks like a browser that has
            # been used before.
            await ctx.add_init_script(STEALTH)
            if extra_js:
                await ctx.add_init_script(extra_js)
            # a fresh page, so the scripts above are on it: the page
            # that was open when the profile loaded has none of them
            page = await ctx.new_page()
            await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1200)

            deadline = time.time() + timeout
            widget_done = False
            last_click = 0.0
            picture_seen = False
            box_ticked = False
            verify_clicked = False
            while time.time() < deadline:
                token = await page.evaluate(TOKEN_JS)
                if token:
                    try:
                        ekey = await page.evaluate(EKEY_JS)
                    except Exception:
                        ekey = ""
                    if ekey:
                        global LAST_EKEY
                        LAST_EKEY = ekey
                    return token
                # the page may bring its own widget up, so only put one
                # up when there is none
                if widget_js and not widget_done:
                    try:
                        said = await page.evaluate(
                            widget_js, [widget_arg, widget_arg2])
                        said = (said or "") if isinstance(said, str) else str(said or "")
                        # the widget says what it did, so the words are
                        # read: "loading" is not the same as being up
                        widget_done = ("the widget is up" in said
                                       or "widget is up" in said)
                        if "loading" in said:
                            widget_done = False
                        if "failed" in said or "outer" in said:
                            LAST_REASON = said[:120]
                            widget_done = True
                    except Exception as e:
                        logger.debug("the widget did not load: %s", e)
                # the picture challenge is not worth solving: it needs
                # a person or a model that can see
                if await _image_challenge_up(page):
                    picture_seen = True
                    logger.info("the hCaptcha picture challenge came up, "
                                "so the captcha is left alone")
                    break
                # the checkbox the visitor would click
                if time.time() - last_click > 3:
                    last_click = time.time()
                    try:
                        if await _click_the_box(page):
                            box_ticked = True
                    except Exception as e:
                        logger.debug("the checkbox was not clickable: %s", e)
                    # and the verify button, which is what asks for the
                    # answer
                    try:
                        if await _click_the_verify(page):
                            verify_clicked = True
                    except Exception as e:
                        logger.debug("the verify button was not clickable: %s", e)
                await page.wait_for_timeout(500)

            # The reason is worked out from what really happened, and
            # what the captcha showed is written down with it.
            shown = await _what_it_showed(page)
            if picture_seen:
                LAST_REASON = ("the box was ticked, and hCaptcha answered with a "
                               "picture challenge")
            elif verify_clicked and box_ticked:
                LAST_REASON = ("the box was ticked and the verify was pressed, "
                               "but no token came")
            elif verify_clicked:
                LAST_REASON = "the verify was pressed, but no token came"
            elif box_ticked:
                LAST_REASON = "the box was ticked, but no token came"
            elif shown:
                LAST_REASON = f"the captcha showed: {shown}"
            else:
                LAST_REASON = "the box was not there to click"
            logger.info("the captcha run ended: %s | it showed: %s",
                        LAST_REASON, shown or "nothing")
            return None
        finally:
            # the browser stays open for the next card
            pass


def _budget(default=60):
    """How long a captcha may take, from the environment."""
    try:
        return int(os.environ.get("CAPTCHA_TIMEOUT", "") or default)
    except Exception:
        return default


async def _keep_the_working_ones(attempts):
    """The addresses that answer, and only those."""
    global _bad_proxies
    if len(attempts) < 2:
        return attempts
    import httpx
    alive = []
    for px in attempts:
        if px in _bad_proxies:
            continue
        pw = _playwright_proxy(px)
        if not pw:
            # the direct address always answers
            alive.append(px)
            continue
        url = pw.get("server", "")
        if url.startswith("http://"):
            url = url[7:]
        auth = ""
        if pw.get("username"):
            auth = "%s:%s@" % (pw.get("username"), pw.get("password"))
        try:
            r = httpx.get("https://api.ipify.org", timeout=12,
                          proxy="http://%s%s" % (auth, url))
            if r.status_code == 200:
                alive.append(px)
            else:
                _bad_proxies.add(px)
        except Exception:
            _bad_proxies.add(px)
    logger.info("the addresses that answer: %s of %s",
                len(alive), len(attempts))
    return alive or attempts[:1]


async def solve_hcaptcha_local(sitekey, pageurl, rqdata=None, proxy=None,
                               timeout=0):
    """An hCaptcha token, straight from a browser.

    The first try goes out the way the caller asked.  If the picture
    challenge comes up, the other addresses are tried, because hCaptcha
    gives the picture to an address it does not like, and a different
    address often gets the plain box.
    """
    global _bad_proxies
    if not pageurl:
        return None
    if not timeout:
        timeout = _budget(60)

    attempts = []
    if proxy:
        attempts.append(proxy)
    for p in _working_proxies():
        if p not in attempts:
            attempts.append(p)
    # the addresses that work are found first, so the run does not
    # waste its time on the ones that are dead
    attempts = await _keep_the_working_ones(attempts)

    last = ""
    for i, px in enumerate(attempts):
        try:
            token = await _solve_in_browser(
                pageurl, timeout=timeout, proxy=px,
                widget_js=HCAPTCHA_WIDGET, widget_arg=sitekey, widget_arg2=rqdata)
        except Exception as e:
            logger.info("the local hCaptcha solver failed: %s", e)
            last = str(e)[:80]
            # a proxy that cannot open a page is not asked again
            if px and ("ERR_" in last or "Timeout" in last or "timeout" in last):
                _bad_proxies.add(px)
            continue
        if token:
            logger.info("the local hCaptcha solver got a token")
            return token
        last = LAST_REASON or "no reason given"
        if "picture" in last:
            # this address gets the picture, so it is not asked again
            _bad_proxies.add(px)
            logger.info("the picture came up on %s, trying another address",
                        (px.split(":")[0] if px else "the direct one"))
        else:
            break
    logger.info("the local hCaptcha solver found no token: %s",
                last or "no reason given")
    return None


async def solve_recaptcha_v3_local(sitekey, pageurl, proxy=None, timeout=0):
    """A reCAPTCHA v3 token, straight from a browser."""
    if not pageurl:
        return None
    if not timeout:
        timeout = _budget(60)
    try:
        token = await _solve_in_browser(
            pageurl, timeout=timeout, proxy=proxy, extra_js=RECAPTCHA_HOOK,
            widget_js=RECAPTCHA_WIDGET, widget_arg=sitekey)
        if token:
            logger.info("the local reCAPTCHA solver got a token")
            return token
        logger.info("the local reCAPTCHA solver found no token")
    except Exception as e:
        logger.info("the local reCAPTCHA solver failed: %s", e)
    return None


async def solve_turnstile_local(sitekey, pageurl, proxy=None, timeout=0,
                                action=None, cdata=None):
    """A Turnstile token, straight from a browser."""
    if not pageurl:
        return None
    if not timeout:
        timeout = _budget(60)
    try:
        token = await _solve_in_browser(
            pageurl, timeout=timeout, proxy=proxy)
        if token:
            logger.info("the local Turnstile solver got a token")
            return token
        logger.info("the local Turnstile solver found no token")
    except Exception as e:
        logger.info("the local Turnstile solver failed: %s", e)
    return None
