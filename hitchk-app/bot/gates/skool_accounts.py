import asyncio
import json
import os
import time
import random
import logging
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("skool_accounts")

SKOOL_API = "https://api2.skool.com"
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skool_accounts.json")
SESSION_TTL = 900
MIN_REQUEST_INTERVAL = 0.5
MAX_CLIENT_AGE = 3600
FAIL_COOLDOWN = 300
CLIENT_TIMEOUT = 45
LOGIN_TIMEOUT = 30
VALIDATE_TIMEOUT = 15
PREFLIGHT_TIMEOUT = 20

PROXY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "proxy.txt")

_proxy_disabled_until = 0


def _load_proxy_list():
    if not os.path.exists(PROXY_FILE):
        return []
    try:
        with open(PROXY_FILE, "r") as f:
            return [line.strip() for line in f if line.strip()]
    except Exception:
        return []


def _mark_proxies_dead():
    global _proxy_disabled_until
    _proxy_disabled_until = time.time() + 300
    logger.warning("All proxies marked dead, disabling for 5 minutes")


def _get_random_proxy():
    global _proxy_disabled_until
    if time.time() < _proxy_disabled_until:
        return None
    proxies = _load_proxy_list()
    if not proxies:
        return None
    raw = random.choice(proxies)
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    elif len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return None

_accounts = []
_account_clients = {}
_account_locks = {}
_account_last_request = {}
_accounts_load_ts = 0
_accounts_lock = asyncio.Lock()
_failed_accounts = {}
_round_robin_index = 0
_user_rr_indices = {}
_warmup_done = False

STATUS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skool_status.json")
_account_statuses = {}
_status_loaded = False


def _load_statuses():
    global _account_statuses, _status_loaded
    if _status_loaded:
        return
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r") as f:
                _account_statuses = json.load(f)
    except Exception:
        _account_statuses = {}
    _status_loaded = True


def _save_statuses():
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(_account_statuses, f)
    except Exception as e:
        logger.warning(f"Error saving statuses: {e}")

CHROME_IMPERSONATE = "chrome131"
# The browser that answers the bot challenge has to look like the same
# browser the api calls pretend to be.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")


def _load_accounts():
    global _accounts, _accounts_load_ts
    now = time.time()
    if _accounts and (now - _accounts_load_ts) < 60:
        return _accounts

    accounts = []

    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                content = f.read().strip()
            if content:
                data = json.loads(content)
                if isinstance(data, list):
                    accounts = data
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to read skool_accounts.json: {e}")
            if _accounts:
                return _accounts
        except Exception as e:
            logger.warning(f"Unexpected error loading skool_accounts.json: {e}")
            if _accounts:
                return _accounts

    env_email = os.environ.get("SKOOL_EMAIL", "")
    env_pass = os.environ.get("SKOOL_PASS", "")
    if env_email and env_pass:
        env_exists = any(a.get("email") == env_email for a in accounts)
        if not env_exists:
            accounts.insert(0, {"email": env_email, "password": env_pass})

    _accounts = accounts
    _accounts_load_ts = now
    return accounts


def get_account_count():
    return len(_load_accounts())


def _get_lock(email):
    if email not in _account_locks:
        _account_locks[email] = asyncio.Lock()
    return _account_locks[email]


async def get_next_account():
    global _round_robin_index
    accounts = _load_accounts()
    if not accounts:
        return None

    now = time.time()
    async with _accounts_lock:
        tried = 0
        while tried < len(accounts):
            idx = _round_robin_index % len(accounts)
            _round_robin_index += 1
            account = accounts[idx]
            email = account.get("email", "")
            fail_info = _failed_accounts.get(email)
            if fail_info and (now - fail_info["ts"]) < FAIL_COOLDOWN and fail_info["count"] >= 5:
                tried += 1
                continue
            return account

        _round_robin_index += 1
        return accounts[_round_robin_index % len(accounts)]


async def get_fallback_account(exclude_email):
    accounts = _load_accounts()
    if not accounts:
        return None
    now = time.time()
    async with _accounts_lock:
        for account in accounts:
            email = account.get("email", "")
            if email == exclude_email:
                continue
            fail_info = _failed_accounts.get(email)
            if fail_info and (now - fail_info["ts"]) < FAIL_COOLDOWN and fail_info["count"] >= 5:
                continue
            return account
    return None


import threading
_file_lock = threading.Lock()

