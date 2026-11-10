"""An httpx that speaks like a browser.

Most of the gates were written against httpx, and httpx has the
handshake of a python library.  A shop reads the handshake before it
reads the card, so a request from httpx is a request from a robot,
whatever the headers say.

This is httpx with the same shape, so a gate does not have to change
to get a browser's fingerprint: the same calls, the same answers, the
same exceptions, but the wire carries a real browser's fingerprint.

The fingerprint follows the name the caller asks for.  A gate that
says it is a phone gets the handshake of a phone, and one that says it
is a computer gets the handshake of a computer: a phone name on a
computer's handshake is a tell in itself.
"""

import asyncio
import json as _json
import os
import sys

_bot_dir = os.path.dirname(os.path.abspath(__file__))
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)

from browser_identity import (ANDROID_IMPERSONATE, ANDROID_UA, BROWSER_UA,
                              IMPERSONATE, api_headers, navigation_headers)


class TimeoutException(Exception):
    """The request took too long."""


class ConnectTimeout(TimeoutException):
    """The connection was not made in time."""


class ReadTimeout(TimeoutException):
    """The answer did not come in time."""


class WriteTimeout(TimeoutException):
    """The request did not go out in time."""


class PoolTimeout(TimeoutException):
    """No connection was free."""


class NetworkError(Exception):
    """Something went wrong on the way to the shop."""


class ConnectError(NetworkError):
    """The shop could not be reached."""


class TransportError(NetworkError):
    """The way to the shop failed."""


class RemoteProtocolError(TransportError):
    """The shop said something that is not http."""


class UnsupportedProtocol(TransportError):
    """The address is not one that can be asked."""


class InvalidURL(Exception):
    """The address cannot be asked."""


class HTTPError(Exception):
    """The answer was an error."""


class HTTPStatusError(HTTPError):
    """The answer was an error."""


class Timeout:
    """How long to wait, the way httpx says it."""

    def __init__(self, timeout=None, *, connect=None, read=None, write=None, pool=None):
        self.timeout = timeout
        self.connect = connect if connect is not None else timeout
        self.read = read if read is not None else timeout
        self.write = write if write is not None else timeout
        self.pool = pool if pool is not None else timeout

    def as_seconds(self):
        """The time to wait, in seconds."""
        for value in (self.read, self.connect, self.timeout):
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None


class Limits:
    """Kept so the gates that set them still run."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class AsyncHTTPTransport:
    """Kept so the gates that name it still run.  The browser has its
    own way of talking, so this one is not used."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class URL(str):
    """An address.  A string works everywhere one is wanted."""


class _Request:
    """The request that was sent, for the callers that ask about it."""

    def __init__(self, method, url, headers):
        self.method = method
        self.url = url
        self.headers = headers


class Response:
    """The answer, with the names the gates use."""

    def __init__(self, raw, request=None):
        self._raw = raw
        self.request = request
        self.status_code = raw.status_code
        self.status = raw.status_code
        self.headers = raw.headers
        self.cookies = getattr(raw, "cookies", {}) or {}
        self.url = str(getattr(raw, "url", "")) if raw is not None else ""
        self.elapsed = 0.0
        self.content_type = ""

    @property
    def text(self):
        return self._raw.text

    @property
    def content(self):
        return self._raw.content

    @property
    def encoding(self):
        return "utf-8"

    def json(self, **kwargs):
        return self._raw.json()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPStatusError("the answer was %s" % self.status_code)
        return self


def _seconds(timeout, fallback=30.0):
    """The time to wait, in seconds, whatever form it comes in."""
    if timeout is None:
        return fallback
    if isinstance(timeout, Timeout):
        value = timeout.as_seconds()
        return fallback if value is None else value
    try:
        return float(timeout)
    except (TypeError, ValueError):
        return fallback


def _merge_headers(first, second):
    """Two sets of headers, one header each.

    The names are compared without regard to how they are written, so
    a header that both sets name is one header, and the later set has
    the last word on it.
    """
    out = {}
    names = {}
    for source in (first, second):
        for key, value in (source or {}).items():
            name = str(key).lower()
            if name in names:
                out[names[name]] = value
            else:
                names[name] = key
                out[key] = value
    return out


def _is_phone(headers):
    """True when the caller says it is a phone."""
    for key in ("user-agent", "User-Agent", "USER-AGENT"):
        value = (headers or {}).get(key)
        if value and "Android" in value:
            return True
    return False


