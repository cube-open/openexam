"""迁移命令行。

    python -m examsys.migrate status      # 看当前版本与待执行项
    python -m examsys.migrate up          # 执行待应用迁移（自动先备份）
    python -m examsys.migrate up --no-backup
    python -m examsys.migrate backup      # 只备份

数据库路径沿用 ``EXAM_DB`` 环境变量，默认项目根目录的 exam.db。
"""
import argparse
import sqlite3
import sys
import time

from . import migrations
from .config import Config


def _connect():
    conn = sqlite3.connect(Config.DB_PATH, timeout=Config.DB_TIMEOUT)
    conn.isolation_level = None          # 迁移执行器要求显式事务
    conn.execute("PRAGMA busy_timeout = %d" % Config.DB_BUSY_TIMEOUT_MS)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _fmt_time(ts) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def cmd_status(conn) -> int:
    rows = migrations.status(conn)
    print("数据库：%s" % Config.DB_PATH)
    print()
    print("  %-4s %-8s %-20s %s" % ("版本", "状态", "应用时间", "名称"))
    print("  " + "-" * 72)
    for row in rows:
        if row["applied"]:
            state = "已应用"
        else:
            state = "待执行"
        if row["changed"]:
            state = "已改动"
        print("  v%-3d %-8s %-20s %s" % (
            row["version"], state, _fmt_time(row["applied_at"]), row["name"]))
    todo = [r for r in rows if not r["applied"]]
    print()
    if todo:
        print("有 %d 个迁移待执行：python -m examsys.migrate up" % len(todo))
        return 0
    print("数据库已是最新。")
    return 0


def cmd_up(conn, do_backup=True) -> int:
    result = migrations.apply_pending(conn, do_backup=do_backup, logger=None)
    if result["backup"]:
        print("迁移前快照：%s" % result["backup"])
    if not result["applied"]:
        print("没有待执行的迁移。")
    for version in result["applied"]:
        print("已应用 v%d" % version)
    for msg in result["warnings"]:
        print("警告：%s" % msg)
    return 0


def cmd_backup(conn) -> int:
    todo = migrations.pending(conn)
    version = int(todo[0].VERSION) if todo else 0
    path = migrations.snapshot(conn, version)
    print("已生成快照：%s" % path)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m examsys.migrate")
    parser.add_argument("command", choices=("status", "up", "backup"),
                        help="status 查看 / up 执行 / backup 备份")
    parser.add_argument("--no-backup", action="store_true",
                        help="up 时不生成迁移前快照（不推荐）")
    args = parser.parse_args(argv)

    conn = _connect()
    try:
        if args.command == "status":
            return cmd_status(conn)
        if args.command == "up":
            return cmd_up(conn, do_backup=not args.no_backup)
        return cmd_backup(conn)
    except migrations.MigrationError as exc:
        print("迁移失败：%s" % exc, file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
