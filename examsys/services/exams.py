"""考试（试卷）相关的读取与可见性判断。"""
from ..db import get_db
from ..utils import now_epoch


def get_exam(exam_id):
    return get_db().execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()


def admin_exam(exam_id):
    """后台取试卷，顺带做组织隔离。

    越权的试卷一律当作不存在（返回 None），调用方已有的 404 分支就够用了——
    不返回 403，避免让人通过状态码差异试探出「这个 id 是存在的」。
    """
    from .. import rbac

    exam = get_exam(exam_id)
    if exam is None:
        return None
    org = rbac.visible_org_id(rbac.current_admin())
    if org is not None and exam["org_id"] != org:
        return None
    return exam


def exam_group_ids(exam_id) -> list:
    """该考试限定的参与分组；空列表表示不限制。"""
    rows = get_db().execute(
        "SELECT group_id FROM exam_groups WHERE exam_id=?", (exam_id,)
    ).fetchall()
    return [r["group_id"] for r in rows]


def exam_visible_to(exam_id, student) -> bool:
    gids = exam_group_ids(exam_id)
    if not gids:
        return True
    return (student["group_id"] or 0) in gids


def get_result(student_id, exam_id):
    return get_db().execute(
        "SELECT * FROM results WHERE student_id=? AND exam_id=?",
        (student_id, exam_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# 服务端强制时限
# ---------------------------------------------------------------------------
#
# 前端计时器只是「提示」，不是「约束」：改掉 JS 或者干脆断网，学生就能一直答下去。
# 所以截止时间必须由服务端算，而且所有入口（保存 / 心跳 / 交卷 / 进考场）用同一套算法，
# 否则会出现「页面说还剩 5 分钟但服务端已经在收卷」这种对不上的状态。


def attempt_deadline(exam, result_row):
    """本次作答的绝对截止时间戳（秒）；没开始过返回 None。

    规则：
    - 时长从 ``started_at`` 起算，``time_offset`` 是老师加/减的秒数（正数=加时间）。
    - 试卷设了 ``end_at``（考试窗口）时，窗口一关就强制收卷。窗口的意义就是
      「过了这个点谁都不许再答」，否则它只是个开考时间提醒而已。

    ``exam`` 与 ``result_row`` 可以是同一条 JOIN 出来的记录，只要它同时含有
    duration_minutes / end_at / started_at / time_offset 四列即可。
    """
    if result_row is None:
        return None
    started = result_row["started_at"]
    if not started:
        return None
    duration = (exam["duration_minutes"] or 0) * 60
    deadline = started + duration + (result_row["time_offset"] or 0)
    end_at = exam["end_at"]
    if end_at and end_at < deadline:
        deadline = end_at
    return deadline


def attempt_remaining(exam, result_row):
    """剩余秒数（不会为负）。"""
    deadline = attempt_deadline(exam, result_row)
    if deadline is None:
        return 0
    return max(0, deadline - now_epoch())


def attempt_overtime(exam, result_row):
    """已经超时多少秒（没超时返回 0）。"""
    deadline = attempt_deadline(exam, result_row)
    if deadline is None:
        return 0
    return max(0, now_epoch() - deadline)


def is_expired(exam, result_row):
    """是否已过截止时间。"""
    return attempt_overtime(exam, result_row) > 0
