"""数据库快照备份。

用 SQLite 的在线备份 API（``Connection.backup``），WAL 模式下也能拿到一致快照，
不需要停服、不会阻塞读写。默认每 24 小时一份，保留最近 7 份。
"""
import glob
import os
import sqlite3
import threading
import time
from datetime import datetime

from ..config import Config


def backup_dir() -> str:
    return Config.BACKUP_DIR


def list_backups() -> list:
    pattern = os.path.join(backup_dir(), "exam_*.db")
    files = []
    for path in glob.glob(pattern):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        files.append({
            "name": os.path.basename(path),
            "path": path,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    files.sort(key=lambda item: item["mtime"], reverse=True)
    return files


def backup_now(tag: str = "auto") -> str:
    """立刻生成一份快照，返回文件路径。"""
    os.makedirs(backup_dir(), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = os.path.join(backup_dir(), "exam_%s_%s.db" % (tag, stamp))

    source = sqlite3.connect(Config.DB_PATH, timeout=Config.DB_TIMEOUT)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    _prune()
    return target


def _prune() -> None:
    keep = max(1, Config.BACKUP_KEEP)
    for item in list_backups()[keep:]:
        try:
            os.remove(item["path"])
        except OSError:
            pass


def _is_due(interval_hours: float) -> bool:
    files = list_backups()
    if not files:
        return True
    return (time.time() - files[0]["mtime"]) >= interval_hours * 3600


def start_scheduler(app, interval_hours: float = None) -> threading.Thread:
    """后台守护线程：启动 30 秒后检查一次，之后每 10 分钟检查是否到期。"""
    interval = interval_hours or Config.BACKUP_INTERVAL_HOURS

    def loop():
        time.sleep(30)
        while True:
            try:
                if _is_due(interval):
                    path = backup_now()
                    app.logger.info("数据库已备份: %s", path)
            except Exception as exc:  # noqa: BLE001 - 备份失败不能影响服务
                app.logger.exception("数据库备份失败: %s", exc)
            time.sleep(600)

    thread = threading.Thread(target=loop, name="db-backup", daemon=True)
    thread.start()
    return thread
