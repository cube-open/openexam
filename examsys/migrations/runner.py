"""版本化数据库迁移执行器。

为什么要有这一层
----------------
原来的迁移是 ``db._MIGRATIONS`` 那种「表里缺哪列就补哪列」的列表，能跑，但回答不了
三个运维最需要的问题：现在这个库是哪个版本？升级挂在哪一步？出事要退回哪个快照？

这里的规则
----------
1. 每个迁移是一个模块 ``mXXXX_名字.py``，暴露 ``VERSION`` / ``NAME`` / ``up(db)``；
2. 执行过的版本记进 ``schema_migrations``（版本、名称、时间、源码校验和）；
3. 有未执行的迁移时，**先落一份快照再动手**，快照不会被自动清理掉；
4. 每个迁移单独一个事务，失败即回滚，不留半截结构；
5. 重复执行无副作用，启动时自动补齐。

写迁移的注意事项
----------------
- ``up(db)`` 里用 ``db.execute()`` 一条一条写，**不要用 executescript**：
  ``executescript`` 会先发一次 COMMIT，把外层事务提前提交掉，失败就没法回滚了。
- 迁移必须幂等（``CREATE TABLE IF NOT EXISTS`` / 先查再加列），因为老库可能被
  手工改过，而 ``schema.sql`` 也已经建好了新库的表。
- 已经发布过的迁移不要再改。改了会被校验和检测出来并告警（不阻断启动）。
"""
import hashlib
import importlib
import os
import pkgutil
import sqlite3
import time

_PKG = __name__.rsplit(".", 1)[0]


class MigrationError(RuntimeError):
    """某个迁移执行失败，已回滚。"""


# ---------------------------------------------------------------------------
# 迁移发现
# ---------------------------------------------------------------------------

def modules() -> list:
    """按版本号升序返回全部迁移模块。"""
    found = []
    here = os.path.dirname(__file__)
    for info in pkgutil.iter_modules([here]):
        # m0001_xxx 这种命名才认，避免把 __init__ / runner 当迁移
        if not (info.name.startswith("m") and info.name[1:5].isdigit()):
            continue
        found.append(importlib.import_module("%s.%s" % (_PKG, info.name)))
    found.sort(key=lambda mod: int(mod.VERSION))
    return found


def _checksum(mod) -> str:
    try:
        with open(mod.__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except (OSError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# 版本表
# ---------------------------------------------------------------------------

def ensure_table(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at INTEGER NOT NULL,"
        " checksum TEXT NOT NULL DEFAULT '')"
    )


def applied(db) -> dict:
    """已执行的迁移：{version: (name, applied_at, checksum)}。"""
    ensure_table(db)
    rows = db.execute(
        "SELECT version, name, applied_at, checksum FROM schema_migrations"
    ).fetchall()
    return {int(r[0]): (r[1], r[2], r[3]) for r in rows}


def pending(db) -> list:
    done = applied(db)
    return [m for m in modules() if int(m.VERSION) not in done]


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def snapshot_path(version: int) -> str:
    from ..config import Config

    os.makedirs(Config.BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(Config.BACKUP_DIR,
                        "premigrate_v%d_%s.db" % (version, stamp))


def snapshot(db, version: int) -> str:
    """用 SQLite 在线备份 API 落一份快照。

    刻意不复用 services.backup.backup_now()：那个函数按 ``exam_*.db`` 命名并参与
    「只留 N 份」的清理，迁移前的安全网不该被后续自动备份挤掉。
    """
    target = snapshot_path(version)
    dest = sqlite3.connect(target)
    try:
        db.backup(dest)
    finally:
        dest.close()
    return target


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def _has_user_data(db) -> bool:
    """空库不必备份，省掉首次启动的 IO。"""
    for table in ("students", "admins", "exams", "results"):
        try:
            row = db.execute("SELECT 1 FROM %s LIMIT 1" % table).fetchone()
        except sqlite3.OperationalError:
            continue
        if row:
            return True
    return False


def apply_pending(db, do_backup=True, logger=None) -> dict:
    """执行所有未应用的迁移。

    返回 ``{"applied": [版本...], "backup": 路径或 None, "warnings": [...]}``。
    ``db`` 必须是 ``isolation_level=None`` 的连接（事务由本函数显式控制）。
    """
    todo = pending(db)
    result = {"applied": [], "backup": None, "warnings": []}

    # 已执行的迁移被改过 —— 不阻断，但要让运维看见
    done = applied(db)
    for mod in modules():
        version = int(mod.VERSION)
        if version in done:
            old = done[version][2] or ""
            new = _checksum(mod)
            if old and new and old != new:
                msg = "迁移 v%d（%s）的源码在应用后被修改过" % (version, mod.NAME)
                result["warnings"].append(msg)
                if logger:
                    logger.warning(msg)

    if not todo:
        return result

    if do_backup and _has_user_data(db):
        result["backup"] = snapshot(db, int(todo[0].VERSION))
        if logger:
            logger.info("迁移前已备份: %s", result["backup"])

    for mod in todo:
        version = int(mod.VERSION)
        started = time.perf_counter()
        db.execute("BEGIN IMMEDIATE")
        try:
            mod.up(db)
            db.execute(
                "INSERT INTO schema_migrations (version, name, applied_at, checksum)"
                " VALUES (?, ?, ?, ?)",
                (version, mod.NAME, int(time.time()), _checksum(mod)),
            )
            db.execute("COMMIT")
        except BaseException as exc:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise MigrationError(
                "迁移 v%d（%s）失败，已回滚：%s" % (version, mod.NAME, exc)
            ) from exc
        cost = (time.perf_counter() - started) * 1000
        result["applied"].append(version)
        if logger:
            logger.info("迁移 v%d 完成（%s）%.1fms", version, mod.NAME, cost)
    return result


def status(db) -> list:
    """给 CLI / 后台看的状态表。"""
    done = applied(db)
    rows = []
    for mod in modules():
        version = int(mod.VERSION)
        info = done.get(version)
        rows.append({
            "version": version,
            "name": mod.NAME,
            "applied": info is not None,
            "applied_at": info[1] if info else None,
            "changed": bool(info and info[2] and info[2] != _checksum(mod)),
        })
    return rows
