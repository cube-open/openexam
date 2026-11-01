"""请求中间件层。

Flask 没有 Django 那样的 MIDDLEWARE 列表，请求前后的通用逻辑就是
`before_request` / `after_request` / `teardown_appcontext` 三组钩子。
这里集中放所有"每个请求都要过一遍"的东西：

- 写请求的同源校验（轻量 CSRF 防线）
- 学生端的 IP 封禁拦截
- 安全响应头
- 慢请求日志
- 请求结束时归还数据库连接
"""
import time
from urllib.parse import urlsplit

from flask import current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from .db import close_db
from .security import is_ip_blocked

# 后台与静态资源不受 IP 封禁影响，避免管理员把自己锁在外面
IP_BAN_EXEMPT_PREFIXES = ("/static/", "/admin", "/healthz")

SLOW_REQUEST_MS = 500

# 必须改初始密码时，仍然放行的页面（否则用户没法改密码）
PWD_CHANGE_ALLOWED = {
    "static", "profile", "logout",
    "admin_password", "admin_logout",
}


def _same_host(url: str) -> bool:
    if not url:
        return True
    try:
        netloc = urlsplit(url).netloc.lower()
    except ValueError:
        return False
    if not netloc:
        return True
    return netloc == (request.host or "").lower()


def same_origin_ok() -> bool:
    """POST/PUT/PATCH/DELETE 校验 Origin / Referer。

    浏览器跨站提交表单会带 Origin，缺失时不拦截（部分老客户端），
    配合 SameSite=Lax 已经能挡住绝大多数 CSRF。
    """
    origin = request.headers.get("Origin")
    if origin and not _same_host(origin):
        return False
    referer = request.headers.get("Referer")
    if not origin and referer and not _same_host(referer):
        return False
    return True


def guard_request():
    """请求前置：同源校验 → IP 封禁 → 首登改密。"""
    g.request_started = time.perf_counter()

    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not same_origin_ok():
        current_app.logger.warning(
            "blocked cross-origin %s %s from %s",
            request.method, request.path, request.remote_addr)
        return jsonify({"ok": False, "msg": "非法请求来源"}), 403

    path = request.path
    if path.startswith(IP_BAN_EXEMPT_PREFIXES):
        return _force_password_change()
    ip = request.remote_addr or ""
    if ip and is_ip_blocked(ip):
        return render_template("blocked.html", ip=ip), 403
    return _force_password_change()


def _force_password_change():
    """初始密码没改之前，除了改密页哪儿都去不了。

    标志在登录时写进 session，所以这里不查库、零开销；改完密码会重新登录。
    """
    if not session.get("must_change_pwd"):
        return None
    endpoint = request.endpoint or ""
    if endpoint in PWD_CHANGE_ALLOWED or request.path.startswith("/static/"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "msg": "请先修改初始密码"}), 403
    target = "admin_password" if session.get("admin_id") else "profile"
    flash("请先修改初始密码，再使用其他功能。", "error")
    return redirect(url_for(target))


def finalize_response(response):
    """请求后置：安全响应头 + 慢请求日志。"""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")

    started = g.get("request_started")
    if started and not request.path.startswith("/static/"):
        cost_ms = (time.perf_counter() - started) * 1000
        if cost_ms > SLOW_REQUEST_MS:
            current_app.logger.warning(
                "slow %s %s %.0fms", request.method, request.path, cost_ms)
    return response


def install(app) -> None:
    app.before_request(guard_request)
    app.after_request(finalize_response)
    app.teardown_appcontext(close_db)
