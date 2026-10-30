"""Streaming HTTP requests through the platform's own networking: the expo/fetch API.

    from pyxpo_fetch import fetch, AbortController

    resp = await fetch("https://api.example.com/items", method="POST",
                       headers={"Content-Type": "application/json"}, body='{"name": "pen"}')
    resp.status, resp.ok, resp.headers.get("content-type")
    data = await resp.json()                      # or .text(), .bytes()

    async for chunk in (await fetch(url)).body:   # bytes, as they arrive
        ...

    ctl = AbortController()
    task = fetch(url, signal=ctl.signal)
    ctl.abort()                                   # the awaiting call raises AbortError

iOS uses `URLSession`, Android uses OkHttp, so system proxies, certificates and cookies apply. The desktop
uses `urllib` in a worker thread. The web build uses the browser's `fetch` (CORS rules apply).
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json as _json
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple, Union

from pyxpo.module_api import call, on_event, platform, require_linked

__version__ = "0.1.0a2"

_METHODS_WITHOUT_BODY = ("GET", "HEAD")
_REDIRECTS = ("follow", "manual", "error")
_CREDENTIALS = ("include", "omit", "same-origin")


class FetchError(Exception):
    """The request failed without a response: no network, DNS, TLS, timeout or a refused redirect."""


class AbortError(FetchError):
    """The request was stopped through its `AbortSignal`."""


class AbortSignal:
    def __init__(self) -> None:
        self.aborted = False
        self.reason: Any = None
        self._handlers: List[Callable[[], Any]] = []

    def _on_abort(self, fn: Callable[[], Any]) -> Callable[[], None]:
        self._handlers.append(fn)
        return lambda: self._handlers.remove(fn) if fn in self._handlers else None

    def throw_if_aborted(self) -> None:
        if self.aborted:
            raise AbortError(str(self.reason) if self.reason is not None else "The operation was aborted.")


class AbortController:
    """`abort()` stops every request that got `controller.signal`, also while its body streams."""

    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: Any = None) -> None:
        s = self.signal
        if s.aborted:
            return
        s.aborted, s.reason = True, reason
        for fn in list(s._handlers):
            fn()


HeadersInit = Union["Headers", Mapping[str, str], Iterable[Tuple[str, str]], None]


class Headers:
    """Case-insensitive HTTP headers. `get` joins repeated values with ", " (as the web does)."""

    def __init__(self, init: HeadersInit = None) -> None:
        self._items: List[Tuple[str, str]] = []
        if isinstance(init, Headers):
            self._items = list(init._items)
        elif isinstance(init, Mapping):
            for k, v in init.items():
                self.append(k, v)
        elif init is not None:
            for k, v in init:
                self.append(k, v)

    def append(self, name: str, value: str) -> None:
        self._items.append((str(name), str(value)))

    def set(self, name: str, value: str) -> None:
        self.delete(name)
        self.append(name, value)

    def delete(self, name: str) -> None:
        low = name.lower()
        self._items = [(k, v) for k, v in self._items if k.lower() != low]

    def get(self, name: str) -> Optional[str]:
        vals = self.get_all(name)
        return ", ".join(vals) if vals else None

    def get_all(self, name: str) -> List[str]:
        low = name.lower()
        return [v for k, v in self._items if k.lower() == low]

    def get_set_cookie(self) -> List[str]:
        return self.get_all("set-cookie")

    def has(self, name: str) -> bool:
        return bool(self.get_all(name))

    def keys(self) -> List[str]:
        return sorted({k.lower() for k, _ in self._items})

    def items(self) -> List[Tuple[str, str]]:
        return [(k, self.get(k) or "") for k in self.keys()]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)

    def __getitem__(self, name: str) -> str:
        v = self.get(name)
        if v is None:
            raise KeyError(name)
        return v

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def __repr__(self) -> str:
        return "Headers(%r)" % dict(self.items())

    getSetCookie = get_set_cookie


class _Body:
    """Chunks pushed by the transport; read once, by iteration or by `Response.bytes()`."""

    def __init__(self, cancel: Callable[[], None]) -> None:
        self._q: "asyncio.Queue[Tuple[str, Any]]" = asyncio.Queue()
        self._cancel = cancel
        self.done = False

    def push(self, chunk: bytes) -> None:
        self._q.put_nowait(("data", chunk))

    def end(self, error: Optional[BaseException] = None) -> None:
        self._q.put_nowait(("end", error))

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while not self.done:
            kind, v = await self._q.get()
            if kind == "data":
                yield v
                continue
            self.done = True
            if v is not None:
                raise v

    async def cancel(self) -> None:
        """Stops the download. Later reads end."""
        if not self.done:
            self.done = True
            self._cancel()


class Response:
    def __init__(self, status: int, status_text: str, headers: Headers, url: str, redirected: bool,
                 body: _Body) -> None:
        self.status = status
        self.status_text = status_text
        self.headers = headers
        self.url = url
        self.redirected = redirected
        self._body = body
        self.body_used = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status <= 299

    @property
    def body(self) -> "_Body":
        """Async iterator over the body chunks (bytes). Reading it uses up the body."""
        self._use()
        return self._body

    def _use(self) -> None:
        if self.body_used:
            raise RuntimeError("the response body was already read")
        self.body_used = True

    async def bytes(self) -> bytes:
        self._use()
        return b"".join([c async for c in self._body])

    async def array_buffer(self) -> bytes:
        return await self.bytes()

    async def text(self) -> str:
        return (await self.bytes()).decode("utf-8", errors="replace")

    async def json(self) -> Any:
        return _json.loads(await self.text())

    def __repr__(self) -> str:
        return "<Response %d %s>" % (self.status, self.url)

    statusText = property(lambda self: self.status_text)
    bodyUsed = property(lambda self: self.body_used)
    arrayBuffer = array_buffer


def _encode_body(body: Any, headers: Headers) -> Optional[bytes]:
    if body is None:
        return None
    if isinstance(body, str):
        if not headers.has("content-type"):
            headers.set("Content-Type", "text/plain;charset=UTF-8")
        return body.encode("utf-8")
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body)
    raise TypeError("body must be str or bytes, not %s (use json.dumps for objects)" % type(body).__name__)


# Native transport: `Fetch.start` replies with the response head; body chunks and the end come as events.
_ids = itertools.count(1)
_bodies: Dict[int, _Body] = {}
_subscribed = False


def _on_chunk(data: Any) -> None:
    b = _bodies.get(int(data.get("id", 0)))
    if b is not None:
        b.push(base64.b64decode(data.get("data") or ""))


def _on_end(data: Any) -> None:
    b = _bodies.pop(int(data.get("id", 0)), None)
    if b is not None:
        err = data.get("error")
        b.end(None if not err else AbortError(err) if data.get("aborted") else FetchError(err))


def _subscribe() -> None:
    global _subscribed
    if not _subscribed:
        on_event("Fetch.chunk", _on_chunk)
        on_event("Fetch.end", _on_end)
        _subscribed = True


async def _native_fetch(url: str, method: str, headers: Headers, data: Optional[bytes], redirect: str,
                        credentials: str, timeout: Optional[float], signal: Optional[AbortSignal]) -> Response:
    require_linked("Fetch", "fetch", package="pyxpo-fetch")
    _subscribe()
    rid = next(_ids)
    body = _Body(lambda: call("Fetch", "cancel", rid))
    _bodies[rid] = body
    off = signal._on_abort(lambda: call("Fetch", "cancel", rid)) if signal is not None else None
    try:
        head = await call("Fetch", "start", rid, url, method, [list(p) for p in headers._items],
                          base64.b64encode(data).decode("ascii") if data is not None else None,
                          redirect, credentials, int(timeout * 1000) if timeout else 0, reply=True)
    except Exception as e:
        _bodies.pop(rid, None)
        if off:
            off()
        if signal is not None and signal.aborted:
            raise AbortError(str(signal.reason) if signal.reason is not None else "The operation was aborted.") from None
        raise FetchError(str(e)) from None
    if off:
        # Keep abort working while the body streams; drop the hook once the body ends.
        orig_end = body.end

        def end(error: Optional[BaseException] = None) -> None:
            off()
            if error is None and signal is not None and signal.aborted:
                error = AbortError("The operation was aborted.")
            orig_end(error)
        body.end = end  # type: ignore[assignment]
    return Response(int(head["status"]), head.get("statusText") or "", Headers(head.get("headers") or []),
                    head.get("url") or url, bool(head.get("redirected")), body)


_desktop_cookies: Any = None


def _in_thread(loop: asyncio.AbstractEventLoop, fn: Callable[[], Any]) -> "asyncio.Future[Any]":
    """Runs `fn` on a daemon thread, so a hung socket never blocks the event loop's shutdown."""
    import threading

    fut = loop.create_future()

    def settle(ok: bool, v: Any) -> None:
        if not fut.done():
            fut.set_result(v) if ok else fut.set_exception(v)

    def run() -> None:
        try:
            r = (True, fn())
        except BaseException as e:  # noqa: BLE001
            r = (False, e)
        try:
            loop.call_soon_threadsafe(settle, *r)
        except RuntimeError:  # the loop closed meanwhile
            pass

    threading.Thread(target=run, daemon=True).start()
    return fut


