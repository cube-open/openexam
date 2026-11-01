"""第一阶段（权限与迁移）专项测试。

覆盖：迁移机制、角色权限、组织隔离、审计日志、首次登录改密、单点登录、
账号禁用。跑法和 smoke_test.py 一样，用临时数据库，不碰正式数据::

    python tests/test_phase1.py
"""
import os
import shutil
import sys
import tempfile

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, BASE)

TMP_DIR = tempfile.mkdtemp(prefix="exam_phase1_")
DB_PATH = os.path.join(TMP_DIR, "test.db")
os.environ["EXAM_DB"] = DB_PATH
os.environ["EXAM_BACKUP_DIR"] = os.path.join(TMP_DIR, "backups")

from examsys import create_app, migrations  # noqa: E402
from examsys.db import get_db  # noqa: E402
from examsys.security import hash_password  # noqa: E402

FAILED = []
PASSED = 0


def check(name, response, expect=(200,), location=None):
    global PASSED
    status = response.status_code
    ok = status in expect
    if ok and location:
        ok = (response.headers.get("Location") or "").endswith(location)
    if ok:
        PASSED += 1
        print("  ok   %-50s %s" % (name, status))
    else:
        FAILED.append(name)
        extra = ""
        if location:
            extra = " 期望跳转 %s，实际 %s" % (location, response.headers.get("Location"))
        print("  FAIL %-50s %s%s" % (name, status, extra))
    return response


def assert_eq(name, actual, expected):
    global PASSED
    if actual == expected:
        PASSED += 1
        print("  ok   %-50s %r" % (name, actual))
    else:
        FAILED.append(name)
        print("  FAIL %-50s 期望 %r 实际 %r" % (name, expected, actual))


def login(c, username, password, admin=True):
    url = "/admin/login" if admin else "/login"
    return c.post(url, data={"username": username, "password": password})


