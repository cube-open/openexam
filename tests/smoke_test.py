"""端到端冒烟测试：在临时数据库上跑一遍所有页面与接口。

用法：
    python tests/smoke_test.py

会覆盖：学生登录 → 开考 → 保存 → 心跳 → 违规 → 交卷 → 成绩 → 原卷 → PDF，
以及后台全部页面、加题 / 导入 / 批量操作 / 导出。
"""
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, BASE)

TMP_DIR = tempfile.mkdtemp(prefix="exam_smoke_")
os.environ["EXAM_DB"] = os.path.join(TMP_DIR, "test.db")
os.environ["EXAM_BACKUP_DIR"] = os.path.join(TMP_DIR, "backups")

from examsys import create_app  # noqa: E402
from examsys import deps  # noqa: E402
from examsys.db import get_db  # noqa: E402

FAILED = []
SKIPPED = []
PASSED = 0


def skip(name, module):
    """可选依赖没装时，跳过对应检查而不是算失败。"""
    SKIPPED.append((name, module))
    print("  skip %-46s 未安装 %s" % (name, module))


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
        FAILED.append((name, status, response.data[:400]))
        print("  FAIL %-46s %s" % (name, status))
    return response


def assert_eq(name, actual, expected):
    global PASSED
    if actual == expected:
        PASSED += 1
        print("  ok   %-46s %s" % (name, actual))
    else:
        FAILED.append((name, "assert", ("期望 %r，实际 %r" % (expected, actual)).encode()))
        print("  FAIL %-46s 期望 %r 实际 %r" % (name, expected, actual))


def check_contains(name, response, needle):
    """断言页面正文里出现了某段文字——用来防「模板忘了渲染 flash」这类静默失败。"""
    global PASSED
    body = response.data.decode("utf-8", "replace")
    if needle in body:
        PASSED += 1
        print("  ok   %-46s 含「%s」" % (name, needle))
    else:
        FAILED.append((name, "missing", ("正文里没找到「%s」" % needle).encode()))
        print("  FAIL %-46s 正文里没找到「%s」" % (name, needle))


