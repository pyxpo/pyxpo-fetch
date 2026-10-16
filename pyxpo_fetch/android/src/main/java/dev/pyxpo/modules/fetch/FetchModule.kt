package dev.pyxpo.modules.fetch

import android.util.Base64
import dev.pyxpo.moduleapi.PxModule
import dev.pyxpo.moduleapi.PxModuleContext
import dev.pyxpo.moduleapi.PxReply
import okhttp3.Call
import okhttp3.Callback
import okhttp3.CookieJar
import okhttp3.Headers
import okhttp3.Cookie
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * expo/fetch on Android with OkHttp. `start` replies with the response head; the body streams as
 * `Fetch.chunk` events and finishes with `Fetch.end`. Cookies live in memory for the app's lifetime.
 */
class FetchModule : PxModule() {
    override val name = "Fetch"

    private val calls = ConcurrentHashMap<Int, Call>()
    private val base: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .cookieJar(MemoryCookieJar())
            .build()
    }

    override fun call(ctx: PxModuleContext, method: String, args: JSONArray, reply: PxReply) {
        when (method) {
            "start" -> start(ctx, args, reply)
            "cancel" -> { calls[args.optInt(0)]?.cancel(); reply(null, null) }
            else -> reply(null, "Fetch has no method '$method'")
        }
    }

    private fun start(ctx: PxModuleContext, args: JSONArray, reply: PxReply) {
        val id = args.getInt(0)
        val redirect = args.optString(5, "follow")
        val request = try {
            val method = args.getString(2)
            val hb = Headers.Builder()
            val pairs = args.optJSONArray(3) ?: JSONArray()
            for (i in 0 until pairs.length()) {
                val p = pairs.getJSONArray(i)
                hb.add(p.getString(0), p.getString(1))
            }
            val headers = hb.build()
            val bytes = if (args.isNull(4)) null else Base64.decode(args.getString(4), Base64.DEFAULT)
            val type = headers["Content-Type"]?.toMediaTypeOrNull()
            val body = bytes?.toRequestBody(type)
                ?: if (method in BODY_METHODS) ByteArray(0).toRequestBody(type) else null
            Request.Builder().url(args.getString(1)).headers(headers).method(method, body).build()
        } catch (e: Exception) {
            reply(null, e.message ?: "invalid request")
            return
        }
        val follow = redirect == "follow"
        val ms = args.optLong(7, 0)
        val b = base.newBuilder().followRedirects(follow).followSslRedirects(follow)
        if (ms > 0) b.callTimeout(ms, TimeUnit.MILLISECONDS)
        if (args.optString(6) == "omit") b.cookieJar(CookieJar.NO_COOKIES)
        val call = b.build().newCall(request)
        calls[id] = call
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                calls.remove(id)
                reply(null, if (call.isCanceled()) "aborted" else (e.message ?: e.toString()))
            }

            override fun onResponse(call: Call, response: Response) {
                response.use { stream(ctx, id, redirect, call, it, reply) }
            }
        })
    }

    /** Runs on an OkHttp thread. */
    private fun stream(ctx: PxModuleContext, id: Int, redirect: String, call: Call, r: Response, reply: PxReply) {
        if (redirect == "error" && r.isRedirect) {
            calls.remove(id)
            reply(null, "redirect refused (redirect='error')")
            return
        }
        val headers = r.headers.map { listOf(it.first, it.second) }
        reply(mapOf(
            "status" to r.code,
            "statusText" to r.message.ifEmpty { reason(r.code) },
            "headers" to headers,
            "url" to r.request.url.toString(),
            "redirected" to (r.priorResponse != null),
        ), null)
        var error: String? = null
        try {
            val source = r.body?.source()
            val buf = ByteArray(65536)
            if (source != null) {
                val input = source.inputStream()
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    if (n > 0) {
                        ctx.emit("Fetch.chunk", mapOf("id" to id, "data" to Base64.encodeToString(buf, 0, n, Base64.NO_WRAP)))
                    }
                }
            }
        } catch (e: IOException) {
            error = if (call.isCanceled()) "aborted" else (e.message ?: e.toString())
        } finally {
            calls.remove(id)
        }
        val end = mutableMapOf<String, Any?>("id" to id)
        if (error != null) {
            end["error"] = error
            if (call.isCanceled()) end["aborted"] = true
        }
        ctx.emit("Fetch.end", end)
    }

    private companion object {
        val BODY_METHODS = setOf("POST", "PUT", "PATCH", "PROPPATCH", "REPORT")
    }

    private fun reason(code: Int): String = when (code) {
        200 -> "OK"; 201 -> "Created"; 202 -> "Accepted"; 204 -> "No Content"; 206 -> "Partial Content"
        301 -> "Moved Permanently"; 302 -> "Found"; 303 -> "See Other"; 304 -> "Not Modified"
        307 -> "Temporary Redirect"; 308 -> "Permanent Redirect"; 400 -> "Bad Request"; 401 -> "Unauthorized"
        403 -> "Forbidden"; 404 -> "Not Found"; 405 -> "Method Not Allowed"; 409 -> "Conflict"; 410 -> "Gone"
        413 -> "Payload Too Large"; 415 -> "Unsupported Media Type"; 422 -> "Unprocessable Entity"
        429 -> "Too Many Requests"; 500 -> "Internal Server Error"; 501 -> "Not Implemented"
        502 -> "Bad Gateway"; 503 -> "Service Unavailable"; 504 -> "Gateway Timeout"
        else -> ""
    }
}

/** Keeps cookies in memory, as a browser session does. */
private class MemoryCookieJar : CookieJar {
    private val store = mutableListOf<Cookie>()

    @Synchronized
    override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
        val now = System.currentTimeMillis()
        for (c in cookies) {
            store.removeAll { it.name == c.name && it.domain == c.domain && it.path == c.path }
            if (c.expiresAt > now) store.add(c)
        }
    }

    @Synchronized
    override fun loadForRequest(url: HttpUrl): List<Cookie> {
        val now = System.currentTimeMillis()
        store.removeAll { it.expiresAt <= now }
        return store.filter { it.matches(url) }
    }
}
