"""后台：学生账号、分组、知识点、IP 封禁、登录日志。"""
from flask import abort, flash, redirect, render_template, request, url_for

from .. import rbac
from ..db import get_db, write_txn
from ..security import hash_many, hash_password, invalidate_blocked_ips
from ..services import audit
from ..services.importers import parse_students
from ..utils import fmt_ts, now_epoch


@rbac.require_perm("user.view")
def admin_students():
    db = get_db()
    if request.method == "POST":
        # 查看权限 ≠ 改动权限：写操作在这里二次校验，不能靠前端藏按钮
        if not rbac.has_perm(rbac.current_admin(), "user.manage"):
            abort(403)
        action = request.form.get("action")
        if action == "create":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            name = (request.form.get("name") or "").strip()
            student_no = (request.form.get("student_no") or "").strip()
            if not all([username, password, name, student_no]):
                flash("请填写完整信息。", "error")
            elif db.execute("SELECT id FROM students WHERE username=? OR student_no=?",
                            (username, student_no)).fetchone():
                flash("用户名或学号已存在。", "error")
            else:
                with write_txn() as txn:
                    cur = txn.execute(
                        "INSERT INTO students (username, password_hash, name, student_no,"
                        " org_id, must_change_password) VALUES (?, ?, ?, ?, ?, 1)",
                        (username, hash_password(password), name, student_no,
                         rbac.visible_org_id(rbac.current_admin()) or 1))
                    audit.log("user.create", target_type="student",
                              target_id=cur.lastrowid, detail=name)
                flash("学生账号已创建，首次登录需修改密码。", "success")
        elif action == "reset":
            sid = request.form.get("student_id", type=int)
            new_pw = request.form.get("password") or ""
            if sid and new_pw:
                with write_txn() as txn:
                    txn.execute("UPDATE students SET password_hash=? WHERE id=?",
                                (hash_password(new_pw), sid))
                flash("密码已重置。", "success")
        elif action == "import":
            inserted, skipped, errors = _bulk_create_students(
                db, (request.form.get("bulk") or ""))
            flash("导入完成：新增 %d 人，跳过 %d 人（重复）" % (inserted, skipped)
                  + ("，错误 %d 行" % len(errors) if errors else "") + "。", "success")
            if errors:
                flash("；".join(errors[:5]) + ("…" if len(errors) > 5 else ""), "error")
        return redirect(url_for("admin_students"))

    org = rbac.visible_org_id(rbac.current_admin())
    org_where, org_params = rbac.org_scope(rbac.current_admin(), "s.org_id")
    students = db.execute(
        "SELECT s.*, COALESCE(x.done, 0) AS done FROM students s "
        "LEFT JOIN (SELECT student_id, COUNT(*) AS done FROM results "
        "           WHERE submitted_at IS NOT NULL GROUP BY student_id) x "
        "       ON x.student_id = s.id "
        "WHERE 1=1" + org_where + " "
        "ORDER BY s.id", org_params
    ).fetchall()
    groups = db.execute(
        "SELECT * FROM groups" + ("" if org is None else " WHERE org_id=?") + " ORDER BY id",
        () if org is None else (org,)).fetchall()
    return render_template("admin/students.html", students=students, all_groups=groups)


def _bulk_create_students(db, text):
    """解析并批量建号。已存在的用户名/学号跳过。"""
    students, errors = parse_students(text)
    if not students:
        return 0, 0, errors

    existing = set()
    for row in db.execute("SELECT username, student_no FROM students").fetchall():
        existing.add(row["username"])
        existing.add(row["student_no"])

    # 一次哈希（并发），一次 executemany 落库
    hashes = hash_many([pwd for _, pwd, _, _ in students])
    org_id = rbac.visible_org_id(rbac.current_admin()) or 1
    rows = []
    skipped = 0
    seen = set()
    for username, password, name, student_no in students:
        if username in existing or student_no in existing or username in seen:
            skipped += 1
            continue
        seen.add(username)
        rows.append((username, hashes[password], name, student_no, org_id))

    if rows:
        with write_txn() as txn:
            txn.executemany(
                "INSERT OR IGNORE INTO students (username, password_hash, name,"
                " student_no, org_id, must_change_password) VALUES (?, ?, ?, ?, ?, 1)",
                rows)
            audit.log("user.import", detail="新增 %d 名学生" % len(rows))
    return len(rows), skipped, errors


