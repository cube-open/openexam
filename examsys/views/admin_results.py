"""后台：成绩列表、改分、导出、答卷查看、反思、学生成长。"""
import io
import json
import zipfile

from flask import (abort, flash, jsonify, redirect, render_template, request,
                   send_file, url_for)

from .. import deps
from .. import rbac
from ..db import get_db, write_txn
from ..services import audit
from ..services import grading
from ..services import questions as qsvc
from ..services.exams import admin_exam
from ..services.pdf import generate_result_pdf, safe_pdf_filename
from ..services.stats import student_kp_analysis, student_rank_history
from ..utils import fmt_ts, json_loads, now_epoch


@rbac.require_perm("grade.view")
def admin_results(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    rows = get_db().execute(
        "SELECT r.*, s.name, s.student_no FROM results r "
        "JOIN students s ON s.id=r.student_id "
        "WHERE r.exam_id=? ORDER BY (r.score IS NULL), r.score DESC, r.submitted_at",
        (exam_id,),
    ).fetchall()
    return render_template("admin/results.html", exam=exam, rows=rows,
                           total=qsvc.total_score(exam_id), fmt_ts=fmt_ts)


@rbac.require_perm("grade.manual")
def admin_set_score(rid):
    row = get_db().execute("SELECT exam_id FROM results WHERE id=?", (rid,)).fetchone()
    if not row:
        abort(404)
    try:
        score = float(request.form.get("score"))
    except (TypeError, ValueError):
        flash("请输入有效分数。", "error")
        return redirect(url_for("admin_results", exam_id=row["exam_id"]))
    with write_txn() as db:
        old = db.execute("SELECT score FROM results WHERE id=?", (rid,)).fetchone()
        db.execute("UPDATE results SET score=? WHERE id=?", (score, rid))
        audit.log("result.set_score", target_type="result", target_id=rid,
                  detail="%s → %s" % (old["score"] if old else "?", score))
    flash("成绩已修改。", "success")
    return redirect(url_for("admin_results", exam_id=row["exam_id"]))


@rbac.require_perm("grade.manual")
def admin_grade(rid):
    db = get_db()
    record = db.execute(
        "SELECT r.*, s.name, s.student_no FROM results r "
        "JOIN students s ON s.id=r.student_id WHERE r.id=?", (rid,)
    ).fetchone()
    if not record or not record["submitted_at"]:
        abort(404)
    exam = admin_exam(record["exam_id"])
    rows = grading.grade(record["exam_id"], json_loads(record["answers"], {}))[1]
    saved = json_loads(record["question_scores"], {})
    for row in rows:
        qid = str(row["id"])
        row["awarded"] = float(saved.get(qid, row["score"] if row["correct"] else 0))

    if request.method == "POST":
        new_scores = {}
        total = 0.0
        for row in rows:
            qid = str(row["id"])
            try:
                value = float(request.form.get("score_" + qid) or 0)
            except (TypeError, ValueError):
                value = 0.0
            new_scores[qid] = value
            total += value
        with write_txn() as txn:
            txn.execute("UPDATE results SET score=?, question_scores=? WHERE id=?",
                        (total, json.dumps(new_scores, ensure_ascii=False), rid))
            audit.log("result.grade", target_type="result", target_id=rid,
                      detail="人工阅卷，总分 %s" % total)
        flash("改分已保存。", "success")
        return redirect(url_for("admin_results", exam_id=record["exam_id"]))

    return render_template("admin/grade.html", exam=exam, result=record, rows=rows,
                           total=qsvc.total_score(record["exam_id"]))


@rbac.require_perm("result.export")
def admin_export_results(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    if not deps.has("openpyxl"):
        flash(deps.message("openpyxl"), "error")
        return redirect(url_for("admin_results", exam_id=exam_id))
    rows = get_db().execute(
        "SELECT r.*, s.name, s.student_no FROM results r "
        "JOIN students s ON s.id=r.student_id "
        "WHERE r.exam_id=? ORDER BY s.student_no, s.name", (exam_id,)
    ).fetchall()
    total = qsvc.total_score(exam_id)

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "成绩单"
    ws.append(["序号", "姓名", "学号", "得分", "满分", "切屏次数", "交卷方式",
               "开始时间", "交卷时间", "是否有笔记"])
    for i, row in enumerate(rows, 1):
        if row["submitted_at"]:
            mode = "超时自动交卷" if row["auto_submitted"] else "正常交卷"
            score = row["score"]
        else:
            mode, score = "未交卷", ""
        has_notes = "是" if (row["notes"] and row["notes"] not in ("", "{}")) else ""
        ws.append([i, row["name"], row["student_no"], score, total, row["violations"],
                   mode, fmt_ts(row["started_at"]), fmt_ts(row["submitted_at"]), has_notes])
    for col, width in zip("ABCDEFGHIJ", [6, 14, 14, 8, 8, 10, 14, 20, 20, 10]):
        ws.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name="成绩单_%s.xlsx" % exam["title"],
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@rbac.require_perm("result.export")
def admin_export_pdf_zip(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    if not deps.has("reportlab"):
        flash(deps.message("reportlab"), "error")
        return redirect(url_for("admin_results", exam_id=exam_id))
    rows = get_db().execute(
        "SELECT r.*, s.name, s.student_no FROM results r "
        "JOIN students s ON s.id=r.student_id "
        "WHERE r.exam_id=? AND r.submitted_at IS NOT NULL ORDER BY s.student_no, s.name",
        (exam_id,),
    ).fetchall()
    if not rows:
        flash("暂无已交卷学生可导出。", "error")
        return redirect(url_for("admin_results", exam_id=exam_id))

    total = qsvc.total_score(exam_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            answers = json_loads(row["answers"], {})
            _, review = grading.grade(exam_id, answers)
            student = {"name": row["name"], "student_no": row["student_no"]}
            pdf_bytes = generate_result_pdf(exam, student, row, review, total,
                                            notes=json_loads(row["notes"], {}))
            zf.writestr(safe_pdf_filename(row["student_no"], row["name"]), pdf_bytes)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=safe_pdf_filename(exam["title"]).replace(".pdf", ".zip"),
                     mimetype="application/zip")


@rbac.require_perm("grade.manual")
def admin_reset_result(rid):
    row = get_db().execute("SELECT exam_id FROM results WHERE id=?", (rid,)).fetchone()
    if not row:
        abort(404)
    with write_txn() as db:
        db.execute("DELETE FROM results WHERE id=?", (rid,))
        audit.log("result.reset", target_type="result", target_id=rid,
                  detail="清除作答记录，允许重考")
    flash("已清除该学生的作答记录（可重新考试）。", "success")
    return redirect(url_for("admin_results", exam_id=row["exam_id"]))


@rbac.require_perm("grade.view")
def admin_reflections(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    rows = get_db().execute(
        "SELECT r.id, r.reflection, r.score, s.name, s.student_no FROM results r "
        "JOIN students s ON s.id=r.student_id "
        "WHERE r.exam_id=? AND r.submitted_at IS NOT NULL "
        "ORDER BY s.student_no, s.name", (exam_id,)
    ).fetchall()
    return render_template("admin/reflections.html", exam=exam, rows=rows)


@rbac.require_perm("grade.manual")
def admin_toggle_reflection(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    with write_txn() as db:
        db.execute("UPDATE exams SET enable_reflection=? WHERE id=?",
                   (0 if exam["enable_reflection"] else 1, exam_id))
    return redirect(url_for("admin_reflections", exam_id=exam_id))


@rbac.require_perm("grade.view")
def admin_reflections_live(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    now = now_epoch()
    rows = get_db().execute(
        "SELECT r.id, r.reflection, r.reflection_at, r.score, s.name, s.student_no "
        "FROM results r JOIN students s ON s.id=r.student_id "
        "WHERE r.exam_id=? AND r.submitted_at IS NOT NULL "
        "ORDER BY s.student_no, s.name", (exam_id,)
    ).fetchall()
    students = [{
        "id": row["id"], "name": row["name"], "student_no": row["student_no"],
        "score": row["score"], "reflection": row["reflection"] or "",
        "writing": bool(row["reflection_at"]) and (now - row["reflection_at"] < 30),
    } for row in rows]
    return jsonify({"ok": True, "enabled": bool(exam["enable_reflection"]),
                    "students": students})


@rbac.require_perm("grade.view")
def admin_result_view(rid):
    db = get_db()
    record = db.execute(
        "SELECT r.*, s.name, s.student_no FROM results r "
        "JOIN students s ON s.id=r.student_id WHERE r.id=?", (rid,)
    ).fetchone()
    if not record:
        abort(404)
    exam = admin_exam(record["exam_id"])
    rows = grading.grade(record["exam_id"], json_loads(record["answers"], {}))[1]
    students = db.execute(
        "SELECT r.id AS rid, s.name, s.student_no, r.score, r.notes FROM results r "
        "JOIN students s ON s.id=r.student_id "
        "WHERE r.exam_id=? AND r.submitted_at IS NOT NULL "
        "ORDER BY s.student_no, s.name", (record["exam_id"],)
    ).fetchall()
    return render_template("admin/result_detail.html", exam=exam, result=record,
                           rows=rows, total=qsvc.total_score(record["exam_id"]),
                           students=students, current_rid=rid,
                           notes=json_loads(record["notes"], {}))


@rbac.require_perm("result.export")
def admin_result_pdf(rid):
    record = get_db().execute(
        "SELECT r.*, s.name, s.student_no FROM results r "
        "JOIN students s ON s.id=r.student_id WHERE r.id=?", (rid,)
    ).fetchone()
    if not record or not record["submitted_at"]:
        abort(404)
    if not deps.has("reportlab"):
        flash(deps.message("reportlab"), "error")
        return redirect(url_for("admin_result_view", rid=rid))
    exam = admin_exam(record["exam_id"])
    student = {"name": record["name"], "student_no": record["student_no"]}
    _, rows = grading.grade(record["exam_id"], json_loads(record["answers"], {}))
    pdf_bytes = generate_result_pdf(
        exam, student, record, rows, qsvc.total_score(record["exam_id"]),
        notes=json_loads(record["notes"], {}))
    return send_file(io.BytesIO(pdf_bytes), as_attachment=True,
                     download_name=safe_pdf_filename(record["name"], exam["title"]),
                     mimetype="application/pdf")


@rbac.require_perm("grade.view")
def admin_student_growth(sid):
    db = get_db()
    student = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if not student:
        abort(404)
    return render_template("admin/student_growth.html",
                           student=student,
                           history=student_rank_history(sid),
                           kp_analysis=student_kp_analysis(sid),
                           all_students=db.execute(
                               "SELECT id, name, student_no FROM students ORDER BY student_no"
                           ).fetchall(),
                           fmt_ts=fmt_ts)


def register(app):
    app.add_url_rule("/admin/exams/<int:exam_id>/results", "admin_results",
                     admin_results)
    app.add_url_rule("/admin/results/<int:rid>/score", "admin_set_score",
                     admin_set_score, methods=["POST"])
    app.add_url_rule("/admin/results/<int:rid>/grade", "admin_grade", admin_grade,
                     methods=["GET", "POST"])
    app.add_url_rule("/admin/results/<int:rid>/view", "admin_result_view",
                     admin_result_view)
    app.add_url_rule("/admin/results/<int:rid>/pdf", "admin_result_pdf",
                     admin_result_pdf)
    app.add_url_rule("/admin/results/<int:rid>/reset", "admin_reset_result",
                     admin_reset_result, methods=["POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/export", "admin_export_results",
                     admin_export_results)
    app.add_url_rule("/admin/exams/<int:exam_id>/export_pdf_zip",
                     "admin_export_pdf_zip", admin_export_pdf_zip)
    app.add_url_rule("/admin/exams/<int:exam_id>/reflections", "admin_reflections",
                     admin_reflections)
    app.add_url_rule("/admin/exams/<int:exam_id>/toggle_reflection",
                     "admin_toggle_reflection", admin_toggle_reflection,
                     methods=["POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/reflections/live",
                     "admin_reflections_live", admin_reflections_live)
    app.add_url_rule("/admin/students/<int:sid>/growth", "admin_student_growth",
                     admin_student_growth)
