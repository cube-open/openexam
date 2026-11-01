"""登录态装饰器与当前用户读取。

登录校验集中在这里，对外提供两类东西：

- ``student_gate()`` / ``admin_gate()``：返回 ``None`` 表示放行，否则返回该直接
  返回的响应。装饰器用它，RBAC 的 ``require_perm`` 也用它，避免逻辑分叉。
- ``login_required_student`` / ``login_required_admin``：装饰器外壳。

会话安全
--------
``session_version`` 存在账号表上，登录时写进 session，每个请求比对一次。
改密码 / 管理员重置密码 / 强制下线时把版本 +1，其它设备上的旧会话立即失效——
这就是「多设备登录冲突」的服务端处理方式，不依赖前端。
"""
import functools

from flask import flash, jsonify, redirect, request, session, url_for

from ..db import get_db


def current_student():
    sid = session.get("student_id")
    if not sid:
        return None
    return get_db().execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()


def _wants_json() -> bool:
    return request.path.startswith("/api/")


def _reject(msg: str, endpoint: str, status: int = 401):
    if _wants_json():
        return jsonify({"ok": False, "msg": msg}), status
    return redirect(url_for(endpoint))


def student_gate():
    """学生端登录校验。返回 None 表示放行。"""
    sid = session.get("student_id")
    if not sid:
        return _reject("未登录", "login")
    row = get_db().execute(
        "SELECT enabled, session_version FROM students WHERE id=?", (sid,)
    ).fetchone()
    if not row or not row["enabled"]:
        session.clear()
        flash("账号已被禁用，请联系管理员。", "error")
        return _reject("账号已被禁用", "login")
    if int(row["session_version"] or 0) != int(session.get("sv") or 0):
        session.clear()
        flash("账号已在其他设备登录或密码已变更，请重新登录。", "error")
        return _reject("会话已失效", "login")
    return None


def admin_gate():
    """后台登录校验。返回 None 表示放行。"""
    aid = session.get("admin_id")
    if not aid:
        return _reject("未登录", "admin_login")
    row = get_db().execute(
        "SELECT enabled, session_version FROM admins WHERE id=?", (aid,)
    ).fetchone()
    if not row or not row["enabled"]:
        session.clear()
        flash("账号已被禁用，请联系超级管理员。", "error")
        return _reject("账号已被禁用", "admin_login")
    if int(row["session_version"] or 0) != int(session.get("sv") or 0):
        session.clear()
        flash("账号已在其他设备登录或密码已变更，请重新登录。", "error")
        return _reject("会话已失效", "admin_login")
    return None


def login_required_student(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        blocked = student_gate()
        if blocked is not None:
            return blocked
        return view(*args, **kwargs)

    return wrapped


def login_required_admin(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        blocked = admin_gate()
        if blocked is not None:
            return blocked
        return view(*args, **kwargs)

    return wrapped
