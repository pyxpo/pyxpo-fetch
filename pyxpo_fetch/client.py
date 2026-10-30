"""An HTTP client on top of `fetch`, in the style of axios: shared defaults, interceptors, retries and parsed bodies.

    from pyxpo_fetch import create_client

    api = create_client(base_url="https://api.example.com/v1", timeout=15, headers={"Accept": "application/json"})

    @api.interceptors.request.use
    def sign(config):
        config.headers.set("Authorization", "Bearer " + token())

    res = await api.get("/items", params={"page": 2})
    res.data                              # parsed JSON (or text), res.status, res.headers
    await api.post("/items", json={"name": "pen"})

Error statuses raise `HTTPError` (turn that off with `raise_for_status=False`). A request that runs past `timeout`
raises `TimeoutError`, one stopped through its `signal` raises `AbortError`, and no network raises `FetchError`.
"""
from __future__ import annotations

import asyncio
import inspect
import json as _json
import random
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote, urlsplit

from . import AbortController, AbortError, AbortSignal, FetchError, Headers, HeadersInit

_RESPONSE_TYPES = ("auto", "json", "text", "bytes", "stream")
_IDEMPOTENT = ("GET", "HEAD", "OPTIONS", "PUT", "DELETE")


class TimeoutError(FetchError):  # noqa: A001 - mirrors axios' ECONNABORTED, and stays a FetchError
    """The request ran past the client's `timeout`."""


class HTTPError(FetchError):
    """The server answered with an error status. The response, its parsed body and the request are attached."""

    def __init__(self, response: "ClientResponse") -> None:
        super().__init__("HTTP %d %s for %s %s" % (response.status, response.status_text, response.config.method,
                                                   response.url))
        self.response = response
        self.config = response.config
        self.status = response.status
        self.data = response.data


class Retry:
    """When and how often to send a request again.

    `attempts` counts retries after the first try. Network failures, timeouts and the `statuses` listed are retried,
    only for the `methods` listed, which are the idempotent ones by default. The wait doubles from `backoff` seconds
    up to `max_backoff`, with jitter, and a `Retry-After` header wins when the server sends one.
    """

    def __init__(self, attempts: int = 2, *, statuses: Iterable[int] = (408, 429, 502, 503, 504),
                 methods: Iterable[str] = _IDEMPOTENT, backoff: float = 0.3, max_backoff: float = 10.0,
                 on_timeout: bool = True, when: Optional[Callable[[BaseException, "RequestConfig"], bool]] = None
                 ) -> None:
        self.attempts = attempts
        self.statuses = tuple(statuses)
        self.methods = tuple(m.upper() for m in methods)
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.on_timeout = on_timeout
        self.when = when

    def should_retry(self, error: BaseException, config: "RequestConfig", attempt: int) -> bool:
        if attempt >= self.attempts or config.method not in self.methods or isinstance(error, AbortError):
            return False
        if self.when is not None:
            return bool(self.when(error, config))
        if isinstance(error, HTTPError):
            return error.status in self.statuses
        if isinstance(error, TimeoutError):
            return self.on_timeout
        return isinstance(error, FetchError)

    def delay(self, error: BaseException, attempt: int) -> float:
        if isinstance(error, HTTPError):
            after = _retry_after(error.response.headers.get("retry-after"))
            if after is not None:
                return min(after, self.max_backoff)
        wait = min(self.backoff * (2 ** attempt), self.max_backoff)
        return wait * (0.5 + random.random() / 2)


def _retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        import time
        return max(parsedate_to_datetime(value).timestamp() - time.time(), 0.0)
    except (TypeError, ValueError):
        return None


