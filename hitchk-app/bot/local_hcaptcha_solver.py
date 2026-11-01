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
import sys
import time

logger = logging.getLogger("local_captcha")

# The browser has to say the same name as the client that hands the
# token in.  A token belongs to the browser that won it, so when the
# two disagree the token is worth nothing.
#
# The browser on the machine is a headless build of another version.
# It is told the name the api client uses, through the browser's own
# setting, so the name, the client hints and the handshake all belong
# to one browser.
import os as _os
import sys as _sys

_bot_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bot_dir not in _sys.path:
    _sys.path.insert(0, _bot_dir)

try:
    from browser_identity import (apply_browser_identity, client_hints_metadata,
                                  USER_AGENT as UA)
except Exception:  # the browser keeps its own name when the module is missing
    UA = None
    apply_browser_identity = None
    client_hints_metadata = None

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

// The screen.  A browser that says it is a computer with one screen
// of a size nobody has is easy to pick out.  Each one is set on its
// own, because one that will not go through must not stop the rest.
const _sizes = [[1920, 1080, 24], [1536, 864, 24], [1440, 900, 24], [1366, 768, 24]];
const _size = _sizes[Math.floor(Math.random() * _sizes.length)];
const _set = (obj, name, value) => {
  try { Object.defineProperty(obj, name, {get: () => value, configurable: true}); }
  catch (e) {}
};
_set(window, 'innerWidth', _size[0] - 16);
_set(window, 'innerHeight', _size[1] - 74);
_set(window, 'outerWidth', _size[0]);
_set(window, 'outerHeight', _size[1]);
_set(window, 'screenX', 0);
_set(window, 'screenY', 0);
_set(window, 'screenLeft', 0);
_set(window, 'screenTop', 0);
_set(window, 'devicePixelRatio', 1);
_set(navigator, 'deviceMemory', 8);
_set(navigator, 'maxTouchPoints', 0);
_set(navigator, 'pdfViewerEnabled', true);
_set(navigator, 'webdriver', undefined);
const _screen = window.screen;
if (_screen) {
  _set(_screen, 'width', _size[0]);
  _set(_screen, 'height', _size[1]);
  _set(_screen, 'availWidth', _size[0]);
  _set(_screen, 'availHeight', _size[1] - 24);
  _set(_screen, 'colorDepth', _size[2]);
  _set(_screen, 'pixelDepth', _size[2]);
}
// a computer with one screen has no screens to choose from
try {
  const _mm = window.matchMedia.bind(window);
  window.matchMedia = (q) => {
    try {
      const r = _mm(q);
      if (q && /display/.test(q)) { r.matches = false; }
      return r;
    } catch (e) {
      return {matches: false, media: q, addListener() {}, removeListener() {},
              addEventListener() {}, removeEventListener() {},
              dispatchEvent() { return false; }};
    }
  };
} catch (e) {}

// The graphics card.  A machine with no graphics card draws with the
// software renderer, and the software renderer is one of the things a
// headless browser is known by.  So the card is named.
try {
  const _getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    const _ext = this.getExtension('WEBGL_debug_renderer_info');
    if (_ext && p === _ext.UNMASKED_RENDERER_WEBGL) {
      return 'ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92) Direct3D11 vs_5_0 ps_5_0, D3D11)';
    }
    if (_ext && p === _ext.UNMASKED_VENDOR_WEBGL) {
      return 'Google Inc. (Intel)';
    }
    if (p === 37445) { return 'Intel'; }              // the renderer
    if (p === 37446) { return 'Intel(R) UHD Graphics 630'; }  // the vendor
    return _getParameter.call(this, p);
  };
} catch (e) {}
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
            // The enterprise captcha is the invisible one, the way
            // stripe does it.  The request data goes in its own place:
            // an ordinary captcha asked with it is answered with a
            // picture instead of the box.
            opts.size = 'invisible';
            opts.enterprisePayload = { rqdata: rqdata };
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


