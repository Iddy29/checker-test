import aiohttp
import asyncio
import os
import re
import json
import logging
from urllib.parse import urlparse, urljoin

logger = logging.getLogger(__name__)

NOPECHA_SUBMIT = "https://api.nopecha.com/token"
NOPECHA_RESULT = "https://api.nopecha.com/token"

CAPTCHAAI_IN = "https://ocr.captchaai.com/in.php"
CAPTCHAAI_RES = "https://ocr.captchaai.com/res.php"

TWOCAPTCHA_IN  = "https://2captcha.com/in.php"
TWOCAPTCHA_RES = "https://2captcha.com/res.php"

CAPSOLVER_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_RES = "https://api.capsolver.com/getTaskResult"

CUSTOM_API_URL = "https://gold-newt-367030.hostingersite.com/api.php"

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

def get_nopecha_key() -> str:
    return _get_config_key("nopecha_api_key", "NOPECHA_API_KEY")

def get_captchaai_key() -> str:
    return _get_config_key("captchaai_api_key", "CAPTCHAAI_API_KEY")

def get_twocaptcha_key() -> str:
    return _get_config_key("twocaptcha_api_key", "TWOCAPTCHA_API_KEY")

def get_capsolver_key() -> str:
    return _get_config_key("capsolver_api_key", "CAPSOLVER_API_KEY")

def get_custom_captcha_token() -> str:
    return _get_config_key("custom_captcha_token", "CUSTOM_CAPTCHA_TOKEN")

def get_custom_captcha_url() -> str:
    url = _get_config_key("custom_captcha_url", "CUSTOM_CAPTCHA_URL")
    return url or CUSTOM_API_URL

def has_any_solver_key() -> bool:
    return bool(get_nopecha_key() or get_captchaai_key() or get_twocaptcha_key() or get_capsolver_key() or get_custom_captcha_token())


# ── Proxy pool ──────────────────────────────────────────────────────────────
# Turnstile tokens must be solved through a proxy, so the solver needs one
# from the same pool the bot uses for its own requests.
_PROXIES_PATH = os.path.join(os.path.dirname(__file__), "user_proxies.json")
_proxy_cache = {"mtime": None, "list": []}


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

    The pool stores "host:port" or "host:port:user:pass".  NopeCHA wants an
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


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"