def _auto_remove_dead_account(email):
    global _accounts, _accounts_load_ts
    removed_from = []

    with _file_lock:
        try:
            if os.path.exists(ACCOUNTS_FILE):
                with open(ACCOUNTS_FILE, "r") as f:
                    global_accs = json.load(f)
                before = len(global_accs)
                global_accs = [a for a in global_accs if a.get("email", "").lower() != email.lower()]
                if len(global_accs) < before:
                    import tempfile
                    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(ACCOUNTS_FILE), suffix=".tmp")
                    try:
                        with os.fdopen(tmp_fd, "w") as tmp_f:
                            json.dump(global_accs, tmp_f, indent=2)
                        os.replace(tmp_path, ACCOUNTS_FILE)
                    except:
                        try:
                            os.remove(tmp_path)
                        except:
                            pass
                        raise
                    removed_from.append("global")
                    _accounts_load_ts = 0
        except Exception as e:
            logger.warning(f"Error removing dead account {email} from global: {e}")

        try:
            if os.path.exists(USER_SKOOL_FILE):
                with open(USER_SKOOL_FILE, "r") as f:
                    user_data = json.load(f)
                changed = False
                for uid in list(user_data.keys()):
                    before = len(user_data[uid])
                    user_data[uid] = [a for a in user_data[uid] if a.get("email", "").lower() != email.lower()]
                    if len(user_data[uid]) < before:
                        changed = True
                        removed_from.append(f"user:{uid}")
                    if not user_data[uid]:
                        del user_data[uid]
                if changed:
                    import tempfile
                    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(USER_SKOOL_FILE), suffix=".tmp")
                    try:
                        with os.fdopen(tmp_fd, "w") as tmp_f:
                            json.dump(user_data, tmp_f, indent=2)
                        os.replace(tmp_path, USER_SKOOL_FILE)
                    except:
                        try:
                            os.remove(tmp_path)
                        except:
                            pass
                        raise
                    global _user_skool_cache, _user_skool_ts
                    _user_skool_cache = user_data
                    _user_skool_ts = time.time()
        except Exception as e:
            logger.warning(f"Error removing dead account {email} from user accounts: {e}")

    if removed_from:
        logger.info(f"Auto-removed dead account {email} from: {', '.join(removed_from)}")

    if email in _account_clients:
        _account_clients[email] = {
            "client": None, "logged_in_ts": 0, "valid": False,
            "created_ts": 0, "last_login_error": "",
        }

_auth_fail_counts = {}

def _mark_account_failed(email, is_login_error=False, pause_only=False):
    """Record a failed attempt.

    pause_only    the failure says nothing about the account (an anti-bot
                  block, a rate limit, a network error) -- the account is
                  skipped for a while and NEVER removed.
    is_login_error the credentials were refused (401) -- after several
                  consecutive ones the account is really dead.
    """
    _load_statuses()
    now = time.time()
    if email not in _failed_accounts:
        _failed_accounts[email] = {"count": 0, "ts": now}
    info = _failed_accounts[email]
    if (now - info["ts"]) > FAIL_COOLDOWN:
        info["count"] = 1
        info["ts"] = now
    else:
        info["count"] += 1
        info["ts"] = now

    if pause_only:
        # keep the account, just stop using it for a while
        if info["count"] >= 3:
            info["count"] = 0
            info["ts"] = now
        return

    if is_login_error:
        if email not in _auth_fail_counts:
            _auth_fail_counts[email] = {"count": 0, "ts": now}
        af = _auth_fail_counts[email]
        if (now - af["ts"]) > FAIL_COOLDOWN * 2:
            af["count"] = 1
            af["ts"] = now
        else:
            af["count"] += 1
            af["ts"] = now
        if af["count"] >= 5:
            # five consecutive 401s: the credentials are gone
            _account_statuses[email] = "dead"
            _save_statuses()
            _auto_remove_dead_account(email)
            logger.info(f"Auto-removed {email} after {af['count']} consecutive 401 logins")
            return

    if info["count"] >= 5:
        _account_statuses[email] = "dead"
        _save_statuses()


def _mark_account_success(email):
    _load_statuses()
    if email in _failed_accounts:
        _failed_accounts[email] = {"count": 0, "ts": 0}
    if email in _auth_fail_counts:
        _auth_fail_counts[email] = {"count": 0, "ts": 0}
    _account_statuses[email] = "active"
    _save_statuses()


def get_account_status(email):
    _load_statuses()
    return _account_statuses.get(email, "unknown")


def get_all_accounts_with_status():
    global _accounts_load_ts
    _load_statuses()
    _accounts_load_ts = 0
    accounts = _load_accounts()
    result = []
    for a in accounts:
        email = a.get("email", "?")
        status = _account_statuses.get(email, "unknown")
        result.append({"email": email, "status": status})
    return result


def mark_account_dead(email):
    _load_statuses()
    _account_statuses[email] = "dead"
    _save_statuses()


def mark_account_active(email):
    _load_statuses()
    _account_statuses[email] = "active"
    _save_statuses()


def _get_client_data(email):
    if email not in _account_clients:
        _account_clients[email] = {
            "client": None,
            "logged_in_ts": 0,
            "valid": False,
            "created_ts": 0,
            "last_login_error": "",
        }
    return _account_clients[email]


