"""登录 / 登出（学生端 + 管理端）。

会话安全的关键点
----------------
登录时把账号的 ``session_version`` 写进 session，每个请求由 ``security.decorators``
比对一次。登录成功会把版本 +1（``Config.ENFORCE_SINGLE_SESSION``），于是**同一账号
在别的设备上的旧会话立即失效**——这是防代考的第一道服务端约束，不依赖前端。
改密码、管理员重置密码同样 +1，效果相同。
"""
from flask import flash, redirect, render_template, request, session, url_for

from ..config import Config
from ..db import get_db, write_txn
from ..security import hash_password, login_limiter, needs_rehash, verify_password
from ..services import audit
from ..utils import now_epoch


def index():
    if session.get("student_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


def _next_version(row) -> int:
    """单点登录开启时每次登录 +1，踢掉旧设备；否则保持原版本。"""
    current = int(row["session_version"] or 0)
    return current + 1 if Config.ENFORCE_SINGLE_SESSION else current


def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        ip = request.remote_addr or ""
        # 按「IP + 用户名」限流：只统计失败次数，避免同出口 NAT 下互相影响
        key = "%s|%s" % (ip, username.lower())
        if login_limiter.exceeded(key):
            flash("尝试次数过多，请稍后再试。", "error")
            return render_template("login.html"), 429

        stu = get_db().execute(
            "SELECT * FROM students WHERE username=?", (username,)
        ).fetchone()

        if not stu or not verify_password(stu["password_hash"], password):
            login_limiter.hit(key)
            flash("用户名或密码错误。", "error")
            return render_template("login.html")

        if not stu["enabled"]:
            flash("该账号已被禁用，请联系管理员。", "error")
            return render_template("login.html")

        session.clear()
        now = now_epoch()
        version = _next_version(stu)
        with write_txn() as db:
            db.execute(
                "UPDATE students SET last_ip=?, last_login_at=?, session_version=?"
                " WHERE id=?",
                (ip, now, version, stu["id"]),
            )
            db.execute(
                "INSERT INTO login_logs (student_id, username, name, ip, login_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (stu["id"], stu["username"], stu["name"], ip, now),
            )
            # 老库里的 scrypt 哈希顺手升级成当前算法（一次性）
            if needs_rehash(stu["password_hash"]):
                db.execute("UPDATE students SET password_hash=? WHERE id=?",
                           (hash_password(password), stu["id"]))

        session["student_id"] = stu["id"]
        session["student_name"] = stu["name"]
        session["sv"] = version
        session["must_change_pwd"] = bool(stu["must_change_password"])
        login_limiter.reset(key)
        if session["must_change_pwd"]:
            flash("首次登录请先修改初始密码。", "error")
        return redirect(url_for("dashboard"))
    return render_template("login.html")


def logout():
    session.clear()
    return redirect(url_for("login"))


def admin_login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        ip = request.remote_addr or ""
        key = "admin|%s|%s" % (ip, username.lower())
        if login_limiter.exceeded(key):
            flash("尝试次数过多，请稍后再试。", "error")
            return render_template("admin/login.html"), 429

        admin = get_db().execute(
            "SELECT a.*, r.code AS role_code FROM admins a"
            " LEFT JOIN roles r ON r.id = a.role_id WHERE a.username=?",
            (username,),
        ).fetchone()

        if not admin or not verify_password(admin["password_hash"], password):
            login_limiter.hit(key)
            flash("用户名或密码错误。", "error")
            return render_template("admin/login.html")

        if not admin["enabled"]:
            flash("该账号已被禁用，请联系超级管理员。", "error")
            return render_template("admin/login.html")

        version = _next_version(admin)
        with write_txn() as db:
            db.execute(
                "UPDATE admins SET session_version=?, last_login_at=? WHERE id=?",
                (version, now_epoch(), admin["id"]),
            )

        session.clear()
        session["admin_id"] = admin["id"]
        session["admin_name"] = admin["username"]
        session["sv"] = version
        session["must_change_pwd"] = bool(admin["must_change_password"])
        session["admin_role"] = admin["role_code"] or ""
        login_limiter.reset(key)
        audit.log("auth.admin_login", target_type="admin", target_id=admin["id"],
                  detail="角色 %s" % (admin["role_code"] or "-"))
        if session["must_change_pwd"]:
            flash("首次登录请先修改初始密码。", "error")
            return redirect(url_for("admin_password"))
        return redirect(url_for("admin_dashboard"))
    return render_template("admin/login.html")


def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


def register(app):
    app.add_url_rule("/", "index", index)
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "logout", logout)
    app.add_url_rule("/admin/login", "admin_login", admin_login, methods=["GET", "POST"])
    app.add_url_rule("/admin/logout", "admin_logout", admin_logout)