async def _nopecha_solve(task_type, sitekey, pageurl, session=None, extra=None):
    key = get_nopecha_key()
    if not key:
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        # Turnstile tokens MUST be solved through a proxy -- NopeCHA rejects
        # the job without one ("Missing proxy data").  The proxy arrives as
        # an object with scheme/host/port/username/password.
        proxy = (extra or {}).get("proxy")
        if task_type in ("turnstile", "hcaptcha", "recaptcha") and not proxy:
            proxy = _get_random_proxy()
        if isinstance(proxy, str):
            proxy = _format_proxy(proxy)

        payload = {
            "type": task_type,
            "sitekey": sitekey,
            "url": pageurl,
            "key": key,
        }
        if proxy:
            payload["proxy"] = proxy
        if extra:
            if "rqdata" in extra:
                payload["data"] = {"rqdata": extra["rqdata"]}
            if extra.get("action"):
                payload["action"] = extra["action"]
            if extra.get("data"):
                payload["data"] = {"cData": extra["data"]}

        logger.info(f"NopeCHA submit: type={task_type}, sitekey={sitekey[:20]}..., url={pageurl[:60]}, proxy={'yes' if proxy else 'no'}")

        task_id = None
        # A proxy in the pool can be dead.  Try up to 4 of them.
        for attempt in range(4):
            async with session.post(NOPECHA_SUBMIT, json=payload) as resp:
                result = await resp.json(content_type=None)
            if "error" in result:
                err = result.get("error")
                err_msg = result.get("message", "")
                err_type = result.get("type", "")
                if "Invalid proxy" in str(err_type) or "Invalid proxy" in str(err_msg):
                    logger.warning(f"NopeCHA: dead proxy, rotating ({err_type or err_msg})")
                    proxy = _get_random_proxy()
                    if proxy:
                        payload["proxy"] = proxy
                    await asyncio.sleep(1)
                    continue
                if err == 11 or "Rate limit" in str(err_msg):
                    logger.warning(f"NopeCHA rate limited: {err_msg}")
                    return None
                logger.warning(f"NopeCHA submit error {err}: {err_msg}")
                return None
            task_id = result.get("data")
            if task_id:
                logger.info(f"NopeCHA task created: {task_id}")
                break
            logger.warning(f"NopeCHA: no task_id in response: {result}")
            return None

        if not task_id:
            return None

        await asyncio.sleep(1)

        # A Turnstile solve through a slow proxy can take a couple of
        # minutes, so poll for longer than the default 40 x 1s.
        for attempt in range(90):
            params = {"id": task_id, "key": key}
            async with session.get(NOPECHA_RESULT, params=params) as resp:
                result = await resp.json(content_type=None)
                if "error" in result:
                    err = result["error"]
                    err_msg = result.get("message", "")
                    err_type = result.get("type", "")
                    if err in (9, 14) or err == "Incomplete" or "Incomplete" in str(err_msg):
                        # still solving -- keep polling
                        pass
                    elif "Invalid proxy" in str(err_type) or "Invalid proxy" in str(err_msg):
                        logger.warning(f"NopeCHA: proxy died mid-job ({err_msg})")
                        return None
                    elif "Missing proxy" in str(err_msg):
                        logger.warning(f"NopeCHA: {err_msg}")
                        return None
                    elif err == 11 or "Rate limit" in str(err_msg):
                        logger.warning(f"NopeCHA rate limited: {err_msg}")
                        return None
                    else:
                        logger.warning(f"NopeCHA poll error {err}: {err_msg}")
                        return None
                else:
                    data = result.get("data")
                    token = data[0] if isinstance(data, list) and data else data
                    if token and isinstance(token, str) and len(token) > 20:
                        logger.info(f"NopeCHA solved! Token: {type(token).__name__}: {str(token)[:30]}...")
                        return token
            await asyncio.sleep(2)

        logger.warning("NopeCHA timeout - max poll attempts reached")
        return None
    except Exception as e:
        logger.error(f"NopeCHA solve error: {e}")
        return None
    finally:
        if own_session:
            await session.close()


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


async def _captchaai_solve(method, sitekey, pageurl, session=None, extra=None):
    key = get_captchaai_key()
    if not key:
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        submit_data = {
            "key": key,
            "method": method,
            "sitekey": sitekey,
            "pageurl": pageurl,
            "json": "1",
        }
        if extra:
            submit_data.update(extra)
        logger.info(f"CaptchaAI submit: method={method}, sitekey={sitekey[:20]}..., url={pageurl[:60]}")
        async with session.post(CAPTCHAAI_IN, data=submit_data) as resp:
            result = await resp.json(content_type=None)
            if result.get("status") != 1:
                logger.warning(f"CaptchaAI submit failed: {result}")
                return None
            task_id = result["request"]
            logger.info(f"CaptchaAI task created: {task_id}")

        await asyncio.sleep(10)

        for attempt in range(MAX_POLL_ATTEMPTS):
            params = {"key": key, "action": "get", "id": task_id, "json": "1"}
            async with session.get(CAPTCHAAI_RES, params=params) as resp:
                result = await resp.json(content_type=None)
                if result.get("status") == 1:
                    token = result["request"]
                    logger.info(f"CaptchaAI solved! Token: {token[:30]}...")
                    return token
                if result.get("request") != "CAPCHA_NOT_READY":
                    logger.warning(f"CaptchaAI error: {result}")
                    return None
            await asyncio.sleep(POLL_INTERVAL)

        logger.warning("CaptchaAI timeout")
        return None
    except Exception as e:
        logger.error(f"CaptchaAI solve error: {e}")
        return None
    finally:
        if own_session:
            await session.close()


