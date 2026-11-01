"""题目读取与缓存。

题目是典型的「读多写少」数据：一场考试里每个学生进考场、交卷、看成绩、
老师看讲评都会反复读同一批题。这里统一缓存，任何写操作显式失效。
"""
import json

from ..cache import TTLCache
from ..config import Config
from ..db import get_db
from ..utils import TYPE_LABELS

_cache = TTLCache(ttl=Config.QUESTION_CACHE_TTL, maxsize=256)


def _load(exam_id: int) -> list:
    rows = get_db().execute(
        "SELECT * FROM questions WHERE exam_id=? ORDER BY id", (exam_id,)
    ).fetchall()
    items = []
    for row in rows:
        try:
            options = json.loads(row["options"] or "[]")
            if not isinstance(options, list):
                options = []
        except (ValueError, TypeError):
            options = []
        items.append({
            "id": row["id"],
            "exam_id": row["exam_id"],
            "parent_id": row["parent_id"],
            "type": row["type"],
            "question": row["question"],
            "options": options,
            "answer": row["answer"],
            "score": row["score"],
            "explanation": row["explanation"] or "",
        })
    return items


def exam_questions(exam_id: int) -> list:
    """该考试的全部题目（含子题），已解析 options。"""
    return _cache.get_or_load(exam_id, lambda: _load(exam_id))


def invalidate(exam_id=None) -> None:
    if exam_id is None:
        _cache.clear()
    else:
        _cache.invalidate(exam_id)


def gradable(exam_id: int) -> list:
    """参与判分的题目：排除 group 容器题。"""
    return [q for q in exam_questions(exam_id) if q["type"] != "group"]


def total_score(exam_id: int) -> int:
    """试卷满分。

    原实现按「没有 parent_id 的题」求和，会把大题容器分算进去、
    却漏掉子题分，和判分口径不一致（判分只累加子题）。这里统一为
    「所有参与判分的题」求和。
    """
    return sum(q["score"] for q in gradable(exam_id))


def children_map(exam_id: int) -> dict:
    mapping: dict = {}
    for q in exam_questions(exam_id):
        if q["parent_id"]:
            mapping.setdefault(q["parent_id"], []).append(q)
    return mapping


def exam_structure(exam_id: int) -> list:
    """考试页用的结构：顶层题 + 大题下的子题列表。"""
    kids = children_map(exam_id)
    result = []
    for q in exam_questions(exam_id):
        if q["parent_id"]:
            continue
        item = {
            "id": q["id"],
            "type": q["type"],
            "question": q["question"],
            "options": q["options"],
            "score": q["score"],
            "parent_id": None,
        }
        if q["type"] == "group":
            item["children"] = [
                {
                    "id": cq["id"],
                    "type": cq["type"],
                    "question": cq["question"],
                    "options": cq["options"],
                    "score": cq["score"],
                }
                for cq in kids.get(q["id"], [])
            ]
        result.append(item)
    return result


def flat_questions(exam_id: int) -> list:
    """按展示顺序摊平的题目（大题展开为子题），带题号和题型标签。"""
    kids = children_map(exam_id)
    flat = []
    for q in exam_questions(exam_id):
        if q["parent_id"]:
            continue
        if q["type"] == "group":
            for cq in kids.get(q["id"], []):
                flat.append(dict(cq, type_label=TYPE_LABELS.get(cq["type"], cq["type"])))
        else:
            flat.append(dict(q, type_label=TYPE_LABELS.get(q["type"], q["type"])))
    return flat
