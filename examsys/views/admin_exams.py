"""后台：试卷 CRUD、监考（消息 / 时间）、重算、实时看板、统计、讲评。"""
import json

from flask import (abort, flash, jsonify, redirect, render_template, request,
                   url_for)

from .. import rbac
from ..db import get_db, write_txn
from ..services import audit
from ..services import grading
from ..services import questions as qsvc
from ..services.exams import admin_exam, attempt_remaining, exam_group_ids
from ..services.stats import compute_lecture_stats
from ..utils import dt_local_input, json_loads, now_epoch, parse_dt


# ---------------------------------------------------------------------------
# 试卷 CRUD
# ---------------------------------------------------------------------------

@rbac.require_perm("exam.manage")
def admin_create_exam():
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    duration = request.form.get("duration_minutes", type=int) or 30
    start_at = parse_dt(request.form.get("start_at"))
    end_at = parse_dt(request.form.get("end_at"))
    show_result = 1 if request.form.get("show_result") else 0
    if not title:
        flash("试卷标题不能为空。", "error")
    else:
        with write_txn() as db:
            cur = db.execute(
                "INSERT INTO exams (title, description, duration_minutes, start_at, end_at, "
                "show_result, enable_egg, show_gongde, org_id)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
                (title, description, duration, start_at, end_at, show_result,
                 rbac.visible_org_id(rbac.current_admin()) or 1),
            )
            audit.log("exam.create", target_type="exam", target_id=cur.lastrowid,
                      detail=title)
        flash("试卷已创建。", "success")
    return redirect(url_for("admin_dashboard"))


@rbac.require_perm("exam.manage")
def admin_delete_exam(exam_id):
    with write_txn() as db:
        db.execute("DELETE FROM questions WHERE exam_id=?", (exam_id,))
        db.execute("DELETE FROM results WHERE exam_id=?", (exam_id,))
        db.execute("DELETE FROM exam_groups WHERE exam_id=?", (exam_id,))
        db.execute("DELETE FROM messages WHERE exam_id=?", (exam_id,))
        db.execute("DELETE FROM exams WHERE id=?", (exam_id,))
        audit.log("exam.delete", target_type="exam", target_id=exam_id)
    qsvc.invalidate(exam_id)
    flash("试卷及其数据已删除。", "success")
    return redirect(url_for("admin_dashboard"))