async def _twocaptcha_solve(method, sitekey, pageurl, session=None, extra=None):
    key = get_twocaptcha_key()
    if not key:
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        submit_data = {
            "key": key,
            "method": method,
            "sitekey": sitekey,
            "pageurl": pageurl,
            "json": "1",
        }
        if extra:
            submit_data.update(extra)
        logger.info(f"2captcha submit: method={method}, sitekey={sitekey[:20]}..., url={pageurl[:60]}")
        async with session.post(TWOCAPTCHA_IN, data=submit_data) as resp:
            result = await resp.json(content_type=None)
            if result.get("status") != 1:
                logger.warning(f"2captcha submit failed: {result}")
                return None
            task_id = result["request"]
            logger.info(f"2captcha task created: {task_id}")

        await asyncio.sleep(15)

        for attempt in range(MAX_POLL_ATTEMPTS):
            params = {"key": key, "action": "get", "id": task_id, "json": "1"}
            async with session.get(TWOCAPTCHA_RES, params=params) as resp:
                result = await resp.json(content_type=None)
                if result.get("status") == 1:
                    token = result["request"]
                    logger.info(f"2captcha solved! Token: {token[:30]}...")
                    return token
                if result.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                    logger.warning(f"2captcha error: {result}")
                    return None
            await asyncio.sleep(POLL_INTERVAL)

        logger.warning("2captcha timeout")
        return None
    except Exception as e:
        logger.error(f"2captcha solve error: {e}")
        return None
    finally:
        if own_session:
            await session.close()


async def _capsolver_solve(task_type, sitekey, pageurl, session=None, extra=None):
    key = get_capsolver_key()
    if not key:
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        task = {
            "type": task_type,
            "websiteURL": pageurl,
            "websiteKey": sitekey,
        }
        if extra:
            task.update(extra)
        payload = {"clientKey": key, "task": task}
        logger.info(f"CapSolver submit: type={task_type}, sitekey={sitekey[:20]}..., url={pageurl[:60]}")
        async with session.post(CAPSOLVER_URL, json=payload) as resp:
            result = await resp.json(content_type=None)
            if result.get("errorId") != 0:
                logger.warning(f"CapSolver submit error: {result.get('errorCode')} {result.get('errorDescription')}")
                return None
            task_id = result.get("taskId")
            if not task_id:
                logger.warning(f"CapSolver: no taskId in response: {result}")
                return None
            logger.info(f"CapSolver task created: {task_id}")

        await asyncio.sleep(5)

        for attempt in range(MAX_POLL_ATTEMPTS):
            poll_payload = {"clientKey": key, "taskId": task_id}
            async with session.post(CAPSOLVER_RES, json=poll_payload) as resp:
                result = await resp.json(content_type=None)
                if result.get("errorId") != 0:
                    logger.warning(f"CapSolver poll error: {result.get('errorCode')}")
                    return None
                status = result.get("status")
                if status == "ready":
                    solution = result.get("solution", {})
                    token = solution.get("gRecaptchaResponse") or solution.get("token") or solution.get("userAgent")
                    if token:
                        logger.info(f"CapSolver solved! Token: {type(token).__name__}: {str(token)[:30]}...")
                        return token
                    logger.warning(f"CapSolver ready but no token in solution: {solution}")
                    return None
                elif status != "processing":
                    logger.warning(f"CapSolver unknown status: {status}")
                    return None
            await asyncio.sleep(POLL_INTERVAL)

        logger.warning("CapSolver timeout")
        return None
    except Exception as e:
        logger.error(f"CapSolver solve error: {e}")
        return None
    finally:
        if own_session:
            await session.close()