def _clear_the_lock():
    """Take the lock of a browser that died away.

    A browser that is killed leaves its lock in the profile, and the
    next browser refuses to start on it.  Then every captcha fails for
    a reason that has nothing to do with the captcha.
    """
    try:
        folder = _profile_dir()
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = os.path.join(folder, name)
            if os.path.islink(path) or os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
    except Exception:
        pass


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
            _profile_dir(), channel="chrome", bypass_csp=True,
            **PAGE_OPTIONS, **launch)
    except Exception as e:
        logger.debug("the saved profile browser did not start: %s", e)
        # a browser that was killed leaves its lock behind, and the next
        # one refuses to start on it
        _clear_the_lock()
        try:
            ctx = await _PW.chromium.launch_persistent_context(
                _profile_dir(), channel="chrome", bypass_csp=True,
                **PAGE_OPTIONS, **launch)
        except Exception as e2:
            logger.debug("the saved profile browser did not start again: %s", e2)
            browser = await _PW.chromium.launch(**launch)
            ctx = await browser.new_context(bypass_csp=True, **PAGE_OPTIONS)
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


# The clock and the size the page in the browser has.  A browser whose
# clock does not match the country it claims to be in is easy to pick
# out, and so is one whose screen is a size nobody has.
PAGE_OPTIONS = {
    "locale": "en-US",
    "timezone_id": "America/Chicago",
    "viewport": {"width": 1366, "height": 900},
}


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
    """The browser work, in a process of its own.

    When the browser dies the driver goes with it, and the driver takes
    the whole bot down with it.  So the browser is run apart, and the
    token comes back through a pipe.
    """
    import json
    import subprocess
    import sys

    job = {
        "page_url": page_url,
        "timeout": timeout,
        "proxy": proxy,
        "extra_js": extra_js,
        "widget_js": widget_js,
        "widget_arg": widget_arg,
        "widget_arg2": widget_arg2,
    }
    me = os.path.abspath(__file__)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, me, "--worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as e:
        logger.info("the browser worker did not start: %s", e)
        return None

    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(job).encode("utf-8")),
            timeout=timeout + 90,
        )
    except asyncio.TimeoutError:
        logger.info("the browser worker took too long and was stopped")
        try:
            proc.kill()
        except Exception:
            pass
        return None
    except Exception as e:
        logger.info("the browser worker failed: %s", e)
        return None

    try:
        answer = json.loads((out or b"{}").decode("utf-8", "replace"))
    except Exception:
        logger.info("the browser worker said nothing that could be read")
        return None

    global LAST_REASON, LAST_EKEY
    LAST_REASON = answer.get("reason") or ""
    LAST_EKEY = answer.get("ekey") or ""
    token = answer.get("token") or ""
    if token:
        logger.info("the local hCaptcha solver got a token")
    return token or None