def _next_user_proxy(current=None):
    """Another proxy from the user's list (or none, for a direct try).

    A proxy that answers 407 or drops the tunnel is skipped instead of
    being used again for the next attempt.
    """
    try:
        from gateways import get_user_proxy_list, _raw_to_formatted
    except Exception:
        return None
    try:
        raws = get_user_proxy_list("1214399247") or []
    except Exception:
        raws = []
    pool = []
    for raw in raws:
        try:
            p = _raw_to_formatted(raw)
        except Exception:
            p = None
        if p and p != current:
            pool.append(p)
    if not pool:
        return None
    return random.choice(pool)


def _cookie_dict(client):
    """The cookies of the client as a plain dict."""
    cookies = getattr(client, "cookies", None)
    if cookies is None:
        return {}
    try:
        return {c.name: c.value for c in cookies.jar}
    except Exception:
        try:
            return dict(cookies)
        except Exception:
            return {}


async def _browser_login(email, password):
    """Log in to Skool in a real browser.

    Skool's bot protection turns a plain request away with a 403, and
    the browser is the only thing that answers the challenge and hands
    out the token the api wants.  So the login is done in the browser and
    the session is taken from there.
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger.info("skool: the browser is not there: %s", str(e)[:70])
        return None
    cookies = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--disable-blink-features=AutomationControlled"])
            try:
                ctx = await browser.new_context(
                    user_agent=UA, viewport={"width": 1366, "height": 900},
                    locale="en-US")
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                page = await ctx.new_page()
                await page.goto("https://www.skool.com/login",
                                wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)
                email_el = await page.query_selector("input[type='email']")
                pass_el = await page.query_selector("input[type='password']")
                if not email_el or not pass_el:
                    logger.info("skool: the login page gave no fields")
                    return None
                await email_el.fill(email)
                await pass_el.fill(password)
                await page.keyboard.press("Enter")
                await asyncio.sleep(8)
                cookies = await ctx.cookies()
            finally:
                await browser.close()
    except Exception as e:
        logger.info("skool: the browser login failed: %s", str(e)[:90])
        return None

    jar = {c["name"]: c["value"] for c in cookies}
    if "auth_token" not in jar:
        logger.info("skool: the browser login gave no session")
        return None
    logger.info("[%s] the browser login gave a session", email[:15])
    return cookies


def _make_fresh_client(user_proxy=None):
    proxy = user_proxy or _get_random_proxy()
    kwargs = {
        "impersonate": CHROME_IMPERSONATE,
        "timeout": CLIENT_TIMEOUT,
        "headers": {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://www.skool.com",
            "Referer": "https://www.skool.com/",
            "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    }
    if proxy:
        kwargs["proxy"] = proxy
        logger.debug(f"Using proxy: {proxy.split('@')[-1] if '@' in proxy else proxy}")
    return AsyncSession(**kwargs)


def _is_client_closed(client):
    if client is None:
        return True
    try:
        if hasattr(client, 'closed') and client.closed:
            return True
    except Exception:
        pass
    return False


async def _close_client(client):
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        try:
            client.close()
        except Exception:
            pass


async def _ensure_client(email, user_proxy=None):
    data = _get_client_data(email)
    now = time.time()
    needs_new = (
        data["client"] is None
        or _is_client_closed(data["client"])
        or (data.get("created_ts", 0) and (now - data["created_ts"]) > MAX_CLIENT_AGE)
    )
    if needs_new:
        await _close_client(data["client"])
        data["client"] = _make_fresh_client(user_proxy=user_proxy)
        data["logged_in_ts"] = 0
        data["valid"] = False
        data["created_ts"] = now
    return data


async def _throttle_request(email):
    now = time.time()
    last = _account_last_request.get(email, 0)
    wait = MIN_REQUEST_INTERVAL - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _account_last_request[email] = time.time()


async def _do_login(client, email, password):
    r = await client.post(
        f"{SKOOL_API}/auth/login",
        json={"email": email, "password": password},
        timeout=LOGIN_TIMEOUT,
    )
    if r.status_code == 403:
        # The bot protection turned the request away.  The browser
        # answers the challenge and hands out the session, so the login
        # is done there and the cookies are put on the client.
        cookies = await _browser_login(email, password)
        if cookies:
            for c in cookies:
                try:
                    client.cookies.set(c["name"], c["value"],
                                       domain=c.get("domain") or ".skool.com",
                                       path=c.get("path") or "/")
                except Exception:
                    pass
            logger.info("[%s] the browser session is on the client", email[:15])
            return _FakeLoginOk()
    return r


class _FakeLoginOk:
    """Stands in for the answer of a login that the browser did."""
    status_code = 200

    def __init__(self):
        self.text = "{}"


async def _preflight(client):
    try:
        r = await client.get(
            "https://www.skool.com/",
            timeout=PREFLIGHT_TIMEOUT,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "max-age=0",
            },
        )
        logger.debug(f"Preflight status: {r.status_code}, cookies: {list(client.cookies.keys()) if hasattr(client, 'cookies') else '?'}")
        return r.status_code < 400
    except Exception as e:
        logger.debug(f"Preflight failed: {e}")
        return False


async def _validate_session(client):
    try:
        r = await client.get(f"{SKOOL_API}/self", timeout=VALIDATE_TIMEOUT)
        if r.status_code == 200:
            body = r.json()
            return bool(body.get("id") or body.get("email"))
        return False
    except Exception:
        return False


async def get_authed_client(account, force_refresh=False, user_proxy=None):
    email = account["email"]
    password = account["password"]

    data = await _ensure_client(email, user_proxy=user_proxy)
    client = data["client"]

    now = time.time()
    session_age = now - data["logged_in_ts"]

    if not force_refresh and data["valid"] and session_age < SESSION_TTL:
        await _throttle_request(email)
        return client

    if not force_refresh and data["valid"] and session_age < SESSION_TTL * 2:
        await _throttle_request(email)
        if await _validate_session(client):
            data["logged_in_ts"] = now
            data["valid"] = True
            _mark_account_success(email)
            return client
        logger.info(f"[{email[:15]}] Session validation failed, re-logging in")

    data["valid"] = False

    for login_attempt in range(3):
        try:
            if login_attempt > 0:
                await _close_client(client)
                client = _make_fresh_client(user_proxy=user_proxy)
                data["client"] = client
                data["created_ts"] = time.time()
                await asyncio.sleep(random.uniform(1.0, 2.5))

            if data.get("created_ts", 0) and (time.time() - data["created_ts"]) < 10:
                await _preflight(client)
                await asyncio.sleep(random.uniform(0.3, 0.8))

            r = await _do_login(client, email, password)

            if r.status_code == 200:
                session_cookies = _cookie_dict(client)
                auth_token = session_cookies.get("auth_token")
                if auth_token:
                    data["logged_in_ts"] = time.time()
                    data["valid"] = True
                    data["last_login_error"] = ""
                    _mark_account_success(email)
                    await _throttle_request(email)
                    logger.info(f"[{email[:15]}] Login OK (auth_token)")
                    return client
                if session_cookies:
                    valid = await _validate_session(client)
                    if valid:
                        data["logged_in_ts"] = time.time()
                        data["valid"] = True
                        data["last_login_error"] = ""
                        _mark_account_success(email)
                        await _throttle_request(email)
                        logger.info(f"[{email[:15]}] Login OK (session validated)")
                        return client
                try:
                    body = r.json()
                    err_detail = str(body)[:100]
                except Exception:
                    err_detail = r.text[:100]
                data["last_login_error"] = f"200 but no auth: {err_detail}"
                logger.warning(f"[{email[:15]}] Login 200 but no auth_token. cookies={list(session_cookies.keys())}, body={err_detail}")

            elif r.status_code == 403:
                # CloudFront blocks the address, not the account: every
                # account fails the same way from the same IP.
                data["last_login_error"] = (
                    "403 blocked (anti-bot) - this IP is blocked by Skool, "
                    "a proxy is needed")
                logger.warning(f"[{email[:15]}] Login 403 - anti-bot block, will retry with new client")
                if login_attempt < 1:
                    # The block is about the address, so the retry uses a
                    # DIFFERENT proxy -- the same one would be blocked
                    # again.  Without a working proxy this is where the
                    # gate gives up.
                    await _close_client(client)
                    _mark_proxies_dead()
                    user_proxy = _next_user_proxy(user_proxy)
                    client = _make_fresh_client(user_proxy=user_proxy)
                    data["client"] = client
                    data["created_ts"] = time.time()
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    await _preflight(client)
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    continue
                # The block is about the IP or the fingerprint, not the
                # account, so the account is only paused.
                _mark_account_failed(email, pause_only=True)
                return None

            elif r.status_code == 429:
                wait = random.uniform(5, 10) * (login_attempt + 1)
                data["last_login_error"] = f"429 rate limited"
                logger.warning(f"[{email[:15]}] Login 429, waiting {wait:.0f}s")
                await asyncio.sleep(wait)
                continue

            elif r.status_code == 401:
                # The credentials really are wrong.  This is the ONLY
                # answer that may take the account away.
                try:
                    body = r.text[:150]
                except Exception:
                    body = "?"
                data["last_login_error"] = f"401 unauthorized: {body}"
                logger.warning(f"[{email[:15]}] Login 401 - wrong credentials")
                _mark_account_failed(email, is_login_error=True)
                # the credentials are wrong -- retrying cannot fix that
                return None

            else:
                try:
                    body = r.text[:150]
                except Exception:
                    body = "?"
                data["last_login_error"] = f"HTTP {r.status_code}: {body}"
                logger.warning(f"[{email[:15]}] Login failed: {r.status_code} - {body}")
                # A 200 without an auth token, a 403 anti-bot page, a 429 or
                # any other answer says nothing about the credentials, so
                # the account is only paused -- it is never removed.
                _mark_account_failed(email)

            if login_attempt < 2:
                await asyncio.sleep(random.uniform(1, 2))
                continue
            return None

        except Exception as e:
            err_str = str(e)
            is_proxy_error = any(s in err_str for s in [
                "407", "CONNECT tunnel", "proxy", "Proxy",
                "Name or service not known", "Errno -2", "getaddrinfo",
                "Connection refused", "tunnel failed",
            ])
            if is_proxy_error:
                # The proxy is the problem, so it must not be used again
                # for this login.  Rebuilding the client with the SAME
                # proxy just failed the same way three times in a row.
                logger.warning(f"[{email[:15]}] Proxy/network error: {err_str[:80]}")
                _mark_proxies_dead()
                await _close_client(client)
                user_proxy = _next_user_proxy(user_proxy)
                client = _make_fresh_client(user_proxy=user_proxy)
                data["client"] = client
                data["created_ts"] = time.time()
                await asyncio.sleep(random.uniform(0.5, 1.2))
                continue
            data["last_login_error"] = f"Exception: {e}"
            logger.warning(f"[{email[:15]}] Login exception: {e}")
            if login_attempt < 2:
                await asyncio.sleep(random.uniform(1, 2))
                continue
            _mark_account_failed(email)
            return None

    _mark_account_failed(email)
    return None


def get_last_login_error(account):
    email = account["email"]
    data = _get_client_data(email)
    return data.get("last_login_error", "Unknown")


def invalidate_session(account):
    email = account["email"]
    data = _get_client_data(email)
    data["valid"] = False
    data["logged_in_ts"] = 0


async def close_client(account):
    email = account["email"]
    data = _get_client_data(email)
    await _close_client(data["client"])
    data["client"] = None
    data["valid"] = False
    data["logged_in_ts"] = 0


def get_account_lock(account):
    return _get_lock(account["email"])


async def reset_all_clients():
    global _warmup_done
    accounts = _load_accounts()
    for account in accounts:
        email = account["email"]
        data = _get_client_data(email)
        await _close_client(data["client"])
        data["client"] = None
        data["valid"] = False
        data["logged_in_ts"] = 0
    _warmup_done = False
    logger.info("All clients reset")


async def refresh_all_clients():
    accounts = _load_accounts()
    if not accounts:
        return 0

    for account in accounts:
        email = account["email"]
        data = _get_client_data(email)
        await _close_client(data["client"])
        data["client"] = _make_fresh_client()
        data["created_ts"] = time.time()
        data["valid"] = False
        data["logged_in_ts"] = 0

    login_sem = asyncio.Semaphore(2)

    async def _relogin(account, idx):
        await asyncio.sleep(idx * 1.0)
        async with login_sem:
            email = account["email"]
            lock = _get_lock(email)
            async with lock:
                try:
                    client = await get_authed_client(account)
                    if client:
                        return True
                except Exception:
                    pass
                return False

    results = await asyncio.gather(*[_relogin(a, i) for i, a in enumerate(accounts)])
    success = sum(1 for r in results if r)
    logger.info(f"Refreshed clients: {success}/{len(accounts)} OK")
    return success


async def warmup_all_sessions():
    global _warmup_done
    accounts = _load_accounts()
    if not accounts:
        return 0

    login_sem = asyncio.Semaphore(2)

    async def _warmup_one(account, idx):
        await asyncio.sleep(idx * 1.0)
        async with login_sem:
            email = account["email"]
            lock = _get_lock(email)
            async with lock:
                try:
                    client = await get_authed_client(account)
                    if client:
                        logger.info(f"[{email[:15]}] Warmup OK")
                        return True
                    else:
                        logger.warning(f"[{email[:15]}] Warmup FAILED")
                        return False
                except Exception as e:
                    logger.warning(f"[{email[:15]}] Warmup error: {e}")
                    return False

    results = await asyncio.gather(*[_warmup_one(a, i) for i, a in enumerate(accounts)])
    success = sum(1 for r in results if r)

    _warmup_done = True
    return success


def is_warmed_up():
    return _warmup_done


async def login_session(client, account, force_refresh=False):
    email = account["email"]
    password = account["password"]

    for login_attempt in range(2):
        try:
            r = await _do_login(client, email, password)

            if r.status_code == 200:
                session_cookies = _cookie_dict(client)
                auth_token = session_cookies.get("auth_token")
                if auth_token:
                    _mark_account_success(email)
                    return True
                if session_cookies:
                    valid = await _validate_session(client)
                    if valid:
                        _mark_account_success(email)
                        return True

            if r.status_code == 429:
                await asyncio.sleep(random.uniform(5, 10) * (login_attempt + 1))
                continue

            _mark_account_failed(email, is_login_error=True)
            if login_attempt < 1:
                await asyncio.sleep(random.uniform(1, 2))
                continue
            return False

        except Exception:
            if login_attempt < 1:
                await asyncio.sleep(random.uniform(1, 2))
                continue
            _mark_account_failed(email)
            return False

    _mark_account_failed(email, is_login_error=True)
    return False


def clear_account_session(account):
    invalidate_session(account)


USER_SKOOL_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "user_skool_accounts.json")
_user_skool_cache = {}
_user_skool_ts = 0


def _load_user_skool_accounts():
    global _user_skool_cache, _user_skool_ts
    now = time.time()
    if _user_skool_cache and (now - _user_skool_ts) < 30:
        return _user_skool_cache
    try:
        if os.path.exists(USER_SKOOL_FILE):
            with open(USER_SKOOL_FILE, "r") as f:
                content = f.read().strip()
            if content:
                parsed = json.loads(content)
                _user_skool_cache = parsed
            elif not _user_skool_cache:
                _user_skool_cache = {}
        elif not _user_skool_cache:
            _user_skool_cache = {}
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to read user_skool_accounts.json: {e}")
        if not _user_skool_cache:
            for attempt in range(2):
                time.sleep(0.2)
                try:
                    with open(USER_SKOOL_FILE, "r") as f:
                        content = f.read().strip()
                    if content:
                        _user_skool_cache = json.loads(content)
                        break
                except Exception:
                    pass
            else:
                _user_skool_cache = {}
    except Exception as e:
        logger.warning(f"Unexpected error loading user_skool_accounts.json: {e}")
        if not _user_skool_cache:
            _user_skool_cache = {}
    _user_skool_ts = now
    return _user_skool_cache


def _save_user_skool_accounts(data):
    global _user_skool_cache, _user_skool_ts
    try:
        import tempfile
        dir_name = os.path.dirname(USER_SKOOL_FILE)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, USER_SKOOL_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _user_skool_cache = data
        _user_skool_ts = time.time()
    except Exception as e:
        logger.warning(f"Error saving user skool accounts: {e}")


def get_user_skool_accounts(user_id):
    data = _load_user_skool_accounts()
    return data.get(str(user_id), [])


def get_all_user_skool_accounts():
    data = _load_user_skool_accounts()
    seen = set()
    result = []
    for uid, accounts in data.items():
        for acc in accounts:
            email = acc.get("email", "")
            if email not in seen:
                seen.add(email)
                result.append({"email": email, "password": acc.get("password", ""), "owner": uid})
    return result


def _clear_account_status(email):
    _load_statuses()
    changed = False
    if email in _account_statuses:
        del _account_statuses[email]
        changed = True
    if email in _failed_accounts:
        del _failed_accounts[email]
        changed = True
    if email in _account_clients:
        _account_clients[email] = {
            "client": None,
            "logged_in_ts": 0,
            "valid": False,
            "created_ts": 0,
            "last_login_error": "",
        }
    if changed:
        _save_statuses()


def _reset_user_rr(user_id):
    uid = str(user_id)
    if uid in _user_rr_indices:
        del _user_rr_indices[uid]


def add_user_skool_account(user_id, email, password):
    data = _load_user_skool_accounts()
    uid = str(user_id)
    if uid not in data:
        data[uid] = []
    for acc in data[uid]:
        if acc.get("email") == email:
            if acc.get("password") != password:
                acc["password"] = password
                _save_user_skool_accounts(data)
                _clear_account_status(email)
                _reset_user_rr(user_id)
                return True, "updated"
            return False, "already exists"
    data[uid].append({"email": email, "password": password})
    _save_user_skool_accounts(data)
    _clear_account_status(email)
    _reset_user_rr(user_id)
    return True, "added"


def remove_user_skool_account(user_id, email):
    data = _load_user_skool_accounts()
    uid = str(user_id)
    if uid not in data:
        return False
    before = len(data[uid])
    data[uid] = [a for a in data[uid] if a.get("email", "").lower() != email.lower()]
    if len(data[uid]) == before:
        return False
    if not data[uid]:
        del data[uid]
    _save_user_skool_accounts(data)
    _clear_account_status(email)
    _reset_user_rr(user_id)
    return True


def clear_user_skool_accounts(user_id):
    data = _load_user_skool_accounts()
    uid = str(user_id)
    if uid in data:
        count = len(data[uid])
        del data[uid]
        _save_user_skool_accounts(data)
        _reset_user_rr(user_id)
        return count
    return 0


def _get_admin_combined_pool():
    seen = set()
    pool = []
    for acc in _load_accounts():
        email = acc.get("email", "")
        if email and email not in seen:
            seen.add(email)
            pool.append(acc)
    for acc in get_all_user_skool_accounts():
        email = acc.get("email", "")
        if email and email not in seen:
            seen.add(email)
            pool.append(acc)
    return pool

_admin_rr_index = 0
_admin_rr_lock = asyncio.Lock()

async def get_next_account_for_user(user_id, is_admin=False):
    global _user_rr_indices, _admin_rr_index

    if is_admin:
        pool = _get_admin_combined_pool()
        if not pool:
            return None, None
        async with _admin_rr_lock:
            now = time.time()
            tried = 0
            while tried < len(pool):
                idx = _admin_rr_index % len(pool)
                _admin_rr_index += 1
                account = pool[idx]
                email = account.get("email", "")
                fail_info = _failed_accounts.get(email)
                if fail_info and (now - fail_info["ts"]) < FAIL_COOLDOWN and fail_info["count"] >= 5:
                    tried += 1
                    continue
                return account, "admin_pool"
            _admin_rr_index += 1
            return pool[_admin_rr_index % len(pool)], "admin_pool"

    user_accounts = get_user_skool_accounts(user_id)
    if user_accounts:
        uid_key = str(user_id)
        if uid_key not in _user_rr_indices:
            _user_rr_indices[uid_key] = 0
        now = time.time()
        tried = 0
        while tried < len(user_accounts):
            idx = _user_rr_indices[uid_key] % len(user_accounts)
            _user_rr_indices[uid_key] += 1
            account = user_accounts[idx]
            email = account.get("email", "")
            fail_info = _failed_accounts.get(email)
            if fail_info and (now - fail_info["ts"]) < FAIL_COOLDOWN and fail_info["count"] >= 5:
                tried += 1
                continue
            return account, "user"
        idx = _user_rr_indices[uid_key] % len(user_accounts)
        _user_rr_indices[uid_key] += 1
        logger.warning(f"All accounts in cooldown for user {user_id}, using account anyway")
        return user_accounts[idx], "user"

    return None, None


async def get_fallback_account_for_user(user_id, exclude_email, is_admin=False):
    if is_admin:
        pool = _get_admin_combined_pool()
        now = time.time()
        fallback_any = None
        for account in pool:
            email = account.get("email", "")
            if email == exclude_email:
                continue
            if fallback_any is None:
                fallback_any = account
            fail_info = _failed_accounts.get(email)
            if fail_info and (now - fail_info["ts"]) < FAIL_COOLDOWN and fail_info["count"] >= 5:
                continue
            return account
        return fallback_any

    user_accounts = get_user_skool_accounts(user_id)
    if user_accounts:
        now = time.time()
        fallback_any = None
        for account in user_accounts:
            email = account.get("email", "")
            if email == exclude_email:
                continue
            if fallback_any is None:
                fallback_any = account
            fail_info = _failed_accounts.get(email)
            if fail_info and (now - fail_info["ts"]) < FAIL_COOLDOWN and fail_info["count"] >= 5:
                continue
            return account
        if fallback_any:
            logger.warning(f"All fallback accounts in cooldown for user {user_id}, using one anyway")
            return fallback_any

    return None


# ----------------------------------------------------------------------
# The skool api, called from inside the browser.
#
# The protection turns a plain request away even when the cookies are the
# ones the browser handed out.  So the browser does the calls itself,
# with the fetch the site makes.  The browser is kept for a while,
# because the login is slow and the token it gets is short lived.
# ----------------------------------------------------------------------

_browser_state = {"playwright": None, "browser": None, "ctx": None,
                  "page": None, "email": None, "at": 0.0, "api": None}
_browser_lock = asyncio.Lock()
BROWSER_TTL = 3600

_FETCH_SCRIPT = """
async ([method, url, payload, form]) => {
    const opts = {method: method, credentials: 'include', headers: {}};
    if (payload !== null && payload !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(payload);
    } else if (form !== null && form !== undefined) {
        opts.headers['Content-Type'] = 'application/x-www-form-urlencoded';
        opts.body = new URLSearchParams(form).toString();
    }
    const call = fetch(url, opts).then(async (r) => [r.status, await r.text()])
                          .catch((e) => [0, String(e)]);
    const guard = new Promise((r) => setTimeout(() => r([0, 'the call took too long']), 15000));
    return await Promise.race([call, guard]);
}
"""


class BrowserResponse:
    """What the gates expect from a response."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body or ""

    def json(self):
        try:
            return json.loads(self._body) if self._body else {}
        except Exception:
            return {}

    @property
    def text(self):
        return self._body

    @property
    def content(self):
        return self._body.encode("utf-8", "replace")