async def _custom_api_solve(task_type, sitekey, pageurl, session=None, extra=None):
    token = get_custom_captcha_token()
    if not token:
        return None
    api_url = get_custom_captcha_url()
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        if task_type and "recaptcha" in task_type.lower():
            captcha_type = "recaptcha"
        elif task_type and "turnstile" in task_type.lower():
            captcha_type = "turnstile"
        else:
            captcha_type = "hcaptcha"

        submit_params = {
            "action": "captcha-solve",
            "token": token,
            "type": captcha_type,
            "sitekey": sitekey,
            "pageurl": pageurl,
        }
        if extra:
            submit_params.update(extra)

        logger.info(f"CustomAPI submit: type={captcha_type}, sitekey={sitekey[:20]}..., url={pageurl[:60]}")
        async with session.post(api_url, json=submit_params) as resp:
            result = await resp.json(content_type=None)
            if not result.get("success") and result.get("error"):
                logger.warning(f"CustomAPI submit error: {result.get('error')}")
                return None

            if result.get("success") and result.get("solution"):
                solved = result["solution"]
                if isinstance(solved, str) and len(solved) > 20:
                    logger.info(f"CustomAPI solved instantly! Token: {solved[:30]}...")
                    return solved
                if isinstance(solved, dict):
                    tok = solved.get("token") or solved.get("gRecaptchaResponse") or solved.get("code")
                    if tok and len(str(tok)) > 20:
                        logger.info(f"CustomAPI solved instantly! Token: {type(tok).__name__}: {str(tok)[:30]}...")
                        return str(tok)

            task_id = result.get("task_id") or result.get("id") or result.get("request")
            if not task_id:
                if result.get("success"):
                    logger.warning(f"CustomAPI: success but no solution/task_id: {result}")
                return None

            logger.info(f"CustomAPI task created: {task_id}")

        await asyncio.sleep(10)

        for attempt in range(MAX_POLL_ATTEMPTS):
            poll_params = {
                "action": "captcha-result",
                "token": token,
                "task_id": str(task_id),
            }
            try:
                async with session.get(api_url, params=poll_params) as resp:
                    result = await resp.json(content_type=None)
                    if result.get("success") and result.get("solution"):
                        solved = result["solution"]
                        if isinstance(solved, str) and len(solved) > 20:
                            logger.info(f"CustomAPI solved! Token: {solved[:30]}...")
                            return solved
                        if isinstance(solved, dict):
                            tok = solved.get("token") or solved.get("gRecaptchaResponse") or solved.get("code")
                            if tok and len(str(tok)) > 20:
                                logger.info(f"CustomAPI solved! Token: {type(tok).__name__}: {str(tok)[:30]}...")
                                return str(tok)
                    if result.get("status") == "processing" or result.get("error") == "CAPCHA_NOT_READY":
                        pass
                    elif result.get("error"):
                        logger.warning(f"CustomAPI poll error: {result.get('error')}")
                        return None
            except Exception:
                pass
            await asyncio.sleep(POLL_INTERVAL)

        logger.warning("CustomAPI timeout - max poll attempts reached")
        return None
    except Exception as e:
        logger.error(f"CustomAPI solve error: {e}")
        return None
    finally:
        if own_session:
            await session.close()


def _external_allowed() -> bool:
    """May a paid solving service be used?

    Only when it is asked for.  The local browser solver has no key and
    no bill, so it is the one that runs by default.
    """
    return str(os.environ.get("CAPTCHA_EXTERNAL", "")).strip().lower() in (
        "1", "true", "yes", "on")


async def solve_hcaptcha(sitekey, pageurl, session=None, proxy=None):
    token = await _local_hcaptcha_solve(sitekey, pageurl, proxy=proxy)
    if token:
        return token
    if not _external_allowed():
        logger.info("no hCaptcha token and the outside services are off")
        return None
    if get_nopecha_key():
        token = await _nopecha_solve("hcaptcha", sitekey, pageurl, session)
        if token:
            return token
        logger.info("NopeCHA hCaptcha failed, trying CapSolver...")
    if get_capsolver_key():
        token = await _capsolver_solve("HCaptchaTask", sitekey, pageurl, session)
        if token:
            return token
        logger.info("CapSolver hCaptcha failed, trying CaptchaAI...")
    if get_captchaai_key():
        token = await _captchaai_solve("hcaptcha", sitekey, pageurl, session)
        if token:
            return token
        logger.info("CaptchaAI hCaptcha failed, trying 2captcha...")
    if get_twocaptcha_key():
        token = await _twocaptcha_solve("hcaptcha", sitekey, pageurl, session)
        if token:
            return token
    if get_custom_captcha_token():
        return await _custom_api_solve("hcaptcha", sitekey, pageurl, session)
    return None


