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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

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
    if (window.__captchaToken) return window.__captchaToken;
    return "";
}
"""

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

# the hCaptcha widget, for a page that does not put one up itself
HCAPTCHA_WIDGET = """
(sitekey) => {
    try {
        if (!window.hcaptcha) {
            if (!document.getElementById('__lc_api')) {
                const s = document.createElement('script');
                s.id = '__lc_api';
                s.src = 'https://js.hcaptcha.com/1/api.js?render=explicit';
                s.async = true;
                s.defer = true;
                document.head.appendChild(s);
            }
            return false;
        }
        if (window.__lc_widget) return true;
        const box = document.createElement('div');
        box.className = 'h-captcha';
        box.dataset.sitekey = sitekey;
        box.style.position = 'fixed';
        box.style.left = '10px';
        box.style.top = '10px';
        box.style.zIndex = '2147483647';
        document.body.appendChild(box);
        try {
            window.__lc_widget = window.hcaptcha.render(box, {sitekey: sitekey});
        } catch (e) {
            window.__lc_widget = null;
        }
        return !!window.__lc_widget;
    } catch (e) {
        return false;
    }
}
"""

# the same for reCAPTCHA v3, which is never a widget
RECAPTCHA_WIDGET = """
(sitekey) => {
    try {
        if (!window.grecaptcha) {
            if (!document.getElementById('__lc_grec')) {
                const s = document.createElement('script');
                s.id = '__lc_grec';
                s.src = 'https://www.google.com/recaptcha/api.js?render=explicit';
                s.async = true;
                s.defer = true;
                document.head.appendChild(s);
            }
            return false;
        }
        if (window.__lc_grec) return true;
        window.__lc_grec = true;
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


async def _click_the_box(page):
    """Click the captcha checkbox, the way a visitor would.

    hCaptcha shows a checkbox before it hands the token over, and
    without the click it never gives one.
    """
    for frame in page.frames:
        try:
            box = frame.locator("#checkbox")
            if await box.count():
                await box.click(timeout=3000)
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


async def _solve_in_browser(page_url, timeout=60, proxy=None, extra_js=None,
                            widget_js=None, widget_arg=None):
    """Open the page in a real browser and read the token out of it."""
    from playwright.async_api import async_playwright

    launch = {
        "headless": not bool(os.environ.get("DISPLAY")),
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                 "--disable-blink-features=AutomationControlled",
                 "--window-size=1366,900"],
    }
    pw_proxy = _playwright_proxy(proxy)
    if pw_proxy:
        launch["proxy"] = pw_proxy

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch)
        try:
            ctx = await browser.new_context(
                user_agent=UA, locale="en-US", timezone_id="America/Chicago",
                viewport={"width": 1366, "height": 900})
            await ctx.add_init_script(STEALTH)
            if extra_js:
                await ctx.add_init_script(extra_js)
            page = await ctx.new_page()
            await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1200)

            deadline = time.time() + timeout
            widget_done = False
            last_click = 0.0
            while time.time() < deadline:
                token = await page.evaluate(TOKEN_JS)
                if token:
                    return token
                # the page may bring its own widget up, so only put one
                # up when there is none
                if widget_js and not widget_done:
                    try:
                        widget_done = bool(await page.evaluate(widget_js, widget_arg))
                    except Exception as e:
                        logger.debug("the widget did not load: %s", e)
                # the checkbox the visitor would click
                if time.time() - last_click > 3:
                    last_click = time.time()
                    try:
                        await _click_the_box(page)
                    except Exception as e:
                        logger.debug("the checkbox was not clickable: %s", e)
                await page.wait_for_timeout(500)
            return None
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def solve_hcaptcha_local(sitekey, pageurl, rqdata=None, proxy=None,
                               timeout=60):
    """An hCaptcha token, straight from a browser."""
    if not pageurl:
        return None
    if rqdata:
        # the enterprise challenge signs the request, and a plain browser
        # cannot reproduce that
        logger.info("the local hCaptcha solver does not do enterprise rqdata")
        return None
    try:
        token = await _solve_in_browser(
            pageurl, timeout=timeout, proxy=proxy,
            widget_js=HCAPTCHA_WIDGET, widget_arg=sitekey)
        if token:
            logger.info("the local hCaptcha solver got a token")
            return token
        logger.info("the local hCaptcha solver found no token")
    except Exception as e:
        logger.info("the local hCaptcha solver failed: %s", e)
    return None


async def solve_recaptcha_v3_local(sitekey, pageurl, proxy=None, timeout=60):
    """A reCAPTCHA v3 token, straight from a browser."""
    if not pageurl:
        return None
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


async def solve_turnstile_local(sitekey, pageurl, proxy=None, timeout=60,
                                action=None, cdata=None):
    """A Turnstile token, straight from a browser."""
    if not pageurl:
        return None
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
