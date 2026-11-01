"""考试过程中的高频接口：保存答案、心跳、违规上报。

心跳是整套系统里最容易被放大成瓶颈的接口：一个班 50 人、每 3 秒一次，
原来每次都要 UPDATE last_seen + SELECT 未读消息 + 提交事务，等于每秒 17 次写库。
现在只有「违规数变了」或「距上次落库超过 3 秒」才真正写，读侧仍然实时。
"""
import json
import threading

from flask import jsonify, request, session, url_for

from ..config import Config
from ..db import get_db, write_txn
from ..security import login_required_student
from ..services.exams import attempt_deadline, attempt_overtime
from ..utils import exam_window, now_epoch

_flush_state: dict = {}
_flush_lock = threading.Lock()


def _should_flush(key, now: int) -> bool:
    with _flush_lock:
        last = _flush_state.get(key)
        if last is not None and now - last < Config.LAST_SEEN_FLUSH_INTERVAL:
            return False
        _flush_state[key] = now
        if len(_flush_state) > 5000:
            cutoff = now - 600
            for k in [k for k, v in _flush_state.items() if v < cutoff]:
                _flush_state.pop(k, None)
        return True


def _reset_flush_state():
    with _flush_lock:
        _flush_state.clear()


@login_required_student
def api_save():
    """保存作答进度（定时上传 / 切屏兜底），不判分。"""
    data = request.get_json(silent=True) or {}
    exam_id = data.get("exam_id")
    answers = data.get("answers") or {}
    if not exam_id:
        return jsonify({"ok": False, "msg": "缺少 exam_id"}), 400
    if not isinstance(answers, dict) or len(answers) > Config.MAX_ANSWERS_ITEMS:
        return jsonify({"ok": False, "msg": "答案格式错误"}), 400
    for value in answers.values():
        if isinstance(value, (dict, list)) or len(str(value)) > Config.MAX_ANSWER_LENGTH:
            return jsonify({"ok": False, "msg": "答案格式错误"}), 400

    payload = json.dumps(answers, ensure_ascii=False)
    now = now_epoch()
    sid = session["student_id"]

    with write_txn() as db:
        exam = db.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
        if not exam:
            return jsonify({"ok": False, "msg": "试卷不存在"}), 404
        row = db.execute(
            "SELECT id, submitted_at, started_at, time_offset FROM results "
            "WHERE student_id=? AND exam_id=?",
            (sid, exam_id),
        ).fetchone()
        if row and row["submitted_at"]:
            # 已交卷的答卷不允许再被改写
            return jsonify({"ok": False, "closed": True, "msg": "已交卷",
                            "redirect": url_for("result", exam_id=exam_id)}), 409
        # 考试窗口已关：不再接收任何作答（也堵住「不点开始、直接调接口」的绕路）
        if exam_window(exam)[0] == "closed":
            return jsonify({"ok": False, "expired": True, "msg": "考试已结束"}), 409
        # 服务端强制时限：过了截止时间一律拒收。改 JS、拔网线都拖不动时间。
        if row and attempt_overtime(exam, row) > 0:
            return jsonify({"ok": False, "expired": True,
                            "msg": "考试时间已到，已自动交卷"}), 409
        if row:
            db.execute(
                "UPDATE results SET answers=?, last_seen=?, answered_count=? WHERE id=?",
                (payload, now, len(answers), row["id"]),
            )
        else:
            db.execute(
                "INSERT INTO results (student_id, exam_id, answers, started_at, last_seen, answered_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, exam_id, payload, now, now, len(answers)),
            )
    return jsonify({"ok": True})


@login_required_student
def api_heartbeat():
    """心跳：更新在线状态、同步客户端切屏次数、下发监考消息与剩余时间。"""
    data = request.get_json(silent=True) or {}
    exam_id = data.get("exam_id")
    if not exam_id:
        return jsonify({"ok": False}), 400

    sid = session["student_id"]
    db = get_db()
    # 一次 JOIN 拿到作答记录 + 考试时长，少一次往返
    row = db.execute(
        "SELECT r.id, r.violations, r.submitted_at, r.started_at, r.time_offset, "
        "       e.duration_minutes, e.end_at "
        "FROM results r JOIN exams e ON e.id = r.exam_id "
        "WHERE r.student_id=? AND r.exam_id=?",
        (sid, exam_id),
    ).fetchone()
    if not row:
        return jsonify({"ok": True, "violations": 0, "messages": [], "remaining": 0})
    if row["submitted_at"]:
        return jsonify({"ok": True, "violations": row["violations"],
                        "messages": [], "remaining": 0})

    try:
        client_violations = int(data.get("violations") or 0)
    except (ValueError, TypeError):
        client_violations = 0
    new_violations = max(row["violations"], client_violations)

    now = now_epoch()
    flush = _should_flush((sid, exam_id), now)
    violations_changed = new_violations != row["violations"]

    unread = db.execute(
        "SELECT id, content, created_at FROM messages "
        "WHERE exam_id=? AND student_id=? AND read_at IS NULL ORDER BY created_at",
        (exam_id, sid),
    ).fetchall()
    messages = [{"id": m["id"], "content": m["content"], "created_at": m["created_at"]}
                for m in unread]

    if violations_changed or flush or messages:
        with write_txn() as txn:
            if violations_changed:
                txn.execute("UPDATE results SET violations=? WHERE id=?",
                            (new_violations, row["id"]))
            if flush:
                txn.execute("UPDATE results SET last_seen=? WHERE id=?", (now, row["id"]))
            if messages:
                txn.execute(
                    "UPDATE messages SET read_at=? "
                    "WHERE exam_id=? AND student_id=? AND read_at IS NULL",
                    (now, exam_id, sid),
                )

    # 剩余时间由服务端统一计算，和保存 / 交卷 / 监考看板用的是同一套算法
    deadline = attempt_deadline(row, row)
    remaining = max(0, deadline - now) if deadline else 0
    expired = bool(deadline) and now > deadline

    return jsonify({
        "ok": True,
        "violations": new_violations,
        "messages": messages,
        "remaining": remaining,
        "expired": expired,
    })


@login_required_student
def api_violation():
    data = request.get_json(silent=True) or {}
    exam_id = data.get("exam_id")
    db = get_db()
    exam = db.execute(
        "SELECT id, max_violations FROM exams WHERE id=?", (exam_id,)
    ).fetchone()
    if not exam:
        return jsonify({"ok": False, "msg": "试卷不存在"}), 400

    with write_txn() as txn:
        txn.execute(
            "UPDATE results SET violations=violations+1 "
            "WHERE student_id=? AND exam_id=?",
            (session["student_id"], exam_id),
        )
        row = txn.execute(
            "SELECT violations FROM results WHERE student_id=? AND exam_id=?",
            (session["student_id"], exam_id),
        ).fetchone()
    return jsonify({
        "ok": True,
        "violations": row["violations"] if row else 0,
        "max": exam["max_violations"],
    })


def register(app):
    app.add_url_rule("/api/save", "api_save", api_save, methods=["POST"])
    app.add_url_rule("/api/heartbeat", "api_heartbeat", api_heartbeat, methods=["POST"])
    app.add_url_rule("/api/violation", "api_violation", api_violation, methods=["POST"])
