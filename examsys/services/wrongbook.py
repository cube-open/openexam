"""错题本：汇总学生所有「成绩已公开」考试里的错题。

数据来源是 results.question_scores（每题得分）+ questions（题干/答案/解析），
所以不需要重新判分，也不会因为题库改动而算错——存的是当时的判分结果。

三个查询搞定：作答记录 → 题目 → 知识点关联，不按考试循环查库。
"""
from ..db import get_db
from ..utils import TYPE_LABELS, json_loads

MAX_ITEMS = 400


def _answer_display(question) -> str:
    qtype = question["type"]
    if qtype == "blank":
        accepted = json_loads(question["answer"], [])
        return " / ".join(str(a) for a in accepted)
    if qtype == "judge":
        return "正确" if question["answer"] == "A" else "错误"
    if qtype == "multi":
        return "、".join(question["answer"])
    return question["answer"]


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def collect(student_id: int, max_items: int = MAX_ITEMS) -> dict:
    db = get_db()
    records = db.execute(
        "SELECT r.exam_id, r.question_scores, r.answers, r.submitted_at, "
        "       e.title, e.show_answer, e.show_explanation "
        "FROM results r JOIN exams e ON e.id = r.exam_id "
        "WHERE r.student_id=? AND r.submitted_at IS NOT NULL AND e.show_result=1 "
        "ORDER BY r.submitted_at DESC",
        (student_id,),
    ).fetchall()
    if not records:
        return {"groups": [], "total": 0, "kp_summary": []}

    parsed = []
    all_qids = set()
    for record in records:
        scores = json_loads(record["question_scores"], {})
        if not isinstance(scores, dict) or not scores:
            continue
        answers = json_loads(record["answers"], {})
        parsed.append((record, scores, answers if isinstance(answers, dict) else {}))
        all_qids.update(str(qid) for qid in scores)

    if not all_qids:
        return {"groups": [], "total": 0, "kp_summary": []}

    qids = tuple(all_qids)
    placeholders = ",".join("?" * len(qids))
    questions = {
        str(row["id"]): row
        for row in db.execute(
            "SELECT id, exam_id, type, question, options, answer, score, explanation "
            "FROM questions WHERE id IN (%s)" % placeholders, qids
        ).fetchall()
    }
    kp_map: dict = {}
    for row in db.execute(
        "SELECT qkp.question_id, kp.name FROM question_kp qkp "
        "JOIN knowledge_points kp ON kp.id = qkp.kp_id "
        "WHERE qkp.question_id IN (%s) ORDER BY kp.name" % placeholders, qids
    ).fetchall():
        kp_map.setdefault(str(row["question_id"]), []).append(row["name"])

    groups = []
    kp_counter: dict = {}
    total = 0

    for record, scores, answers in parsed:
        items = []
        for qid, awarded in scores.items():
            qid = str(qid)
            question = questions.get(qid)
            if question is None:
                continue  # 题目已被删除
            full = _num(question["score"])
            if _num(awarded) >= full and full > 0:
                continue  # 拿满分，不算错题

            given = str(answers.get(qid) or "").strip()
            names = kp_map.get(qid, [])
            for name in names:
                kp_counter[name] = kp_counter.get(name, 0) + 1

            items.append({
                "id": qid,
                "type": question["type"],
                "type_label": TYPE_LABELS.get(question["type"], question["type"]),
                "question": question["question"],
                "options": json_loads(question["options"], []),
                "answer": question["answer"],
                "answer_display": _answer_display(question),
                "given": given,
                "explanation": question["explanation"] or "",
                "score": full,
                "awarded": _num(awarded),
                "kp": names,
            })
            total += 1
            if total >= max_items:
                break

        if items:
            groups.append({
                "exam_id": record["exam_id"],
                "title": record["title"],
                "submitted_at": record["submitted_at"],
                "show_answer": record["show_answer"],
                "show_explanation": record["show_explanation"],
                "items": items,
            })
        if total >= max_items:
            break

    kp_summary = sorted(
        ({"name": k, "count": v} for k, v in kp_counter.items()),
        key=lambda x: (-x["count"], x["name"]),
    )[:8]

    return {"groups": groups, "total": total, "kp_summary": kp_summary}