@rbac.require_perm("exam.manage")
def admin_edit_exam(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        duration = request.form.get("duration_minutes", type=int) or 30
        start_at = parse_dt(request.form.get("start_at"))
        end_at = parse_dt(request.form.get("end_at"))
        flags = [
            1 if request.form.get(name) else 0
            for name in ("show_result", "show_answer", "show_explanation", "training_mode",
                         "show_rank", "show_score_monitor", "enable_egg", "show_original",
                         "show_gongde", "enable_reflection")
        ]
        violation_alert = request.form.get("violation_alert", type=int) or 10
        if not title:
            flash("试卷标题不能为空。", "error")
        else:
            groups = request.form.getlist("groups", type=int)
            with write_txn() as db:
                # 参数顺序必须与 SQL 中占位符顺序严格一致
                db.execute(
                    "UPDATE exams SET title=?, description=?, duration_minutes=?, "
                    "start_at=?, end_at=?, show_result=?, show_answer=?, show_explanation=?, "
                    "training_mode=?, show_rank=?, show_score_monitor=?, violation_alert=?, "
                    "enable_egg=?, show_original=?, show_gongde=?, enable_reflection=? WHERE id=?",
                    (title, description, duration, start_at, end_at,
                     flags[0], flags[1], flags[2], flags[3], flags[4], flags[5],
                     violation_alert,
                     flags[6], flags[7], flags[8], flags[9],
                     exam_id),
                )
                db.execute("DELETE FROM exam_groups WHERE exam_id=?", (exam_id,))
                if groups:
                    db.executemany(
                        "INSERT OR IGNORE INTO exam_groups (exam_id, group_id) VALUES (?, ?)",
                        [(exam_id, gid) for gid in groups],
                    )
                audit.log("exam.update", target_type="exam", target_id=exam_id,
                          detail=title)
            flash("试卷已更新。", "success")
            return redirect(url_for("admin_dashboard"))

    groups = get_db().execute("SELECT * FROM groups ORDER BY id").fetchall()
    selected = set(exam_group_ids(exam_id))
    return render_template("admin/exam_edit.html", exam=exam,
                           dt_local_input=dt_local_input, groups=groups,
                           selected_groups=selected)


# ---------------------------------------------------------------------------
# 监考
# ---------------------------------------------------------------------------

@rbac.require_perm("exam.monitor")
def admin_send_message(exam_id):
    """向指定考生（或全体已开考学生）发送提醒。"""
    if not admin_exam(exam_id):
        return jsonify({"ok": False, "msg": "试卷不存在"}), 404
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    student_ids = data.get("student_ids") or []
    if not content:
        return jsonify({"ok": False, "msg": "消息内容不能为空"}), 400
    if not student_ids:
        return jsonify({"ok": False, "msg": "请选择接收学生"}), 400

    db = get_db()
    if student_ids == ["all"]:
        rows = db.execute(
            "SELECT DISTINCT student_id FROM results "
            "WHERE exam_id=? AND started_at IS NOT NULL",
            (exam_id,),
        ).fetchall()
        target_ids = [r["student_id"] for r in rows]
    else:
        target_ids = [sid for sid in student_ids if isinstance(sid, int)]

    now = now_epoch()
    with write_txn() as txn:
        txn.executemany(
            "INSERT INTO messages (exam_id, student_id, content, created_at) VALUES (?,?,?,?)",
            [(exam_id, sid, content, now) for sid in target_ids],
        )
    return jsonify({"ok": True, "count": len(target_ids)})


@rbac.require_perm("exam.monitor")
def admin_set_time(exam_id):
    """调整某个学生的剩余答题时间。"""
    exam = admin_exam(exam_id)
    if not exam:
        return jsonify({"ok": False, "msg": "试卷不存在"}), 404
    data = request.get_json(silent=True) or {}
    student_id = data.get("student_id")
    action = data.get("action")  # set / add / reduce
    notify = data.get("notify", True)
    if not student_id or not action:
        return jsonify({"ok": False, "msg": "参数不完整"}), 400
    try:
        minutes = int(data.get("minutes", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "msg": "时间格式错误"}), 400

    db = get_db()
    record = db.execute(
        "SELECT id, started_at, time_offset FROM results "
        "WHERE student_id=? AND exam_id=? AND submitted_at IS NULL",
        (student_id, exam_id),
    ).fetchone()
    if not record:
        return jsonify({"ok": False, "msg": "该学生未在考试中"}), 400

    now = now_epoch()
    total_seconds = exam["duration_minutes"] * 60
    elapsed = now - (record["started_at"] or now)
    offset = record["time_offset"] or 0

    if action == "set":
        new_offset = minutes * 60 - (total_seconds - elapsed)
    elif action == "add":
        new_offset = offset + minutes * 60
    elif action == "reduce":
        new_offset = offset - minutes * 60
    else:
        return jsonify({"ok": False, "msg": "未知操作"}), 400

    with write_txn() as txn:
        txn.execute("UPDATE results SET time_offset=? WHERE id=?", (new_offset, record["id"]))
        if notify:
            verb = {"set": "设置为", "add": "增加了", "reduce": "减少了"}[action]
            text = ("老师已将你的答题时间%s %d 分钟" % (verb, minutes)
                    if action == "set" else "老师%s了你的答题时间 %d 分钟" % (verb, minutes))
            txn.execute(
                "INSERT INTO messages (exam_id, student_id, content, created_at) VALUES (?,?,?,?)",
                (exam_id, student_id, text, now),
            )
        audit.log("exam.set_time", target_type="result", target_id=record["id"],
                  detail="%s %d 分钟" % (action, minutes))

    new_row = dict(record)
    new_row["time_offset"] = new_offset
    new_remaining = attempt_remaining(exam, new_row)
    return jsonify({"ok": True, "new_remaining": new_remaining})


@rbac.require_perm("exam.manage")
def admin_regrade(exam_id):
    """重新计算所有已交卷学生的成绩（题目只读一次）。"""
    if not admin_exam(exam_id):
        return jsonify({"ok": False, "msg": "试卷不存在"}), 404
    db = get_db()
    records = db.execute(
        "SELECT id, answers FROM results WHERE exam_id=? AND submitted_at IS NOT NULL",
        (exam_id,),
    ).fetchall()
    gradable = qsvc.gradable(exam_id)
    updates = []
    for record in records:
        score, rows = grading.grade(exam_id, json_loads(record["answers"], {}), gradable)
        updates.append((score, json.dumps(grading.grade_to_question_scores(rows),
                                          ensure_ascii=False), record["id"]))
    if updates:
        with write_txn() as txn:
            txn.executemany("UPDATE results SET score=?, question_scores=? WHERE id=?", updates)
            audit.log("exam.regrade", target_type="exam", target_id=exam_id,
                      detail="重算 %d 份答卷" % len(updates))
    return jsonify({"ok": True, "count": len(updates)})


@rbac.require_perm("exam.monitor")
def admin_monitor(exam_id):
    if not admin_exam(exam_id):
        abort(404)
    return render_template("admin/monitor.html", exam=admin_exam(exam_id))


@rbac.require_perm("exam.monitor")
def admin_live(exam_id):
    """监考实时看板（前端每 2 秒轮询一次，所以这里要尽量轻）。"""
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)

    db = get_db()
    total = len(qsvc.gradable(exam_id))
    gids = exam_group_ids(exam_id)

    sql = (
        "SELECT s.id AS student_id, s.name, s.student_no, r.started_at, r.submitted_at, "
        "       r.score, r.violations, r.answered_count, r.last_seen, r.time_offset "
        "FROM students s LEFT JOIN results r ON r.student_id=s.id AND r.exam_id=? "
        "WHERE s.enabled=1"
    )
    params = [exam_id]
    if gids:
        sql += " AND s.group_id IN (%s)" % ",".join("?" * len(gids))
        params.extend(gids)
    sql += " ORDER BY s.student_no, s.name"

    now = now_epoch()
    data = []
    for row in db.execute(sql, params).fetchall():
        if not row["started_at"]:
            status, answered, score, online, remaining = "未开始", 0, None, False, 0
        elif row["submitted_at"]:
            status, answered, score, online = "已交卷", total, row["score"], False
            remaining = 0
        else:
            status, score = "答题中", None
            answered = row["answered_count"] or 0
            online = bool(row["last_seen"]) and (now - row["last_seen"] < 20)
            # 剩余时间和学生端、监考接口用同一套算法（含考试窗口封顶）
            remaining = attempt_remaining(exam, row)
        data.append({
            "student_id": row["student_id"], "name": row["name"],
            "student_no": row["student_no"], "status": status,
            "answered": answered, "total": total, "score": score,
            "violations": row["violations"] or 0, "online": online,
            "submitted_at": row["submitted_at"], "remaining": remaining,
        })
    return jsonify({"ok": True, "total": total, "students": data})


