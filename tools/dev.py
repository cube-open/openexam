"""开发服务器：自动重载模板 / 静态资源，改完刷新页面即可看到效果。

用法：
    python tools/dev.py            # 用临时数据库（不影响正式数据）
    python tools/dev.py --real     # 用正式 exam.db
    python tools/dev.py --port 5055

与 run.py 的区别：run.py 是生产模式（ waitress 多线程，模板不热重载），
这个是开发模式（单线程，模板每次请求重新读盘）。
"""
import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5055)
    parser.add_argument("--real", action="store_true", help="使用正式数据库")
    parser.add_argument("--seed", action="store_true", help="临时库灌入演示数据")
    parser.add_argument("--admin-pw", default="admin123",
                        help="临时库里的 admin 密码（正式库不受影响）")
    args = parser.parse_args()

    if not args.real:
        # 临时库：从正式库复制一份，改动不会污染正式数据
        tmp = tempfile.mkdtemp(prefix="examdev_")
        src = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "exam.db")
        if os.path.exists(src):
            shutil.copy(src, os.path.join(tmp, "dev.db"))
        os.environ["EXAM_DB"] = os.path.join(tmp, "dev.db")
        os.environ["EXAM_BACKUP_DIR"] = os.path.join(tmp, "backups")
        print("临时数据库：%s" % os.environ["EXAM_DB"])

    os.environ["EXAM_PORT"] = str(args.port)
    os.environ["EXAM_TEMPLATES_RELOAD"] = "1"

    from examsys import create_app
    from examsys.db import init_db

    app = create_app()
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    init_db()

    # 临时库里把 admin 密码重置成已知值，方便本地调试（正式库不受影响）
    if not args.real:
        from examsys.db import get_db, write_txn
        from examsys.security import hash_password

        with app.app_context():
            with write_txn() as db:
                db.execute("UPDATE admins SET password_hash=?, enabled=1 "
                           "WHERE username='admin'",
                           (hash_password(args.admin_pw),))
        print("临时库 admin 密码：%s" % args.admin_pw)

    print("=" * 52)
    print("  开发模式  http://127.0.0.1:%d" % args.port)
    print("  模板自动重载已开启，改完刷新即可")
    print("  Python 代码改动会自动重启服务")
    print("=" * 52)
    # reloader 会监视 .py 改动并重启；模板由 Jinja 的 auto_reload 负责。
    # 调试器关闭，避免 reloader 子进程里再开一个调试器导致断点失效。
    app.run(host="127.0.0.1", port=args.port, debug=True, use_reloader=True)


if __name__ == "__main__":
    main()
