"""题目导入专项测试：解析容错、查重、预览确认全流程。

跑法和 smoke_test.py 一样，用临时数据库，不碰正式数据::

    python tests/test_import.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, BASE)

TMP_DIR = tempfile.mkdtemp(prefix="exam_import_")
os.environ["EXAM_DB"] = os.path.join(TMP_DIR, "test.db")
os.environ["EXAM_BACKUP_DIR"] = os.path.join(TMP_DIR, "backups")

from examsys import create_app  # noqa: E402
from examsys.db import get_db, write_txn  # noqa: E402
from examsys.services import importers  # noqa: E402

FAILED = []
PASSED = 0


def ok(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  ok   %-52s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %-52s %s" % (name, detail))


def check(name, response, expect=(200,)):
    global PASSED
    status = response.status_code
    if status in expect:
        PASSED += 1
        print("  ok   %-52s %s" % (name, status))
    else:
        FAILED.append(name)
        print("  FAIL %-52s %s" % (name, status))
    return response


def contains(name, response, needle):
    global PASSED
    body = response.data.decode("utf-8", "replace")
    if needle in body:
        PASSED += 1
        print("  ok   %-52s 含「%s」" % (name, needle))
    else:
        FAILED.append(name)
        print("  FAIL %-52s 正文没有「%s」" % (name, needle))


def parse_one(raw):
    """便捷函数：解析单题 JSON，返回 (item, errors)。"""
    items, errors = importers.parse_questions_json(json.dumps([raw], ensure_ascii=False))
    return (items[0] if items else None), errors


def qcount(db, exam_id):
    return db.execute("SELECT COUNT(*) c FROM questions WHERE exam_id=?",
                      (exam_id,)).fetchone()["c"]


def main():
    app = create_app()
    with app.app_context():
        db = get_db()
        exam_id = db.execute("SELECT id FROM exams ORDER BY id LIMIT 1").fetchone()["id"]
        db.execute("INSERT INTO knowledge_points (name) VALUES ('图论')")
        db.execute("INSERT INTO knowledge_points (name) VALUES ('动态规划')")

    print("\n[JSON 解析：答案容错]")
    it, errs = parse_one({"type": "choice", "question": "Q1",
                          "options": ["苹果", "香蕉", "梨"], "answer": "B"})
    ok("单选答案字母", it and it["answer"] == "B" and not errs, errs)
    it, errs = parse_one({"type": "choice", "question": "Q2",
                          "options": ["苹果", "香蕉", "梨"], "answer": "2"})
    ok("单选答案序号", it and it["answer"] == "B", errs)
    it, errs = parse_one({"type": "choice", "question": "Q3",
                          "options": ["苹果", "香蕉", "梨"], "answer": "香蕉"})
    ok("单选答案选项原文", it and it["answer"] == "B", errs)
    it, errs = parse_one({"type": "choice", "question": "Q4",
                          "options": ["苹果", "香蕉", "梨"], "answer": "b."})
    ok("单选答案带句点", it and it["answer"] == "B", errs)
    it, errs = parse_one({"type": "multi", "question": "Q5",
                          "options": ["1", "2", "3", "4"], "answer": "ABD"})
    ok("多选答案连写", it and it["answer"] == "ABD", errs)
    it, errs = parse_one({"type": "multi", "question": "Q6",
                          "options": ["1", "2", "3", "4"], "answer": "A,D,B"})
    ok("多选答案逗号+排序去重", it and it["answer"] == "ABD", errs)
    it, errs = parse_one({"type": "多选", "question": "Q7",
                          "options": ["1", "2", "3", "4"], "answer": "A、B、D"})
    ok("多选答案顿号", it and it["answer"] == "ABD", errs)
    it, errs = parse_one({"type": "multi", "question": "Q8",
                          "options": ["苹果", "香蕉", "梨", "枣"], "answer": "苹果,梨"})
    ok("多选答案选项原文", it and it["answer"] == "AD", errs)
    for word, expect in [("对", "A"), ("错", "B"), ("T", "A"), ("F", "B"),
                         ("√", "A"), ("×", "B"), ("正确", "A"), ("错误", "B"),
                         ("是", "A"), ("否", "B")]:
        it, errs = parse_one({"type": "judge", "question": "QJ", "answer": word})
        ok("判断答案「%s」" % word, it and it["answer"] == expect, errs)
    it, errs = parse_one({"type": "judge", "question": "QJ", "answer": "A"})
    ok("判断答案 A", it and it["answer"] == "A", errs)
    it, errs = parse_one({"type": "blank", "question": "QB", "answer": "80|443"})
    ok("填空多答案", it and it["answer"] == ["80", "443"], errs)

    print("\n[JSON 解析：题型识别与选项清洗]")
    for alias, expect in [("单选", "choice"), ("单项选择题", "choice"), ("选择题", "choice"),
                          ("多选", "multi"), ("多项选择题", "multi"),
                          ("判断", "judge"), ("是非题", "judge"),
                          ("填空", "blank"), ("填空题", "blank"),
                          ("CHOICE", "choice"), ("Multiple", "multi")]:
        it, errs = parse_one({"type": alias, "question": "QT",
                              "options": ["a", "b"] if expect in ("choice", "multi") else [],
                              "answer": "A" if expect != "blank" else "x"})
        ok("题型别名「%s」" % alias, it and it["type"] == expect, errs)
    it, errs = parse_one({"question": "无题型有选项", "options": ["a", "b"], "answer": "A"})
    ok("题型留空→推断单选", it and it["type"] == "choice" and it["inferred"], errs)
    it, errs = parse_one({"question": "无题型多答案", "options": ["a", "b", "c"], "answer": "AC"})
    ok("题型留空→推断多选", it and it["type"] == "multi", errs)
    it, errs = parse_one({"question": "无题型判断", "answer": "正确"})
    ok("题型留空→推断判断", it and it["type"] == "judge", errs)
    it, errs = parse_one({"question": "无题型填空", "answer": "80"})
    ok("题型留空→推断填空", it and it["type"] == "blank", errs)
    it, errs = parse_one({"type": "choice", "question": "手写前缀",
                          "options": ["A. 苹果", "B．香蕉"], "answer": "A"})
    ok("选项去手写前缀", it and it["options"] == ["苹果", "香蕉"], it and it["options"])
    it, errs = parse_one({"type": "choice", "question": "默认分值", "options": ["a", "b"],
                          "answer": "A"})
    ok("分值留空默认 5", it and it["score"] == 5, errs)
    it, errs = parse_one({"type": "choice", "question": "坏分值", "options": ["a", "b"],
                          "answer": "A", "score": "五分"})
    ok("分值无法识别→报错", it is None and errs and "分值" in errs[0], errs)
    it, errs = parse_one({"type": "choice", "question": "超范围分值",
                          "options": ["a", "b"], "answer": "A", "score": 200})
    ok("分值超范围→报错", it is None and errs, errs)
    it, errs = parse_one({"type": "choice", "question": "选项不足", "options": ["a"],
                          "answer": "A"})
    ok("只有一个选项→报错", it is None and errs, errs)
    it, errs = parse_one({"type": "choice", "question": "答案超范围",
                          "options": ["a", "b"], "answer": "C"})
    ok("答案字母超范围→报错", it is None and errs, errs)
    it, errs = parse_one({"type": "choice", "question": "单选填多个", "options": ["a", "b"],
                          "answer": "AB"})
    ok("单选识别出多个→报错", it is None and errs, errs)
    it, errs = parse_one({"type": "judge", "question": "判断答案不认识", "answer": "也许"})
    ok("判断答案不认识→报错", it is None and errs, errs)
    it, errs = parse_one({"type": "unknown", "question": "题型不认识", "answer": "x"})
    ok("题型不认识→报错", it is None and errs, errs)

    print("\n[JSON 解析：子题 / 知识点 / 语法错误]")
    items, errs = importers.parse_questions_json(json.dumps([{
        "type": "group", "question": "材料题",
        "sub": [
            {"type": "judge", "question": "子1", "answer": "A"},
            {"type": "choice", "question": "子2", "options": ["x", "y"], "answer": "B"},
        ]}]))
    ok("子题挂到父题上", len(items) == 1 and len(items[0]["subs"]) == 2 and not errs, errs)
    ok("group 父题无选项无答案", items and items[0]["options"] == [] and items[0]["answer"] == "")
    with app.app_context():
        items, errs = importers.parse_questions_json(json.dumps([
            {"type": "blank", "question": "挂知识点", "answer": "x", "kp": ["图论"]},
            {"type": "blank", "question": "坏知识点", "answer": "x", "kp": ["不存在"]},
        ]), get_db())
    ok("已知知识点挂上", items and items[0]["kps"] == ["图论"], items and items[0]["kps"])
    ok("未知知识点警告但不丢题", len(items) == 2 and items[1]["kps"] == []
       and any("不存在" in e for e in errs), errs)
    try:
        importers.parse_questions_json("{不是 json")
        ok("JSON 语法错误抛 ValueError", False)
    except ValueError as exc:
        ok("JSON 语法错误抛 ValueError", "JSON" in str(exc))

    print("\n[Excel / CSV 解析]")
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["题型", "题干", "选项A", "选项B", "选项C", "选项D", "答案", "分值", "解析", "知识点"])
    ws.append(["单选", "Excel 题", "1", "2", "3", "4", "B", 5, "解析内容", "图论"])
    ws.append(["", "没填题型", "甲", "乙", "", "", "A", 3, "", ""])
    ws.append(["判断", "Excel 判断", "", "", "", "", "对", 2, "", ""])
    ws.append(["多选", "Excel 多选", "w", "x", "y", "z", "A,C", 4, "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    with app.app_context():
        items, errs = importers.parse_questions_excel(buf, get_db())
    ok("Excel 四行全解析", len(items) == 4 and not errs, errs)
    ok("Excel 表头自动跳过", items and items[0]["question"] == "Excel 题")
    ok("Excel 答案字母", items and items[0]["answer"] == "B")
    ok("Excel 题型留空自动识别", items and items[1]["type"] == "choice" and items[1]["inferred"])
    ok("Excel 判断题同义词", items and items[2]["answer"] == "A")
    ok("Excel 多选逗号答案", items and items[3]["answer"] == "AC")
    ok("Excel 知识点列", items and items[0]["kps"] == ["图论"])

    # 无表头 CSV（utf-8-sig，模拟 Excel 另存）
    csv_text = "单选,CSV 题,1,2,3,4,B,5,,\n判断,CSV 判断,,,,,错误,2,,\n"
    with app.app_context():
        items, errs = importers.parse_questions_csv(csv_text.encode("utf-8-sig"), get_db())
    ok("CSV 无表头两行", len(items) == 2 and not errs, errs)
    ok("CSV 判断题「错误」", items and items[1]["answer"] == "B")

    # GBK 编码 + 表头
    csv_text2 = "题型,题干,选项A,选项B,答案,分值\n单选,GBK 题,甲,乙,A,5\n"
    with app.app_context():
        items, errs = importers.parse_questions_csv(csv_text2.encode("gbk"), get_db())
    ok("CSV GBK 编码+表头跳过", len(items) == 1 and not errs
       and items[0]["question"] == "GBK 题", errs)

    print("\n[查重]")
    with app.app_context():
        db = get_db()
        before = qcount(db, exam_id)
        items, _ = importers.parse_questions_json(json.dumps([
            {"type": "blank", "question": "重复测试题", "answer": "x"},
            {"type": "blank", "question": "重复测试题", "answer": "y"},
            {"type": "blank", "question": "新鲜题", "answer": "z"},
        ]))
        dups = importers.find_duplicates(db, exam_id, items)
        ok("文件内重复被标记", dups == 1 and items[1]["dup"] and not items[0]["dup"], dups)
        with write_txn() as txn:
            inserted, skipped = importers.insert_questions(txn, exam_id, items)
        ok("插入时跳过重复", inserted == 2 and skipped == 1, (inserted, skipped))
        after = qcount(db, exam_id)
        ok("题库只多两条", after - before == 2, after - before)
        # 二次导入同一份 → 全部判重
        items2, _ = importers.parse_questions_json(json.dumps([
            {"type": "blank", "question": "重复测试题", "answer": "x"},
        ]))
        dups2 = importers.find_duplicates(db, exam_id, items2)
        ok("库内重复被标记", dups2 == 1 and items2[0]["dup"], dups2)
        importers.clear_dup_flags(items2)
        ok("clear_dup_flags 清标记", not items2[0]["dup"])
        # 子题 + 知识点落库
        items3, _ = importers.parse_questions_json(json.dumps([{
            "type": "choice", "question": "父题带子题", "options": ["a", "b"], "answer": "A",
            "kp": ["图论"],
            "sub": [{"type": "blank", "question": "子题", "answer": "x", "kp": ["动态规划"]}],
        }]))
        with write_txn() as txn:
            inserted3, _ = importers.insert_questions(txn, exam_id, items3)
        row = db.execute("SELECT id FROM questions WHERE question='父题带子题'").fetchone()
        subs = db.execute("SELECT id, parent_id FROM questions WHERE question='子题'").fetchall()
        kp_links = db.execute(
            "SELECT COUNT(*) c FROM question_kp WHERE question_id IN (?, ?)",
            (row["id"], subs[0]["id"])).fetchone()["c"]
        ok("父子题一起落库", inserted3 == 2 and subs and subs[0]["parent_id"] == row["id"])
        ok("知识点关联父子题", kp_links == 2, kp_links)

    print("\n[预览确认全流程（HTTP）]")
    with app.test_client() as c:
        c.post("/admin/login", data={"username": "admin", "password": "admin123"})
        bulk = json.dumps([
            {"type": "choice", "question": "流程单选题", "options": ["a", "b"], "answer": "A"},
            {"type": "judge", "question": "流程判断题", "answer": "对"},
            {"type": "blank", "question": "流程填空题", "answer": "80"},
        ], ensure_ascii=False)
        check("POST 导入跳转预览", c.post(
            "/admin/exams/%d/questions/import" % exam_id, data={"bulk": bulk}), (302,))
        contains("预览页展示题目", c.get(
            "/admin/exams/%d/questions/import/preview" % exam_id), "流程单选题")
        check("确认导入", c.post(
            "/admin/exams/%d/questions/import/preview" % exam_id,
            data={"action": "confirm", "skip_dups": "1"}), (302,))
        with app.app_context():
            n = get_db().execute(
                "SELECT COUNT(*) c FROM questions WHERE exam_id=? AND question LIKE '流程%'",
                (exam_id,)).fetchone()["c"]
        ok("确认后三题落库", n == 3, n)
        # 预览过期
        check("预览过期被拒", c.get(
            "/admin/exams/%d/questions/import/preview" % exam_id), (302,))

        # 重复导入 → 预览标重复 → 跳过
        c.post("/admin/exams/%d/questions/import" % exam_id, data={"bulk": bulk})
        contains("预览页标出重复", c.get(
            "/admin/exams/%d/questions/import/preview" % exam_id), "重复")
        c.post("/admin/exams/%d/questions/import/preview" % exam_id,
               data={"action": "confirm", "skip_dups": "1"})
        with app.app_context():
            n = get_db().execute(
                "SELECT COUNT(*) c FROM questions WHERE exam_id=? AND question LIKE '流程%'",
                (exam_id,)).fetchone()["c"]
        ok("重复导入被跳过", n == 3, n)

        # 不跳过重复 → 全导
        c.post("/admin/exams/%d/questions/import" % exam_id, data={"bulk": bulk})
        c.post("/admin/exams/%d/questions/import/preview" % exam_id,
               data={"action": "confirm"})
        with app.app_context():
            n = get_db().execute(
                "SELECT COUNT(*) c FROM questions WHERE exam_id=? AND question LIKE '流程%'",
                (exam_id,)).fetchone()["c"]
        ok("不跳过重复全导入", n == 6, n)

        # 取消
        c.post("/admin/exams/%d/questions/import" % exam_id, data={"bulk": bulk})
        c.post("/admin/exams/%d/questions/import/preview" % exam_id,
               data={"action": "cancel"})
        with app.app_context():
            n = get_db().execute(
                "SELECT COUNT(*) c FROM questions WHERE exam_id=? AND question LIKE '流程%'",
                (exam_id,)).fetchone()["c"]
        ok("取消后不导入", n == 6, n)

        # CSV 文件上传
        csv_bytes = "题型,题干,选项A,选项B,答案,分值\n单选,上传的CSV题,甲,乙,B,4\n".encode(
            "utf-8-sig")
        check("POST 上传 CSV", c.post(
            "/admin/exams/%d/questions/import_excel" % exam_id,
            data={"file": (io.BytesIO(csv_bytes), "q.csv")},
            content_type="multipart/form-data"), (302,))
        contains("CSV 预览页", c.get(
            "/admin/exams/%d/questions/import/preview" % exam_id), "上传的CSV题")
        c.post("/admin/exams/%d/questions/import/preview" % exam_id,
               data={"action": "confirm", "skip_dups": "1"})
        with app.app_context():
            n = get_db().execute(
                "SELECT COUNT(*) c FROM questions WHERE exam_id=? AND question='上传的CSV题'",
                (exam_id,)).fetchone()["c"]
        ok("CSV 上传确认落库", n == 1, n)

        # 错误文件类型
        check("错误扩展名被拒", c.post(
            "/admin/exams/%d/questions/import_excel" % exam_id,
            data={"file": (io.BytesIO(b"x"), "q.txt")},
            content_type="multipart/form-data"), (302,))

        # 空 JSON
        check("空 JSON 被拒", c.post(
            "/admin/exams/%d/questions/import" % exam_id, data={"bulk": "  "}), (302,))

        # 模板下载
        r = c.get("/admin/questions/template")
        ok("xlsx 模板可下载", r.status_code == 200 and len(r.data) > 1000)
        r = c.get("/admin/questions/template?fmt=csv")
        body = r.data.decode("utf-8-sig")
        ok("csv 模板可下载且带 BOM", r.status_code == 200
           and r.data[:3] == b"\xef\xbb\xbf" and "题型" in body and "知识点" in body)

        # 单题表单：答案超范围
        check("单题表单答案超范围被拒", c.post(
            "/admin/exams/%d/questions" % exam_id, data={
                "type": "choice", "question": "范围题", "option": ["a", "b"],
                "choice_answer": "D"}), (302,))
        with app.app_context():
            n = get_db().execute(
                "SELECT COUNT(*) c FROM questions WHERE question='范围题'",
                ).fetchone()["c"]
        ok("超范围答案没有落库", n == 0, n)

    print("\n============================================================")
    print("通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
    if FAILED:
        print("失败项：%s" % "、".join(FAILED))
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
