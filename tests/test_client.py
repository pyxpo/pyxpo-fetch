"""The axios-style client: defaults, interceptors, parsed bodies, errors, timeouts, aborts and retries."""
import asyncio
import json

import pytest

import pyxpo_fetch as F


def run(coro):
    return asyncio.run(coro)


class Raw:
    def __init__(self, status=200, body="", headers=None, url=""):
        self.status = status
        self.status_text = "OK" if status < 400 else "Error"
        self.headers = F.Headers(headers)
        self.url = url
        self._body = body.encode() if isinstance(body, str) else body

    async def text(self):
        return self._body.decode()

    async def bytes(self):
        return self._body

    @property
    def body(self):
        async def chunks():
            for i in range(0, len(self._body), 4):
                yield self._body[i:i + 4]
        return chunks()


class Transport:
    """Answers from a list (or one function) and records every call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, url, *, method, headers, body, signal, **extra):
        self.calls.append({"url": url, "method": method, "headers": dict(headers.items()), "body": body,
                           "signal": signal, **extra})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = await answer(url, signal) if asyncio.iscoroutinefunction(answer) else answer(url)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def j(data, status=200, headers=None):
    return Raw(status, json.dumps(data), {"content-type": "application/json", **(headers or {})})


def test_base_url_params_headers_and_json():
    t = Transport(j({"ok": True}))
    api = F.create_client(base_url="https://api.test/v1/", headers={"Accept": "application/json"},
                          params={"lang": "en"}, transport=t)
    res = run(api.post("/items", {"name": "pen"}, params={"page": 2, "q": "a b", "skip": None, "on": True},
                       headers={"X-Id": "7"}))
    call = t.calls[0]
    assert call["url"] == "https://api.test/v1/items?lang=en&page=2&q=a%20b&on=true"
    assert call["method"] == "POST" and call["body"] == '{"name":"pen"}'
    assert call["headers"]["content-type"] == "application/json" and call["headers"]["x-id"] == "7"
    assert call["headers"]["accept"] == "application/json"
    assert "redirect" not in call and "credentials" not in call
    assert res.ok and res.status == 200 and res.data == {"ok": True} and res.config.method == "POST"


def test_absolute_url_wins_and_lists_repeat():
    t = Transport(Raw(200, "hi", {"content-type": "text/plain"}))
    api = F.create_client(base_url="https://api.test", transport=t)
    res = run(api.get("https://other.test/x?a=1", params={"t": ["x", "y"]}))
    assert t.calls[0]["url"] == "https://other.test/x?a=1&t=x&t=y" and res.data == "hi"


def test_response_types_and_form():
    t = Transport(Raw(200, '{"a":1}'), Raw(200, "abc"), Raw(200, ""))
    api = F.create_client(transport=t)
    assert run(api.get("https://x.test/", response_type="text")).data == '{"a":1}'
    assert run(api.post("https://x.test/", form={"a": "1 2"}, response_type="bytes")).data == b"abc"
    assert t.calls[1]["body"] == "a=1%202" and t.calls[1]["headers"]["content-type"].startswith(
        "application/x-www-form-urlencoded")
    assert run(api.delete("https://x.test/")).data is None
    with pytest.raises(ValueError):
        F.RequestConfig("GET", "/", response_type="xml")


def test_error_status_raises_with_the_parsed_body():
    t = Transport(j({"error": "nope"}, status=422))
    api = F.create_client(transport=t)
    with pytest.raises(F.HTTPError) as caught:
        run(api.get("https://x.test/items"))
    err = caught.value
    assert err.status == 422 and err.data == {"error": "nope"} and err.response.config.url == "https://x.test/items"
    assert isinstance(err, F.FetchError)
    quiet = api.extend(raise_for_status=False)
    assert run(quiet.get("https://x.test/items")).status == 422


def test_request_interceptors_in_order_async_and_eject():
    t = Transport(j({}))
    api = F.create_client(transport=t)
    order = []

    @api.interceptors.request.use
    def first(config):
        order.append("first")
        config.headers.set("Authorization", "Bearer abc")

    async def second(config):
        order.append("second")
        config.params["sig"] = "1"
        return config

    second_id = api.interceptors.request.add(second)
    run(api.get("https://x.test/"))
    assert order == ["first", "second"] and t.calls[0]["headers"]["authorization"] == "Bearer abc"
    assert t.calls[0]["url"].endswith("?sig=1")
    api.interceptors.request.eject(second_id)
    api.interceptors.request.eject(first)
    run(api.get("https://x.test/"))
    assert len(api.interceptors.request) == 0 and "authorization" not in t.calls[1]["headers"]


def test_response_interceptor_reshapes_and_recovers_with_a_resend():
    state = {"token": "old"}

    def answer(url):
        return j({"me": 1}) if t.calls[-1]["headers"].get("authorization") == "Bearer new" else j({}, status=401)

    t = Transport(answer)
    api = F.create_client(transport=t)
    api.interceptors.request.use(lambda c: c.headers.set("Authorization", "Bearer " + state["token"]))

    async def refresh(error):
        if isinstance(error, F.HTTPError) and error.status == 401 and not error.config.meta.get("retried"):
            state["token"] = "new"
            error.config.meta["retried"] = True
            return await api.request(error.config)
        raise error

    # The resend runs every interceptor again, so the reshaping one goes first and the error side returns its result.
    api.interceptors.response.add(lambda res: setattr(res, "data", {"wrapped": res.data}) or res)
    api.interceptors.response.add(None, refresh)
    res = run(api.get("https://x.test/me"))
    assert res.data == {"wrapped": {"me": 1}} and len(t.calls) == 2


def test_response_rejection_can_raise_another_error():
    api = F.create_client(transport=Transport(j({}, status=500)))

    class Mine(Exception):
        pass

    def convert(error):
        raise Mine(error.status)

    api.interceptors.response.add(None, convert)
    with pytest.raises(Mine):
        run(api.get("https://x.test/"))


def test_timeout_and_caller_abort_are_told_apart():
    async def slow(url, signal):
        stopped = asyncio.Event()
        signal._on_abort(stopped.set)
        await stopped.wait()
        raise F.AbortError("stopped")

    api = F.create_client(timeout=0.05, transport=Transport(slow))
    with pytest.raises(F.TimeoutError):
        run(api.get("https://x.test/"))

    async def abort_soon():
        c = F.AbortController()
        asyncio.get_running_loop().call_later(0.02, c.abort, "user left")
        return await api.get("https://x.test/", signal=c.signal, timeout=5)

    with pytest.raises(F.AbortError) as caught:
        run(abort_soon())
    assert not isinstance(caught.value, F.TimeoutError) and "user left" in str(caught.value)
    done = F.AbortController()
    done.abort()
    with pytest.raises(F.AbortError):
        run(api.get("https://x.test/", signal=done.signal))


def test_retries_idempotent_requests_and_honors_retry_after():
    t = Transport(F.FetchError("offline"), j({}, status=503, headers={"retry-after": "0"}), j({"n": 3}))
    api = F.create_client(retry=F.Retry(3, backoff=0.001), transport=t)
    res = run(api.get("https://x.test/"))
    assert res.data == {"n": 3} and len(t.calls) == 3 and res.config.attempt == 2
    posts = Transport(j({}, status=503), j({}))
    with pytest.raises(F.HTTPError):
        run(F.create_client(retry=2, transport=posts).post("https://x.test/", {}))
    assert len(posts.calls) == 1
    gone = Transport(j({}, status=404), j({}))
    with pytest.raises(F.HTTPError):
        run(F.create_client(retry=F.Retry(2, backoff=0), transport=gone).get("https://x.test/"))
    assert len(gone.calls) == 1


def test_retry_after_dates_and_seconds():
    from pyxpo_fetch.client import _retry_after
    assert _retry_after("2") == 2.0 and _retry_after(None) is None and _retry_after("soon") is None
    assert _retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_download_progress_and_stream():
    seen = []
    api = F.create_client(transport=Transport(Raw(200, "0123456789", {"content-length": "10"})))
    res = run(api.get("https://x.test/", response_type="text",
                      on_download_progress=lambda got, total: seen.append((got, total))))
    assert res.data == "0123456789" and seen == [(4, 10), (8, 10), (10, 10)]

    async def stream():
        r = await api.get("https://x.test/", response_type="stream")
        return b"".join([c async for c in r.data])

    assert run(stream()) == b"0123456789"


def test_extend_copies_defaults_and_interceptors():
    t = Transport(j({}))
    base = F.create_client(base_url="https://a.test", headers={"A": "1"}, transport=t)
    base.interceptors.request.use(lambda c: c.headers.set("X-Base", "y"))
    child = base.extend(headers={"B": "2"}, base_url="https://b.test")
    run(child.get("/p"))
    call = t.calls[0]
    assert call["url"] == "https://b.test/p" and call["headers"]["a"] == "1" and call["headers"]["b"] == "2"
    assert call["headers"]["x-base"] == "y" and base.base_url == "https://a.test"
    with pytest.raises(TypeError):
        base.extend(nope=1)


def test_redirect_and_credentials_pass_through_when_set():
    t = Transport(j({}))
    run(F.create_client(transport=t, credentials="omit").get("https://x.test/", redirect="manual"))
    assert t.calls[0]["credentials"] == "omit" and t.calls[0]["redirect"] == "manual"


def test_gather_settled():
    api = F.create_client(transport=Transport(j({"a": 1}), j({}, status=500)))

    async def both():
        return await F.gather_settled(api.get("https://x.test/1"), api.get("https://x.test/2"))

    ok, failed = run(both())
    assert ok.data == {"a": 1} and isinstance(failed, F.HTTPError)


def test_default_transport_is_fetch(monkeypatch):
    seen = {}

    async def fake_fetch(url, **kw):
        seen.update(url=url, **kw)
        return j({"x": 1})

    monkeypatch.setattr(F, "fetch", fake_fetch)
    res = run(F.create_client(base_url="https://x.test").get("/y"))
    assert res.data == {"x": 1} and seen["url"] == "https://x.test/y" and seen["method"] == "GET"
