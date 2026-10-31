"""The browser that a check pretends to be.

A check is a browser talking to a shop, and the shop can tell how the
request was made: the TLS handshake, the HTTP/2 settings, the headers
and the order they come in all give it away.  A plain http client says
"I am a python program" in every one of those places, even when it
writes "Chrome" in its user agent.

The captcha a shop hands out belongs to the browser that solved it.  So
the request that carries the token has to come from the same browser as
the one that solved it, and the two have to agree on the name they
give.  When they do not, the token is worth nothing and the card never
reaches the bank.

So the name of the browser is written down once, here.  The client
puts on the handshake of that browser and sends the headers that
browser sends, so the two cannot drift apart, and the browser that
solves the captcha is told the same name.
"""

import logging
import os

logger = logging.getLogger("browser_identity")

# The Chrome that can be impersonated.  The name, the client hints and
# the handshake have to be the same version: a mix of two is worse
# than any single one.
CHROME_VERSION = os.environ.get("BROWSER_CHROME_VERSION", "136").strip() or "136"
IMPERSONATE = os.environ.get("BROWSER_IMPERSONATE", f"chrome{CHROME_VERSION}").strip() \
    or f"chrome{CHROME_VERSION}"

# The name the browser gives and the client hints that go with it.  The
# library sends these itself, so the handshake and the headers belong
# to the same browser.  They are written out here for the browser that
# solves the captcha, which has to say the same thing.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

# A gate that says it is a phone gets the handshake of a phone.  A
# phone name on a computer's handshake is a tell in itself.
ANDROID_IMPERSONATE = os.environ.get("BROWSER_ANDROID_IMPERSONATE",
                                      "chrome131_android").strip() or "chrome131_android"
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
SEC_CH_UA = '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'
SEC_CH_UA_MOBILE = "?0"
SEC_CH_UA_PLATFORM = '"macOS"'
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
ACCEPT_ENCODING = "gzip, deflate, br, zstd"

# The same name, the way the callers ask for it.
BROWSER_UA = USER_AGENT

# The page a browser opens.
PAGE_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7")


def client_hints_metadata():
    """The client hints the browser reports, for the browser override.

    The browser on the machine is a different version and a different
    system, so it is told what to say.  It has to say what the api
    client says, or the token it wins belongs to a browser that the
    request carrying it does not look like.
    """
    version = f"{CHROME_VERSION}.0.0.0"
    return {
        "brands": [
            {"brand": "Chromium", "version": CHROME_VERSION},
            {"brand": "Google Chrome", "version": CHROME_VERSION},
            {"brand": "Not.A/Brand", "version": "99"},
        ],
        "fullVersionList": [
            {"brand": "Chromium", "version": version},
            {"brand": "Google Chrome", "version": version},
            {"brand": "Not.A/Brand", "version": "99.0.0.0"},
        ],
        "fullVersion": version,
        "platform": "macOS",
        "platformVersion": "15.6.0",
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "wow64": False,
    }


def _common(accept):
    return {
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        "user-agent": USER_AGENT,
        "accept": accept,
    }


def navigation_headers(accept=None, referer=None):
    """The headers a Chrome sends when it opens a page."""
    headers = _common(accept or PAGE_ACCEPT)
    headers.update({
        "upgrade-insecure-requests": "1",
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-user": "?1",
        "sec-fetch-dest": "document",
        "accept-encoding": ACCEPT_ENCODING,
        "accept-language": ACCEPT_LANGUAGE,
        "priority": "u=0, i",
    })
    if referer:
        headers["referer"] = referer
    return headers


def api_headers(origin=None, referer=None,
                content_type="application/x-www-form-urlencoded",
                accept="application/json"):
    """The headers a Chrome sends when it calls an api from a page.

    An api call is not a page: it is a fetch from the checkout page, so
    it says so, and it carries no "user" and no "upgrade" header,
    because those belong to a page.
    """
    headers = _common(accept)
    if content_type:
        headers["content-type"] = content_type
    if origin:
        headers["origin"] = origin
    if referer:
        headers["referer"] = referer
    headers.update({
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "accept-encoding": ACCEPT_ENCODING,
        "accept-language": ACCEPT_LANGUAGE,
        "priority": "u=1, i",
    })
    return headers


async def apply_browser_identity(page, metadata=None):
    """Tell the browser which browser it is.

    The browser on the machine is a headless build of another version.
    It is told the same name the api client uses, so the captcha token
    it wins is good for the request that carries it.
    """
    try:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Emulation.setUserAgentOverride", {
            "userAgent": USER_AGENT,
            "acceptLanguage": ACCEPT_LANGUAGE,
            "platform": "MacIntel",
            "userAgentMetadata": metadata or client_hints_metadata(),
        })
        return True
    except Exception as e:
        logger.debug("the browser was not told which browser it is: %s", e)
        return False


