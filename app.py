"""兼容入口。

原来的 2855 行单文件已经拆到 examsys/ 包里：
    examsys/config.py        配置
    examsys/db.py            连接池 / 事务 / 建表迁移
    examsys/security/        登录态、限流、IP 封禁、同源校验
    examsys/services/        判分、统计、题目缓存、PDF、导入
    examsys/views/           按业务拆分的路由

`python app.py` 仍可直接跑（开发模式）；生产用 `python run.py`。
"""
import os

from examsys import create_app
from examsys.db import init_db  # noqa: F401  保留旧写法 `from app import init_db` 的兼容

app = create_app()

if __name__ == "__main__":
    debug = os.environ.get("EXAM_DEBUG", "").lower() in ("1", "true", "yes")
    if debug:
        app.logger.warning("开发模式已开启（EXAM_DEBUG=1），请勿用于正式考试")
    app.run(host="0.0.0.0", port=int(os.environ.get("EXAM_PORT", "5000")),
            debug=debug, threaded=True)