class RequestConfig:
    """Everything about one request. Interceptors may change any field; `meta` is free for your own flags."""

    def __init__(self, method: str = "GET", url: str = "", *, base_url: str = "", params: Optional[Mapping[str, Any]] = None,
                 headers: HeadersInit = None, json: Any = None, data: Union[str, bytes, None] = None,
                 form: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None,
                 signal: Optional[AbortSignal] = None, response_type: str = "auto",
                 retry: Union[Retry, int, None] = None, raise_for_status: Union[bool, Callable[[int], bool]] = True,
                 redirect: Optional[str] = None, credentials: Optional[str] = None,
                 on_download_progress: Optional[Callable[[int, Optional[int]], Any]] = None,
                 meta: Optional[Dict[str, Any]] = None) -> None:
        if response_type not in _RESPONSE_TYPES:
            raise ValueError("response_type must be one of %s" % ", ".join(_RESPONSE_TYPES))
        self.method = method.upper()
        self.url = url
        self.base_url = base_url
        self.params: Dict[str, Any] = dict(params or {})
        self.headers = Headers(headers)
        self.json = json
        self.data = data
        self.form = dict(form) if form is not None else None
        self.timeout = timeout
        self.signal = signal
        self.response_type = response_type
        self.retry = Retry(retry) if isinstance(retry, int) else retry
        self.raise_for_status = raise_for_status
        self.redirect = redirect
        self.credentials = credentials
        self.on_download_progress = on_download_progress
        self.meta: Dict[str, Any] = dict(meta or {})
        self.attempt = 0

    def full_url(self) -> str:
        """`base_url` joined with `url` (an absolute `url` wins), then `params` as the query string."""
        url = self.url
        if self.base_url and not urlsplit(url).scheme:
            url = self.base_url.rstrip("/") + "/" + url.lstrip("/") if url else self.base_url
        query = encode_params(self.params)
        if query:
            url += ("&" if "?" in url else "?") + query
        return url

    def body(self) -> Union[str, bytes, None]:
        """The bytes to send, setting `Content-Type` for JSON and form bodies unless it is already set."""
        if self.json is not None:
            if not self.headers.has("content-type"):
                self.headers.set("Content-Type", "application/json")
            return _json.dumps(self.json, separators=(",", ":"), default=str)
        if self.form is not None:
            if not self.headers.has("content-type"):
                self.headers.set("Content-Type", "application/x-www-form-urlencoded")
            return encode_params(self.form)
        return self.data

    def copy(self, **changes: Any) -> "RequestConfig":
        clone = RequestConfig.__new__(RequestConfig)
        clone.__dict__.update(self.__dict__)
        clone.params = dict(self.params)
        clone.headers = Headers(self.headers.items())
        clone.meta = dict(self.meta)
        for key, value in changes.items():
            setattr(clone, key, Headers(value) if key == "headers" else value)
        return clone

    def __repr__(self) -> str:
        return "<RequestConfig %s %s>" % (self.method, self.full_url())


def encode_params(params: Optional[Mapping[str, Any]]) -> str:
    """`a=1&b=x%20y`: None values are skipped, booleans become true/false, lists repeat the key."""
    parts: List[str] = []
    for key, value in (params or {}).items():
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            if v is None:
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"
            parts.append("%s=%s" % (quote(str(key), safe=""), quote(str(v), safe="")))
    return "&".join(parts)


class ClientResponse:
    """A finished response: `data` holds the parsed body (or the stream, for `response_type="stream"`)."""

    def __init__(self, raw: Any, config: RequestConfig, data: Any) -> None:
        self.raw = raw
        self.config = config
        self.data = data
        self.status: int = raw.status
        self.status_text: str = getattr(raw, "status_text", "") or ""
        headers = getattr(raw, "headers", None)
        self.headers = headers if isinstance(headers, Headers) else Headers(headers)
        self.url: str = getattr(raw, "url", "") or config.full_url()

    @property
    def ok(self) -> bool:
        return 200 <= self.status <= 299

    def json(self) -> Any:
        """The body as JSON, parsing text when `data` came back as a string."""
        return _json.loads(self.data) if isinstance(self.data, (str, bytes)) else self.data

    def __repr__(self) -> str:
        return "<ClientResponse %d %s>" % (self.status, self.url)


Handler = Callable[[Any], Any]


class InterceptorManager:
    """An ordered list of handlers. `use` returns an id for `eject`, and works as a decorator."""

    def __init__(self) -> None:
        self._handlers: Dict[int, Tuple[Optional[Handler], Optional[Handler]]] = {}
        self._next = 0

    def use(self, fulfilled: Optional[Handler] = None, rejected: Optional[Handler] = None) -> Any:
        self._next += 1
        self._handlers[self._next] = (fulfilled, rejected)
        # As a decorator, hand the function back so the name still refers to it.
        return fulfilled if fulfilled is not None and rejected is None else self._next

    def add(self, fulfilled: Optional[Handler] = None, rejected: Optional[Handler] = None) -> int:
        """Like `use`, but always returns the id."""
        self._next += 1
        self._handlers[self._next] = (fulfilled, rejected)
        return self._next

    def eject(self, handler_or_id: Any) -> None:
        for key, (fulfilled, rejected) in list(self._handlers.items()):
            if handler_or_id in (key, fulfilled, rejected):
                del self._handlers[key]

    def clear(self) -> None:
        self._handlers.clear()

    def __iter__(self) -> Any:
        return iter(list(self._handlers.values()))

    def __len__(self) -> int:
        return len(self._handlers)


class Interceptors:
    def __init__(self) -> None:
        self.request = InterceptorManager()
        self.response = InterceptorManager()


async def _call(fn: Handler, value: Any) -> Any:
    result = fn(value)
    if inspect.isawaitable(result):
        result = await result
    return result


Transport = Callable[..., Awaitable[Any]]