class BrowserApi:
    """The skool api, seen from the browser.

    The page fetch is the call the site makes, so it is the one the
    protection lets in.  The plain client is the second try, for when
    the page cannot reach the api at all.
    """

    def __init__(self, page, fallback=None):
        self._page = page
        self._fallback = fallback
        self.stale = False

    async def _call(self, method, url, **kw):
        payload = kw.get("json")
        form = kw.get("data")
        params = kw.get("params")
        if params:
            # the query is part of the call, so it goes on the url
            from urllib.parse import urlencode
            url = url + ("&" if "?" in url else "?") + urlencode(params)
        try:
            status, body = await asyncio.wait_for(
                self._page.evaluate(_FETCH_SCRIPT, [method, url, payload, form]),
                timeout=20)
            if status:
                if status in (401, 403):
                    # the session is gone, so the next call needs a new one
                    self.stale = True
                return BrowserResponse(status, body)
            logger.info("skool: the page could not reach the api (%s)", str(body)[:60])
        except Exception as e:
            logger.info("skool: the browser call failed: %s", str(e)[:70])
        if self._fallback is not None:
            try:
                if method == "GET":
                    r = await self._fallback.get(url, timeout=kw.get("timeout"))
                elif payload is not None:
                    r = await self._fallback.post(url, json=payload,
                                                 headers=kw.get("headers"),
                                                 timeout=kw.get("timeout"))
                else:
                    r = await self._fallback.post(url, data=kw.get("data"),
                                                 headers=kw.get("headers"),
                                                 timeout=kw.get("timeout"))
                return BrowserResponse(r.status_code, r.text)
            except Exception as e:
                logger.info("skool: the plain call failed too: %s", str(e)[:70])
        return BrowserResponse(0, "")

    async def get(self, url, **kw):
        return await self._call("GET", url, **kw)

    async def post(self, url, **kw):
        return await self._call("POST", url, **kw)