async def _desktop_fetch(url: str, method: str, headers: Headers, data: Optional[bytes], redirect: str,
                         credentials: str, timeout: Optional[float], signal: Optional[AbortSignal]) -> Response:
    import http.cookiejar
    import urllib.error
    import urllib.request

    global _desktop_cookies

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a: Any, **k: Any) -> None:
            if redirect == "error":
                raise FetchError("redirect refused (redirect='error')")
            return None

    loop = asyncio.get_running_loop()
    handlers: List[Any] = [_NoRedirect()] if redirect != "follow" else []
    if credentials != "omit":
        if _desktop_cookies is None:
            _desktop_cookies = http.cookiejar.CookieJar()
        handlers.append(urllib.request.HTTPCookieProcessor(_desktop_cookies))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers._items))
    stop = {"flag": False}
    body = _Body(lambda: stop.update(flag=True))

    def open_() -> Any:
        try:
            return opener.open(req, timeout=timeout or 60)
        except urllib.error.HTTPError as e:
            return e

    fut = _in_thread(loop, open_)
    off = signal._on_abort(lambda: fut.cancel()) if signal is not None else None
    try:
        resp = await fut
    except asyncio.CancelledError:
        if signal is not None and signal.aborted:
            raise AbortError("The operation was aborted.") from None
        raise
    except FetchError:
        raise
    except Exception as e:  # noqa: BLE001
        raise FetchError(str(e)) from None
    finally:
        if off:
            off()

    if signal is not None:
        def abort_body() -> None:
            stop["flag"] = True
            body.end(AbortError("The operation was aborted."))
        off = signal._on_abort(abort_body)

    def finish(error: Optional[BaseException]) -> None:
        if off:
            off()
        body.end(error)

    def pump() -> None:
        read = getattr(resp, "read1", resp.read)  # read1 returns what has arrived instead of waiting for 64 KiB
        error: Optional[BaseException] = None
        try:
            while not stop["flag"]:
                chunk = read(65536)
                if not chunk:
                    break
                loop.call_soon_threadsafe(body.push, chunk)
        except Exception as e:  # noqa: BLE001
            error = FetchError(str(e))
        finally:
            resp.close()
        try:
            loop.call_soon_threadsafe(finish, error)
        except RuntimeError:
            pass

    _in_thread(loop, pump)
    final = resp.geturl() if hasattr(resp, "geturl") else url
    return Response(int(resp.status if hasattr(resp, "status") else resp.code), getattr(resp, "reason", "") or "",
                    Headers(list(resp.headers.items())), final, final != url, body)