# ---------------------------------------------------------------------------
# 统计 / 讲评
# ---------------------------------------------------------------------------

@rbac.require_perm("grade.view")
def admin_stats(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    db = get_db()
    full = qsvc.total_score(exam_id)

    student_total = db.execute(
        "SELECT COUNT(*) c FROM students WHERE enabled=1").fetchone()["c"]
    submitted = db.execute(
        "SELECT r.score, r.violations, r.auto_submitted, r.notes, r.reflection "
        "FROM results r WHERE r.exam_id=? AND r.submitted_at IS NOT NULL",
        (exam_id,),
    ).fetchall()
    n_submitted = len(submitted)
    n_in_progress = db.execute(
        "SELECT COUNT(*) c FROM results WHERE exam_id=? AND submitted_at IS NULL",
        (exam_id,),
    ).fetchone()["c"]
    n_not_started = max(0, student_total - n_submitted - n_in_progress)

    scores = [r["score"] for r in submitted if r["score"] is not None]
    avg = round(sum(scores) / len(scores), 1) if scores else None
    hi = max(scores) if scores else None
    lo = min(scores) if scores else None
    pass_line = full * 0.6
    n_passed = sum(1 for s in scores if s >= pass_line)
    pass_rate = round(n_passed / len(scores) * 100) if scores else None
    n_full = sum(1 for s in scores if s >= full)
    total_violations = sum(r["violations"] or 0 for r in submitted)
    n_auto = sum(1 for r in submitted if r["auto_submitted"])
    n_notes = sum(1 for r in submitted if r["notes"] and r["notes"] not in ("", "{}"))
    n_reflection = sum(1 for r in submitted if r["reflection"])

    buckets = [0] * 10
    for score in scores:
        if full > 0:
            pct = score / full
            idx = 9 if pct >= 1 else min(9, int(pct * 10))
        else:
            idx = 9 if score > 0 else 0
        buckets[idx] += 1
    max_bucket = max(buckets) or 1
    labels = ["0~10%", "10~20%", "20~30%", "30~40%", "40~50%",
              "50~60%", "60~70%", "70~80%", "80~90%", "90~100%"]
    dist = [{"label": labels[i], "count": buckets[i],
             "pct": round(buckets[i] / max_bucket * 100)} for i in range(10)]

    return render_template(
        "admin/stats.html", exam=exam, full=full,
        student_total=student_total, n_submitted=n_submitted,
        n_in_progress=n_in_progress, n_not_started=n_not_started,
        avg=avg, hi=hi, lo=lo, pass_rate=pass_rate, n_passed=n_passed,
        n_full=n_full, total_violations=total_violations, n_auto=n_auto,
        n_notes=n_notes, n_reflection=n_reflection, dist=dist,
        lecture=compute_lecture_stats(exam_id),
    )


@rbac.require_perm("grade.view")
def admin_lecture(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    stats = compute_lecture_stats(exam_id)
    return render_template("admin/lecture.html", exam=exam,
                           stats_json=json.dumps(stats, ensure_ascii=False))


def register(app):
    app.add_url_rule("/admin/exams", "admin_create_exam", admin_create_exam, methods=["POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/delete", "admin_delete_exam",
                     admin_delete_exam, methods=["POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/edit", "admin_edit_exam",
                     admin_edit_exam, methods=["GET", "POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/send_message", "admin_send_message",
                     admin_send_message, methods=["POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/set_time", "admin_set_time",
                     admin_set_time, methods=["POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/regrade", "admin_regrade",
                     admin_regrade, methods=["POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/monitor", "admin_monitor", admin_monitor)
    app.add_url_rule("/admin/exams/<int:exam_id>/live", "admin_live", admin_live)
    app.add_url_rule("/admin/exams/<int:exam_id>/stats", "admin_stats", admin_stats)
    app.add_url_rule("/admin/exams/<int:exam_id>/lecture", "admin_lecture", admin_lecture)
