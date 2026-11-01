"""WSGI gzip 中间件。

waitress 本身不做压缩，而考试页一次要拉 katex.min.js（269KB）+ exam.js + style.css，
50 人同时进考场就是十几 MB 的局域网流量。这里在 WSGI 层统一压：

    katex.min.js   269KB → 74KB
    chart.min.js   204KB → 69KB
    style.css       40KB →  8KB

压缩一次约 7ms，所以静态文件的压缩结果按 (路径, ETag) 缓存，第二次直接命中。
"""
import zlib

COMPRESSIBLE_TYPES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "application/xhtml+xml",
    "image/svg+xml",
)

SKIP_STATUS = {204, 304}


class GzipMiddleware:
    def __init__(self, wsgi_app, minimum_size=800, level=6, cache_size=64):
        self.wsgi_app = wsgi_app
        self.minimum_size = minimum_size
        self.level = level
        self.cache_size = cache_size
        self._cache = {}

    # -- WSGI 入口 ---------------------------------------------------------
    def __call__(self, environ, start_response):
        if "gzip" not in (environ.get("HTTP_ACCEPT_ENCODING") or "").lower():
            return self.wsgi_app(environ, start_response)

        captured = {}

        def capture(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers
            captured["exc_info"] = exc_info
            return lambda data: None  # 不支持 write() 直写，本项目不会用到

        app_iter = self.wsgi_app(environ, capture)

        if "status" not in captured or captured.get("exc_info"):
            # 应用没走 start_response，或者正在传播异常，原样交回去
            start_response(captured.get("status", "500 Internal Server Error"),
                           captured.get("headers", []), captured.get("exc_info"))
            return app_iter

        status = captured["status"]
        headers = list(captured["headers"])

        if not self._should_compress(status, headers):
            start_response(status, headers, captured.get("exc_info"))
            return app_iter

        cache_key = self._cache_key(environ, status, headers)
        if cache_key and cache_key in self._cache:
            self._close(app_iter)
            payload = self._cache[cache_key]
            start_response(status, self._rewrite_headers(headers, len(payload)),
                           captured.get("exc_info"))
            return [payload]

        start_response(status, self._rewrite_headers(headers, None),
                       captured.get("exc_info"))
        if cache_key:
            return self._compress_and_cache(app_iter, cache_key)
        return self._compress_iter(app_iter)

    # -- 判定与工具 --------------------------------------------------------
    @staticmethod
    def _close(app_iter) -> None:
        closer = getattr(app_iter, "close", None)
        if closer:
            closer()

    @staticmethod
    def _should_compress(status: str, headers) -> bool:
        try:
            code = int(status.split(" ", 1)[0])
        except (ValueError, IndexError):
            return False
        if code in SKIP_STATUS or code < 200:
            return False

        lowered = {k.lower(): v for k, v in headers}
        if "content-encoding" in lowered:
            return False
        if "no-transform" in (lowered.get("cache-control") or ""):
            return False

        content_type = (lowered.get("content-type") or "").split(";")[0].strip().lower()
        if not content_type.startswith(COMPRESSIBLE_TYPES):
            return False

        try:
            length = int(lowered.get("content-length") or 0)
        except ValueError:
            length = 0
        # 太小压了反而变大；长度未知（流式）时按需压
        return length == 0 or length >= 800

    @staticmethod
    def _rewrite_headers(headers, payload_len):
        out = []
        has_vary = False
        for key, value in headers:
            low = key.lower()
            if low in ("content-length", "content-encoding"):
                continue
            if low == "vary":
                has_vary = True
                if "accept-encoding" not in value.lower():
                    value = value + ", Accept-Encoding"
            out.append((key, value))
        if not has_vary:
            out.append(("Vary", "Accept-Encoding"))
        out.append(("Content-Encoding", "gzip"))
        if payload_len is not None:
            out.append(("Content-Length", str(payload_len)))
        return out

    @staticmethod
    def _cache_key(environ, status, headers):
        path = environ.get("PATH_INFO") or ""
        if not path.startswith("/static/") or not status.startswith("200"):
            return None
        for key, value in headers:
            if key.lower() == "etag":
                return (path, value)
        return None

    # -- 压缩 --------------------------------------------------------------
    def _compressor(self):
        return zlib.compressobj(self.level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)

    def _compress_iter(self, app_iter):
        def generate():
            compressor = self._compressor()
            try:
                for chunk in app_iter:
                    if chunk:
                        yield compressor.compress(chunk)
                yield compressor.flush()
            finally:
                self._close(app_iter)

        return generate()

    def _compress_and_cache(self, app_iter, cache_key):
        def generate():
            compressor = self._compressor()
            buffer = []
            try:
                for chunk in app_iter:
                    if chunk:
                        buffer.append(compressor.compress(chunk))
                buffer.append(compressor.flush())
                payload = b"".join(buffer)
            finally:
                self._close(app_iter)
            if len(self._cache) >= self.cache_size:
                self._cache.clear()
            self._cache[cache_key] = payload
            yield payload

        return generate()