def _js_init(headers: Headers, method: str, data: Optional[bytes], redirect: str, credentials: str) -> Dict[str, Any]:
    """The `RequestInit` fields for the browser's `fetch` (kept apart so it is testable without Pyodide)."""
    init: Dict[str, Any] = {"method": method, "headers": [list(p) for p in headers._items],
                            "redirect": redirect, "credentials": "include" if credentials == "include" else "omit"}
    if data is not None:
        init["body"] = data
    return init


async def _web_fetch(url: str, method: str, headers: Headers, data: Optional[bytes], redirect: str,
                     credentials: str, timeout: Optional[float], signal: Optional[AbortSignal]) -> Response:
    import js  # type: ignore[import-not-found]  # Pyodide only
    from pyodide.ffi import JsException, to_js  # type: ignore[import-not-found]

    ctl = js.AbortController.new()
    why = {"reason": None}

    def abort(reason: str) -> None:
        why["reason"] = reason
        ctl.abort()

    off = signal._on_abort(lambda: abort("aborted")) if signal is not None else None
    timer = asyncio.get_running_loop().call_later(timeout, abort, "timeout") if timeout else None
    init = to_js(_js_init(headers, method, data, redirect, credentials), dict_converter=js.Object.fromEntries)
    init.signal = ctl.signal

    def fail(e: BaseException) -> BaseException:
        if why["reason"] == "aborted":
            return AbortError("The operation was aborted.")
        if why["reason"] == "timeout":
            return FetchError("the request timed out after %ss" % timeout)
        return FetchError(str(e))

    try:
        resp = await js.fetch(url, init)
    except JsException as e:
        if timer:
            timer.cancel()
        if off:
            off()
        raise fail(e) from None
    body = _Body(lambda: abort("aborted"))

    async def pump() -> None:
        error: Optional[BaseException] = None
        try:
            reader = resp.body.getReader() if resp.body else None
            while reader is not None:
                part = await reader.read()
                if part.done:
                    break
                body.push(part.value.to_bytes())
        except JsException as e:
            error = fail(e)
        finally:
            if timer:
                timer.cancel()
            if off:
                off()
        body.end(error)

    asyncio.ensure_future(pump())
    h = Headers([[k, v] for k, v in resp.headers.entries()])
    return Response(int(resp.status), str(resp.statusText or ""), h, str(resp.url or url), bool(resp.redirected), body)