def main():
    app = create_app()

    # ------------------------------------------------------------------
    # 造数据：第二个组织 + 各角色账号 + 两场不同组织的考试
    # ------------------------------------------------------------------
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO organizations (id, name, code) VALUES (2, '第二机构', 'org2')")
        roles = {r["code"]: r["id"] for r in db.execute("SELECT id, code FROM roles")}
        people = [
            ("teacher1", "teacher", 1),
            ("orgadmin2", "org_admin", 2),
            ("teacher2", "teacher", 2),
        ]
        for username, role, org in people:
            cur = db.execute(
                "INSERT INTO admins (username, password_hash, org_id, role_id)"
                " VALUES (?, ?, ?, ?)",
                (username, hash_password("pass1234"), org, roles[role]))
            assert cur.lastrowid
        db.execute("UPDATE exams SET org_id=1")
        db.execute(
            "INSERT INTO exams (title, org_id, duration_minutes) VALUES ('第二机构试卷', 2, 30)")
        exam_org2 = db.execute(
            "SELECT id FROM exams WHERE title='第二机构试卷'").fetchone()["id"]
        db.execute(
            "INSERT INTO students (username, password_hash, name, student_no, org_id)"
            " VALUES ('stu_org2', ?, '外机构学生', '9001', 2)",
            (hash_password("123456"),))
        exam_org1 = db.execute(
            "SELECT id FROM exams WHERE org_id=1 ORDER BY id LIMIT 1").fetchone()["id"]
        # 模板里 张三 是输入框占位示例，断言会误判，换个独特名字
        db.execute("UPDATE students SET name='本部学生甲' WHERE username='student1'")
        db.commit()

    print("\n[迁移机制]")
    with app.app_context():
        db = get_db()
        done = migrations.applied(db)
        assert_eq("迁移 v1 已应用", 1 in done, True)
        assert_eq("迁移 v2 已应用", 2 in done, True)
        assert_eq("没有待执行迁移", migrations.pending(db), [])
        assert_eq("考试表有 org_id 列",
                  any(r[1] == "org_id" for r in db.execute("PRAGMA table_info(exams)")), True)
        assert_eq("管理员表有 role_id 列",
                  any(r[1] == "role_id" for r in db.execute("PRAGMA table_info(admins)")), True)

    print("\n[角色权限]")
    with app.test_client() as c:
        check("教师登录", login(c, "teacher1", "pass1234"), (302,), "/admin")
        check("教师 工作台", c.get("/admin"))
        check("教师 学生名单（有 user.view）", c.get("/admin/students"))
        check("教师 题库（有 bank.manage）", c.get("/admin/exams/%d/questions" % exam_org1))
        check("教师 成绩（有 grade.view）", c.get("/admin/exams/%d/results" % exam_org1))
        check("教师 系统设置 → 403", c.get("/admin/data"), (403,))
        check("教师 账号权限 → 403", c.get("/admin/admins"), (403,))
        check("教师 封禁IP → 403", c.get("/admin/ips"), (403,))
        check("教师 批量建号 → 403", c.post("/admin/students/batch", data={
            "bulk": "x1,p1,越权,3001"}), (403,))
        check("教师 建分组 → 403", c.post("/admin/groups", data={
            "action": "create", "name": "越权分组"}), (403,))

    with app.test_client() as c:
        check("组织管理员登录", login(c, "orgadmin2", "pass1234"), (302,))
        check("组织管理员 学生名单", c.get("/admin/students"))
        check("组织管理员 审计日志（有 system.audit）", c.get("/admin/audit"))
        check("组织管理员 账号权限 → 403", c.get("/admin/admins"), (403,))
        check("组织管理员 系统设置 → 403", c.get("/admin/data"), (403,))

    print("\n[组织隔离]")
    with app.test_client() as c:
        login(c, "orgadmin2", "pass1234")
        resp = c.get("/admin")
        assert_eq("组织管理员工作台可访问", resp.status_code, 200)
        body = resp.get_data(as_text=True)
        assert_eq("工作台含本机构考试", "第二机构试卷" in body, True)
        assert_eq("工作台不含外机构考试", "计算机基础测验" in body, False)
        check("访问外机构考试编辑页 → 404",
              c.get("/admin/exams/%d/edit" % exam_org1), (404,))
        check("访问外机构题目页 → 404",
              c.get("/admin/exams/%d/questions" % exam_org1), (404,))
        check("访问外机构成绩页 → 404",
              c.get("/admin/exams/%d/results" % exam_org1), (404,))
        body = c.get("/admin/students").get_data(as_text=True)
        assert_eq("名单含本机构学生", "外机构学生" in body, True)
        assert_eq("名单不含外机构学生", "本部学生甲" in body, False)

    with app.test_client() as c:
        login(c, "admin", "admin123")
        body = c.get("/admin").get_data(as_text=True)
        assert_eq("超管能看到两场考试",
                  "第二机构试卷" in body and "计算机基础测验" in body, True)
        check("超管 账号权限", c.get("/admin/admins"))

    print("\n[审计日志]")
    with app.app_context():
        db = get_db()
        before = db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    with app.test_client() as c:
        login(c, "admin", "admin123")
        c.post("/admin/exams/%d/delete" % exam_org2)
    with app.app_context():
        db = get_db()
        after = db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        assert_eq("删除考试写入了审计", after > before, True)
        row = db.execute(
            "SELECT * FROM audit_logs WHERE action='exam.delete' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert_eq("审计记录了动作", row["action"], "exam.delete")
        assert_eq("审计记录了操作者", row["actor_name"], "admin")
        assert_eq("审计记录了目标", row["target_id"], exam_org2)

    print("\n[首次登录改密]")
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO students (username, password_hash, name, student_no, org_id,"
            " must_change_password) VALUES ('newbie', ?, '新生', '7001', 1, 1)",
            (hash_password("123456"),))
        db.commit()
    with app.test_client() as c:
        check("新账号登录", login(c, "newbie", "123456", admin=False), (302,))
        check("未改密访问面板 → 跳改密页", c.get("/dashboard"), (302,), "/profile")
        check("未改密访问错题本 → 跳改密页", c.get("/student/wrong-book"), (302,), "/profile")
        check("改密页本身可访问", c.get("/profile"))
        check("改密后正常", c.post("/profile", data={
            "name": "新生", "old_password": "123456",
            "new_password": "newpass1"}), (302,))
        check("改密后可以看面板", c.get("/dashboard"))
    with app.app_context():
        db = get_db()
        row = db.execute("SELECT must_change_password FROM students WHERE username='newbie'").fetchone()
        assert_eq("改密后标记已清除", row["must_change_password"], 0)

    print("\n[单点登录 / 账号禁用]")
    # 两个独立 client 模拟两台设备（不能用 with，Flask 的上下文栈不支持嵌套）
    dev_a = app.test_client()
    dev_b = app.test_client()
    login(dev_a, "newbie", "newpass1", admin=False)
    check("设备A 可访问", dev_a.get("/dashboard"))
    login(dev_b, "newbie", "newpass1", admin=False)
    check("设备B 登录成功", dev_b.get("/dashboard"))
    check("设备A 被踢下线", dev_a.get("/dashboard"), (302,), "/login")

    with app.app_context():
        db = get_db()
        db.execute("UPDATE admins SET enabled=0 WHERE username='teacher1'")
        db.commit()
    with app.test_client() as c:
        resp = login(c, "teacher1", "pass1234")
        assert_eq("禁用账号不能登录", resp.status_code, 200)
        assert_eq("提示已禁用", "禁用" in resp.get_data(as_text=True), True)

    print("\n[迁移重跑与快照]")
    with app.app_context():
        db = get_db()
        # 模拟「v2 需要重跑」：删掉版本记录并回退结构。
        # 注意 DROP COLUMN 会连带丢掉 org_id 的取值，所以放在最后做。
        db.execute("DELETE FROM schema_migrations WHERE version=2")
        db.execute("DROP INDEX IF EXISTS idx_exams_org")
        db.execute("ALTER TABLE exams DROP COLUMN org_id")
        db.commit()
        result = migrations.apply_pending(db)
        assert_eq("重跑时应用了 v2", result["applied"], [2])
        assert_eq("重跑前生成了快照", bool(result["backup"]), True)
        assert_eq("快照文件存在", os.path.exists(result["backup"] or ""), True)
        assert_eq("回填后无 NULL 组织",
                  db.execute("SELECT COUNT(*) FROM exams WHERE org_id IS NULL").fetchone()[0], 0)
        assert_eq("再次执行无动作", migrations.apply_pending(db)["applied"], [])

    print("\n" + "=" * 64)
    print("通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