def _seconds(timeout):
    """The time to wait, in seconds.

    One set of callers hands over the timeout object of the library
    they were written against and the other a plain number.
    """
    if timeout is None:
        return None
    total = getattr(timeout, "total", None)
    if total is not None:
        return total
    try:
        return float(timeout)
    except (TypeError, ValueError):
        return None


class _Text(str):
    """The body of a page, which some callers await.

    One set of callers reads the body as a plain string and another
    awaits it, because the two http libraries say it in their own way.
    This answers to both.
    """

    def __await__(self):
        async def _give():
            return str(self)
        return _give().__await__()


class _Response:
    """A response, the way the http libraries hand it over."""

    def __init__(self, raw):
        self._raw = raw

    @property
    def status(self):
        return self._raw.status_code

    @property
    def status_code(self):
        return self._raw.status_code

    @property
    def url(self):
        return self._raw.url

    @property
    def text(self):
        return _Text(self._raw.text)

    @property
    def headers(self):
        return self._raw.headers

    @property
    def content(self):
        return self._raw.content

    def json(self):
        return self._raw.json()


class _Request:
    """A request that is either awaited or used as a context manager."""

    def __init__(self, coro):
        self._coro = coro
        self._response = None

    async def _run(self):
        if self._response is None:
            self._response = _Response(await self._coro)
        return self._response

    def __await__(self):
        return self._run().__await__()

    async def __aenter__(self):
        return await self._run()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class BrowserSession:
    """A session for the callers that hold a session of their own.

    It answers like a browser, and it answers like the http library the
    callers were written against, so both work.
    """

    def __init__(self, proxy=None, timeout=20, **_):
        self._client = BrowserClient(proxy=proxy, timeout=timeout)

    def get(self, url, headers=None, allow_redirects=True, timeout=None,
            params=None, **kwargs):
        headers = headers if headers is not None else navigation_headers()
        return _Request(self._client.get(url, headers=headers, params=params,
                                         timeout=_seconds(timeout),
                                         follow_redirects=allow_redirects))

    def post(self, url, data=None, json=None, headers=None, allow_redirects=True,
             timeout=None, params=None, **kwargs):
        headers = headers if headers is not None else api_headers()
        return _Request(self._client.post(url, headers=headers, data=data, json=json,
                                           params=params,
                                           timeout=_seconds(timeout),
                                           follow_redirects=allow_redirects))

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()


class BrowserClient:
    """A client that answers like a browser, not like a http library.

    It keeps the shape of the httpx client the gates were written
    against, so the callers do not have to change, but the handshake
    and the headers behind it are a browser's.
    """

    def __init__(self, proxy=None, timeout=15, headers=None,
                 follow_redirects=True, verify=True, trust_env=False, **_):
        from curl_cffi.requests import AsyncSession

        self._session = AsyncSession(
            impersonate=IMPERSONATE,
            proxy=proxy or None,
            timeout=timeout,
            max_redirects=20,
        )
        self.follow_redirects = follow_redirects
        self.timeout = timeout
        self.headers = headers or {}

    def _merged(self, headers):
        # The parts every request of this browser carries.  The rest is
        # left to the caller: a page says "document" and an api call
        # says "empty", and a header that belongs to the other one is a
        # tell.
        merged = {
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
            "user-agent": USER_AGENT,
            "accept-encoding": ACCEPT_ENCODING,
            "accept-language": ACCEPT_LANGUAGE,
        }
        merged.update(self.headers)
        if headers:
            merged.update(headers)
        return merged

    async def _request(self, method, url, headers=None, params=None, data=None,
                       json=None, timeout=None, follow_redirects=None):
        kwargs = {
            "headers": self._merged(headers),
            # The library's own headers are left out: the set above is
            # the whole header set, and a header the browser does not
            # send on this kind of call is a tell.
            "default_headers": False,
            "allow_redirects": (self.follow_redirects
                                if follow_redirects is None else follow_redirects),
        }
        if params is not None:
            kwargs["params"] = params
        if data is not None:
            kwargs["data"] = data
        if json is not None:
            kwargs["json"] = json
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._session.request(method, url, **kwargs)

    async def get(self, url, *, headers=None, params=None, timeout=None,
                  follow_redirects=None, **kwargs):
        return await self._request("GET", url, headers=headers, params=params,
                                    timeout=timeout, follow_redirects=follow_redirects)

    async def post(self, url, *, headers=None, params=None, data=None, json=None,
                   timeout=None, follow_redirects=None, **kwargs):
        return await self._request("POST", url, headers=headers, params=params,
                                   data=data, json=json, timeout=timeout,
                                   follow_redirects=follow_redirects)

    @property
    def cookies(self):
        return self._session.cookies

    async def aclose(self):
        try:
            await self._session.close()
        except Exception:
            pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
