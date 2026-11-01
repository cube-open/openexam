"""生产启动入口：waitress 多线程 WSGI 服务器。

用法：python run.py   （或双击 start.bat）

可调环境变量：
    EXAM_PORT=5000          监听端口
    EXAM_THREADS=32         工作线程数（与数据库连接池大小保持接近）
    EXAM_SECRET_KEY=...     会话签名密钥（不设则自动生成并保存在 .secret_key）
"""
import os
import socket
import sys

from waitress import serve

from examsys import create_app
from examsys.config import Config
from examsys.services.backup import start_scheduler

PORT = int(os.environ.get("EXAM_PORT", "5000"))
THREADS = int(os.environ.get("EXAM_THREADS", "32"))


def get_local_ips():
    """获取本机局域网 IP，方便学生连接。"""
    ips = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ips.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if not ip.startswith("127.") and ":" not in ip:
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main() -> int:
    app = create_app()

    print("=" * 52)
    print("  %s · 上线模式" % Config.BRAND_NAME)
    print("=" * 52)
    print("  本机访问（老师自己）： http://127.0.0.1:%d" % PORT)
    for ip in get_local_ips():
        print("  学生访问（同一局域网）： http://%s:%d" % (ip, PORT))
    print("  管理后台：             /admin/login")
    print("  数据库：               %s" % Config.DB_PATH)
    print("  线程 / 连接池：         %d / %d" % (THREADS, Config.DB_POOL_SIZE))
    print("  自动备份：             每 %d 小时，保留 %d 份"
          % (Config.BACKUP_INTERVAL_HOURS, Config.BACKUP_KEEP))
    print("  按 Ctrl+C 停止服务")
    print("=" * 52)

    start_scheduler(app)

    try:
        serve(
            app,
            host="0.0.0.0",
            port=PORT,
            threads=THREADS,
            channel_timeout=120,
            max_request_header_size=262144,
            max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
            connection_limit=1000,
            ident="exam-system",
        )
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
