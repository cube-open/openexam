"""后台：题目管理（单题增删改、JSON / Excel / CSV 导入、批量操作）。"""
import csv
import io
import json
import os
import re
import tempfile
import time
import uuid

from flask import (abort, flash, redirect, render_template, request, send_file,
                   session, url_for)

from .. import deps
from .. import rbac
from ..db import get_db, write_txn
from ..services import importers
from ..services import questions as qsvc
from ..services.exams import admin_exam
from ..utils import TYPE_LABELS, json_loads


def _parse_question_form(form):
    """把表单解析成统一的题目结构，返回 (data, error)。"""
    typ = form.get("type")
    question = (form.get("question") or "").strip()
    score = form.get("score", type=int) or 5
    explanation = (form.get("explanation") or "").strip()

    if typ == "choice":
        options = [o.strip() for o in form.getlist("option") if o.strip()]
        answer = (form.get("choice_answer") or "").strip()
        if not question or len(options) < 2 or not answer:
            return None, "请填写题干、至少两个选项并指定正确答案。"
        if answer not in "ABCDEFGH"[:len(options)]:
            return None, "正确答案超出了选项范围。"
    elif typ == "multi":
        options = [o.strip() for o in form.getlist("option") if o.strip()]
        answer = "".join(sorted(set((form.get("multi_answer") or "").strip().upper())))
        if not question or len(options) < 2 or not answer:
            return None, "请填写题干、至少两个选项并填写正确答案（如 ABD）。"
        if any(a not in "ABCDEFGH"[:len(options)] for a in answer):
            return None, "正确答案包含不存在的选项字母。"
    elif typ == "judge":
        options = ["正确", "错误"]
        answer = (form.get("judge_answer") or "").strip()
        if not question or answer not in ("A", "B"):
            return None, "请填写题干并选择判断答案。"
    elif typ == "blank":
        options = []
        answer = "|".join(a.strip() for a in (form.get("blank_answer") or "").split("|")
                          if a.strip())
        if not question or not answer:
            return None, "请填写题干和至少一个正确答案（多个用 | 分隔）。"
    else:
        return None, "未知题型。"

    return {
        "type": typ, "question": question, "options": options,
        "answer": answer, "score": score, "explanation": explanation,
    }, None


def _answer_column(data) -> str:
    if data["type"] == "blank":
        return json.dumps([a for a in data["answer"].split("|") if a], ensure_ascii=False)
    return data["answer"]


def _save_kp_links(db, question_id: int, kp_ids) -> None:
    db.execute("DELETE FROM question_kp WHERE question_id=?", (question_id,))
    rows = []
    for raw in kp_ids:
        try:
            rows.append((question_id, int(raw)))
        except (ValueError, TypeError):
            continue
    if rows:
        db.executemany(
            "INSERT OR IGNORE INTO question_kp (question_id, kp_id) VALUES (?, ?)", rows)


@rbac.require_perm("bank.manage")
def admin_questions(exam_id):
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    db = get_db()

    if request.method == "POST":
        data, error = _parse_question_form(request.form)
        if error:
            flash(error, "error")
        else:
            with write_txn() as txn:
                cur = txn.execute(
                    "INSERT INTO questions (exam_id, type, question, options, answer, score, explanation) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (exam_id, data["type"], data["question"],
                     json.dumps(data["options"], ensure_ascii=False),
                     _answer_column(data), data["score"], data["explanation"]),
                )
                # 用 lastrowid 而不是「再查一次最新一条」，否则并发加题会挂错题
                _save_kp_links(txn, cur.lastrowid, request.form.getlist("kp_ids"))
            qsvc.invalidate(exam_id)
            flash("%s题已添加。" % TYPE_LABELS.get(data["type"], ""), "success")
        return redirect(url_for("admin_questions", exam_id=exam_id))

    rows = db.execute(
        "SELECT * FROM questions WHERE exam_id=? ORDER BY id", (exam_id,)
    ).fetchall()

    items = []
    by_id = {}
    for row in rows:
        item = dict(row)
        item["options_list"] = json_loads(row["options"], [])
        item["type_label"] = TYPE_LABELS.get(row["type"], row["type"])
        item["kp_list"] = []
        item["children"] = []
        by_id[item["id"]] = item
        items.append(item)

    if by_id:
        placeholders = ",".join("?" * len(by_id))
        kp_rows = db.execute(
            "SELECT qkp.question_id, kp.id, kp.name FROM question_kp qkp "
            "JOIN knowledge_points kp ON kp.id=qkp.kp_id "
            "WHERE qkp.question_id IN (%s) ORDER BY kp.name" % placeholders,
            tuple(by_id.keys()),
        ).fetchall()
        for kr in kp_rows:
            by_id[kr["question_id"]]["kp_list"].append({"id": kr["id"], "name": kr["name"]})

    top_questions = []
    for item in items:
        parent_id = item["parent_id"]
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(item)
        else:
            top_questions.append(item)

    all_kps = db.execute("SELECT * FROM knowledge_points ORDER BY name").fetchall()
    return render_template("admin/questions.html", exam=exam,
                           questions=top_questions, all_kps=all_kps)


