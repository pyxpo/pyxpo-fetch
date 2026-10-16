import Foundation

/// expo/fetch on iOS: one `URLSession` with a data delegate. `start` replies with the response head; the body
/// streams as `Fetch.chunk` events and finishes with `Fetch.end`.
final class FetchModule: PxModule {
    static let moduleName = "Fetch"

    private let delegate = FetchDelegate()
    private lazy var session: URLSession = {
        URLSession(configuration: .default, delegate: delegate, delegateQueue: delegate.queue)
    }()

    required init() {}

    deinit { session.invalidateAndCancel() }

    func call(_ ctx: PxModuleContext, _ method: String, _ args: [Any], _ reply: @escaping PxReply) {
        switch method {
        case "start": start(ctx, args, reply)
        case "cancel":
            let id = (args.first as? NSNumber)?.intValue ?? 0
            delegate.queue.addOperation { self.delegate.cancel(id) }
            reply(nil, nil)
        default:
            reply(nil, "Fetch has no method '\(method)'")
        }
    }

    private func start(_ ctx: PxModuleContext, _ args: [Any], _ reply: @escaping PxReply) {
        guard args.count >= 8, let id = (args[0] as? NSNumber)?.intValue, let urlString = args[1] as? String,
              let url = URL(string: urlString), let method = args[2] as? String else {
            reply(nil, "invalid fetch arguments")
            return
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        for pair in args[3] as? [[Any]] ?? [] where pair.count == 2 {
            if let k = pair[0] as? String, let v = pair[1] as? String { req.addValue(v, forHTTPHeaderField: k) }
        }
        if let b64 = args[4] as? String {
            guard let data = Data(base64Encoded: b64) else { reply(nil, "invalid request body"); return }
            req.httpBody = data
        }
        let redirect = args[5] as? String ?? "follow"
        req.httpShouldHandleCookies = (args[6] as? String) != "omit"
        let ms = (args[7] as? NSNumber)?.doubleValue ?? 0
        if ms > 0 { req.timeoutInterval = ms / 1000 }
        let task = session.dataTask(with: req)
        let st = FetchDelegate.Request(id: id, redirect: redirect, ctx: ctx, reply: reply, task: task)
        delegate.queue.addOperation {
            self.delegate.add(st)
            task.resume()
        }
    }
}

/// All state lives on `queue` (serial).
final class FetchDelegate: NSObject, URLSessionDataDelegate {
    final class Request {
        let id: Int
        let redirect: String
        weak var ctx: PxModuleContext?
        var reply: PxReply?
        let task: URLSessionDataTask
        var redirected = false
        var aborted = false
        var failure: String?

        init(id: Int, redirect: String, ctx: PxModuleContext, reply: @escaping PxReply, task: URLSessionDataTask) {
            self.id = id
            self.redirect = redirect
            self.ctx = ctx
            self.reply = reply
            self.task = task
        }
    }

    let queue: OperationQueue = {
        let q = OperationQueue()
        q.maxConcurrentOperationCount = 1
        q.name = "dev.pyxpo.fetch"
        return q
    }()
    private var byTask: [Int: Request] = [:]
    private var byId: [Int: Request] = [:]

    func add(_ r: Request) {
        byTask[r.task.taskIdentifier] = r
        byId[r.id] = r
    }

    func cancel(_ id: Int) {
        guard let r = byId[id] else { return }
        r.aborted = true
        r.task.cancel()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let r = byTask[task.taskIdentifier] else { completionHandler(request); return }
        switch r.redirect {
        case "manual":
            completionHandler(nil)  // the 3xx response is delivered as the final response
        case "error":
            r.failure = "redirect refused (redirect='error')"
            completionHandler(nil)
            task.cancel()
        default:
            r.redirected = true
            completionHandler(request)
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let r = byTask[dataTask.taskIdentifier], r.failure == nil, let reply = r.reply else {
            completionHandler(.cancel)
            return
        }
        r.reply = nil
        let http = response as? HTTPURLResponse
        let status = http?.statusCode ?? 200
        var headers: [[String]] = []
        for (k, v) in http?.allHeaderFields ?? [:] {
            if let k = k as? String { headers.append([k, "\(v)"]) }
        }
        reply([
            "status": status,
            "statusText": FetchDelegate.reason(status),
            "headers": headers,
            "url": response.url?.absoluteString ?? dataTask.originalRequest?.url?.absoluteString ?? "",
            "redirected": r.redirected,
        ], nil)
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard let r = byTask[dataTask.taskIdentifier], !r.aborted else { return }
        r.ctx?.emit("Fetch.chunk", ["id": r.id, "data": data.base64EncodedString()])
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let r = byTask.removeValue(forKey: task.taskIdentifier) else { return }
        byId[r.id] = nil
        let message: String? = r.failure ?? (r.aborted ? "aborted" : error?.localizedDescription)
        if let reply = r.reply {
            r.reply = nil
            reply(nil, message ?? "no response")
            return
        }
        var end: [String: Any] = ["id": r.id]
        if let m = message {
            end["error"] = m
            if r.aborted { end["aborted"] = true }
        }
        r.ctx?.emit("Fetch.end", end)
    }

    /// HTTP reason phrases; `HTTPURLResponse.localizedString` gives lowercase descriptions instead.
    static func reason(_ code: Int) -> String {
        let table: [Int: String] = [
            200: "OK", 201: "Created", 202: "Accepted", 204: "No Content", 206: "Partial Content",
            301: "Moved Permanently", 302: "Found", 303: "See Other", 304: "Not Modified",
            307: "Temporary Redirect", 308: "Permanent Redirect", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 410: "Gone",
            413: "Payload Too Large", 415: "Unsupported Media Type", 422: "Unprocessable Entity",
            429: "Too Many Requests", 500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway",
            503: "Service Unavailable", 504: "Gateway Timeout",
        ]
        return table[code] ?? HTTPURLResponse.localizedString(forStatusCode: code).capitalized
    }
}
