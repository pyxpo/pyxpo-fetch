# pyxpo-fetch

HTTP requests with streaming bodies for [pyxpo](https://github.com/pyxpo/pyxpo) apps, through the platform's own
networking. The API follows [expo/fetch](https://docs.expo.dev/versions/latest/sdk/expo/#expofetch-api), which is the
WinterCG `fetch`.

```sh
pip install pyxpo-fetch
```

```toml
[tool.pyxpo]
modules = ["fetch"]
```

```python
import json
from pyxpo_fetch import fetch, AbortController, FetchError

resp = await fetch("https://api.example.com/items", method="POST",
                   headers={"Content-Type": "application/json"}, body=json.dumps({"name": "pen"}))
if resp.ok:
    item = await resp.json()
print(resp.status, resp.status_text, resp.headers.get("content-type"))

# Stream the body as it arrives (for example server-sent events or an LLM response).
resp = await fetch("https://example.com/stream")
async for chunk in resp.body:        # bytes
    ...

# Stop a request, also while its body streams.
ctl = AbortController()
resp = await fetch(url, signal=ctl.signal)
ctl.abort()                          # pending awaits raise AbortError
```

## `fetch(url, *, method="GET", headers=None, body=None, redirect="follow", credentials="include", signal=None, timeout=None)`

Returns a `Response` once the headers arrive. HTTP error statuses (404, 500) do not raise: check `response.ok`.

| Argument | Notes |
| --- | --- |
| `url` | `http://` or `https://`. Plain HTTP needs the server allowed by the app (pyxpo apps allow local hosts). |
| `headers` | dict, list of pairs, or `Headers`. |
| `body` | `str` (sent as UTF-8, `Content-Type` defaults to `text/plain;charset=UTF-8`) or `bytes`. Not allowed for GET and HEAD. |
| `redirect` | `"follow"`, `"manual"` (return the 3xx response) or `"error"` (raise `FetchError` on a redirect). |
| `credentials` | `"include"` sends and stores cookies; `"omit"` does neither. `"same-origin"` acts like `"include"`. |
| `signal` | `AbortController().signal`. |
| `timeout` | Seconds. |

Errors: `FetchError` for network failures (no connection, DNS, TLS, timeout, refused redirect); `AbortError`
(a `FetchError`) when the signal aborts.

## `Response`

| Member | Notes |
| --- | --- |
| `status`, `status_text`, `ok` | `ok` is true for 200-299. |
| `headers` | `Headers`: case-insensitive; `get` joins repeated values with `", "`; `get_set_cookie()` lists `Set-Cookie` values. |
| `url`, `redirected` | The final URL, and whether a redirect led there. |
| `body` | Async iterator of `bytes` chunks. |
| `await text()`, `await json()`, `await bytes()`, `await array_buffer()` | Read the whole body. |
| `body_used` | The body can be read once. |

camelCase spellings work too (`statusText`, `bodyUsed`, `arrayBuffer`, `getSetCookie`).

## Platforms

- iOS 15+: `URLSession`. Cookies use the shared cookie store. `URLSession` holds back the first 512 bytes of a
  body for MIME sniffing; streaming servers should send `X-Content-Type-Options: nosniff` (or a type such as
  `text/event-stream`) so the first chunks arrive at once.
- Android 7 (API 24)+: OkHttp 4.12. Cookies live in memory while the app runs. Adds the `INTERNET` permission.
- Desktop: `urllib` in a worker thread, with an in-memory cookie jar.