async def fetch(url: str, *, method: str = "GET", headers: HeadersInit = None, body: Union[str, bytes, None] = None,
                redirect: str = "follow", credentials: str = "include", signal: Optional[AbortSignal] = None,
                timeout: Optional[float] = None) -> Response:
    """Sends an HTTP request and returns once the response headers arrive; read the body from the response.

    `redirect`: "follow" (default), "manual" (return the 3xx response) or "error" (fail on a redirect).
    `credentials`: "include" (default) sends and stores cookies, "omit" does neither. `timeout` is in seconds.
    HTTP error statuses (404, 500) do not raise: check `response.ok`. Network failures raise `FetchError`,
    and an aborted request raises `AbortError`.
    """
    method = method.upper()
    if redirect not in _REDIRECTS:
        raise ValueError("redirect must be one of %s" % ", ".join(_REDIRECTS))
    if credentials not in _CREDENTIALS:
        raise ValueError("credentials must be one of %s" % ", ".join(_CREDENTIALS))
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("fetch needs an http(s) URL, got %r" % url)
    h = Headers(headers)
    data = _encode_body(body, h)
    if data is not None and method in _METHODS_WITHOUT_BODY:
        raise ValueError("a %s request cannot have a body" % method)
    if signal is not None:
        signal.throw_if_aborted()
    p = platform()
    if p == "web":
        return await _web_fetch(url, method, h, data, redirect, credentials, timeout, signal)
    if p == "desktop":
        return await _desktop_fetch(url, method, h, data, redirect, credentials, timeout, signal)
    return await _native_fetch(url, method, h, data, redirect, credentials, timeout, signal)


__all__ = ["fetch", "Headers", "Response", "AbortController", "AbortSignal", "FetchError", "AbortError"]

from .client import (Client, ClientResponse, HTTPError, RequestConfig, Retry, TimeoutError,  # noqa: E402
                     create_client, encode_params, gather_settled)

__all__ += ["Client", "ClientResponse", "HTTPError", "RequestConfig", "Retry", "TimeoutError", "create_client",
            "encode_params", "gather_settled"]
