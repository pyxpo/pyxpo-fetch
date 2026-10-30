# Changelog

## 0.1.0a2 (2026-10-30)

- Now needs pyxpo 0.1.0a4 or newer, the first release with the web target.
- New: `create_client`, an axios-style HTTP client on top of `fetch`. It has a base URL, default headers and params,
  request and response interceptors (sync or async), parsed JSON in `res.data`, `HTTPError` for error statuses,
  `TimeoutError` apart from `AbortError`, retries with backoff and `Retry-After`, form bodies, response types,
  download progress, `extend()` and a swappable transport.
- Web: requests now use the browser's `fetch`, with streamed bodies, abort and timeout. They used to fail with `FetchError`.
- Moved to its own repository, github.com/pyxpo/pyxpo-fetch, with CI on Python 3.9 and 3.12 and releases from tags.
- Added a security policy, a contributing guide and issue templates.

## 0.1.0a1

- Docs: detailed README with screenshots, and fuller PyPI metadata (keywords, classifiers, project URLs).
- First release.
