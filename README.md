# pyxpo-fetch

[![CI](https://github.com/pyxpo/pyxpo-fetch/actions/workflows/ci.yml/badge.svg)](https://github.com/pyxpo/pyxpo-fetch/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/pyxpo-fetch)](https://pypi.org/project/pyxpo-fetch/) [![Python](https://img.shields.io/pypi/pyversions/pyxpo-fetch)](https://pypi.org/project/pyxpo-fetch/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

HTTP requests with streaming response bodies for [pyxpo](https://github.com/pyxpo/pyxpo) apps written in Python. It uses
the platform's own networking, so system proxies, certificates and cookies apply. The API follows
[expo/fetch](https://docs.expo.dev/versions/latest/sdk/expo/#expofetch-api), which is the WinterCG `fetch`: iOS uses
`URLSession` and Android uses OkHttp.

<p align="center">
  <img src="https://raw.githubusercontent.com/pyxpo/pyxpo-fetch/main/docs/images/hero.png" width="820" alt="A GET request to example.com on iOS, with status, content type and body size. The Android emulator in the screenshot had no internet.">
</p>

```python
from pyxpo_fetch import fetch

resp = await fetch("https://api.example.com/items")
if resp.ok:
    items = await resp.json()
```

## Features

- **Familiar API:** `fetch(url, method=, headers=, body=)` returns a `Response` with `status`, `ok`, `headers`, `text()`, `json()`, `bytes()`.
- **Streaming:** `async for chunk in resp.body` gives bytes as they arrive: server-sent events, LLM replies, big downloads.
- **Abort:** an `AbortController` stops a request, even while its body streams.
- **Timeouts, redirects, cookies:** `timeout`, `redirect="follow" | "manual" | "error"`, `credentials="include" | "omit"`.
- **Honest errors:** HTTP 404 and 500 do not raise. Network failures raise `FetchError`.
- **Case-insensitive `Headers`** that joins repeated values as the web does.
- **HTTP client, axios style:** `create_client(base_url=, headers=, timeout=, retry=)` with request and response
  interceptors, parsed JSON in `res.data`, `HTTPError` on error statuses, retries with backoff and `Retry-After`,
  query `params`, form bodies, download progress and `extend()` for derived clients.
- **Typed:** ships `py.typed`.

## Contents

| If you are... | Start here |
| --- | --- |
| Calling a JSON API | [Quick start](#quick-start) |
| Sending JSON or bytes | [Sending data](#sending-data) |
| Streaming a response | [Streaming](#streaming) |
| Cancelling or timing out | [Abort and timeouts](#abort-and-timeouts) |
| Handling failures | [Errors](#errors) |
| Wanting interceptors, retries, a base URL | [HTTP client](#http-client) |
| Exact signatures | [API reference](#api-reference) |
| Seeing it in a real app | [Recipe: a search box](#recipe-a-search-box) |
| Stuck | [Testing](#testing) · [Troubleshooting](#troubleshooting) · [FAQ](#faq) |

## Platform support

| Target | What works |
| --- | --- |
| Android, API 24+ | OkHttp 4.12. Adds the `INTERNET` permission. Cookies live in memory while the app runs. |
| iOS 15+ | `URLSession`. Cookies use the shared cookie store. |
| Desktop window | `urllib` in a worker thread, with an in-memory cookie jar. |
| Web page | The browser's own `fetch`, streamed with a `ReadableStream` reader. CORS applies: the server must allow your page's origin. `redirect="manual"` gives an opaque response, as in the browser. |

Requires Python 3.9+ and `pyxpo>=0.1.0a1`.

## Install

```sh
pip install pyxpo-fetch
```

```toml
[tool.pyxpo]
modules = ["fetch"]
```

Rebuild the app. There are no plugin options. Plain `http://` URLs need the server to be allowed by the app (pyxpo apps
allow local hosts), so prefer `https://`.

## Quick start

```python
from pyxpo import Button, Text, View, component, use_async, use_state
from pyxpo_fetch import FetchError, fetch


@component
def Hello():
    taps, set_taps = use_state(0)
    line, set_line = use_state("Press the button")

    async def go():
        if taps == 0:
            return
        try:
            resp = await fetch("https://example.com/", timeout=10)
            text = await resp.text()
            set_line("%s %s, %d bytes" % (resp.status, resp.status_text, len(text)))
        except FetchError as e:
            set_line("Offline? %s" % e)

    use_async(go, [taps])
    return View(Text(line), Button("GET example.com", lambda: set_taps(taps + 1)))
```

> **Tip:** Start a request from a button with a counter in state and `use_async(go, [counter])`. A `lambda: go()` only
> builds a coroutine that nobody awaits.

<p align="center">
  <img src="https://raw.githubusercontent.com/pyxpo/pyxpo-fetch/main/docs/images/response.png" width="720" alt="The iOS result of fetch(): status 200, content type, body size, first line and time">
</p>

The screenshot is from iOS. The Android emulator used for the screenshots had no internet access, so its request ended in
a `FetchError`, which is the behavior to expect on a phone in airplane mode.

## Sending data

```python
import json

resp = await fetch(
    "https://api.example.com/items",
    method="POST",
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    body=json.dumps({"name": "pen"}),
)
```

- `body` is `str` (UTF-8, `Content-Type` defaults to `text/plain;charset=UTF-8`) or `bytes`. Dicts are not accepted
  (use `json.dumps`). GET and HEAD cannot have a body (`ValueError`).
- `headers` is a dict, a list of pairs, or a `Headers` object.
- `method` is upper-cased for you.

## Reading the response

| Member | Notes |
| --- | --- |
| `status`, `status_text`, `ok` | `ok` is true for 200 to 299. |
| `headers` | `get(name)`, `get_all(name)`, `get_set_cookie()`, `has(name)`, `keys()`, `items()`, and `name in headers`. |
| `url`, `redirected` | The final URL, and whether a redirect led there. |
| `await text()` / `json()` / `bytes()` / `array_buffer()` | Read the whole body. |
| `body` | An async iterator of `bytes` chunks. |
| `body_used` | A body can be read only once. A second read raises `RuntimeError`. |

## Streaming

```python
resp = await fetch("https://example.com/events")
async for chunk in resp.body:
    handle(chunk)          # bytes, as they arrive
```

Stop early with `await resp.body.cancel()`. On iOS `URLSession` holds back the first 512 bytes of a body for MIME
sniffing, so a streaming server should send `X-Content-Type-Options: nosniff` or a type such as `text/event-stream`, or
the first chunks arrive late.

## Abort and timeouts

```python
import asyncio

from pyxpo_fetch import AbortController, AbortError

ctl = AbortController()
task = asyncio.ensure_future(fetch(url, signal=ctl.signal))   # pass the signal
...
ctl.abort()                              # the awaiting call raises AbortError
```

An already aborted signal raises `AbortError` before the request starts. `timeout=` is in seconds. `redirect="manual"`
returns the 3xx response itself; `"error"` raises `FetchError`. `credentials="omit"` sends and stores no cookies.

## Errors

| Error | When | What to do |
| --- | --- | --- |
| `FetchError` | No connection, DNS, TLS, timeout, refused redirect, or a body that breaks off. | Show a retry. Do not assume the server saw the request. |
| `AbortError` (a `FetchError`) | The signal was aborted. | Usually nothing: the user or your code cancelled. |
| `ValueError` | Bad `redirect` or `credentials`, a non-http(s) URL, or a body on GET or HEAD. | Fix the call. |
| `TypeError` | `body` is not `str` or `bytes`. | Use `json.dumps(...)`. |
| `RuntimeError` | The body was read twice. | Read it once and keep the result. |
| `ModuleNotLinkedError` (from `pyxpo.module_api`) | Built without `"fetch"` in `modules`. | Add it and rebuild. |
| `resp.ok` is `False` | The server answered 4xx or 5xx. This is not an exception. | Check `resp.status`, and read the body for the reason. |

## Config

None.

## Web and desktop

On the desktop window requests use `urllib` on a worker thread, with an in-memory cookie jar, so the UI stays smooth. The
web page calls the browser's `fetch`: cookies follow the browser's rules, and a server on another origin must send
CORS headers for your page (and allow any custom headers you send). A CORS refusal raises `FetchError`.

## HTTP client

`fetch` stays close to the web. For an app talking to one API, `create_client` adds what axios gives you: shared
defaults, interceptors, parsed bodies, errors for error statuses, retries and a timeout per request.

```python
from pyxpo_fetch import HTTPError, Retry, TimeoutError, create_client

api = create_client(
    base_url="https://api.example.com/v1",
    headers={"Accept": "application/json"},
    timeout=15,                       # seconds, per attempt
    retry=Retry(2, backoff=0.3),      # GET/PUT/DELETE on network errors, timeouts, 408/429/502/503/504
)

@api.interceptors.request.use
def sign(config):
    config.headers.set("Authorization", "Bearer " + session.token)

async def refresh_once(error):
    if isinstance(error, HTTPError) and error.status == 401 and not error.config.meta.get("refreshed"):
        await session.refresh()
        error.config.meta["refreshed"] = True
        return await api.request(error.config)    # runs the interceptors again, so it carries the new token
    raise error

api.interceptors.response.use(None, refresh_once)

res = await api.get("/posts", params={"page": 2, "draft": False, "tag": ["a", "b"]})
res.data            # parsed JSON (text when the body is not JSON)
res.status, res.headers, res.config

await api.post("/posts", {"title": "Hello"})            # JSON body, Content-Type set for you
await api.post("/login", form={"user": "ana"})          # form body
await api.get("/export", response_type="bytes", on_download_progress=lambda got, total: print(got, total))
```

- **Interceptors** run in the order you add them and may be `async`. A request interceptor gets the `RequestConfig`;
  returning None keeps it. A response interceptor gets the `ClientResponse`. Its error side gets the exception: return
  a response to recover, or raise. `use()` works as a decorator; `add()` returns an id; `eject(id_or_function)`.
- **Errors:** an error status raises `HTTPError` with `.status`, `.data`, `.response` and `.config`. Pass
  `raise_for_status=False` (or a function of the status) to get the response instead. Running past `timeout` raises
  `TimeoutError`; your own `signal` raises `AbortError`; no network raises `FetchError`. All three are `FetchError`s.
- **`response_type`:** `"auto"` (JSON when it looks like JSON), `"json"`, `"text"`, `"bytes"` or `"stream"`
  (`res.data` is then the body's async iterator).
- **`config.meta`** is a free dict for your own flags, such as "already retried".
- **`api.extend(headers=..., timeout=...)`** makes a new client with merged defaults and a copy of the interceptors.
- **`transport=`** swaps the function that sends the request. Tests pass a fake with the `fetch` signature.
- **`gather_settled(*calls)`** runs requests together and returns each response or error.

## Recipe: a search box

Look up what the user types in a JSON API, with a timeout and clear messages when it fails.

```python
from urllib.parse import quote

from pyxpo import Text, TextInput, View, component, use_async, use_state
from pyxpo_fetch import FetchError, fetch


@component
def Search():
    q, set_q = use_state("")
    result, set_result = use_state("")

    async def look_up():
        if len(q) < 2:
            set_result("")
            return
        try:
            resp = await fetch("https://api.example.com/search?q=" + quote(q), timeout=8)
            if not resp.ok:
                set_result("Server said %d" % resp.status)
                return
            data = await resp.json()
            set_result(", ".join(item["name"] for item in data.get("items", [])))
        except FetchError as e:
            set_result("Could not search: %s" % e)

    use_async(look_up, [q])
    return View(TextInput(value=q, on_change_text=set_q, placeholder="Search"), Text(result))
```

## Production checklist

- [ ] Every call is inside `try/except FetchError` and shows a retry.
- [ ] You check `resp.ok` before using the body.
- [ ] You set a `timeout`.
- [ ] You use `https://`, and secrets are in headers (from `pyxpo-secure-store`), not in URLs.
- [ ] Streaming servers send `nosniff` or an event-stream type so iOS delivers chunks at once.
- [ ] You tested in airplane mode.
- [ ] For the web build, the API allows your site's origin and custom headers (CORS).

## Testing

The module is easy to fake. Fakes can replace `fetch` in your own module, or replace the native bridge:

```python
import asyncio

import pytest

import pyxpo_fetch as mod
from pyxpo_fetch import Headers


def test_headers_case_insensitive():
    h = Headers({"Content-Type": "text/plain"})
    h.append("Set-Cookie", "a=1")
    h.append("set-cookie", "b=2")
    assert h.get("content-type") == "text/plain"
    assert h.get_set_cookie() == ["a=1", "b=2"]


def test_error_status_does_not_raise(monkeypatch):
    class Fake:
        status, ok = 404, False

        async def text(self):
            return "nope"

    async def fake_fetch(url, **kw):
        return Fake()

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    resp = asyncio.run(mod.fetch("https://x.test/"))
    assert not resp.ok


def test_validation():
    with pytest.raises(ValueError):
        asyncio.run(mod.fetch("ftp://x.test/"))
    with pytest.raises(ValueError):
        asyncio.run(mod.fetch("https://x.test/", method="GET", body="x"))
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `FetchError` on the Android emulator | The emulator has no internet. Check its network, or test on a phone. |
| `ValueError: fetch needs an http(s) URL` | Add the scheme. |
| Plain `http://` fails on a device | Use `https://`, or a local host allowed by the app. |
| Streaming chunks arrive late on iOS | Add `X-Content-Type-Options: nosniff` or `Content-Type: text/event-stream` on the server. |
| `RuntimeError: already read` | A body can be read once. Store the text. |
| Works on a phone, `FetchError` in the browser | CORS: the server does not allow your page's origin or one of your headers. Check the browser console. |
| `ModuleNotLinkedError` | Add `"fetch"` to `modules` and rebuild. |

## FAQ

**Does a 404 raise?** No. Check `resp.ok` or `resp.status`.

**Are cookies kept?** Yes with `credentials="include"` (the default): the shared store on iOS, memory on Android and
desktop. Use `"omit"` to turn them off.

**Can I upload a file?** Send its `bytes` as `body`.

**Is there `fetch` on the web page?** Yes, the browser's own, with the same API.

## API reference

### `fetch(url, *, method="GET", headers=None, body=None, redirect="follow", credentials="include", signal=None, timeout=None)`

Returns a `Response` once the headers arrive.

| Argument | Notes |
| --- | --- |
| `url` | `http://` or `https://`. |
| `redirect` | `"follow"`, `"manual"`, `"error"`. |
| `credentials` | `"include"`, `"omit"`, `"same-origin"` (acts like include). |
| `signal` | `AbortController().signal`. |
| `timeout` | Seconds. |

### Classes

| Name | Description |
| --- | --- |
| `Response` | `status`, `status_text`, `ok`, `headers`, `url`, `redirected`, `body`, `body_used`, `text()`, `json()`, `bytes()`, `array_buffer()`. camelCase aliases work (`statusText`, `bodyUsed`, `arrayBuffer`). |
| `Headers` | `append`, `set`, `delete`, `get`, `get_all`, `get_set_cookie`, `has`, `keys`, `items`. |
| `AbortController` | `signal`, `abort(reason=None)`. |
| `AbortSignal` | `aborted`, `reason`, `throw_if_aborted()`. |
| `FetchError`, `AbortError` | Network failure, and cancellation (a `FetchError`). |
| `create_client(**defaults)` / `Client` | `base_url`, `headers`, `params`, `timeout`, `retry`, `raise_for_status`, `response_type`, `redirect`, `credentials`, `transport`. Methods `request`, `get`, `head`, `options`, `delete`, `post`, `put`, `patch`, `extend`, `build`, and `interceptors.request` / `interceptors.response`. |
| `RequestConfig` | `method`, `url`, `base_url`, `params`, `headers`, `json`, `data`, `form`, `timeout`, `signal`, `response_type`, `retry`, `raise_for_status`, `on_download_progress`, `meta`, `attempt`, `full_url()`. |
| `ClientResponse` | `data`, `status`, `status_text`, `ok`, `headers`, `url`, `config`, `raw`, `json()`. |
| `Retry(attempts=2, *, statuses, methods, backoff, max_backoff, on_timeout, when)` | Retry policy; `Retry-After` wins over backoff. |
| `HTTPError`, `TimeoutError` | Error status (with `.response`, `.status`, `.data`, `.config`), and a timeout. Both `FetchError`s. |

## License

MIT