@rbac.require_perm("user.manage")
def admin_delete_student(sid):
    with write_txn() as db:
        db.execute("DELETE FROM results WHERE student_id=?", (sid,))
        db.execute("DELETE FROM messages WHERE student_id=?", (sid,))
        db.execute("DELETE FROM students WHERE id=?", (sid,))
        audit.log("user.delete", target_type="student", target_id=sid)
    flash("学生账号已删除。", "success")
    return redirect(url_for("admin_students"))


@rbac.require_perm("user.manage")
def admin_toggle_student(sid):
    db = get_db()
    row = db.execute("SELECT enabled FROM students WHERE id=?", (sid,)).fetchone()
    if row:
        with write_txn() as txn:
            txn.execute("UPDATE students SET enabled=? WHERE id=?",
                        (0 if row["enabled"] else 1, sid))
            audit.log("user.enable" if not row["enabled"] else "user.disable",
                      target_type="student", target_id=sid)
    return redirect(url_for("admin_students"))


@rbac.require_perm("user.manage")
def admin_students_quick():
    """快速批量添加：每行一个姓名，用户名=姓名，默认密码 123456，学号自动生成。"""
    raw = request.form.get("names") or ""
    names, seen = [], set()
    for line in raw.splitlines():
        name = line.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        flash("请输入至少一个学生姓名。", "error")
        return redirect(url_for("admin_students"))

    db = get_db()
    default_hash = hash_password("123456")
    stamp = now_epoch()
    org_id = rbac.visible_org_id(rbac.current_admin()) or 1
    with write_txn() as txn:
        inserted = skipped = 0
        for idx, name in enumerate(names):
            if txn.execute("SELECT id FROM students WHERE username=?", (name,)).fetchone():
                skipped += 1
                continue
            cur = txn.execute(
                "INSERT INTO students (username, password_hash, name, student_no,"
                " org_id, must_change_password) VALUES (?, ?, ?, ?, ?, 1)",
                (name, default_hash, name, "__tmp%d_%d" % (idx, stamp), org_id))
            txn.execute("UPDATE students SET student_no=? WHERE id=?",
                        (str(cur.lastrowid), cur.lastrowid))
            inserted += 1
        audit.log("user.quick_create", detail="新增 %d 名学生（默认密码）" % inserted)
    flash("已添加 %d 名学生（用户名=姓名，默认密码 123456）" % inserted
          + ("，跳过 %d 个重名" % skipped if skipped else "") + "。", "success")
    return redirect(url_for("admin_students"))


@rbac.require_perm("user.manage")
def admin_students_batch():
    """批量操作：改密码 / 禁用 / 启用 / 删除 / 分组。"""
    action = request.form.get("action")
    ids = [int(x) for x in (request.form.get("ids") or "").split(",") if x.strip().isdigit()]
    if not ids:
        flash("请先勾选学生。", "error")
        return redirect(url_for("admin_students"))

    placeholders = ",".join("?" * len(ids))
    if action == "reset":
        pw = request.form.get("password") or ""
        if len(pw) < 6:
            flash("新密码至少 6 位。", "error")
        else:
            digest = hash_password(pw)
            with write_txn() as db:
                db.executemany("UPDATE students SET password_hash=? WHERE id=?",
                               [(digest, sid) for sid in ids])
            flash("已重置 %d 名学生的密码。" % len(ids), "success")
    elif action == "disable":
        with write_txn() as db:
            db.execute("UPDATE students SET enabled=0 WHERE id IN (%s)" % placeholders, ids)
        flash("已禁用 %d 名学生。" % len(ids), "success")
    elif action == "enable":
        with write_txn() as db:
            db.execute("UPDATE students SET enabled=1 WHERE id IN (%s)" % placeholders, ids)
        flash("已启用 %d 名学生。" % len(ids), "success")
    elif action == "delete":
        with write_txn() as db:
            db.execute("DELETE FROM results WHERE student_id IN (%s)" % placeholders, ids)
            db.execute("DELETE FROM students WHERE id IN (%s)" % placeholders, ids)
        flash("已删除 %d 名学生及其成绩。" % len(ids), "success")
    elif action == "group":
        gid = request.form.get("group_id", type=int)
        with write_txn() as db:
            db.execute("UPDATE students SET group_id=? WHERE id IN (%s)" % placeholders,
                       [gid or None] + ids)
        flash("已为 %d 名学生设置分组。" % len(ids), "success")
    else:
        flash("未知操作。", "error")
    return redirect(url_for("admin_students"))