def main():
    app = create_app()
    app.config.update(TESTING=False)

    with app.app_context():
        db = get_db()
        exam_id = db.execute("SELECT id FROM exams ORDER BY id LIMIT 1").fetchone()["id"]
        student_id = db.execute("SELECT id FROM students LIMIT 1").fetchone()["id"]
        db.execute("INSERT INTO knowledge_points (name) VALUES ('测试知识点')")
        db.execute(
            "INSERT INTO question_kp (question_id, kp_id) "
            "SELECT q.id, kp.id FROM questions q, knowledge_points kp "
            "WHERE q.exam_id=? LIMIT 1", (exam_id,))

    print("\n[学生端]")
    with app.test_client() as c:
        check("GET /", c.get("/"), (302,))
        check("GET /login", c.get("/login"))
        check("POST /login 错误密码", c.post("/login", data={
            "username": "student1", "password": "wrong"}))
        check("POST /login 正确", c.post("/login", data={
            "username": "student1", "password": "123456"}), (302,))
        check("GET /dashboard", c.get("/dashboard"))
        check("GET /profile", c.get("/profile"))
        check("GET /student/growth", c.get("/student/growth"))
        check("GET /student/wrong-book", c.get("/student/wrong-book"))
        check("POST /exam/start", c.post("/exam/%d/start" % exam_id), (302,))
        check("GET /exam/<id>", c.get("/exam/%d" % exam_id))
        check("POST /api/save", c.post("/api/save", json={
            "exam_id": exam_id,
            "answers": {"1": "B", "2": "B", "3": "80", "4": "超文本标记语言"},
        }), expect_json={"ok": True})
        check("POST /api/heartbeat", c.post("/api/heartbeat", json={
            "exam_id": exam_id, "violations": 2}), expect_json={"ok": True})
        check("POST /api/violation", c.post("/api/violation", json={
            "exam_id": exam_id}), expect_json={"ok": True})
        check("POST /exam/submit", c.post("/exam/%d/submit" % exam_id, json={
            "answers": {"1": "B", "2": "B", "3": "80", "4": "超文本标记语言"},
        }), expect_json={"ok": True})
        # 交卷后不允许再改答案
        check("POST /api/save 交卷后拒绝", c.post("/api/save", json={
            "exam_id": exam_id, "answers": {"1": "A"}}), (409,))

        # 判分正确性：4 题全对 = 满分
        with app.app_context():
            from examsys.services import questions as qsvc
            row = get_db().execute(
                "SELECT score FROM results WHERE student_id=? AND exam_id=?",
                (student_id, exam_id)).fetchone()
            full = qsvc.total_score(exam_id)
            assert_eq("判分：得分等于满分", row["score"], full)
            assert_eq("满分与判分口径一致", full, 20)
        check("GET /exam/<id> 交卷后跳转", c.get("/exam/%d" % exam_id), (302,))
        check("GET /result", c.get("/exam/%d/result" % exam_id))
        check("GET /original", c.get("/exam/%d/original" % exam_id))
        if deps.has("reportlab"):
            check("GET /pdf", c.get("/exam/%d/pdf" % exam_id))
        else:
            skip("GET /pdf", "reportlab")
        check("GET /reflection", c.get("/exam/%d/reflection" % exam_id), (302,))
        check("GET /logout", c.get("/logout"), (302,))

    print("\n[后台]")
    with app.test_client() as c:
        check("GET /admin 未登录跳转", c.get("/admin"), (302,))
        check("POST /admin/login", c.post("/admin/login", data={
            "username": "admin", "password": "admin123"}), (302,))
        for path in ("/admin", "/admin/students", "/admin/groups",
                     "/admin/knowledge_points", "/admin/ips", "/admin/logins",
                     "/admin/admins", "/admin/password", "/admin/data"):
            check("GET %s" % path, c.get(path))
        for suffix in ("edit", "questions", "results", "monitor", "stats",
                       "lecture", "reflections"):
            check("GET /admin/exams/<id>/%s" % suffix,
                  c.get("/admin/exams/%d/%s" % (exam_id, suffix)))
        check("GET /admin/exams/<id>/live",
              c.get("/admin/exams/%d/live" % exam_id), expect_json={"ok": True})
        check("GET /admin/exams/<id>/reflections/live",
              c.get("/admin/exams/%d/reflections/live" % exam_id),
              expect_json={"ok": True})
        if deps.has("openpyxl"):
            check("GET /admin/exams/<id>/export",
                  c.get("/admin/exams/%d/export" % exam_id))
        else:
            skip("GET /admin/exams/<id>/export", "openpyxl")
        if deps.has("reportlab"):
            check("GET /admin/exams/<id>/export_pdf_zip",
                  c.get("/admin/exams/%d/export_pdf_zip" % exam_id))
        else:
            skip("GET /admin/exams/<id>/export_pdf_zip", "reportlab")
        if deps.has("openpyxl"):
            check("GET /admin/questions/template",
                  c.get("/admin/questions/template"))
        else:
            skip("GET /admin/questions/template", "openpyxl")
        check("GET /admin/students/<id>/growth",
              c.get("/admin/students/%d/growth" % student_id))

        with app.app_context():
            rid = get_db().execute(
                "SELECT id FROM results ORDER BY id DESC LIMIT 1").fetchone()["id"]
        check("GET /admin/results/<id>/view", c.get("/admin/results/%d/view" % rid))
        check("GET /admin/results/<id>/grade", c.get("/admin/results/%d/grade" % rid))
        if deps.has("reportlab"):
            check("GET /admin/results/<id>/pdf", c.get("/admin/results/%d/pdf" % rid))
        else:
            skip("GET /admin/results/<id>/pdf", "reportlab")
        check("POST /admin/results/<id>/score", c.post(
            "/admin/results/%d/score" % rid, data={"score": "17"}), (302,))
        # flash 必须能在成绩页真正显示出来（模板漏渲染曾是静默 bug）
        check_contains("flash 在成绩页可见", c.post(
            "/admin/results/%d/score" % rid, data={"score": "18"},
            follow_redirects=True), "成绩已修改")
        check("POST /admin/exams/<id>/regrade", c.post(
            "/admin/exams/%d/regrade" % exam_id), expect_json={"ok": True})

        print("\n[加题 / 导入 / 批量]")
        check("POST 添加单选题", c.post("/admin/exams/%d/questions" % exam_id, data={
            "type": "choice", "question": "冒烟测试题？",
            "option": ["甲", "乙"], "choice_answer": "A", "score": "3",
        }), (302,))
        check("POST 添加填空题", c.post("/admin/exams/%d/questions" % exam_id, data={
            "type": "blank", "question": "冒烟填空 ____", "blank_answer": "a|b",
            "score": "3",
        }), (302,))
        check("POST JSON 导入 → 预览页", c.post("/admin/exams/%d/questions/import" % exam_id,
                                       data={"bulk": json.dumps([
                                           {"type": "多选", "question": "多选冒烟",
                                            "options": ["1", "2", "3"], "answer": "A,C"},
                                           {"type": "判断", "question": "判断冒烟",
                                            "answer": "对"},
                                       ], ensure_ascii=False)}), (302,))
        check_contains("GET 导入预览页", c.get(
            "/admin/exams/%d/questions/import/preview" % exam_id), "多选冒烟")
        check("POST 确认导入", c.post(
            "/admin/exams/%d/questions/import/preview" % exam_id,
            data={"action": "confirm", "skip_dups": "1"}), (302,))
        with app.app_context():
            n = get_db().execute(
                "SELECT COUNT(*) c FROM questions WHERE exam_id=? AND question IN ('多选冒烟','判断冒烟')",
                (exam_id,)).fetchone()["c"]
        assert_eq("导入后题目落库", n, 2)
        with app.app_context():
            qid = get_db().execute(
                "SELECT id FROM questions WHERE exam_id=? ORDER BY id DESC LIMIT 1",
                (exam_id,)).fetchone()["id"]
        check("GET 编辑题目", c.get("/admin/questions/%d/edit" % qid))
        check("POST 编辑题目", c.post("/admin/questions/%d/edit" % qid, data={
            "type": "blank", "question": "改过的题干", "blank_answer": "x", "score": "2",
        }), (302,))
        check("POST 批量改分", c.post("/admin/exams/%d/questions/batch" % exam_id,
                                      json={"qids": [qid], "action": "set_score",
                                            "score": 4}), expect_json={"ok": True})
        check("POST 删除题目", c.post("/admin/questions/%d/delete" % qid), (302,))

        print("\n[学生 / 分组]")
        check("POST 批量建号", c.post("/admin/students", data={
            "action": "import",
            "bulk": "u1,p1,测试甲,2001\nu2,p2,测试乙,2002"}), (302,))
        check("POST 快速建号", c.post("/admin/students/quick", data={
            "names": "测试丙\n测试丁"}), (302,))
        check("POST 建分组", c.post("/admin/groups", data={
            "action": "create", "name": "一班"}), (302,))
        check("POST 批量禁用", c.post("/admin/students/batch", data={
            "action": "disable", "ids": "1,2"}), (302,))
        check("POST 批量启用", c.post("/admin/students/batch", data={
            "action": "enable", "ids": "1,2"}), (302,))
        check("POST 封禁 IP", c.post("/admin/ips/block", data={
            "ip": "10.9.9.9", "remark": "冒烟"}), (302,))
        check("POST 解封 IP", c.post("/admin/ips/unblock", data={
            "ip": "10.9.9.9"}), (302,))
        check("POST 发送消息", c.post("/admin/exams/%d/send_message" % exam_id,
                                      json={"content": "请注意时间",
                                            "student_ids": ["all"]}),
              expect_json={"ok": True})
        # 造一个「考试中」的记录，用于测试调整时间
        with app.app_context():
            db = get_db()
            live_sid = db.execute(
                "SELECT id FROM students WHERE username='u1'").fetchone()["id"]
            db.execute(
                "INSERT OR IGNORE INTO results (student_id, exam_id, started_at) "
                "VALUES (?, ?, strftime('%s','now'))", (live_sid, exam_id))
        check("POST 调整时间", c.post("/admin/exams/%d/set_time" % exam_id,
                                      json={"student_id": live_sid,
                                            "action": "add", "minutes": 5}),
              expect_json={"ok": True})
        check("GET /admin/export_all", c.get("/admin/export_all"))
        check("POST /admin/backup_now", c.post("/admin/backup_now"), (302,))
        check_contains("flash 在数据页可见", c.post(
            "/admin/backup_now", follow_redirects=True), "已生成快照")
        check("GET /healthz", c.get("/healthz"), expect_json={"ok": True})

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项%s" % (
        PASSED, len(FAILED),
        "，跳过 %d 项（可选依赖缺失）" % len(SKIPPED) if SKIPPED else ""))
    if SKIPPED:
        for name, module in SKIPPED:
            print("  - 跳过 %s：pip install %s 后重跑" % (name, module))
    for name, status, body in FAILED:
        print("\n--- %s (HTTP %s) ---" % (name, status))
        print(body.decode("utf-8", "replace")[:600])
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
