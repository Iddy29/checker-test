import aiohttp
import asyncio
import os
import re
import json
import logging
import random
import time
from urllib.parse import urlparse, urljoin

logger = logging.getLogger(__name__)

# The captchas are answered on this machine, in a browser, by the
# solver in local_hcaptcha_solver.py.  Nothing is sent away and nothing
# is paid for, so there is no outside service here to answer for us.
#
# The same solver is also an api of its own, captcha_api.py, so that
# anyone the owner gives the key to may ask it for an answer.

MAX_POLL_ATTEMPTS = 20
POLL_INTERVAL = 2

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def _get_config_key(key: str, env_var: str) -> str:
    try:
        with open(_CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        val = cfg.get(key, "")
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(env_var, "")








def _load_proxy_pool():
    try:
        mtime = os.path.getmtime(_PROXIES_PATH)
    except OSError:
        return []
    if _proxy_cache["mtime"] == mtime:
        return _proxy_cache["list"]
    try:
        with open(_PROXIES_PATH, "r") as f:
            data = json.load(f)
    except Exception:
        return []
    pool = []
    if isinstance(data, dict):
        for entry in data.values():
            if isinstance(entry, dict):
                for p in entry.get("proxies", []) or []:
                    if p:
                        pool.append(str(p))
    elif isinstance(data, list):
        pool = [str(p) for p in data if p]
    _proxy_cache["mtime"] = mtime
    _proxy_cache["list"] = pool
    return pool


def _get_random_proxy():
    """One proxy from the pool, formatted the way the solvers expect."""
    import random
    pool = _load_proxy_pool()
    if not pool:
        return None
    return _format_proxy(random.choice(pool))


def _format_proxy(raw):
    """Turn a pool entry into the shape the solver APIs want.

    The pool stores "host:port" or "host:port:user:pass".  The solver wants an
    object with scheme/host/port and optional username/password; the other
    solvers want a URL.
    """
    raw = str(raw).strip()
    if not raw:
        return None

    scheme = "http"
    if "://" in raw:
        scheme, raw = raw.split("://", 1)

    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
    elif len(parts) == 3:
        host, port, user = parts
        pwd = ""
    elif len(parts) >= 2:
        host, port = parts[0], parts[1]
        user = pwd = ""
    else:
        return None

    return {
        "scheme": scheme,
        "host": host,
        "port": int(port) if port.isdigit() else port,
        "username": user,
        "password": pwd,
    }


def _proxy_url(proxy):
    """The same proxy as a URL, for the solvers that take a string."""
    if not proxy:
        return None
    if isinstance(proxy, str):
        return proxy
    auth = ""
    if proxy.get("username"):
        auth = f"{proxy['username']}:{proxy.get('password', '')}@"
    return f"{proxy.get('scheme', 'http')}://{auth}{proxy['host']}:{proxy['port']}"


# The browser this solver pretends to be.  It has to be the same
# browser the api client uses, or the token it wins is thrown away.
import os as _os
import sys as _sys

_bot_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bot_dir not in _sys.path:
    _sys.path.insert(0, _bot_dir)

try:
    from browser_identity import (BrowserSession, api_headers, navigation_headers,
                                  USER_AGENT as UA)
except Exception:  # the old name when the module is missing
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
    BrowserSession = None
    api_headers = None
    navigation_headers = None



async def _local_hcaptcha_solve(sitekey, pageurl, rqdata=None, proxy=None):
    """The browser solver, with no key and no bill.

    The browser goes out through the same proxy as the rest of the
    request.  If the browser and the api come from two different
    addresses, hCaptcha notices and puts up a picture challenge
    instead of the box.
    """
    try:
        from local_hcaptcha_solver import solve_hcaptcha_local
        token = await solve_hcaptcha_local(sitekey, pageurl, rqdata=rqdata,
                                          proxy=proxy)
        if token:
            return token
        logger.info("the local hCaptcha solver found no token")
    except ImportError:
        logger.info("the local hCaptcha solver is not on the box")
    except Exception as e:
        logger.info(f"the local hCaptcha solver failed: {e}")
    return None







# The captcha api on this machine: the same words as 2captcha.com, and
# the same solver, but here and for nothing.
CAPTCHA_API_URL = (os.environ.get("CAPTCHA_API_URL") or
                   "http://127.0.0.1:8090").rstrip("/")
CAPTCHA_API_KEY_FILE = os.path.join(os.path.dirname(__file__), "captcha_api_key.txt")


def _the_api_key() -> str:
    """The key of the captcha api on this machine.

    The key comes from the environment first and from the file after
    it, in that order, which is the order the api itself uses.  When
    the two used a different order the client and the api could end up
    with a key each, and every request would be turned away.
    """
    key = (os.environ.get("CAPTCHA_API_KEY") or "").strip()
    if key:
        return key
    try:
        with open(CAPTCHA_API_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


async def _api_solve(kind, sitekey, pageurl, proxy=None, rqdata=None, timeout=0,
                      manual=False, fingerprint=None):
    """The captcha is asked for through the api, the way 2captcha is asked for.

    The work is asked for in one call and the answer is asked for in
    another, and the answer comes when it is ready.
    """
    import aiohttp

    key = _the_api_key()
    if not key:
        return None

    # the legacy words: in.php asks for the work, res.php asks again
    # for the answer, and the status is never 2 -- the token and the
    # error codes are both carried in "request".
    params = {"key": key, "method": kind, "pageurl": pageurl, "json": 1}
    if kind == "recaptcha":
        params["googlekey"] = sitekey
    else:
        params["google_sitekey"] = sitekey
    if proxy:
        params["proxy"] = proxy
    if rqdata:
        params["data"] = rqdata
    if manual:
        # the work is left for a person: some captchas are a picture
        params["manual"] = "1"
    if fingerprint is not None:
        # The name of the browser goes with the work, so the browser
        # that answers is the one the request will come from.
        try:
            params["browser"] = json.dumps(
                fingerprint.wire() if hasattr(fingerprint, "wire")
                else fingerprint)
        except Exception:
            pass

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get("%s/in.php" % CAPTCHA_API_URL, params=params,
                                timeout=30) as resp:
                first = await resp.json(content_type=None)
            if not isinstance(first, dict) or first.get("status") != 1:
                logger.info("the api would not take the work: %s",
                            first.get("request") if isinstance(first, dict) else first)
                return None
            job = first.get("request") or ""
            if not job:
                return None
            deadline = time.time() + (timeout or 150)
            while time.time() < deadline:
                await asyncio.sleep(2)
                async with sess.get("%s/res.php" % CAPTCHA_API_URL,
                                    params={"key": key, "action": "get",
                                            "id": job, "json": 1},
                                    timeout=30) as resp:
                    second = await resp.json(content_type=None)
                if not isinstance(second, dict):
                    continue
                said = str(second.get("request") or "")
                if second.get("status") == 1:
                    if said:
                        logger.info("the api answered the %s", kind)
                        return said
                    continue
                if said == "CAPCHA_NOT_READY":
                    continue
                if said == "ERROR_WRONG_USER_KEY":
                    logger.warning("the api did not take our key; the key of "
                                   "the client and the key of the api are not "
                                   "the same one")
                else:
                    logger.info("the api could not answer the %s: %s", kind, said)
                return None
    except Exception as e:
        logger.info("the captcha api could not be reached: %s: %s",
                    type(e).__name__, str(e)[:70])
    return None


def has_local_solver() -> bool:
    """Is the solver on this machine ready to answer?

    The gates ask this before they give up on a captcha, so there is no
    outside service to fall back to.
    """
    try:
        from local_hcaptcha_solver import solve_hcaptcha_local
    except Exception:
        return False
    return callable(solve_hcaptcha_local)


async def solve_hcaptcha(sitekey, pageurl, session=None, proxy=None,
                     fingerprint=None):
    """An hCaptcha is answered on this machine.

    The api is asked first, so that every captcha of every gate goes
    through one place that can be watched.  When the api is not there,
    the solver is used directly.
    """
    token = await _api_solve("hcaptcha", sitekey, pageurl, proxy=proxy,
                             fingerprint=fingerprint)
    if token:
        return token
    return await _local_hcaptcha_solve(sitekey, pageurl, proxy=proxy)


async def solve_hcaptcha_enterprise(sitekey, pageurl, rqdata=None, session=None,
                                    proxy=None):
    """An enterprise hCaptcha is answered on this machine.

    The request data belongs at the top of what is asked for, not
    inside it: the answer the captcha gives is for that request data.
    """
    return await _local_hcaptcha_solve(sitekey, pageurl, rqdata=rqdata, proxy=proxy)


async def solve_recaptcha_v2(sitekey, pageurl, session=None, proxy=None):
    """A reCAPTCHA is answered on this machine."""
    token = await _api_solve("recaptcha", sitekey, pageurl, proxy=proxy)
    if token:
        return token
    return await _local_hcaptcha_solve(sitekey, pageurl, proxy=proxy)


async def solve_turnstile(sitekey, pageurl, session=None, proxy=None, action=None,
                          cdata=None):
    """A Cloudflare Turnstile is answered on this machine."""
    token = await _api_solve("turnstile", sitekey, pageurl, proxy=proxy,
                             rqdata=cdata)
    if token:
        return token
    from local_hcaptcha_solver import solve_turnstile_local
    return await solve_turnstile_local(sitekey, pageurl, proxy=proxy,
                                       action=action, cdata=cdata)


def _extract_hcaptcha_sitekey(html):
    patterns = [
        r'data-sitekey=["\']([a-f0-9\-]{36})["\']',
        r'"sitekey"\s*:\s*"([a-f0-9\-]{36})"',
        r"'sitekey'\s*:\s*'([a-f0-9\-]{36})'",
        r'hcaptcha\.com[^"\']*sitekey=([a-f0-9\-]{36})',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _extract_recaptcha_sitekey(html):
    patterns = [
        r'data-sitekey=["\']([A-Za-z0-9_\-]{20,})["\']',
        r'"sitekey"\s*:\s*"([A-Za-z0-9_\-]{20,})"',
        r'grecaptcha\.render\([^,]*,\s*\{[^}]*["\']sitekey["\']\s*:\s*["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _extract_turnstile_params(html):
    params = {'sitekey': None, 'action': None, 'cdata': None}

    sitekey_patterns = [
        r'class=["\'][^"\']*cf-turnstile[^"\']*["\'][^>]*data-sitekey=["\']([^"\']+)["\']',
        r'data-sitekey=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*cf-turnstile',
        r'challenges\.cloudflare\.com/turnstile[^"\']*sitekey=([^&"\'&amp;]+)',
        r'data-sitekey=["\']([0-9a-zA-Z_x-]+)["\']',
        r'sitekey[\s"\'=:]+["\']?(0x[0-9a-zA-Z_-]+)',
        r'"sitekey"\s*:\s*"([^"]+)"',
        r"'sitekey'\s*:\s*'([^']+)'",
        r'siteKey["\s:=]+["\']([^"\']+)["\']',
    ]
    for pat in sitekey_patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m and len(m.group(1)) > 10:
            params['sitekey'] = m.group(1)
            break

    turnstile_block = re.search(r'(<[^>]*cf-turnstile[^>]*>)', html, re.IGNORECASE | re.DOTALL)
    if turnstile_block:
        block = turnstile_block.group(1)
        action_m = re.search(r'data-action=["\']([^"\']+)["\']', block, re.IGNORECASE)
        if action_m:
            params['action'] = action_m.group(1)
        cdata_m = re.search(r'data-cdata=["\']([^"\']+)["\']', block, re.IGNORECASE)
        if cdata_m:
            params['cdata'] = cdata_m.group(1)

    if not params['action']:
        action_m = re.search(r'data-action=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if action_m:
            params['action'] = action_m.group(1)
    if not params['cdata']:
        cdata_m = re.search(r'data-cdata=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if cdata_m:
            params['cdata'] = cdata_m.group(1)

    js_action = re.search(r'["\']action["\']\s*:\s*["\']([^"\']+)["\']', html)
    if js_action and not params['action']:
        params['action'] = js_action.group(1)
    js_cdata = re.search(r'["\']cData["\']\s*:\s*["\']([^"\']+)["\']', html)
    if js_cdata and not params['cdata']:
        params['cdata'] = js_cdata.group(1)

    return params


def _detect_captcha_type(html):
    html_lower = html.lower()
    if 'cf-turnstile' in html_lower or 'challenges.cloudflare.com/turnstile' in html_lower:
        return 'turnstile'
    if 'hcaptcha' in html_lower or 'h-captcha' in html_lower:
        return 'hcaptcha'
    if 'turnstile' in html_lower:
        return 'turnstile'
    if 'recaptcha' in html_lower or 'g-recaptcha' in html_lower:
        return 'recaptcha'
    return 'turnstile'


def _extract_form_data(html):
    data = {}

    auth_patterns = [
        r'name=["\']authenticity_token["\'][^>]*value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']authenticity_token["\']',
        r'"authenticity_token"\s*:\s*"([^"]+)"',
    ]
    for pat in auth_patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            data['authenticity_token'] = m.group(1)
            break

    hidden_inputs = re.findall(
        r'<input[^>]+type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
        html, re.IGNORECASE
    )
    hidden_inputs2 = re.findall(
        r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\'][^>]*type=["\']hidden["\']',
        html, re.IGNORECASE
    )
    for name, value in hidden_inputs + hidden_inputs2:
        if name not in data:
            data[name] = value

    return data


def _extract_form_action(html, page_url):
    action_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if action_match:
        action_url = action_match.group(1)
        if action_url.startswith("http"):
            return action_url
        parsed = urlparse(page_url)
        if action_url.startswith("/"):
            return f"{parsed.scheme}://{parsed.netloc}{action_url}"
        return urljoin(page_url, action_url)
    return page_url


async def solve_captcha_on_page(html, page_url, session=None):
    captcha_type = _detect_captcha_type(html)
    logger.info(f"solve_captcha_on_page: detected={captcha_type}, url={page_url[:60]}")

    token = None
    if captcha_type == "hcaptcha":
        sitekey = _extract_hcaptcha_sitekey(html)
        if not sitekey:
            logger.warning("hCaptcha detected but no sitekey found")
            return None
        token = await solve_hcaptcha(sitekey, page_url, session)
    elif captcha_type == "recaptcha":
        sitekey = _extract_recaptcha_sitekey(html)
        if not sitekey:
            logger.warning("reCaptcha detected but no sitekey found")
            return None
        token = await solve_recaptcha_v2(sitekey, page_url, session)
    elif captcha_type == "turnstile":
        params = _extract_turnstile_params(html)
        if not params['sitekey']:
            logger.warning("Turnstile detected but no sitekey found")
            return None
        token = await solve_turnstile(params['sitekey'], page_url, session, params['action'], params['cdata'])

    return token


async def submit_captcha_token(session, page_url, token, captcha_type, html):
    try:
        form_action = _extract_form_action(html, page_url)
        form_data = _extract_form_data(html)

        if captcha_type == "hcaptcha":
            form_data["h-captcha-response"] = token
            form_data["g-recaptcha-response"] = token
        elif captcha_type == "recaptcha":
            form_data["g-recaptcha-response"] = token
        elif captcha_type == "turnstile":
            form_data["cf-turnstile-response"] = token
        else:
            form_data["cf-turnstile-response"] = token
            form_data["h-captcha-response"] = token
            form_data["g-recaptcha-response"] = token

        if api_headers is not None:
            headers = api_headers(
                origin=f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}",
                referer=page_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        else:
            headers = {
                'User-Agent': UA,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': page_url,
                'Origin': f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}",
            }

        logger.info(f"Submitting captcha token to: {form_action[:80]}, fields: {list(form_data.keys())}")

        async with session.post(
            form_action,
            data=form_data,
            headers=headers,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            final_url = str(resp.url)
            status = resp.status
            text = await resp.text()
            logger.info(f"Captcha submit response: status={status}, final_url={final_url[:80]}")

            if "checkpoint" not in final_url.lower() and "captcha" not in final_url.lower():
                logger.info("Captcha solved - redirected away from checkpoint")
                return final_url, text, True

            if "checkpoint" not in text.lower() and "captcha" not in text.lower():
                logger.info("Captcha solved - no more captcha content on page")
                return final_url, text, True

            logger.warning(f"Captcha submission did not clear checkpoint. Status: {status}")
            return final_url, text, False

    except Exception as e:
        logger.error(f"submit_captcha_token error: {e}", exc_info=True)
        return page_url, "", False


async def solve_checkpoint(checkpoint_url, http_session):
    try:
        headers = (navigation_headers() if navigation_headers is not None else
                   {'User-Agent': UA,
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9'})

        logger.info(f"Fetching checkpoint URL: {checkpoint_url[:80]}")
        async with http_session.get(
            checkpoint_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as resp:
            html = await resp.text()
            page_url = str(resp.url)
            status = resp.status
            logger.info(f"Checkpoint page status: {status}, url: {page_url[:80]}, html_len: {len(html)}")

        if not html or len(html) < 50:
            logger.warning(f"Checkpoint page empty or too short ({len(html)} chars)")
            return None

        captcha_type = _detect_captcha_type(html)
        token = await solve_captcha_on_page(html, page_url, http_session)

        if not token:
            logger.warning("Failed to solve captcha - no token received")
            return None

        logger.info(f"Got captcha token, submitting to checkpoint...")
        _, _, success = await submit_captcha_token(http_session, page_url, token, captcha_type, html)
        return success or None

    except Exception as e:
        logger.error(f"solve_checkpoint error: {e}", exc_info=True)
        return None