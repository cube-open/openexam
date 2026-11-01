"""后台操作审计日志。

记录「谁、什么时候、从哪个 IP、对什么东西、做了什么」。只追加，不修改。

写日志不能影响业务
------------------
审计失败（表被锁、字段超长）不应该让改分、删考试这些操作跟着失败，
所以内部捕获异常只记 warning。

与调用方事务的关系
------------------
调用方常常已经处在 ``write_txn()`` 里。此时**复用同一个事务**（不再开新的
``BEGIN IMMEDIATE``），审计记录和业务改动一起提交，要么都成、要么都不成；
不在事务里时才自己开一个。
"""
import json

from flask import current_app, request, session

from ..db import get_db, write_txn

MAX_DETAIL = 2000


def _actor():
    from ..rbac import current_admin

    aid = session.get("admin_id")
    if aid:
        admin = current_admin()
        return "admin", aid, (admin["username"] if admin else ""), (
            admin["org_id"] if admin else None)
    sid = session.get("student_id")
    if sid:
        return "student", sid, session.get("student_name") or "", None
    return "system", None, "", None


def log(action: str, target_type: str = "", target_id=None,
        detail="", actor=None) -> None:
    """写一条审计记录。

    :param action: 动作标识，如 ``exam.delete`` / ``user.reset_password``
    :param target_type: 目标类型，如 ``exam`` / ``student`` / ``admin``
    :param target_id: 目标主键
    :param detail: 补充说明；传 dict / list 会序列化成 JSON
    :param actor: 覆盖默认的「从 session 推断操作者」
    """
    try:
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        detail = (detail or "")[:MAX_DETAIL]

        if actor is None:
            actor_type, actor_id, actor_name, org_id = _actor()
        else:
            actor_type, actor_id, actor_name, org_id = actor

        try:
            ip = request.remote_addr or ""
        except RuntimeError:      # 没有请求上下文（脚本/定时任务）
            ip = ""

        params = (org_id, actor_type, actor_id, actor_name, action,
                  target_type or "", target_id, detail, ip)

        db = get_db()
        if db.in_transaction:
            # 复用调用方事务：审计与业务改动同生共死
            db.execute(
                "INSERT INTO audit_logs (org_id, actor_type, actor_id, actor_name,"
                " action, target_type, target_id, detail, ip, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))", params)
        else:
            with write_txn() as txn:
                txn.execute(
                    "INSERT INTO audit_logs (org_id, actor_type, actor_id, actor_name,"
                    " action, target_type, target_id, detail, ip, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))", params)
    except Exception as exc:  # noqa: BLE001 - 审计不能拖垮业务
        current_app.logger.warning("审计日志写入失败（%s）：%s", action, exc)


def recent(limit: int = 200, org_id=None, action=None):
    """后台审计页用的查询。"""
    sql = ("SELECT * FROM audit_logs WHERE 1=1")
    params = []
    if org_id is not None:
        sql += " AND (org_id = ? OR org_id IS NULL)"
        params.append(org_id)
    if action:
        sql += " AND action LIKE ?"
        params.append(action + "%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))
    return get_db().execute(sql, params).fetchall()