@rbac.require_perm("user.manage")
def admin_set_student_group(sid):
    gid = request.form.get("group_id", type=int)
    db = get_db()
    if gid and not db.execute("SELECT id FROM groups WHERE id=?", (gid,)).fetchone():
        flash("分组不存在。", "error")
        return redirect(url_for("admin_students"))
    with write_txn() as txn:
        txn.execute("UPDATE students SET group_id=? WHERE id=?", (gid or None, sid))
    return redirect(url_for("admin_students"))


@rbac.require_perm("user.view")
def admin_groups():
    db = get_db()
    if request.method == "POST":
        if not rbac.has_perm(rbac.current_admin(), "user.manage"):
            abort(403)
        action = request.form.get("action")
        if action == "create":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("请输入分组名。", "error")
            elif db.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone():
                flash("分组名已存在。", "error")
            else:
                with write_txn() as txn:
                    txn.execute("INSERT INTO groups (name, org_id) VALUES (?, ?)",
                                (name, rbac.visible_org_id(rbac.current_admin()) or 1))
                flash("分组已创建。", "success")
        elif action == "rename":
            gid = request.form.get("group_id", type=int)
            name = (request.form.get("name") or "").strip()
            if gid and name:
                if db.execute("SELECT id FROM groups WHERE name=? AND id!=?",
                              (name, gid)).fetchone():
                    flash("分组名已存在。", "error")
                else:
                    with write_txn() as txn:
                        txn.execute("UPDATE groups SET name=? WHERE id=?", (name, gid))
                    flash("已重命名。", "success")
        elif action == "delete":
            gid = request.form.get("group_id", type=int)
            if gid:
                with write_txn() as txn:
                    txn.execute("UPDATE students SET group_id=NULL WHERE group_id=?", (gid,))
                    txn.execute("DELETE FROM exam_groups WHERE group_id=?", (gid,))
                    txn.execute("DELETE FROM groups WHERE id=?", (gid,))
                flash("分组已删除，其学生已移入未分组。", "success")
        return redirect(url_for("admin_groups"))

    groups = db.execute(
        "SELECT g.*, (SELECT COUNT(*) FROM students s WHERE s.group_id=g.id) AS cnt "
        "FROM groups g ORDER BY g.id"
    ).fetchall()
    ungrouped = db.execute(
        "SELECT COUNT(*) c FROM students WHERE group_id IS NULL").fetchone()["c"]
    return render_template("admin/groups.html", groups=groups, ungrouped=ungrouped)


@rbac.require_perm("bank.manage")
def admin_knowledge_points():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("请输入知识点名称。", "error")
            elif db.execute("SELECT id FROM knowledge_points WHERE name=?", (name,)).fetchone():
                flash("知识点已存在。", "error")
            else:
                with write_txn() as txn:
                    txn.execute("INSERT INTO knowledge_points (name) VALUES (?)", (name,))
                flash("知识点已创建。", "success")
        elif action == "rename":
            kpid = request.form.get("kp_id", type=int)
            name = (request.form.get("name") or "").strip()
            if kpid and name:
                if db.execute("SELECT id FROM knowledge_points WHERE name=? AND id!=?",
                              (name, kpid)).fetchone():
                    flash("知识点名称已存在。", "error")
                else:
                    with write_txn() as txn:
                        txn.execute("UPDATE knowledge_points SET name=? WHERE id=?",
                                    (name, kpid))
                    flash("已重命名。", "success")
        elif action == "delete":
            kpid = request.form.get("kp_id", type=int)
            if kpid:
                with write_txn() as txn:
                    txn.execute("DELETE FROM question_kp WHERE kp_id=?", (kpid,))
                    txn.execute("DELETE FROM knowledge_points WHERE id=?", (kpid,))
                flash("知识点已删除。", "success")
        return redirect(url_for("admin_knowledge_points"))

    kps = db.execute(
        "SELECT kp.*, (SELECT COUNT(*) FROM question_kp q WHERE q.kp_id=kp.id) AS cnt "
        "FROM knowledge_points kp ORDER BY kp.id"
    ).fetchall()
    return render_template("admin/knowledge_points.html", kps=kps)


