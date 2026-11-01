"""并发压测：模拟 N 个学生同时「登录 → 开考 → 持续保存 / 心跳 → 交卷」。

用法：
    python load_test.py                  # 50 人，持续 20 秒
    python load_test.py 100 --duration 30 --threads 32

会在本机 5051 端口起一个临时 waitress 服务（独立临时数据库），
不影响正式 exam.db。输出各接口的成功率与延迟分位。
"""
import argparse
import concurrent.futures
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

TMP_DIR = tempfile.mkdtemp(prefix="exam_load_")
os.environ["EXAM_DB"] = os.path.join(TMP_DIR, "load.db")
os.environ.setdefault("EXAM_THREADS", "32")

import requests  # noqa: E402
from waitress import serve  # noqa: E402

from examsys import create_app  # noqa: E402
from examsys.db import get_db  # noqa: E402
from examsys.security import hash_password  # noqa: E402

HOST = "http://127.0.0.1:5051"
SAMPLES = {}
SAMPLES_LOCK = threading.Lock()


def record(name, elapsed):
    with SAMPLES_LOCK:
        SAMPLES.setdefault(name, []).append(elapsed * 1000)


def seed(app, n):
    with app.app_context():
        db = get_db()
        digest = hash_password("123456")
        rows = [("load%d" % i, digest, "压测%d" % i, "L%04d" % i) for i in range(n)]
        db.execute("BEGIN IMMEDIATE")
        db.executemany(
            "INSERT INTO students (username, password_hash, name, student_no) VALUES (?,?,?,?)",
            rows)
        db.execute("COMMIT")


def start_server(app, threads):
    serve(app, host="127.0.0.1", port=5051, threads=threads, connection_limit=1000)


def wait_ready(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(HOST + "/healthz", timeout=1)
            return True
        except requests.RequestException:
            time.sleep(0.1)
    return False


def student_flow(i, delay, stop_at):
    """一个学生的完整考试过程。delay 用于把登录分散开，避免所有请求同时打进来。"""
    if delay:
        time.sleep(delay)
    session = requests.Session()
    stats = {"ok": 0, "fail": 0}

    def call(name, method, path, **kwargs):
        t0 = time.perf_counter()
        try:
            resp = session.request(method, HOST + path, timeout=15, **kwargs)
            record(name, time.perf_counter() - t0)
            if resp.status_code < 400:
                stats["ok"] += 1
            else:
                stats["fail"] += 1
            return resp
        except requests.RequestException:
            record(name, time.perf_counter() - t0)
            stats["fail"] += 1
            return None

    call("login", "POST", "/login",
         data={"username": "load%d" % i, "password": "123456"}, allow_redirects=False)
    call("start", "POST", "/exam/1/start", allow_redirects=False)
    call("exam_page", "GET", "/exam/1")

    answers = {"1": "B", "2": "B", "3": "80", "4": "超文本标记语言"}
    last_save = 0.0
    while time.time() < stop_at:
        # 真实客户端 3 秒一次心跳、10 秒一次保存
        call("heartbeat", "POST", "/api/heartbeat",
             json={"exam_id": 1, "violations": 0})
        now = time.time()
        if now - last_save > 10:
            call("save", "POST", "/api/save",
                 json={"exam_id": 1, "answers": answers})
            last_save = now
        time.sleep(3)

    call("submit", "POST", "/exam/1/submit",
         json={"answers": answers, "auto": False})
    call("result", "GET", "/exam/1/result")
    return stats


def percentile(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1))))
    return values[idx]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("students", nargs="?", type=int, default=50)
    parser.add_argument("--duration", type=float, default=20, help="持续心跳时长（秒）")
    parser.add_argument("--threads", type=int, default=32, help="waitress 工作线程")
    parser.add_argument("--stagger", type=float, default=0,
                        help="把登录分散到 N 秒内（更接近真实开考）")
    args = parser.parse_args()

    app = create_app()
    seed(app, args.students)

    threading.Thread(target=start_server, args=(app, args.threads), daemon=True).start()
    if not wait_ready():
        print("服务器启动失败")
        return 1

    print("压测中：%d 名学生，持续 %.0f 秒，%d 线程，登录分散 %.0f 秒…"
          % (args.students, args.duration, args.threads, args.stagger))
    started = time.time()
    stop_at = started + args.duration

    gap = args.stagger / args.students if args.stagger and args.students else 0
    stop_at += args.stagger
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.students) as pool:
        futures = [pool.submit(student_flow, i, i * gap, stop_at)
                   for i in range(args.students)]
        results = [f.result() for f in futures]

    elapsed = time.time() - started
    ok = sum(r["ok"] for r in results)
    fail = sum(r["fail"] for r in results)
    total = ok + fail

    print("\n===== 结果 =====")
    print("学生数        : %d" % args.students)
    print("总请求        : %d（成功 %d / 失败 %d）" % (total, ok, fail))
    print("总耗时        : %.2fs" % elapsed)
    print("吞吐          : %.0f 请求/秒" % (total / elapsed if elapsed else 0))
    print("")
    print("%-12s %7s %8s %8s %8s" % ("接口", "次数", "p50", "p95", "p99"))
    for name in ("login", "start", "exam_page", "heartbeat", "save", "submit", "result"):
        values = SAMPLES.get(name, [])
        if not values:
            continue
        print("%-12s %7d %7.0fms %7.0fms %7.0fms" % (
            name, len(values), percentile(values, 50),
            percentile(values, 95), percentile(values, 99)))

    if SAMPLES.get("heartbeat"):
        print("\n平均心跳延迟 %.1f ms（均值）"
              % statistics.mean(SAMPLES["heartbeat"]))
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
