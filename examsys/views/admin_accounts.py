"""后台：管理员账号、角色与密码。

这一页就是「组织与权限管理」：给账号分配角色和组织、重置密码、删除账号。
所有动作都要求 ``org.manage`` 权限（目前只有超级管理员有），并在审计日志留痕。
"""
from flask import flash, redirect, render_template, request, session, url_for

from .. import rbac
from ..db import get_db, write_txn
from ..security import hash_password, login_required_admin, verify_password
from ..services import audit
from ..utils import fmt_ts


@login_required_admin
def admin_password():
    admin = get_db().execute(
        "SELECT * FROM admins WHERE id=?", (session["admin_id"],)
    ).fetchone()
    if request.method == "POST":
        old_pw = request.form.get("old_password") or ""
        new_pw = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not verify_password(admin["password_hash"], old_pw):
            flash("当前密码不正确。", "error")
        elif len(new_pw) < 6:
            flash("新密码至少 6 位。", "error")
        elif new_pw != confirm:
            flash("两次输入的新密码不一致。", "error")
        else:
            # 改密码即踢掉其它设备：版本 +1，同时清掉「必须改密」标记
            new_sv = int(admin["session_version"] or 0) + 1
            with write_txn() as db:
                db.execute(
                    "UPDATE admins SET password_hash=?, session_version=?,"
                    " must_change_password=0 WHERE id=?",
                    (hash_password(new_pw), new_sv, admin["id"]),
                )
                audit.log("admin.change_password", target_type="admin",
                          target_id=admin["id"])
            session["sv"] = new_sv
            session["must_change_pwd"] = False
            flash("管理员密码已修改。", "success")
            return redirect(url_for("admin_password"))
    return render_template("admin/password.html", username=admin["username"],
                           must_change=bool(admin["must_change_password"]))


@rbac.require_perm("org.manage")
def admin_admins():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "reset":
            aid = request.form.get("admin_id", type=int)
            pw = request.form.get("password") or ""
            if aid and len(pw) >= 6:
                with write_txn() as txn:
                    # 重置密码 = 旧会话全部失效 + 下次登录必须先改密码
                    txn.execute(
                        "UPDATE admins SET password_hash=?, must_change_password=1,"
                        " session_version=session_version+1 WHERE id=?", 
                        (hash_password(pw), aid))
                    audit.log("admin.reset_password", target_type="admin",
                              target_id=aid)
                flash("密码已重置，对方下次登录需修改密码。", "success")
            else:
                flash("密码至少 6 位。", "error")
        elif action == "rename":
            aid = request.form.get("admin_id", type=int)
            username = (request.form.get("username") or "").strip()
            if aid and username:
                if db.execute("SELECT id FROM admins WHERE username=? AND id!=?",
                              (username, aid)).fetchone():
                    flash("用户名已被占用。", "error")
                else:
                    with write_txn() as txn:
                        txn.execute("UPDATE admins SET username=? WHERE id=?",
                                    (username, aid))
                        audit.log("admin.rename", target_type="admin", target_id=aid,
                                  detail="新用户名 %s" % username)
                    flash("用户名已修改。", "success")
        elif action == "set_role":
            aid = request.form.get("admin_id", type=int)
            role_code = request.form.get("role_code") or ""
            org_id = request.form.get("org_id", type=int)
            if aid == session["admin_id"]:
                flash("不能修改自己的角色，请让其他超级管理员操作。", "error")
            elif not aid or role_code not in rbac.ADMIN_ROLES:
                flash("请选择有效角色。", "error")
            else:
                with write_txn() as txn:
                    rbac.assign_role(txn, aid, role_code)
                    if org_id:
                        txn.execute("UPDATE admins SET org_id=? WHERE id=?",
                                    (org_id, aid))
                    audit.log("admin.set_role", target_type="admin", target_id=aid,
                              detail="角色=%s 组织=%s" % (role_code, org_id or "-"))
                flash("角色已更新。", "success")
        else:  # create
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            role_code = request.form.get("role_code") or rbac.ROLE_TEACHER
            org_id = request.form.get("org_id", type=int) or 1
            if not username or len(password) < 6:
                flash("请填写用户名和至少 6 位密码。", "error")
            elif role_code not in rbac.ADMIN_ROLES:
                flash("请选择有效角色。", "error")
            elif db.execute("SELECT id FROM admins WHERE username=?", (username,)).fetchone():
                flash("用户名已存在。", "error")
            else:
                with write_txn() as txn:
                    cur = txn.execute(
                        "INSERT INTO admins (username, password_hash, org_id,"
                        " must_change_password) VALUES (?, ?, ?, 1)",
                        (username, hash_password(password), org_id))
                    rbac.assign_role(txn, cur.lastrowid, role_code)
                    audit.log("admin.create", target_type="admin",
                              target_id=cur.lastrowid,
                              detail="角色=%s 组织=%s" % (role_code, org_id))
                flash("账号已创建，对方首次登录需修改密码。", "success")
        return redirect(url_for("admin_admins"))

    admins = db.execute(
        "SELECT a.id, a.username, a.org_id, a.role_id, a.enabled,"
        "       a.must_change_password, a.last_login_at,"
        "       r.code AS role_code, r.name AS role_name, o.name AS org_name"
        "  FROM admins a"
        "  LEFT JOIN roles r ON r.id = a.role_id"
        "  LEFT JOIN organizations o ON o.id = a.org_id"
        " ORDER BY a.id").fetchall()
    roles = db.execute(
        "SELECT * FROM roles WHERE code != ? ORDER BY sort_order",
        (rbac.ROLE_STUDENT,)).fetchall()
    orgs = db.execute("SELECT id, name FROM organizations ORDER BY id").fetchall()
    return render_template("admin/admins.html", admins=admins, me=session["admin_id"],
                           roles=roles, orgs=orgs, fmt_ts=fmt_ts,
                           permissions=rbac.PERMISSIONS,
                           perm_map=rbac.ROLE_PERMISSIONS)


