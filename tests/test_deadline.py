"""服务端强制时限测试。

用法：
    python tests/test_deadline.py

背景：前端的计时器只是「提示」，不是「约束」——改掉 JS 或者干脆断网，
学生就能一直答下去。这组用例锁住服务端的强制时限行为：

- 过了截止时间，/api/save 一律拒收，且不落库
- /api/heartbeat 报告 expired，剩余时间归零
- 重新进考场时服务端直接收卷，不再让页面挂着拖时间
- 超时交卷：宽限期内采信客户端答案，超出宽限只认截止前已落库的答案
- 考试窗口（end_at）关闭后同样拒收
- 超时秒数被记录下来，给老师看
"""
import json
import os
import sys
import tempfile

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, BASE)

TMP_DIR = tempfile.mkdtemp(prefix="exam_deadline_")
os.environ["EXAM_DB"] = os.path.join(TMP_DIR, "test.db")
os.environ["EXAM_BACKUP_DIR"] = os.path.join(TMP_DIR, "backups")

from examsys import create_app  # noqa: E402
from examsys.db import get_db  # noqa: E402
from examsys.utils import now_epoch  # noqa: E402

FAILED = []
PASSED = 0
GRACE = 30  # 与 Config.EXAM_SUBMIT_GRACE_SECONDS 保持一致
DURATION = 30  # 测试卷时长（分钟）


def check(name, response, expect=(200,), expect_json=None):
    global PASSED
    status = response.status_code
    ok = status in expect
    if ok and expect_json is not None:
        try:
            payload = json.loads(response.data.decode("utf-8"))
            ok = all(payload.get(k) == v for k, v in expect_json.items())
        except (ValueError, AttributeError):
            ok = False
    if ok:
        PASSED += 1
        print("  ok   %-46s %s" % (name, status))
    else:
        FAILED.append((name, status, response.data[:300]))
        print("  FAIL %-46s %s" % (name, status))
    return response


def assert_eq(name, actual, expected):
    global PASSED
    if actual == expected:
        PASSED += 1
        print("  ok   %-46s %r" % (name, actual))
    else:
        FAILED.append((name, "assert", ("期望 %r，实际 %r" % (expected, actual)).encode()))
        print("  FAIL %-46s 期望 %r 实际 %r" % (name, expected, actual))


def make_exam(app, title, duration=DURATION, end_at=None):
    """建一张测试卷 + 一道单选题（答案 B，10 分），返回 (exam_id, question_id)。"""
    with app.app_context():
        db = get_db()
        cur = db.execute(
            "INSERT INTO exams (title, description, duration_minutes, end_at, org_id) "
            "VALUES (?, '', ?, ?, 1)", (title, duration, end_at))
        exam_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO questions (exam_id, type, question, options, answer, score) "
            "VALUES (?, 'choice', '1+1=？', '[\"A\",\"B\",\"C\"]', 'B', 10)", (exam_id,))
        return exam_id, cur.lastrowid


def shift_start(app, exam_id, seconds_ago):
    """把本次作答的开始时间往前拨，制造「已经超时」的状态。"""
    with app.app_context():
        get_db().execute("UPDATE results SET started_at=? WHERE exam_id=?",
                         (now_epoch() - seconds_ago, exam_id))


def row_of(app, exam_id):
    with app.app_context():
        return get_db().execute(
            "SELECT * FROM results WHERE exam_id=? ORDER BY id DESC LIMIT 1",
            (exam_id,)).fetchone()


def saved_answers(app, exam_id):
    row = row_of(app, exam_id)
    return json.loads(row["answers"] or "{}")


def login(c):
    c.post("/login", data={"username": "student1", "password": "123456"})