@rbac.require_perm("system.settings")
def admin_ips():
    rows = get_db().execute(
        "SELECT * FROM blocked_ips ORDER BY created_at DESC").fetchall()
    return render_template("admin/ips.html", rows=rows)


@rbac.require_perm("system.settings")
def admin_block_ip():
    ip = (request.form.get("ip") or "").strip()
    remark = (request.form.get("remark") or "").strip()
    if ip:
        with write_txn() as db:
            db.execute(
                "INSERT INTO blocked_ips (ip, remark) VALUES (?, ?) "
                "ON CONFLICT(ip) DO UPDATE SET remark=excluded.remark", (ip, remark))
        invalidate_blocked_ips()
        flash("IP %s 已封禁。" % ip, "success")
    return redirect(request.form.get("back") or url_for("admin_ips"))


@rbac.require_perm("system.settings")
def admin_update_remark():
    ip = (request.form.get("ip") or "").strip()
    remark = (request.form.get("remark") or "").strip()
    if ip:
        with write_txn() as db:
            db.execute("UPDATE blocked_ips SET remark=? WHERE ip=?", (remark, ip))
        invalidate_blocked_ips()
        flash("备注已更新。", "success")
    return redirect(url_for("admin_ips"))


@rbac.require_perm("system.settings")
def admin_unblock_ip():
    ip = (request.form.get("ip") or "").strip()
    if ip:
        with write_txn() as db:
            db.execute("DELETE FROM blocked_ips WHERE ip=?", (ip,))
        invalidate_blocked_ips()
        flash("IP %s 已解除封禁。" % ip, "success")
    return redirect(url_for("admin_ips"))


@rbac.require_perm("system.audit")
def admin_logins():
    rows = get_db().execute(
        "SELECT * FROM login_logs ORDER BY login_at DESC, id DESC LIMIT 1000").fetchall()
    return render_template("admin/logins.html", rows=rows, fmt_ts=fmt_ts)


def register(app):
    app.add_url_rule("/admin/students", "admin_students", admin_students,
                     methods=["GET", "POST"])
    app.add_url_rule("/admin/students/<int:sid>/delete", "admin_delete_student",
                     admin_delete_student, methods=["POST"])
    app.add_url_rule("/admin/students/<int:sid>/toggle", "admin_toggle_student",
                     admin_toggle_student, methods=["POST"])
    app.add_url_rule("/admin/students/<int:sid>/group", "admin_set_student_group",
                     admin_set_student_group, methods=["POST"])
    app.add_url_rule("/admin/students/quick", "admin_students_quick",
                     admin_students_quick, methods=["POST"])
    app.add_url_rule("/admin/students/batch", "admin_students_batch",
                     admin_students_batch, methods=["POST"])
    app.add_url_rule("/admin/groups", "admin_groups", admin_groups,
                     methods=["GET", "POST"])
    app.add_url_rule("/admin/knowledge_points", "admin_knowledge_points",
                     admin_knowledge_points, methods=["GET", "POST"])
    app.add_url_rule("/admin/ips", "admin_ips", admin_ips)
    app.add_url_rule("/admin/ips/block", "admin_block_ip", admin_block_ip,
                     methods=["POST"])
    app.add_url_rule("/admin/ips/remark", "admin_update_remark", admin_update_remark,
                     methods=["POST"])
    app.add_url_rule("/admin/ips/unblock", "admin_unblock_ip", admin_unblock_ip,
                     methods=["POST"])
    app.add_url_rule("/admin/logins", "admin_logins", admin_logins)