@rbac.require_perm("org.manage")
def admin_delete_admin(aid):
    if aid == session["admin_id"]:
        flash("不能删除当前登录的账号。", "error")
        return redirect(url_for("admin_admins"))
    db = get_db()
    row = db.execute("SELECT username FROM admins WHERE id=?", (aid,)).fetchone()
    if not row:
        flash("账号不存在。", "error")
        return redirect(url_for("admin_admins"))
    if db.execute("SELECT COUNT(*) c FROM admins WHERE enabled=1").fetchone()["c"] <= 1:
        flash("至少保留一个管理员账号。", "error")
        return redirect(url_for("admin_admins"))
    with write_txn() as txn:
        txn.execute("DELETE FROM admins WHERE id=?", (aid,))
        audit.log("admin.delete", target_type="admin", target_id=aid,
                  detail=row["username"])
    flash("管理员账号已删除。", "success")
    return redirect(url_for("admin_admins"))


@rbac.require_perm("system.audit")
def admin_audit():
    """后台操作审计：谁在什么时候动了什么。"""
    org = rbac.visible_org_id(rbac.current_admin())
    action = (request.args.get("action") or "").strip()
    rows = audit.recent(limit=300, org_id=org, action=action)
    return render_template("admin/audit.html", rows=rows, fmt_ts=fmt_ts,
                           action=action)


def register(app):
    app.add_url_rule("/admin/password", "admin_password", admin_password,
                     methods=["GET", "POST"])
    app.add_url_rule("/admin/admins", "admin_admins", admin_admins,
                     methods=["GET", "POST"])
    app.add_url_rule("/admin/admins/<int:aid>/delete", "admin_delete_admin",
                     admin_delete_admin, methods=["POST"])
    app.add_url_rule("/admin/audit", "admin_audit", admin_audit)