class Client:
    """Requests that share a base URL, headers, a timeout, a retry policy and interceptors.

    Request interceptors run in the order they were added and get the `RequestConfig`; returning None keeps it.
    Response interceptors get the `ClientResponse`, and their `rejected` side gets the error: return a response
    there to recover (for example after refreshing a token: `return await client.request(error.config)`), or raise.
    """

    def __init__(self, *, base_url: str = "", headers: HeadersInit = None, params: Optional[Mapping[str, Any]] = None,
                 timeout: Optional[float] = None, retry: Union[Retry, int, None] = None,
                 raise_for_status: Union[bool, Callable[[int], bool]] = True, response_type: str = "auto",
                 redirect: Optional[str] = None, credentials: Optional[str] = None,
                 transport: Optional[Transport] = None) -> None:
        self.base_url = base_url
        self.headers = Headers(headers)
        self.params: Dict[str, Any] = dict(params or {})
        self.timeout = timeout
        self.retry = Retry(retry) if isinstance(retry, int) else retry
        self.raise_for_status = raise_for_status
        self.response_type = response_type
        self.redirect = redirect
        self.credentials = credentials
        self.transport = transport
        self.interceptors = Interceptors()

    def extend(self, **defaults: Any) -> "Client":
        """A new client with these defaults changed. Headers and params merge; interceptors are copied."""
        child = Client(
            base_url=defaults.pop("base_url", self.base_url),
            headers=list(self.headers.items()) + list(Headers(defaults.pop("headers", None)).items()),
            params={**self.params, **(defaults.pop("params", None) or {})},
            timeout=defaults.pop("timeout", self.timeout), retry=defaults.pop("retry", self.retry),
            raise_for_status=defaults.pop("raise_for_status", self.raise_for_status),
            response_type=defaults.pop("response_type", self.response_type),
            redirect=defaults.pop("redirect", self.redirect), credentials=defaults.pop("credentials", self.credentials),
            transport=defaults.pop("transport", self.transport),
        )
        if defaults:
            raise TypeError("unknown client options: %s" % ", ".join(sorted(defaults)))
        for fulfilled, rejected in self.interceptors.request:
            child.interceptors.request.add(fulfilled, rejected)
        for fulfilled, rejected in self.interceptors.response:
            child.interceptors.response.add(fulfilled, rejected)
        return child

    def build(self, method: str, url: str, **options: Any) -> RequestConfig:
        """The `RequestConfig` a call would send: the client's defaults under the call's own options."""
        headers = Headers(self.headers.items())
        for name, value in Headers(options.pop("headers", None)).items():
            headers.set(name, value)
        options.setdefault("base_url", self.base_url)
        options.setdefault("timeout", self.timeout)
        options.setdefault("retry", self.retry)
        options.setdefault("raise_for_status", self.raise_for_status)
        options.setdefault("response_type", self.response_type)
        options.setdefault("redirect", self.redirect)
        options.setdefault("credentials", self.credentials)
        params = {**self.params, **(options.pop("params", None) or {})}
        return RequestConfig(method, url, params=params, headers=headers, **options)

    async def request(self, config_or_method: Union[RequestConfig, str], url: str = "", **options: Any
                      ) -> ClientResponse:
        """Sends a request through every interceptor. Pass a `RequestConfig` (say `error.config`) to send it again."""
        if isinstance(config_or_method, RequestConfig):
            config = config_or_method
        else:
            config = self.build(config_or_method, url, **options)
        for fulfilled, rejected in self.interceptors.request:
            try:
                if fulfilled is not None:
                    config = (await _call(fulfilled, config)) or config
            except Exception as error:  # noqa: BLE001 - handed to the interceptor's own error side
                if rejected is None:
                    raise
                config = (await _call(rejected, error)) or config
        try:
            response: Any = await self._send_with_retry(config)
        except Exception as error:  # noqa: BLE001 - every failure goes through the response interceptors
            response = error
        for fulfilled, rejected in self.interceptors.response:
            if isinstance(response, BaseException):
                if rejected is None:
                    continue
                try:
                    response = await _call(rejected, response)
                except Exception as error:  # noqa: BLE001
                    response = error
            elif fulfilled is not None:
                try:
                    response = (await _call(fulfilled, response)) or response
                except Exception as error:  # noqa: BLE001
                    response = error
        if isinstance(response, BaseException):
            raise response
        return response

    async def _send_with_retry(self, config: RequestConfig) -> ClientResponse:
        while True:
            try:
                return await self._send(config)
            except Exception as error:  # noqa: BLE001
                policy = config.retry
                if policy is None or not policy.should_retry(error, config, config.attempt):
                    raise
                await asyncio.sleep(policy.delay(error, config.attempt))
                config.attempt += 1

    async def _send(self, config: RequestConfig) -> ClientResponse:
        if config.signal is not None and config.signal.aborted:
            config.signal.throw_if_aborted()
        # One controller carries both the timeout and the caller's signal, so the two can be told apart.
        controller = AbortController()
        why: Dict[str, Optional[str]] = {"reason": None}

        def stop(reason: str) -> None:
            if why["reason"] is None:
                why["reason"] = reason
            controller.abort(reason)

        forget = config.signal._on_abort(lambda: stop("caller")) if config.signal is not None else None
        timer = (asyncio.get_running_loop().call_later(config.timeout, stop, "timeout") if config.timeout
                 else None)
        kwargs: Dict[str, Any] = {}
        if config.redirect is not None:
            kwargs["redirect"] = config.redirect
        if config.credentials is not None:
            kwargs["credentials"] = config.credentials
        url = config.full_url()
        try:
            body = config.body()
            raw = await self._transport()(url, method=config.method, headers=config.headers, body=body,
                                          signal=controller.signal, **kwargs)
            data = await self._read(raw, config)
        except (AbortError, asyncio.CancelledError) as error:
            if why["reason"] == "timeout":
                raise TimeoutError("timeout of %gs exceeded for %s %s" % (config.timeout, config.method, url)
                                   ) from None
            if why["reason"] == "caller" or isinstance(error, AbortError):
                raise AbortError(str(config.signal.reason) if config.signal is not None and
                                 config.signal.reason is not None else "The operation was aborted.") from None
            raise
        finally:
            if timer is not None:
                timer.cancel()
            if forget is not None:
                forget()
        response = ClientResponse(raw, config, data)
        check = config.raise_for_status
        failed = check(response.status) if callable(check) else (check and not response.ok)
        if failed:
            raise HTTPError(response)
        return response

    def _transport(self) -> Transport:
        if self.transport is not None:
            return self.transport
        from . import fetch
        return fetch

    async def _read(self, raw: Any, config: RequestConfig) -> Any:
        kind = config.response_type
        if kind == "stream":
            return raw.body
        if config.on_download_progress is not None and hasattr(raw, "body"):
            total_header = raw.headers.get("content-length") if getattr(raw, "headers", None) else None
            total = int(total_header) if total_header and total_header.isdigit() else None
            chunks: List[bytes] = []
            received = 0
            async for chunk in raw.body:
                chunks.append(chunk)
                received += len(chunk)
                await _call(lambda r: config.on_download_progress(r, total), received)  # type: ignore[misc]
            payload: Union[bytes, str] = b"".join(chunks)
            if kind == "bytes":
                return payload
            text = payload.decode("utf-8", errors="replace")
        elif kind == "bytes":
            return await raw.bytes()
        else:
            text = await raw.text()
        if kind == "text":
            return text
        if kind == "json":
            return _json.loads(text) if text else None
        # auto: JSON when the server says so or the text looks like it, otherwise the text.
        if not text:
            return None
        headers = getattr(raw, "headers", None)
        content_type = (headers.get("content-type") if headers is not None else None) or ""
        if "json" in content_type or text.lstrip()[:1] in ("{", "["):
            try:
                return _json.loads(text)
            except ValueError:
                return text
        return text

    # -- shortcuts -------------------------------------------------------------------------------------------

    async def get(self, url: str, **options: Any) -> ClientResponse:
        return await self.request("GET", url, **options)

    async def head(self, url: str, **options: Any) -> ClientResponse:
        return await self.request("HEAD", url, **options)

    async def options(self, url: str, **options: Any) -> ClientResponse:
        return await self.request("OPTIONS", url, **options)

    async def delete(self, url: str, **options: Any) -> ClientResponse:
        return await self.request("DELETE", url, **options)

    async def post(self, url: str, json: Any = None, **options: Any) -> ClientResponse:
        return await self.request("POST", url, json=json, **options)

    async def put(self, url: str, json: Any = None, **options: Any) -> ClientResponse:
        return await self.request("PUT", url, json=json, **options)

    async def patch(self, url: str, json: Any = None, **options: Any) -> ClientResponse:
        return await self.request("PATCH", url, json=json, **options)


def create_client(**defaults: Any) -> Client:
    """A `Client` with these defaults: base_url, headers, params, timeout, retry, raise_for_status, response_type,
    redirect, credentials, transport."""
    return Client(**defaults)


async def gather_settled(*calls: Awaitable[Any]) -> Sequence[Any]:
    """Runs requests together and returns each result or the error it raised, like `Promise.allSettled`."""
    return await asyncio.gather(*calls, return_exceptions=True)


__all__ = ["Client", "ClientResponse", "HTTPError", "InterceptorManager", "Interceptors", "RequestConfig", "Retry",
           "TimeoutError", "create_client", "encode_params", "gather_settled"]
