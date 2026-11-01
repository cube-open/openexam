"""日志：控制台 + 文件（5MB 滚动，保留 3 份）。"""
import logging
import os
from logging.handlers import RotatingFileHandler

from .config import BASE_DIR

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3


def setup_logging(app) -> None:
    """接管 Flask 默认日志。

    注意：第一次访问 ``app.logger`` 时 Flask 会塞一个 default_handler 进去，
    所以不能靠 ``if app.logger.handlers: return`` 判断（那样永远为真，
    文件日志会静默失效）。这里改成把默认 handler 摘掉再装自己的。
    """
    if getattr(app, "_exam_logging_ready", False):
        return
    app._exam_logging_ready = True

    try:
        from flask.logging import default_handler

        if default_handler in app.logger.handlers:
            app.logger.removeHandler(default_handler)
    except (ImportError, ValueError):
        pass

    formatter = logging.Formatter(LOG_FORMAT)
    handlers = []

    try:
        os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
        handlers.append(RotatingFileHandler(
            os.path.join(BASE_DIR, "logs", "app.log"),
            maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"))
    except OSError:
        # 目录不可写时至少保留控制台输出
        pass

    handlers.append(logging.StreamHandler())

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
        app.logger.addHandler(handler)

    app.logger.setLevel(logging.INFO)
    # 不向 root 传播，避免 waitress / werkzeug 的日志再打一遍
    app.logger.propagate = False