async def _solve_in_a_process(page_url, timeout=60, proxy=None, extra_js=None,
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
            # the browser is told which browser it is, so the token it
            # wins belongs to the browser the request comes from
            if apply_browser_identity is not None:
                await apply_browser_identity(page, client_hints_metadata())
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


# How many addresses one solve tries.  Every address costs a browser
# of its own, and the card has only so much time.
MAX_ADDRESSES_PER_SOLVE = int(os.environ.get("CAPTCHA_ADDRESSES", "4") or 4)

# The addresses that answered the last time, and when that was.  A run
# asks for the same addresses over and over, so the answer is
# remembered instead of being asked for again.
_ALIVE_UNTIL = 0.0
_ALIVE_LIST = []


async def _keep_the_working_ones(attempts):
    """The addresses that answer, and only those.

    The addresses are asked all at once, because one that takes its
    time holds up the whole run.  The answer is remembered for a while,
    so the next card does not pay for it again.
    """
    global _bad_proxies, _ALIVE_UNTIL, _ALIVE_LIST
    if len(attempts) < 2:
        return attempts

    now = time.time()
    if _ALIVE_LIST and now < _ALIVE_UNTIL:
        known = set(_ALIVE_LIST)
        alive = [px for px in attempts if px in known]
        if alive:
            logger.info("the addresses that answer: %s of %s (known)",
                        len(alive), len(attempts))
            return alive

    import httpx

    async def ask(px):
        """Ask the address if it is there.

        Three answers: it answered, it is not there, or it was slow.
        A slow one is not marked dead: it may well answer the next
        time, and an address that is wrongly put aside is one the
        captcha never gets to try.
        """
        pw = _playwright_proxy(px)
        if not pw:
            # the direct address always answers
            return px, True
        url = pw.get("server", "")
        if url.startswith("http://"):
            url = url[7:]
        auth = ""
        if pw.get("username"):
            auth = "%s:%s@" % (pw.get("username"), pw.get("password"))
        try:
            r = await asyncio.to_thread(
                httpx.get, "https://api.ipify.org", timeout=20,
                proxy="http://%s%s" % (auth, url))
            return px, (r.status_code == 200)
        except httpx.ConnectError:
            return px, False
        except httpx.ConnectTimeout:
            return px, False
        except Exception:
            return px, None

    # a few at a time.  These addresses share a host, so only a couple
    # of them answer at once: too many at once and they all time out.
    gate = asyncio.Semaphore(4)

    async def ask_one(px):
        async with gate:
            return await ask(px)

    answers = await asyncio.gather(*[ask_one(px) for px in attempts],
                                   return_exceptions=True)
    alive = []
    for answer in answers:
        if not isinstance(answer, tuple):
            continue
        px, ok = answer
        if ok is True:
            alive.append(px)
        elif ok is False:
            _bad_proxies.add(px)
        # a slow one is left alone

    _ALIVE_LIST = list(alive)
    _ALIVE_UNTIL = time.time() + 600
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

    # The whole solve has to fit in the time the card has.  Every
    # address costs a browser of its own, so only a few are tried, and
    # the time that is left is what each one gets.
    deadline = time.time() + timeout
    left = len(attempts)
    if left > MAX_ADDRESSES_PER_SOLVE:
        left = MAX_ADDRESSES_PER_SOLVE
        attempts = attempts[:left]

    last = ""
    for i, px in enumerate(attempts):
        left = deadline - time.time()
        if left <= 10:
            logger.info("the local hCaptcha solver ran out of time")
            break
        # the last address gets what is left, the others a share of it
        share = max(20, int(left / max(1, len(attempts) - i)))
        try:
            token = await _solve_in_browser(
                pageurl, timeout=min(share, 90), proxy=px,
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
        elif "no token came" in last or "not there to click" in last:
            # the box was there and the answer never came.  Another
            # address is worth a try: the answer is often refused
            # because too many captchas came from the same one.
            logger.info("no token came on %s, trying another address",
                        (px.split(":")[0] if px else "the direct one"))
        else:
            # the page itself is wrong, so another address will not
            # help either
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


async def _worker(job):
    """The browser work, as a program of its own.

    This is what runs in the other process.  The answer comes back as
    one line of json.
    """
    import json
    token = ""
    reason = ""
    ekey = ""
    try:
        token = await _solve_in_a_process(
            job.get("page_url") or "",
            timeout=job.get("timeout") or 60,
            proxy=job.get("proxy"),
            extra_js=job.get("extra_js"),
            widget_js=job.get("widget_js"),
            widget_arg=job.get("widget_arg"),
            widget_arg2=job.get("widget_arg2"),
        ) or ""
    except Exception as e:
        reason = "%s: %s" % (type(e).__name__, str(e)[:80])
    if not reason:
        reason = LAST_REASON or ""
    ekey = LAST_EKEY or ""
    sys.stdout.write(json.dumps({"token": token, "reason": reason, "ekey": ekey}))
    sys.stdout.flush()


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


if __name__ == "__main__" and "--worker" in sys.argv:
    import json
    import sys

    async def _run():
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = ""
        try:
            job = json.loads(raw)
        except Exception:
            job = {}
        await _worker(job)

    try:
        asyncio.run(_run())
    except Exception:
        pass
    os._exit(0)