# ---------------------------------------------------------------------------
# 批量导入：解析 → 预览 → 确认（确认前不写库）
# ---------------------------------------------------------------------------

_PREVIEW_PREFIX = "exam_import_"
_PREVIEW_MAX_AGE = 6 * 3600  # 预览临时文件保留 6 小时
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _preview_path(token):
    return os.path.join(tempfile.gettempdir(), _PREVIEW_PREFIX + token + ".json")


def _sweep_previews():
    """清掉过期未确认的预览文件（上传后直接关页面就会留下）。"""
    try:
        names = os.listdir(tempfile.gettempdir())
    except OSError:
        return
    now = time.time()
    for name in names:
        if not (name.startswith(_PREVIEW_PREFIX) and name.endswith(".json")):
            continue
        try:
            if now - os.path.getmtime(os.path.join(tempfile.gettempdir(), name)) > _PREVIEW_MAX_AGE:
                os.remove(os.path.join(tempfile.gettempdir(), name))
        except OSError:
            pass


def _save_preview(payload):
    _sweep_previews()
    token = uuid.uuid4().hex
    with open(_preview_path(token), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return token


def _load_preview(token):
    if not token or not _TOKEN_RE.match(str(token)):
        return None
    try:
        with open(_preview_path(token), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _drop_preview(token):
    try:
        os.remove(_preview_path(token))
    except OSError:
        pass


def _start_import(exam_id, items, errors, dups, source):
    token = _save_preview({
        "exam_id": exam_id, "source": source,
        "items": items, "errors": errors, "dups": dups,
    })
    session["import_preview"] = {"token": token, "exam_id": exam_id}
    return redirect(url_for("admin_question_import_preview", exam_id=exam_id))


@rbac.require_perm("bank.manage")
def admin_import_questions(exam_id):
    """粘贴 JSON → 预览。"""
    if not admin_exam(exam_id):
        abort(404)
    raw = request.form.get("bulk") or ""
    if not raw.strip():
        flash("请先粘贴 JSON 内容。", "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))
    db = get_db()
    try:
        items, errors = importers.parse_questions_json(raw, db)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))
    if not items:
        flash("没有可导入的题目：%s" % ("；".join(errors[:5]) or "内容为空"), "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))
    dups = importers.find_duplicates(db, exam_id, items)
    return _start_import(exam_id, items, errors, dups, "粘贴的 JSON")


@rbac.require_perm("bank.manage")
def admin_import_questions_excel(exam_id):
    """上传 .xlsx / .csv → 预览。"""
    if not admin_exam(exam_id):
        abort(404)
    file = request.files.get("file")
    if not file or not file.filename:
        flash("请选择文件。", "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))
    name = file.filename.lower()
    if name.endswith(".csv"):
        parser, arg = importers.parse_questions_csv, file.read()
    elif name.endswith(".xlsx"):
        if not deps.has("openpyxl"):
            flash(deps.message("openpyxl"), "error")
            return redirect(url_for("admin_questions", exam_id=exam_id))
        parser, arg = importers.parse_questions_excel, file.stream
    else:
        flash("仅支持 .xlsx / .csv 文件。", "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))

    db = get_db()
    try:
        items, errors = parser(arg, db)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))
    except Exception as exc:
        flash("文件读取失败：%s" % exc, "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))
    if not items:
        flash("没有可导入的题目：%s" % ("；".join(errors[:5]) or "文件为空"), "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))
    dups = importers.find_duplicates(db, exam_id, items)
    return _start_import(exam_id, items, errors, dups, file.filename)


@rbac.require_perm("bank.manage")
def admin_question_import_preview(exam_id):
    """导入预览：确认才落库，可取消、可选跳过重复题。"""
    exam = admin_exam(exam_id)
    if not exam:
        abort(404)
    held = session.get("import_preview") or {}
    payload = _load_preview(held.get("token")) if held.get("exam_id") == exam_id else None
    if not payload:
        flash("没有待确认的导入（预览已过期），请重新上传。", "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))

    if request.method == "POST":
        action = request.form.get("action") or "confirm"
        _drop_preview(held["token"])
        session.pop("import_preview", None)
        if action == "cancel":
            flash("已取消本次导入。", "info")
            return redirect(url_for("admin_questions", exam_id=exam_id))

        items = payload["items"]
        if request.form.get("skip_dups") != "1":
            importers.clear_dup_flags(items)  # 用户要求连重复一起导入
        with write_txn() as txn:
            inserted, skipped = importers.insert_questions(txn, exam_id, items)
        qsvc.invalidate(exam_id)

        msg = "成功导入 %d 题" % inserted
        if skipped:
            msg += "，跳过重复 %d 题" % skipped
        err_n = len(payload.get("errors") or [])
        if err_n:
            msg += "，%d 条问题未导入" % err_n
        flash(msg + "。", "success" if inserted else "error")
        return redirect(url_for("admin_questions", exam_id=exam_id))

    return render_template("admin/import_preview.html", exam=exam, payload=payload)


@rbac.require_perm("bank.manage")
def admin_question_template():
    """下载导入模板。?fmt=csv 下 CSV，默认 xlsx。"""
    header = ["题型", "题干", "选项A", "选项B", "选项C", "选项D", "选项E", "选项F",
              "选项G", "选项H", "答案", "分值", "解析", "知识点"]
    samples = [
        ["单选", "示例：1 + 1 = ?", "1", "2", "3", "4", "", "", "", "", "B", 5,
         "十进制 1+1=2", ""],
        ["多选", "示例：下列哪些是偶数？", "2", "3", "4", "5", "", "", "", "", "AC", 5,
         "偶数是能被 2 整除的数", ""],
        ["判断", "示例：地球是圆的", "", "", "", "", "", "", "", "", "A", 5, "", ""],
        ["填空", "示例：HTTP 默认端口号", "", "", "", "", "", "", "", "", "80", 5,
         "HTTP 默认 80，HTTPS 默认 443", ""],
    ]

    if (request.args.get("fmt") or "").lower() == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        writer.writerows(samples)
        # 带 BOM，Excel 双击打开不乱码
        data = buf.getvalue().encode("utf-8-sig")
        return send_file(io.BytesIO(data), as_attachment=True,
                         download_name="题目导入模板.csv", mimetype="text/csv")

    if not deps.has("openpyxl"):
        flash(deps.message("openpyxl"), "error")
        return redirect(request.referrer or url_for("admin_dashboard"))
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "题目模板"
    ws.append(header)
    for row in samples:
        ws.append(row)
    for col, width in zip("ABCDEFGHIJKLMN",
                          [8, 42, 16, 16, 16, 16, 14, 14, 14, 14, 10, 8, 40, 18]):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="题目导入模板.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@rbac.require_perm("bank.manage")
def admin_delete_question(qid):
    db = get_db()
    row = db.execute("SELECT exam_id FROM questions WHERE id=?", (qid,)).fetchone()
    if not row:
        abort(404)
    exam_id = row["exam_id"]
    with write_txn() as txn:
        txn.execute(
            "DELETE FROM question_kp WHERE question_id IN "
            "(SELECT id FROM questions WHERE parent_id=? UNION SELECT ?)",
            (qid, qid),
        )
        txn.execute("DELETE FROM questions WHERE parent_id=?", (qid,))
        txn.execute("DELETE FROM questions WHERE id=?", (qid,))
    qsvc.invalidate(exam_id)
    flash("题目已删除。", "success")
    return redirect(url_for("admin_questions", exam_id=exam_id))


@rbac.require_perm("bank.manage")
def admin_edit_question(qid):
    db = get_db()
    question_row = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not question_row:
        abort(404)
    exam_id = question_row["exam_id"]

    if request.method == "POST":
        data, error = _parse_question_form(request.form)
        if error:
            flash("题目信息不完整，未保存。", "error")
        else:
            with write_txn() as txn:
                txn.execute(
                    "UPDATE questions SET type=?, question=?, options=?, answer=?, "
                    "score=?, explanation=? WHERE id=?",
                    (data["type"], data["question"],
                     json.dumps(data["options"], ensure_ascii=False),
                     _answer_column(data), data["score"], data["explanation"], qid),
                )
                _save_kp_links(txn, qid, request.form.getlist("kp_ids"))
            qsvc.invalidate(exam_id)
            flash("题目已更新。", "success")
        return redirect(url_for("admin_questions", exam_id=exam_id))

    options = json_loads(question_row["options"], [])
    blank_answer = ""
    if question_row["type"] == "blank":
        blank_answer = " | ".join(json_loads(question_row["answer"], []))
    exam = admin_exam(exam_id)
    selected_kps = [r["kp_id"] for r in db.execute(
        "SELECT kp_id FROM question_kp WHERE question_id=?", (qid,)).fetchall()]
    all_kps = db.execute("SELECT * FROM knowledge_points ORDER BY name").fetchall()
    return render_template("admin/question_edit.html", q=question_row, exam=exam,
                           options=options, blank_answer=blank_answer,
                           all_kps=all_kps, selected_kps=selected_kps)


@rbac.require_perm("bank.manage")
def admin_batch_questions(exam_id):
    data = request.get_json(silent=True) or {}
    qids = [int(q) for q in (data.get("qids") or []) if str(q).isdigit()]
    action = data.get("action")
    if not qids:
        return jsonify_error("未选择题目")
    if len(qids) > 500:
        return jsonify_error("单次最多操作 500 题")

    placeholders = ",".join("?" * len(qids))
    if action == "delete":
        with write_txn() as db:
            db.execute(
                "DELETE FROM question_kp WHERE question_id IN "
                "(SELECT id FROM questions WHERE parent_id IN (%s) UNION SELECT id FROM questions WHERE id IN (%s))"
                % (placeholders, placeholders),
                qids + qids,
            )
            db.execute("DELETE FROM questions WHERE parent_id IN (%s)" % placeholders, qids)
            db.execute("DELETE FROM questions WHERE id IN (%s) AND exam_id=?" % placeholders,
                       qids + [exam_id])
        qsvc.invalidate(exam_id)
        return jsonify_ok("已删除 %d 道题" % len(qids))

    if action == "set_score":
        try:
            score = int(data.get("score") or 5)
        except (ValueError, TypeError):
            score = 5
        with write_txn() as db:
            db.executemany("UPDATE questions SET score=? WHERE id=? AND exam_id=?",
                           [(score, qid, exam_id) for qid in qids])
        qsvc.invalidate(exam_id)
        return jsonify_ok("已将 %d 道题分值设为 %d" % (len(qids), score))

    if action == "set_type":
        typ = data.get("type")
        if typ not in ("choice", "multi"):
            return jsonify_error("仅支持在单选/多选之间批量切换")
        db = get_db()
        updates = []
        for qid in qids:
            row = db.execute("SELECT answer FROM questions WHERE id=?", (qid,)).fetchone()
            if not row:
                continue
            answer = (row["answer"] or "")[:1] if typ == "choice" else (row["answer"] or "")
            updates.append((typ, answer, qid, exam_id))
        if updates:
            with write_txn() as txn:
                txn.executemany(
                    "UPDATE questions SET type=?, answer=? WHERE id=? AND exam_id=?", updates)
        qsvc.invalidate(exam_id)
        return jsonify_ok("已更新 %d 道题题型" % len(updates))

    return jsonify_error("未知操作")


def jsonify_ok(msg):
    from flask import jsonify
    return jsonify({"ok": True, "msg": msg})


def jsonify_error(msg):
    from flask import jsonify
    return jsonify({"ok": False, "msg": msg}), 400


def register(app):
    app.add_url_rule("/admin/exams/<int:exam_id>/questions", "admin_questions",
                     admin_questions, methods=["GET", "POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/questions/import", "admin_import_questions",
                     admin_import_questions, methods=["POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/questions/import_excel",
                     "admin_import_questions_excel", admin_import_questions_excel,
                     methods=["POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/questions/import/preview",
                     "admin_question_import_preview", admin_question_import_preview,
                     methods=["GET", "POST"])
    app.add_url_rule("/admin/questions/template", "admin_question_template",
                     admin_question_template)
    app.add_url_rule("/admin/questions/<int:qid>/delete", "admin_delete_question",
                     admin_delete_question, methods=["POST"])
    app.add_url_rule("/admin/questions/<int:qid>/edit", "admin_edit_question",
                     admin_edit_question, methods=["GET", "POST"])
    app.add_url_rule("/admin/exams/<int:exam_id>/questions/batch", "admin_batch_questions",
                     admin_batch_questions, methods=["POST"])
