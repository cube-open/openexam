"""学生端：面板 / 个人资料 / 成长分析。"""
from flask import flash, redirect, render_template, request, session, url_for

from ..db import get_db, write_txn
from ..security import current_student, hash_password, login_required_student, verify_password
from ..services import wrongbook
from ..services.stats import student_kp_analysis, student_rank_history
from ..utils import exam_window, fmt_ts


def _group_visibility(db, student):
    """一次性取出「考试 → 允许分组」映射，替代原来每场考试查一次。"""
    mapping: dict = {}
    for row in db.execute("SELECT exam_id, group_id FROM exam_groups").fetchall():
        mapping.setdefault(row["exam_id"], set()).add(row["group_id"])
    return mapping


@login_required_student
def dashboard():
    """考试大厅：按「进行中 / 待开考 / 已结束」分组展示。"""
    db = get_db()
    stu = current_student()
    exams = db.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
    allowed = _group_visibility(db, stu)
    # 一次取回该学生全部作答记录，替代原来「每场考试一次查询」
    mine = {
        row["exam_id"]: row
        for row in db.execute(
            "SELECT * FROM results WHERE student_id=?", (stu["id"],)
        ).fetchall()
    }

    items = []
    for exam in exams:
        gids = allowed.get(exam["id"])
        if gids and (stu["group_id"] or 0) not in gids:
            continue
        record = mine.get(exam["id"])
        if record and record["submitted_at"]:
            status, score = "submitted", record["score"]
        elif record and record["started_at"]:
            status, score = "in_progress", None
        else:
            status, score = "new", None
        win, win_label = exam_window(exam)
        items.append({
            "exam": exam,
            "status": status,
            "score": score,
            "result_id": record["id"] if record else None,
            "win": win,
            "win_label": win_label,
        })

    # 分组：
    #   进行中 —— 已经开考、没交卷
    #   待参加 —— 还没开考，或者正在开放但学生没开始（upcoming / open）
    #   已结束 —— 已交卷，或者考试窗口已关闭
    ongoing = [x for x in items if x["status"] == "in_progress"]
    upcoming = [x for x in items
                if x["status"] == "new" and x["win"] in ("open", "upcoming")]
    finished = [x for x in items
                if x["status"] == "submitted"
                or (x["status"] == "new" and x["win"] == "closed")]

    return render_template(
        "dashboard.html",
        stu=stu,
        items=items,
        ongoing=ongoing,
        upcoming=upcoming,
        finished=finished,
    )


@login_required_student
def profile():
    stu = current_student()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        old_pw = request.form.get("old_password") or ""
        new_pw = request.form.get("new_password") or ""
        if not verify_password(stu["password_hash"], old_pw):
            flash("当前密码不正确。", "error")
            return redirect(url_for("profile"))
        if not name:
            flash("姓名不能为空。", "error")
            return redirect(url_for("profile"))
        if new_pw and len(new_pw) < 6:
            flash("新密码至少 6 位。", "error")
            return redirect(url_for("profile"))

        with write_txn() as db:
            db.execute("UPDATE students SET name=? WHERE id=?", (name, stu["id"]))
            if new_pw:
                # 改密码 = 踢掉其它设备：版本 +1，并把自己这次会话同步过去
                new_sv = int(stu["session_version"] or 0) + 1
                db.execute(
                    "UPDATE students SET password_hash=?, session_version=?,"
                    " must_change_password=0 WHERE id=?",
                    (hash_password(new_pw), new_sv, stu["id"]),
                )
                session["sv"] = new_sv
                session["must_change_pwd"] = False
        session["student_name"] = name
        flash("资料已更新。", "success")
        return redirect(url_for("profile"))
    return render_template("profile.html", stu=stu)


@login_required_student
def student_growth():
    sid = current_student()["id"]
    return render_template(
        "growth.html",
        history=student_rank_history(sid),
        kp_analysis=student_kp_analysis(sid),
        fmt_ts=fmt_ts,
    )


@login_required_student
def student_wrong_book():
    """错题本：跨考试汇总所有做错的题。"""
    stu = current_student()
    data = wrongbook.collect(stu["id"])
    return render_template("wrong_book.html", stu=stu, fmt_ts=fmt_ts, **data)


def register(app):
    app.add_url_rule("/dashboard", "dashboard", dashboard)
    app.add_url_rule("/profile", "profile", profile, methods=["GET", "POST"])
    app.add_url_rule("/student/growth", "student_growth", student_growth)
    app.add_url_rule("/student/wrong-book", "student_wrong_book", student_wrong_book)
