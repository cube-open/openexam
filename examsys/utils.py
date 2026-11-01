"""通用小工具：时间、JSON、题型常量。"""
import json
import time
from datetime import datetime

# 题型别名 → 内部类型（choice 单选 / multi 多选 / judge 判断 / blank 填空 / group 大题）
TYPE_ALIASES = {
    "choice": "choice", "single": "choice", "singlechoice": "choice",
    "单选": "choice", "单选题": "choice", "单项选择题": "choice", "选择题": "choice",
    "multi": "multi", "multiple": "multi", "multiplechoice": "multi",
    "多选": "multi", "多选题": "multi", "多项选择": "multi", "多项选择题": "multi",
    "judge": "judge", "truefalse": "judge", "tf": "judge",
    "判断": "judge", "判断题": "judge", "是非": "judge", "是非题": "judge",
    "blank": "blank", "fill": "blank", "fillblank": "blank", "fillintheblank": "blank",
    "填空": "blank", "填空题": "blank",
    "group": "group", "大题": "group", "材料题": "group",
}

TYPE_LABELS = {"choice": "单选", "multi": "多选", "judge": "判断", "blank": "填空", "group": "大题"}

JUDGE_TRUE = ("正确", "对", "√", "✓", "t", "true", "a", "是", "yes", "y", "1", "right")
JUDGE_FALSE = ("错误", "错", "×", "✗", "x", "f", "false", "b", "否", "no", "n", "0", "wrong")

LETTERS = "ABCDEFGH"


def now_epoch() -> int:
    return int(time.time())


def fmt_ts(ts) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "-"


def parse_dt(s):
    """解析 datetime-local 输入（如 2026-08-16T14:00）为 epoch；空则返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def dt_local_input(ts) -> str:
    """epoch -> datetime-local 输入框用的本地时间字符串。"""
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%dT%H:%M")


def json_loads(text, default):
    if not text:
        return default
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def exam_window(exam) -> tuple:
    """返回 (status, label)。status: upcoming / open / closed。"""
    now = now_epoch()
    start_at = exam["start_at"]
    end_at = exam["end_at"]
    if start_at and now < start_at:
        return "upcoming", "未开始 · " + fmt_ts(start_at)
    if end_at and now > end_at:
        return "closed", "已结束 · " + fmt_ts(end_at)
    return "open", "进行中"


def remaining_seconds(exam_duration_minutes: int, started_at, time_offset: int = 0) -> int:
    elapsed = now_epoch() - (started_at or now_epoch())
    return max(0, exam_duration_minutes * 60 - elapsed + (time_offset or 0))


def truthy(value) -> bool:
    return value in (1, True, "1", "true", "True")
