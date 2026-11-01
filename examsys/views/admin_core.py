"""后台：首页、整站数据备份 / 恢复。"""
import io
import json
import os
from datetime import datetime

from flask import (flash, jsonify, redirect, render_template, request,
                   send_file, url_for)

from ..config import Config
from .. import rbac
from ..db import get_db, write_txn
from ..security import login_required_admin
from ..utils import fmt_ts, now_epoch

BACKUP_TABLES = [
    "admins", "groups", "students", "exams", "questions",
    "results", "exam_groups", "knowledge_points", "question_kp",
    "blocked_ips", "login_logs",
]


@login_required_admin
def admin_dashboard():
    db = get_db()
    org = rbac.visible_org_id(rbac.current_admin())
    if org is None:
        exams = db.execute(
            "SELECT e.*, "
            "  (SELECT COUNT(*) FROM questions q WHERE q.exam_id=e.id) AS q_count, "
            "  (SELECT COUNT(*) FROM results r WHERE r.exam_id=e.id) AS r_count "
            "FROM exams e ORDER BY e.id DESC"
        ).fetchall()
        stats_sql = (
            "SELECT (SELECT COUNT(*) FROM exams) AS exams,"
            "       (SELECT COUNT(*) FROM students) AS students,"
            "       (SELECT COUNT(*) FROM questions) AS questions,"
            "       (SELECT COUNT(*) FROM results WHERE submitted_at IS NOT NULL) AS submitted"
        )
        params = ()
    else:
        exams = db.execute(
            "SELECT e.*, "
            "  (SELECT COUNT(*) FROM questions q WHERE q.exam_id=e.id) AS q_count, "
            "  (SELECT COUNT(*) FROM results r WHERE r.exam_id=e.id) AS r_count "
            "FROM exams e WHERE e.org_id=? ORDER BY e.id DESC", (org,)
        ).fetchall()
        stats_sql = (
            "SELECT (SELECT COUNT(*) FROM exams WHERE org_id=?) AS exams,"
            "       (SELECT COUNT(*) FROM students WHERE org_id=?) AS students,"
            "       (SELECT COUNT(*) FROM questions q JOIN exams e ON e.id=q.exam_id"
            "         WHERE e.org_id=?) AS questions,"
            "       (SELECT COUNT(*) FROM results r JOIN exams e ON e.id=r.exam_id"
            "         WHERE r.submitted_at IS NOT NULL AND e.org_id=?) AS submitted"
        )
        params = (org, org, org, org)
    row = db.execute(stats_sql, params).fetchone()
    stats = {k: row[k] for k in ("exams", "students", "questions", "submitted")}
    return render_template("admin/dashboard.html", exams=exams, stats=stats, fmt_ts=fmt_ts)


@rbac.require_perm("system.settings")
def admin_data():
    from ..services.backup import list_backups

    return render_template(
        "admin/data.html",
        backups=list_backups(),
        backup_hours=Config.BACKUP_INTERVAL_HOURS,
        backup_keep=Config.BACKUP_KEEP,
        fmt_ts=fmt_ts,
    )


@rbac.require_perm("system.settings")
def admin_backup_now():
    """手动打一份数据库快照（自动备份之外的手动兜底）。"""
    from ..services.backup import backup_now

    try:
        path = backup_now(tag="manual")
    except Exception as exc:  # noqa: BLE001
        flash("备份失败：%s" % exc, "error")
        return redirect(url_for("admin_data"))
    flash("已生成快照：%s" % os.path.basename(path), "success")
    return redirect(url_for("admin_data"))


def _dump_all(db) -> dict:
    dump = {}
    for table in BACKUP_TABLES:
        rows = db.execute("SELECT * FROM %s" % table).fetchall()
        dump[table] = [dict(r) for r in rows]
    dump["_meta"] = {"version": 1, "exported_at": now_epoch()}
    return dump


@rbac.require_perm("system.settings")
def admin_export_all():
    dump = _dump_all(get_db())
    buf = io.BytesIO(json.dumps(dump, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.seek(0)
    filename = "exam_backup_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/json")


@rbac.require_perm("system.settings")
def admin_import_preview():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "msg": "请选择文件"})
    try:
        data = json.loads(file.read().decode("utf-8"))
    except Exception as exc:
        return jsonify({"ok": False, "msg": "JSON 解析失败：%s" % exc})
    if not isinstance(data, dict):
        return jsonify({"ok": False, "msg": "文件格式错误"})
    summary = {t: len(data[t]) for t in BACKUP_TABLES if t in data}
    return jsonify({"ok": True, "summary": summary})


@rbac.require_perm("system.settings")
def admin_import_all():
    confirm = request.form.get("confirm") or ""
    if confirm != "我确认覆盖所有数据":
        flash("确认文字不正确，操作已取消。", "error")
        return redirect(url_for("admin_data"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("请选择文件。", "error")
        return redirect(url_for("admin_data"))
    try:
        data = json.loads(file.read().decode("utf-8"))
    except Exception as exc:
        flash("JSON 解析失败：%s" % exc, "error")
        return redirect(url_for("admin_data"))
    if not isinstance(data, dict):
        flash("文件格式错误。", "error")
        return redirect(url_for("admin_data"))

    backup_path = os.path.join(os.path.dirname(Config.DB_PATH),
                               "exam_backup_before_import.json")
    try:
        with open(backup_path, "w", encoding="utf-8") as fh:
            json.dump(_dump_all(get_db()), fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        flash("自动备份失败，已取消导入：%s" % exc, "error")
        return redirect(url_for("admin_data"))

    try:
        with write_txn() as db:
            for table in BACKUP_TABLES:
                if table not in data:
                    continue
                db.execute("DELETE FROM %s" % table)
                rows = data[table]
                if not rows:
                    continue
                cols = list(rows[0].keys())
                placeholders = ",".join("?" * len(cols))
                sql = "INSERT INTO %s (%s) VALUES (%s)" % (
                    table, ",".join(cols), placeholders)
                db.executemany(sql, [[row.get(c) for c in cols] for row in rows])
        _invalidate_all_caches()
        flash("数据已覆盖！自动备份保存在 %s" % backup_path, "success")
    except Exception as exc:
        flash("导入失败，已回滚：%s" % exc, "error")
    return redirect(url_for("admin_data"))


def _invalidate_all_caches():
    from ..services import questions as qsvc
    from ..security import invalidate_blocked_ips
    qsvc.invalidate()
    invalidate_blocked_ips()


def register(app):
    app.add_url_rule("/admin", "admin_dashboard", admin_dashboard)
    app.add_url_rule("/admin/data", "admin_data", admin_data)
    app.add_url_rule("/admin/backup_now", "admin_backup_now", admin_backup_now,
                     methods=["POST"])
    app.add_url_rule("/admin/export_all", "admin_export_all", admin_export_all)
    app.add_url_rule("/admin/import_preview", "admin_import_preview",
                     admin_import_preview, methods=["POST"])
    app.add_url_rule("/admin/import_all", "admin_import_all",
                     admin_import_all, methods=["POST"])
