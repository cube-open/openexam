"""判分。

一次取题、一次判完，不再「每题查一次库」。
"""
import json

from ..utils import TYPE_LABELS
from . import questions as qsvc


def _blank_accepted(q) -> list:
    try:
        value = json.loads(q["answer"] or "[]")
    except (ValueError, TypeError):
        value = []
    if not isinstance(value, list):
        value = [str(value)]
    return [str(a).strip().lower() for a in value]


def _answer_text(q) -> str:
    t = q["type"]
    if t == "blank":
        return " / ".join(_blank_accepted(q))
    if t == "judge":
        return "正确" if q["answer"] == "A" else "错误"
    if t == "multi":
        return "、".join(q["answer"])
    return q["answer"]


def grade(exam_id: int, raw_answers: dict, questions=None):
    """返回 (总分, 逐题详情)。

    raw_answers: {qid(str): 答案(str)}
    """
    if questions is None:
        questions = qsvc.gradable(exam_id)

    raw_answers = raw_answers or {}
    rows = []
    total = 0
    for q in questions:
        given = str(raw_answers.get(str(q["id"])) or "").strip()
        t = q["type"]
        if t == "blank":
            accepted = _blank_accepted(q)
            correct = given.lower() in accepted
            answer_display = " / ".join(accepted)
        elif t == "multi":
            correct = bool(given) and set(given) == set(q["answer"])
            answer_display = "、".join(q["answer"])
        elif t == "judge":
            correct = given == q["answer"]
            answer_display = "正确" if q["answer"] == "A" else "错误"
        else:  # choice：答案可能是多个字母（如 "AC"），命中任意一个即算对
            correct = bool(given) and given in q["answer"]
            answer_display = " / ".join(q["answer"]) if len(q["answer"]) > 1 else q["answer"]

        if correct:
            total += q["score"]
        rows.append({
            "id": q["id"],
            "type": t,
            "type_label": TYPE_LABELS.get(t, t),
            "question": q["question"],
            "options": q["options"],
            "answer": q["answer"],
            "answer_display": answer_display,
            "score": q["score"],
            "given": given,
            "correct": correct,
            "explanation": q["explanation"],
        })
    return total, rows


def grade_to_question_scores(rows) -> dict:
    return {str(r["id"]): (r["score"] if r["correct"] else 0) for r in rows}


def total_score(exam_id: int) -> int:
    return qsvc.total_score(exam_id)