async def solve_hcaptcha_enterprise(sitekey, pageurl, rqdata=None, session=None,
                                    proxy=None):
    extra_cs = {}
    if rqdata:
        extra_cs["enterprisePayload"] = {"rqdata": rqdata}

    token = await _local_hcaptcha_solve(sitekey, pageurl, rqdata=rqdata, proxy=proxy)
    if token:
        return token
    if not _external_allowed():
        logger.info("no enterprise hCaptcha token and the outside services are off")
        return None
    if get_nopecha_key():
        nopecha_extra = {}
        if rqdata:
            nopecha_extra["rqdata"] = rqdata
        token = await _nopecha_solve("hcaptcha", sitekey, pageurl, session, extra=nopecha_extra or None)
        if token and len(token) <= 5000:
            return token
        elif token:
            logger.info(f"NopeCHA enterprise token too long ({len(token)} chars), skipping")
        logger.info("NopeCHA enterprise failed, trying CapSolver...")
    if get_capsolver_key():
        token = await _capsolver_solve("HCaptchaEnterpriseTask", sitekey, pageurl, session, extra=extra_cs or None)
        if token:
            return token
        logger.info("CapSolver enterprise failed, trying CaptchaAI...")
    if get_captchaai_key():
        captchaai_extra = {}
        if rqdata:
            captchaai_extra["data"] = rqdata
        token = await _captchaai_solve("hcaptcha", sitekey, pageurl, session, extra=captchaai_extra or None)
        if token:
            return token
        logger.info("CaptchaAI enterprise failed, trying 2captcha...")
    if get_twocaptcha_key():
        extra_2c = {"enterprise": "1"}
        if rqdata:
            extra_2c["data"] = rqdata
        token = await _twocaptcha_solve("hcaptcha", sitekey, pageurl, session, extra=extra_2c)
        if token:
            return token
    if get_custom_captcha_token():
        custom_extra = {}
        if rqdata:
            custom_extra["rqdata"] = rqdata
            custom_extra["enterprise"] = "1"
        return await _custom_api_solve("hcaptcha", sitekey, pageurl, session, extra=custom_extra or None)
    return None


async def solve_recaptcha_v2(sitekey, pageurl, session=None, proxy=None):
    token = await _local_hcaptcha_solve(sitekey, pageurl)
    if token:
        return token
    if not _external_allowed():
        logger.info("no reCaptcha token and the outside services are off")
        return None
    if get_nopecha_key():
        token = await _nopecha_solve("recaptcha", sitekey, pageurl, session,
                                     extra={"proxy": proxy} if proxy else None)
        if token:
            return token
        logger.info("NopeCHA reCaptcha failed, trying CaptchaAI...")
    if get_captchaai_key():
        token = await _captchaai_solve("userrecaptcha", sitekey, pageurl, session)
        if token:
            return token
        logger.info("CaptchaAI reCaptcha failed, trying 2captcha...")
    if get_twocaptcha_key():
        token = await _twocaptcha_solve("userrecaptcha", sitekey, pageurl, session)
        if token:
            return token
        logger.info("2captcha reCaptcha failed, trying CapSolver...")
    return await _capsolver_solve("ReCaptchaV2TaskProxyless", sitekey, pageurl, session)


async def solve_turnstile(sitekey, pageurl, session=None, action=None, cdata=None,
                          proxy=None):
    # the local browser solver first: no key and no bill
    try:
        from local_hcaptcha_solver import solve_turnstile_local
        token = await solve_turnstile_local(sitekey, pageurl, proxy=proxy)
        if token:
            return token
    except Exception as e:
        logger.info("the local Turnstile solver failed: %s", e)
    if not _external_allowed():
        logger.info("no Turnstile token and the outside services are off")
        return None
    # NopeCHA next -- the others need a paid key
    if get_nopecha_key():
        extra = {}
        if action:
            extra["action"] = action
        if cdata:
            extra["data"] = cdata
        if proxy:
            extra["proxy"] = proxy
        token = await _nopecha_solve("turnstile", sitekey, pageurl, session,
                                     extra=extra or None)
        if token:
            return token
        logger.info("NopeCHA Turnstile failed, trying CaptchaAI...")
    if get_captchaai_key():
        extra = {}
        if action:
            extra["action"] = action
        if cdata:
            extra["data"] = cdata
        token = await _captchaai_solve("turnstile", sitekey, pageurl, session, extra)
        if token:
            return token
        logger.info("CaptchaAI Turnstile failed, trying 2captcha...")
    if get_twocaptcha_key():
        extra = {}
        if action:
            extra["action"] = action
        if cdata:
            extra["data"] = cdata
        token = await _twocaptcha_solve("turnstile", sitekey, pageurl, session, extra)
        if token:
            return token
        logger.info("2captcha Turnstile failed, trying CapSolver...")
    cs_extra = {}
    if action:
        cs_extra["action"] = action
    return await _capsolver_solve("AntiTurnstileTaskProxyless", sitekey, pageurl, session, extra=cs_extra or None)


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
        headers = {
            'User-Agent': UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }

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