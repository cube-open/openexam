"""统计：排名历史、知识点分析、讲评统计。"""
import json

from ..db import get_db
from ..utils import LETTERS
from . import questions as qsvc


def student_rank_history(student_id: int) -> list:
    """该学生参加过的所有考试的排名（单条 SQL）。"""
    rows = get_db().execute(
        "SELECT r.exam_id, r.score, r.submitted_at, e.title, "
        "  (SELECT COUNT(*)+1 FROM results r2 "
        "    WHERE r2.exam_id=r.exam_id AND r2.submitted_at IS NOT NULL AND r2.score > r.score) AS rank, "
        "  (SELECT COUNT(*) FROM results r3 "
        "    WHERE r3.exam_id=r.exam_id AND r3.submitted_at IS NOT NULL) AS total "
        "FROM results r JOIN exams e ON e.id=r.exam_id "
        "WHERE r.student_id=? AND r.submitted_at IS NOT NULL "
        "ORDER BY r.submitted_at",
        (student_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def student_kp_analysis(student_id: int) -> list:
    """按知识点聚合正确率，按正确率升序。

    原实现把 results 和该考试的全部题目做笛卡尔积再在 Python 里过滤，
    一场 50 题的考试考 10 次就是 500 行。现在只读 question_scores，
    每个 result 只解析一次 JSON。
    """
    db = get_db()
    rows = db.execute(
        "SELECT question_scores FROM results "
        "WHERE student_id=? AND submitted_at IS NOT NULL AND question_scores IS NOT NULL",
        (student_id,),
    ).fetchall()

    awarded_map: dict = {}  # qid(str) -> [每次考试的得分]
    for row in rows:
        try:
            scores = json.loads(row["question_scores"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(scores, dict):
            continue
        for qid, awarded in scores.items():
            awarded_map.setdefault(str(qid), []).append(awarded)

    if not awarded_map:
        return []

    qids = list(awarded_map.keys())
    placeholders = ",".join("?" * len(qids))
    full_map = {
        str(r["id"]): r["score"]
        for r in db.execute(
            "SELECT id, score FROM questions WHERE id IN (%s)" % placeholders, qids
        ).fetchall()
    }
    stats: dict = {}
    for qid, values in awarded_map.items():
        full = full_map.get(qid)
        if full is None:
            continue
        correct = 0
        for value in values:
            try:
                if float(value) == float(full):
                    correct += 1
            except (TypeError, ValueError):
                pass
        stats[qid] = [len(values), correct]

    if not stats:
        return []

    kp_rows = db.execute(
        "SELECT qkp.question_id, kp.id AS kp_id, kp.name "
        "FROM question_kp qkp JOIN knowledge_points kp ON kp.id=qkp.kp_id "
        "WHERE qkp.question_id IN (%s)" % placeholders,
        qids,
    ).fetchall()

    kp_stats: dict = {}
    for kr in kp_rows:
        qid = str(kr["question_id"])
        if qid not in stats:
            continue
        bucket = kp_stats.setdefault(kr["kp_id"], {"name": kr["name"], "total": 0, "correct": 0})
        bucket["total"] += stats[qid][0]
        bucket["correct"] += stats[qid][1]

    result = []
    for kpid, st in kp_stats.items():
        rate = round(st["correct"] / st["total"] * 100, 1) if st["total"] else 0
        result.append({
            "kp_id": kpid, "name": st["name"],
            "total": st["total"], "correct": st["correct"], "rate": rate,
        })
    result.sort(key=lambda x: x["rate"])
    return result


def compute_lecture_stats(exam_id: int) -> list:
    """讲评模式：每题作答统计 + 选项分布 + 选择该选项的学生名单。

    优化点：每个学生的 answers 只 json.loads 一次（原来每题解析一次，
    N 个学生 × Q 道题 = N×Q 次），答案集合也预先算好。
    """
    all_qs = qsvc.exam_questions(exam_id)
    qid_map = {q["id"]: q for q in all_qs}
    kids = qsvc.children_map(exam_id)

    flat = []
    for q in all_qs:
        if q["parent_id"]:
            continue
        if q["type"] == "group":
            flat.extend(kids.get(q["id"], []))
        else:
            flat.append(q)

    rows = get_db().execute(
        "SELECT r.answers, s.name FROM results r JOIN students s ON s.id=r.student_id "
        "WHERE r.exam_id=? AND r.submitted_at IS NOT NULL",
        (exam_id,),
    ).fetchall()

    parsed = []
    for row in rows:
        try:
            answers = json.loads(row["answers"] or "{}")
        except (ValueError, TypeError):
            answers = {}
        if not isinstance(answers, dict):
            answers = {}
        parsed.append((row["name"], answers))
    total = len(parsed)

    data = []
    for idx, q in enumerate(flat, 1):
        qid = str(q["id"])
        options = q["options"]
        letters = LETTERS[:max(len(options), 2)]
        option_counts = {letter: 0 for letter in letters}
        option_students = {letter: [] for letter in letters}

        if q["type"] == "blank":
            accepted = {a.strip().lower() for a in _json_list(q["answer"])}
        elif q["type"] == "multi":
            expected = set(q["answer"])
        else:
            expected = q["answer"]

        answered = correct = blank = 0
        correct_students, wrong_students, blank_students = [], [], []

        for name, answers in parsed:
            given = str(answers.get(qid) or "").strip()
            if not given:
                blank += 1
                blank_students.append(name)
                continue
            answered += 1
            if q["type"] == "blank":
                ok = given.lower() in accepted
            elif q["type"] == "multi":
                ok = set(given) == expected
                for letter in given:
                    if letter in option_counts:
                        option_counts[letter] += 1
                        option_students[letter].append(name)
            else:  # choice / judge
                ok = given == expected
                if given in option_counts:
                    option_counts[given] += 1
                    option_students[given].append(name)
            if ok:
                correct += 1
                correct_students.append(name)
            else:
                wrong_students.append(name)

        if q["type"] == "blank":
            answer_text = " / ".join(_json_list(q["answer"]))
        elif q["type"] == "judge":
            answer_text = "正确" if q["answer"] == "A" else "错误"
        elif q["type"] == "multi":
            answer_text = "、".join(q["answer"])
        else:
            answer_text = q["answer"]

        parent_text = None
        if q["parent_id"] and q["parent_id"] in qid_map:
            parent_text = qid_map[q["parent_id"]]["question"]

        data.append({
            "num": idx, "type": q["type"], "question": q["question"],
            "options": options, "answer": q["answer"], "answer_text": answer_text,
            "explanation": q["explanation"],
            "parent_text": parent_text,
            "stats": {
                "total": total, "answered": answered, "correct": correct,
                "wrong": answered - correct, "blank": blank,
                "rate": round(correct / answered * 100) if answered else 0,
                "option_counts": option_counts, "option_students": option_students,
                "correct_students": correct_students, "wrong_students": wrong_students,
                "blank_students": blank_students,
            },
        })
    return data


def _json_list(text) -> list:
    try:
        value = json.loads(text or "[]")
    except (ValueError, TypeError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]
