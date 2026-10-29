"""pyxpo-fetch: the expo/fetch API over the native stream protocol and the desktop urllib transport."""
import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pyxpo.module_api import dispatch
from pyxpo.module_testing import FakeBridge

import pyxpo_fetch as F


def run(coro):
    return asyncio.run(coro)


def b64(s):
    return base64.b64encode(s).decode()


def test_headers_case_insensitive():
    h = F.Headers({"Content-Type": "text/plain"})
    h.append("Set-Cookie", "a=1")
    h.append("set-cookie", "b=2")
    assert h.get("content-type") == "text/plain" and h["CONTENT-TYPE"] == "text/plain"
    assert h.get("Set-Cookie") == "a=1, b=2" and h.getSetCookie() == ["a=1", "b=2"]
    assert "content-type" in h and "x" not in h and h.get("x") is None
    h.set("content-type", "application/json")
    assert h.get_all("Content-Type") == ["application/json"]
    h.delete("SET-COOKIE")
    assert list(h) == ["content-type"]
    with pytest.raises(KeyError):
        h["x"]
    assert F.Headers([("a", "1")]).get("A") == "1"


def test_argument_checks():
    with pytest.raises(ValueError):
        run(F.fetch("ftp://x"))
    with pytest.raises(ValueError):
        run(F.fetch("http://x", body="no"))
    with pytest.raises(ValueError):
        run(F.fetch("http://x", redirect="bogus"))
    with pytest.raises(TypeError):
        run(F.fetch("http://x", method="POST", body={"a": 1}))
    c = F.AbortController()
    c.abort("stop")
    with pytest.raises(F.AbortError):
        run(F.fetch("http://x", signal=c.signal))


def _start_handler(chunks, status=200, error=None, aborted=False):
    """A `Fetch.start` handler that replies with the head, then streams `chunks` as events."""
    seen = {}

    def start(rid, url, method, headers, body, redirect, credentials, timeout_ms):
        seen.update(rid=rid, url=url, method=method, headers=headers, body=body, redirect=redirect,
                    credentials=credentials, timeout_ms=timeout_ms)
        loop = asyncio.get_running_loop()
        for c in chunks:
            loop.call_soon(dispatch, {"name": "Fetch.chunk", "data": {"id": rid, "data": b64(c)}})
        end = {"id": rid}
        if error:
            end.update(error=error, aborted=aborted)
        loop.call_soon(dispatch, {"name": "Fetch.end", "data": end})
        return {"status": status, "statusText": "OK" if status == 200 else "Not Found",
                "headers": [["Content-Type", "application/json"], ["Set-Cookie", "a=1"], ["set-cookie", "b=2"]],
                "url": url + "/final", "redirected": True}
    return start, seen


def test_native_request_and_json():
    with FakeBridge("ios") as fb:
        start, seen = _start_handler([b'{"a": ', b"[1, 2]}"])
        fb.on("Fetch", "start", start)

        async def go():
            r = await F.fetch("https://api.test/x", method="post", headers={"X-Key": "1"}, body="hi", timeout=2.5)
            assert r.ok and r.status == 200 and r.statusText == "OK" and r.redirected
            assert r.url == "https://api.test/x/final"
            assert r.headers["content-type"] == "application/json"
            assert r.headers.get_set_cookie() == ["a=1", "b=2"]
            assert await r.json() == {"a": [1, 2]}
            assert r.body_used
            with pytest.raises(RuntimeError):
                await r.text()
        run(go())
        assert seen["method"] == "POST" and seen["body"] == b64(b"hi") and seen["timeout_ms"] == 2500
        assert ["X-Key", "1"] in seen["headers"] and ["Content-Type", "text/plain;charset=UTF-8"] in seen["headers"]
        assert seen["redirect"] == "follow" and seen["credentials"] == "include"


def test_native_stream_chunks_and_errors():
    with FakeBridge("android") as fb:
        start, _ = _start_handler([b"one", b"two"], status=404)
        fb.on("Fetch", "start", start)

        async def stream():
            r = await F.fetch("http://h/s")
            assert not r.ok and r.status == 404
            return [c async for c in r.body]
        assert run(stream()) == [b"one", b"two"]

        start, _ = _start_handler([b"x"], error="connection reset")
        fb.on("Fetch", "start", start)

        async def broken():
            r = await F.fetch("http://h/s")
            await r.text()
        with pytest.raises(F.FetchError, match="connection reset"):
            run(broken())

        def refuse(*a):
            raise RuntimeError("no route to host")
        fb.on("Fetch", "start", refuse)
        with pytest.raises(F.FetchError, match="no route"):
            run(F.fetch("http://h/s"))


def test_native_abort_while_streaming():
    with FakeBridge("ios") as fb:
        state = {}

        def start(rid, *a):
            state["rid"] = rid
            return {"status": 200, "headers": [], "url": a[0], "redirected": False}

        def cancel(rid):
            state["cancelled"] = rid
            asyncio.get_running_loop().call_soon(
                dispatch, {"name": "Fetch.end", "data": {"id": rid, "error": "aborted", "aborted": True}})
        fb.on("Fetch", "start", start)
        fb.on("Fetch", "cancel", cancel)

        async def go():
            ctl = F.AbortController()
            r = await F.fetch("http://h/slow", signal=ctl.signal)
            dispatch({"name": "Fetch.chunk", "data": {"id": state["rid"], "data": b64(b"part")}})
            got = []
            with pytest.raises(F.AbortError):
                async for c in r.body:
                    got.append(c)
                    ctl.abort()
            return got
        assert run(go()) == [b"part"]
        assert state["cancelled"] == state["rid"]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/echo")
            self.end_headers()
            return
        code = 404 if self.path == "/missing" else 200
        body = json.dumps({"path": self.path, "x": self.headers.get("X-Test")}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(201)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def test_desktop(server):
    async def go():
        r = await F.fetch(server + "/echo", headers={"X-Test": "yes"})
        assert r.ok and r.headers["content-type"] == "application/json"
        assert await r.json() == {"path": "/echo", "x": "yes"}
        r = await F.fetch(server + "/missing")
        assert r.status == 404 and not r.ok and (await r.json())["path"] == "/missing"
        r = await F.fetch(server + "/p", method="POST", body=b"\x00\x01")
        assert r.status == 201 and await r.bytes() == b"\x00\x01"
        r = await F.fetch(server + "/redirect")
        assert r.redirected and r.url.endswith("/echo")
        await r.text()
        r = await F.fetch(server + "/redirect", redirect="manual")
        assert r.status == 302 and not r.redirected
        await r.text()
        with pytest.raises(F.FetchError, match="redirect"):
            await F.fetch(server + "/redirect", redirect="error")
        with pytest.raises(F.FetchError):
            await F.fetch("http://127.0.0.1:9/")
    run(go())
