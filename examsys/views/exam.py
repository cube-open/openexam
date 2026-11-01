"""学生端考试流程：开考 / 答题页 / 交卷 / 成绩 / 原卷 / 反思 / PDF。"""
import io
import json

from flask import (abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

from .. import deps
from ..config import Config
from ..db import get_db, write_txn
from ..security import current_student, login_required_student
from ..services import grading
from ..services import questions as qsvc
from ..services.exams import (attempt_overtime, attempt_remaining, exam_visible_to,
                              get_exam)
from ..services.pdf import generate_result_pdf, safe_pdf_filename
from ..utils import exam_window, json_loads, now_epoch


def _finalize_expired(exam, record):
    """服务端强制收卷：按截止时间前已落库的答案判分、结卷。

    学生还开着页面也没用——时间一到，卷子就锁了。
    """
    answers = json_loads(record["answers"], {})
    score, rows = grading.grade(exam["id"], answers)
    qscores = grading.grade_to_question_scores(rows)
    with write_txn() as txn:
        txn.execute(
            "UPDATE results SET score=?, question_scores=?, auto_submitted=1, "
            "submitted_at=?, answered_count=?, overtime_seconds=? "
            "WHERE id=? AND submitted_at IS NULL",
            (score, json.dumps(qscores, ensure_ascii=False), now_epoch(),
             len(answers), attempt_overtime(exam, record), record["id"]),
        )


@login_required_student
def start_exam(exam_id):
    exam = get_exam(exam_id)
    if not exam:
        abort(404)
    stu = current_student()
    if not exam_visible_to(exam_id, stu):
        flash("你所在的班级不参加该考试。", "error")
        return redirect(url_for("dashboard"))
    win, win_label = exam_window(exam)
    if win != "open":
        flash("该考试" + win_label + "，暂不能开始。", "error")
        return redirect(url_for("dashboard"))

    now = now_epoch()
    with write_txn() as db:
        # 先补 started_at：原来用 INSERT OR IGNORE，记录已存在但 started_at 为空时
        # 不会更新，学生刷新页面就能把计时器清零
        cur = db.execute(
            "UPDATE results SET started_at=? "
            "WHERE student_id=? AND exam_id=? AND started_at IS NULL",
            (now, stu["id"], exam_id),
        )
        if cur.rowcount == 0:
            db.execute(
                "INSERT OR IGNORE INTO results (student_id, exam_id, started_at) VALUES (?, ?, ?)",
                (stu["id"], exam_id, now),
            )
    return redirect(url_for("exam_page", exam_id=exam_id))


@login_required_student
def exam_page(exam_id):
    db = get_db()
    stu = current_student()
    exam = get_exam(exam_id)
    if not exam:
        abort(404)
    if not exam_visible_to(exam_id, stu):
        flash("你所在的班级不参加该考试。", "error")
        return redirect(url_for("dashboard"))

    result = db.execute(
        "SELECT * FROM results WHERE student_id=? AND exam_id=?",
        (stu["id"], exam_id),
    ).fetchone()
    if not result:
        return redirect(url_for("dashboard"))
    if result["submitted_at"]:
        return redirect(url_for("result", exam_id=exam_id))

    # 服务端强制时限：时间一到就直接收卷，页面留着也没用
    if attempt_overtime(exam, result) > 0:
        _finalize_expired(exam, result)
        return redirect(url_for("result", exam_id=exam_id))

    remaining = attempt_remaining(exam, result)
    saved_answers = json_loads(result["answers"], {})

    return render_template(
        "exam.html",
        exam=exam,
        stu=stu,
        questions=qsvc.exam_structure(exam_id),
        saved_answers=json.dumps(saved_answers, ensure_ascii=False),
        remaining=remaining,
        violations=result["violations"],
        nodetect=request.args.get("nodetect") == "1",
    )


@login_required_student
def submit_exam(exam_id):
    exam = get_exam(exam_id)
    if not exam:
        abort(404)
    db = get_db()
    result = db.execute(
        "SELECT * FROM results WHERE student_id=? AND exam_id=?",
        (session["student_id"], exam_id),
    ).fetchone()
    if not result:
        return jsonify({"ok": False, "msg": "无作答记录"}), 400
    if result["submitted_at"]:
        return jsonify({"ok": True, "redirect": url_for("result", exam_id=exam_id)})

    data = request.get_json(silent=True) or {}
    answers = data.get("answers") or {}
    if not isinstance(answers, dict):
        answers = {}

    # 服务端强制时限：超时交卷只认宽限期内的答案；超出宽限就只认截止前已落库的那份
    overtime = attempt_overtime(exam, result)
    if overtime > Config.EXAM_SUBMIT_GRACE_SECONDS:
        answers = json_loads(result["answers"], {})
        auto = 1
    else:
        auto = 1 if data.get("auto") else 0

    score, rows = grading.grade(exam_id, answers)
    qscores = grading.grade_to_question_scores(rows)

    with write_txn() as txn:
        cur = txn.execute(
            "UPDATE results SET answers=?, score=?, question_scores=?, auto_submitted=?, "
            "submitted_at=?, answered_count=?, overtime_seconds=? "
            "WHERE student_id=? AND exam_id=? AND submitted_at IS NULL",
            (json.dumps(answers, ensure_ascii=False), score,
             json.dumps(qscores, ensure_ascii=False), auto,
             now_epoch(), len(answers), overtime, session["student_id"], exam_id),
        )
        if cur.rowcount == 0:
            # 并发重复提交：以先落库的那次为准，不覆盖成绩
            return jsonify({"ok": True, "redirect": url_for("result", exam_id=exam_id)})
    return jsonify({"ok": True, "redirect": url_for("result", exam_id=exam_id),
                    "late": overtime})


@login_required_student
def result(exam_id):
    db = get_db()
    stu = current_student()
    exam = get_exam(exam_id)
    if not exam:
        abort(404)
    record = db.execute(
        "SELECT * FROM results WHERE student_id=? AND exam_id=?",
        (stu["id"], exam_id),
    ).fetchone()
    if not record or not record["submitted_at"]:
        return redirect(url_for("dashboard"))
    if not exam["show_result"]:
        return render_template("result_hidden.html", exam=exam, stu=stu)

    answers = json_loads(record["answers"], {})
    _, all_rows = grading.grade(exam_id, answers)

    rows_map = {row["id"]: row for row in all_rows}
    kids = qsvc.children_map(exam_id)
    rows = []
    for q in qsvc.exam_questions(exam_id):
        if q["parent_id"]:
            continue
        if q["type"] == "group":
            children = [rows_map[c["id"]] for c in kids.get(q["id"], []) if c["id"] in rows_map]
            rows.append({"type": "group", "question": q["question"], "children": children})
        elif q["id"] in rows_map:
            rows.append(rows_map[q["id"]])

    return render_template(
        "result.html", exam=exam, stu=stu, result=record, rows=rows,
        notes=json_loads(record["notes"], {}), total=qsvc.total_score(exam_id),
    )


@login_required_student
def save_notes(exam_id):
    exam = get_exam(exam_id)
    if not exam or not exam["training_mode"]:
        flash("该考试未开启笔记功能。", "error")
        return redirect(url_for("result", exam_id=exam_id))
    record = get_db().execute(
        "SELECT * FROM results WHERE student_id=? AND exam_id=?",
        (session["student_id"], exam_id),
    ).fetchone()
    if not record or not record["submitted_at"]:
        return redirect(url_for("dashboard"))

    notes = {}
    for key, val in request.form.items():
        if key.startswith("note_") and val.strip():
            notes[key[5:]] = val.strip()
    with write_txn() as db:
        db.execute(
            "UPDATE results SET notes=? WHERE id=?",
            (json.dumps(notes, ensure_ascii=False), record["id"]),
        )
    flash("笔记已保存。", "success")
    return redirect(url_for("result", exam_id=exam_id))


@login_required_student
def exam_original(exam_id):
    exam = get_exam(exam_id)
    if not exam:
        abort(404)
    if not exam["show_original"]:
        flash("原卷暂未开放。", "error")
        return redirect(url_for("dashboard"))
    record = get_db().execute(
        "SELECT * FROM results WHERE student_id=? AND exam_id=?",
        (session["student_id"], exam_id),
    ).fetchone()
    if not record or not record["submitted_at"]:
        return redirect(url_for("dashboard"))

    kids = qsvc.children_map(exam_id)
    questions = []
    qno = 0
    for q in qsvc.exam_questions(exam_id):
        if q["parent_id"]:
            continue
        if q["type"] == "group":
            children = []
            for cq in kids.get(q["id"], []):
                qno += 1
                children.append({
                    "num": qno, "type": cq["type"],
                    "type_label": qsvc.TYPE_LABELS.get(cq["type"], cq["type"]),
                    "question": cq["question"], "options": cq["options"],
                    "score": cq["score"],
                })
            questions.append({
                "type": "group", "question": q["question"],
                "score": q["score"], "children": children,
            })
        else:
            qno += 1
            questions.append({
                "num": qno, "type": q["type"],
                "type_label": qsvc.TYPE_LABELS.get(q["type"], q["type"]),
                "question": q["question"], "options": q["options"],
                "score": q["score"],
            })
    return render_template("original.html", exam=exam, questions=questions)


@login_required_student
def reflection(exam_id):
    exam = get_exam(exam_id)
    if not exam:
        abort(404)
    if not exam["enable_reflection"]:
        flash("该考试未开启反思功能。", "error")
        return redirect(url_for("dashboard"))
    record = get_db().execute(
        "SELECT * FROM results WHERE student_id=? AND exam_id=?",
        (session["student_id"], exam_id),
    ).fetchone()
    if not record or not record["submitted_at"]:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        data = request.get_json(silent=True)
        text = (data.get("reflection") if data else request.form.get("reflection")) or ""
        text = text.strip()
        with write_txn() as db:
            db.execute(
                "UPDATE results SET reflection=?, reflection_at=? WHERE id=?",
                (text, now_epoch(), record["id"]),
            )
        if data is not None:
            return jsonify({"ok": True})
        flash("反思已保存。", "success")
        return redirect(url_for("reflection", exam_id=exam_id))

    return render_template("reflection.html", exam=exam, result=record,
                           stu=current_student())


@login_required_student
def exam_pdf(exam_id):
    exam = get_exam(exam_id)
    if not exam:
        abort(404)
    stu = current_student()
    record = get_db().execute(
        "SELECT * FROM results WHERE student_id=? AND exam_id=?",
        (stu["id"], exam_id),
    ).fetchone()
    if not record or not record["submitted_at"]:
        return redirect(url_for("dashboard"))
    if not exam["show_result"]:
        flash("成绩与答案解析暂未开放下载。", "error")
        return redirect(url_for("dashboard"))
    if not deps.has("reportlab"):
        flash(deps.message("reportlab"), "error")
        return redirect(url_for("result", exam_id=exam_id))

    answers = json_loads(record["answers"], {})
    _, rows = grading.grade(exam_id, answers)
    notes = {}
    if request.args.get("notes") == "1" and exam["training_mode"]:
        notes = json_loads(record["notes"], {})
    pdf_bytes = generate_result_pdf(
        exam, stu, record, rows, qsvc.total_score(exam_id), notes=notes
    )

    filename = safe_pdf_filename(stu["name"], exam["title"])
    return send_file(io.BytesIO(pdf_bytes), as_attachment=True,
                     download_name=filename, mimetype="application/pdf")


def register(app):
    app.add_url_rule("/exam/<int:exam_id>/start", "start_exam", start_exam, methods=["POST"])
    app.add_url_rule("/exam/<int:exam_id>", "exam_page", exam_page)
    app.add_url_rule("/exam/<int:exam_id>/submit", "submit_exam", submit_exam, methods=["POST"])
    app.add_url_rule("/exam/<int:exam_id>/result", "result", result)
    app.add_url_rule("/exam/<int:exam_id>/notes", "save_notes", save_notes, methods=["POST"])
    app.add_url_rule("/exam/<int:exam_id>/original", "exam_original", exam_original)
    app.add_url_rule("/exam/<int:exam_id>/reflection", "reflection", reflection,
                     methods=["GET", "POST"])
    app.add_url_rule("/exam/<int:exam_id>/pdf", "exam_pdf", exam_pdf)