def _playwright_proxy(proxy):
    """The proxy in the shape the browser wants."""
    if not proxy:
        return None
    server = proxy if "://" in proxy else "http://" + proxy
    username = password = None
    if "@" in server:
        creds, server = server.rsplit("@", 1)
        server = server.split("://", 1)[-1]
        if ":" in creds:
            username, password = creds.split(":", 1)
        else:
            username = creds
        server = "http://" + server
    out = {"server": server}
    if username:
        out["username"] = username
        out["password"] = password or ""
    return out


async def _open_browser_page(email, password, proxy=None):
    """Open the browser, log in and keep the page."""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger.info("skool: the browser is not there: %s", str(e)[:70])
        return None
    try:
        if _browser_state["playwright"] is None:
            _browser_state["playwright"] = await async_playwright().start()
        if _browser_state["browser"] is None:
            _browser_state["browser"] = await _browser_state["playwright"].chromium.launch(
                headless=True, args=[
                    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                    "--disable-gpu", "--disable-blink-features=AutomationControlled"])
        browser = _browser_state["browser"]
        ctx = await browser.new_context(
            user_agent=UA, viewport={"width": 1366, "height": 900}, locale="en-US",
            proxy=_playwright_proxy(proxy))
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()
        # the page needs a while before its script is ready, and the
        # login is worth a second try
        for try_no in (1, 2):
            await page.goto("https://www.skool.com/login",
                            wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(9 if try_no == 1 else 13)
            email_el = await page.query_selector("input[type='email']")
            pass_el = await page.query_selector("input[type='password']")
            if not email_el or not pass_el:
                logger.info("skool: the login page gave no fields")
                continue
            await email_el.fill(email)
            await pass_el.fill(password)
            await page.keyboard.press("Enter")
            await asyncio.sleep(11)
            cookies = await ctx.cookies()
            jar = {c["name"]: c["value"] for c in cookies}
            if "auth_token" in jar:
                logger.info("[%s] the browser is in", email[:15])
                return ctx, page
            logger.info("skool: the browser login gave no session (try %d)", try_no)
        await ctx.close()
        return None
    except Exception as e:
        logger.info("skool: the browser login failed: %s", str(e)[:90])
        return None


async def get_browser_api(account, force_refresh=False, fallback=None, proxy=None):
    """The skool api as the browser sees it.

    The browser holds the session the api accepts, so the calls go
    through it.
    """
    if isinstance(account, dict):
        email = account.get("email")
        password = account.get("password")
    else:
        email = str(account)
        password = ""
    async with _browser_lock:
        now = time.time()
        api = _browser_state.get("api")
        if (not force_refresh and api is not None and not api.stale
                and _browser_state["page"] is not None
                and _browser_state["email"] == email
                and now - _browser_state["at"] < BROWSER_TTL):
            return api
        old_ctx = _browser_state["ctx"]
        got = await _open_browser_page(email, password, proxy)
        if old_ctx is not None:
            try:
                await old_ctx.close()
            except Exception:
                pass
        if not got:
            _browser_state["ctx"] = None
            _browser_state["page"] = None
            _browser_state["email"] = None
            return None
        ctx, page = got
        _browser_state["ctx"] = ctx
        _browser_state["page"] = page
        _browser_state["email"] = email
        _browser_state["at"] = time.time()
        api = BrowserApi(page, fallback)
        _browser_state["api"] = api
        return api
