"""Flask 应用工厂。

``create_app()`` 只负责组装，具体能力都在各自模块里：

    config.py          配置
    db.py              连接池 / 事务 / 建表迁移
    logging_setup.py   日志
    context.py         模板全局变量
    hooks.py           请求中间件（同源校验、IP 封禁、安全头、慢请求日志）
    errors.py          错误页
    security/          登录态、限流、IP 封禁数据、口令哈希
    services/          判分、统计、题目缓存、PDF、导入
    views/             按业务拆分的路由

路由仍用 ``add_url_rule`` + 显式 endpoint 名注册，名称与旧版一致，
所以 templates 里的 url_for 和前端 /api/* 契约都不用改。
"""
import os

from flask import Flask

from .config import BASE_DIR, Config
from .db import init_db

_initialized = False


def create_app(config_object=Config, init=True) -> Flask:
    global _initialized

    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, "templates"),
        static_folder=os.path.join(BASE_DIR, "static"),
    )
    app.config.from_object(config_object)
    app.json.ensure_ascii = False

    if init and not _initialized:
        init_db()
        _initialized = True

    from . import context, errors, hooks, logging_setup

    logging_setup.setup_logging(app)
    context.install(app)
    hooks.install(app)
    errors.install(app)

    # 最外层 WSGI 中间件：压缩响应（静态资源收益最大）
    from .gzip import GzipMiddleware

    app.wsgi_app = GzipMiddleware(app.wsgi_app)

    from .views import (admin_accounts, admin_core, admin_exams, admin_questions,
                        admin_results, admin_students, api, auth, exam, health,
                        student)
    for module in (auth, student, exam, api, health, admin_core, admin_accounts,
                   admin_exams, admin_questions, admin_students, admin_results):
        module.register(app)

    app.logger.info("exam system ready, db=%s", app.config["DB_PATH"])
    return app