def main():
    app = create_app()
    app.config.update(TESTING=False)

    with app.app_context():
        sid = get_db().execute("SELECT id FROM students LIMIT 1").fetchone()["id"]

    # ---------------------------------------------------------------
    print("\n[过期后：保存被拒、心跳报警、重新进考场自动收卷]")
    exam_id, qid = make_exam(app, "时限测试卷")
    with app.test_client() as c:
        login(c)
        check("开考", c.post("/exam/%d/start" % exam_id), (302,))
        check("正常保存作答", c.post("/api/save", json={
            "exam_id": exam_id, "answers": {str(qid): "B"}}), expect_json={"ok": True})

        # 时长 30 分钟，把开始时间往前拨 40 分钟 → 已超时 10 分钟
        shift_start(app, exam_id, DURATION * 60 + 600)

        r = c.post("/api/save", json={"exam_id": exam_id, "answers": {str(qid): "A"}})
        check("过期后 /api/save 拒收", r, (409,), expect_json={"expired": True})
        assert_eq("过期后的答案没被改写", saved_answers(app, exam_id), {str(qid): "B"})

        r = c.post("/api/heartbeat", json={"exam_id": exam_id, "violations": 0})
        check("过期后心跳报告 expired", r, expect_json={"expired": True, "remaining": 0})

        check("过期后进考场触发收卷", c.get("/exam/%d" % exam_id), (302,))
        row = row_of(app, exam_id)
        assert_eq("已自动结卷", bool(row["submitted_at"]), True)
        assert_eq("标记为自动交卷", row["auto_submitted"], 1)
        assert_eq("按截止前落库的答案判分", row["score"], 10)
        assert_eq("记录了超时秒数", row["overtime_seconds"] > 0, True)

    # ---------------------------------------------------------------
    print("\n[考试窗口关闭后：一律拒收]")
    closed_exam, q2 = make_exam(app, "窗口已关卷", end_at=now_epoch() - 60)
    with app.test_client() as c:
        login(c)
        with app.app_context():
            # 模拟「窗口关闭前已经开考」的答卷
            get_db().execute(
                "INSERT INTO results (student_id, exam_id, started_at) VALUES (?,?,?)",
                (sid, closed_exam, now_epoch() - 3600))
        r = c.post("/api/save", json={"exam_id": closed_exam, "answers": {str(q2): "B"}})
        check("窗口已关拒绝保存", r, (409,), expect_json={"expired": True})
        check("窗口已关不能开考", c.post("/exam/%d/start" % closed_exam), (302,))

    # ---------------------------------------------------------------
    print("\n[超时交卷：宽限期内采信客户端答案]")
    exam3, q3 = make_exam(app, "宽限期交卷")
    with app.test_client() as c:
        login(c)
        c.post("/exam/%d/start" % exam3)
        c.post("/api/save", json={"exam_id": exam3, "answers": {str(q3): "B"}})
        # 只超时 10 秒，在 30 秒宽限内
        shift_start(app, exam3, DURATION * 60 + 10)
        r = c.post("/exam/%d/submit" % exam3, json={"answers": {str(q3): "A"}})
        check("宽限期内交卷被受理", r, expect_json={"ok": True})
        assert_eq("宽限期内采信客户端答案", saved_answers(app, exam3), {str(q3): "A"})
        assert_eq("宽限期内不算超时", row_of(app, exam3)["overtime_seconds"] > 0, True)

    # ---------------------------------------------------------------
    print("\n[超时交卷：超出宽限只认截止前落库的答案]")
    exam4, q4 = make_exam(app, "超时交卷")
    with app.test_client() as c:
        login(c)
        c.post("/exam/%d/start" % exam4)
        c.post("/api/save", json={"exam_id": exam4, "answers": {str(q4): "B"}})
        # 超时 10 分钟，远超 30 秒宽限
        shift_start(app, exam4, DURATION * 60 + 600)
        r = c.post("/exam/%d/submit" % exam4, json={"answers": {str(q4): "A"}})
        check("超时交卷仍被受理", r, expect_json={"ok": True})
        assert_eq("超出宽限丢弃晚到的答案", saved_answers(app, exam4), {str(q4): "B"})
        row = row_of(app, exam4)
        assert_eq("超时按自动交卷处理", row["auto_submitted"], 1)
        assert_eq("超时秒数已记录", row["overtime_seconds"] > 500, True)
        assert_eq("按截止前的答案判分", row["score"], 10)

    # ---------------------------------------------------------------
    print("\n[老师加时间后，剩余时间跟着变]")
    exam5, q5 = make_exam(app, "加时间")
    with app.test_client() as c:
        login(c)
        c.post("/exam/%d/start" % exam5)
        r = c.post("/api/heartbeat", json={"exam_id": exam5, "violations": 0})
        payload = json.loads(r.data.decode("utf-8"))
        assert_eq("未超时时 remaining 正常", payload["expired"], False)
        assert_eq("未超时时 remaining 接近时长", payload["remaining"] > DURATION * 60 - 30, True)

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
    if FAILED:
        for name, status, body in FAILED:
            print("  FAIL %s (%s)" % (name, status))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