class AsyncClient:
    """The client the gates write against, with a browser's handshake.

    The calls are the ones httpx has.  The request goes out with the
    fingerprint of a real browser, and the browser's own headers for the
    kind of call it is.
    """

    def __init__(self, timeout=None, headers=None, proxy=None, proxies=None,
                 follow_redirects=False, verify=True, trust_env=False,
                 limits=None, transport=None, http2=True, base_url="", **_):
        from curl_cffi.requests import AsyncSession

        self.timeout = timeout
        self.headers = dict(headers or {})
        self.follow_redirects = follow_redirects
        self.base_url = str(base_url or "")

        address = proxy
        if not address and proxies:
            if isinstance(proxies, dict):
                address = proxies.get("https") or proxies.get("http")
            else:
                address = proxies
        if address and "://" not in str(address):
            address = "http://%s" % address
        self._proxy = address or None

        self._session = None
        self._target = None

    def _session_for(self, headers):
        """The browser that matches the name the caller uses."""
        target = ANDROID_IMPERSONATE if _is_phone(headers) else IMPERSONATE
        if self._session is not None and self._target == target:
            return self._session
        from curl_cffi.requests import AsyncSession

        if self._session is not None:
            try:
                asyncio.get_event_loop().create_task(self._session.close())
            except Exception:
                pass
        self._session = AsyncSession(
            impersonate=target,
            proxy=self._proxy,
            timeout=_seconds(self.timeout, 30.0),
            max_redirects=20,
        )
        self._target = target
        return self._session

    # --------------------------------------------------------------- the
    # headers of the request, the way the browser would send them.
    def _headers(self, headers, method="GET"):
        # A header says one thing, not two.  The caller's name and the
        # browser's name are the same header, whichever way it is
        # written, so the two are put together by name and not by
        # spelling: a request that carried both "Content-Type" and
        # "content-type" was refused by the server as an invalid
        # header.
        merged = _merge_headers(self.headers, headers)
        phone = _is_phone(merged)
        filled = api_headers() if method == "POST" else navigation_headers()
        if not merged:
            # a request that names nothing still has to look like a
            # browser, so the kind of call decides the headers
            merged = filled
        else:
            # the headers the caller gave are the ones that matter; the
            # browser's own only fill in the gaps
            have = {str(k).lower() for k in merged}
            for key, value in filled.items():
                if str(key).lower() not in have:
                    merged[key] = value
        if phone:
            merged.setdefault("sec-ch-ua-mobile", "?1")
        return merged

    async def _send(self, method, url, headers=None, params=None, data=None,
                    json=None, content=None, files=None, timeout=None,
                    follow_redirects=None, **_):
        if not str(url).startswith("http"):
            url = self.base_url + str(url)

        sent_headers = self._headers(headers, method=method)
        kwargs = {
            "headers": sent_headers,
            "timeout": _seconds(timeout, _seconds(self.timeout, 30.0)),
            "allow_redirects": (self.follow_redirects
                                if follow_redirects is None else follow_redirects),
        }
        if params:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        elif data is not None:
            kwargs["data"] = data
        elif content is not None:
            kwargs["content"] = content
        if files:
            kwargs["files"] = files

        session = self._session_for(sent_headers)
        try:
            raw = await session.request(method, url, **kwargs)
        except Exception as e:
            raise _translate(e)
        return Response(raw, request=_Request(method, url, sent_headers))

    async def get(self, url, **kwargs):
        return await self._send("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._send("POST", url, **kwargs)

    async def put(self, url, **kwargs):
        return await self._send("PUT", url, **kwargs)

    async def patch(self, url, **kwargs):
        return await self._send("PATCH", url, **kwargs)

    async def delete(self, url, **kwargs):
        return await self._send("DELETE", url, **kwargs)

    async def request(self, method, url, **kwargs):
        return await self._send(str(method).upper(), url, **kwargs)

    async def aclose(self):
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()


def _translate(error):
    """The failure, in the words the gates know."""
    name = type(error).__name__
    text = str(error)
    if "timeout" in text.lower() or "timed out" in name.lower():
        return TimeoutException(text)
    if "resolve" in text.lower() or "name or service" in text.lower() \
            or "refused" in text.lower() or "connect" in text.lower() \
            or "ssl" in text.lower() or "certificate" in text.lower():
        return ConnectError(text)
    if "too many redirects" in text.lower():
        return RemoteProtocolError(text)
    if "unsupported protocol" in text.lower():
        return UnsupportedProtocol(text)
    return NetworkError(text)


class Client(AsyncClient):
    """The synchronous name, for the callers that ask for it."""
