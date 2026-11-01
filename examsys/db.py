"""SQLite 连接池、事务封装、建表与迁移。

并发模型说明
------------
SQLite 在 WAL 模式下允许多读单写。真正的瓶颈不是连接数，而是：
1. 每个请求重复执行 PRAGMA（原来每次请求 7 条）；
2. 读事务升级成写事务时拿不到锁，直接抛 "database is locked"；
3. 长事务把写锁占住，其它写全部排队。

这里用三个手段解决：
- 连接池复用连接，PRAGMA 只在建连时执行一次；
- 写操作统一走 ``write_txn()``，用 ``BEGIN IMMEDIATE`` 一开始就申请写锁，
  避免"读锁升级"导致的死锁，并带指数退避重试；
- 连接用完归还池中，异常时回滚，不让脏事务污染下一个请求。
"""
import os
import queue
import random
import sqlite3
import threading
import time
from contextlib import contextmanager

from flask import g

from .config import Config

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

_pool: "queue.LifoQueue[sqlite3.Connection]" = queue.LifoQueue(maxsize=Config.DB_POOL_SIZE)
_pool_created = 0
_pool_lock = threading.Lock()


def _configure(conn: sqlite3.Connection) -> None:
    """每条新连接只执行一次。

    busy_timeout 必须最先设置：池在高峰期扩容时，设置 journal_mode 可能撞上
    别人正在写，没有 busy_timeout 会直接抛 "database is locked"。
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % Config.DB_BUSY_TIMEOUT_MS)
    try:
        conn.execute("PRAGMA journal_mode = WAL")   # 读写不互斥
    except sqlite3.OperationalError:
        # 库已经是 WAL 时这一步本应无锁；真撞上锁也不影响后续使用
        pass
    conn.execute("PRAGMA synchronous = NORMAL")     # WAL 下足够安全
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA cache_size = -%d" % Config.DB_CACHE_SIZE_KB)
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA wal_autocheckpoint = 200")
    # 内存映射读：省掉一次内核态拷贝，大表扫描（统计/讲评）受益明显
    try:
        conn.execute("PRAGMA mmap_size = %d" % (256 * 1024 * 1024))
    except sqlite3.OperationalError:
        pass


def _new_conn() -> sqlite3.Connection:
    # isolation_level=None：关闭隐式事务，事务边界由我们显式控制
    conn = sqlite3.connect(
        Config.DB_PATH,
        timeout=Config.DB_TIMEOUT,
        check_same_thread=False,
        isolation_level=None,
    )
    _configure(conn)
    return conn


def _acquire() -> sqlite3.Connection:
    global _pool_created
    try:
        conn = _pool.get_nowait()
    except queue.Empty:
        with _pool_lock:
            if _pool_created < Config.DB_POOL_SIZE:
                _pool_created += 1
                return _new_conn()
        # 池已满额，等别人归还
        conn = _pool.get(timeout=Config.DB_TIMEOUT)
    try:
        conn.execute("SELECT 1")
    except sqlite3.Error:
        # 连接已损坏（例如被外部进程删除/替换了库文件），换一条新的
        try:
            conn.close()
        except sqlite3.Error:
            pass
        return _new_conn()
    return conn


def _release(conn: sqlite3.Connection) -> None:
    if conn is None:
        return
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
    try:
        _pool.put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def get_db() -> sqlite3.Connection:
    """当前请求绑定的连接。"""
    conn = getattr(g, "_db_conn", None)
    if conn is None:
        conn = _acquire()
        g._db_conn = conn
    return conn


def close_db(exc=None) -> None:
    conn = g.pop("_db_conn", None)
    _release(conn)


def _is_busy(err: sqlite3.Error) -> bool:
    msg = str(err).lower()
    return "locked" in msg or "busy" in msg


def _backoff(attempt: int) -> None:
    time.sleep(min(0.4, 0.02 * (2 ** attempt)) + random.random() * 0.02)


@contextmanager
def write_txn():
    """写事务：BEGIN IMMEDIATE + 自动重试 + 自动回滚。

    用法::

        with write_txn() as db:
            db.execute("UPDATE ...")

    正常退出即提交；抛异常则回滚并把异常继续抛出。
    """
    db = get_db()
    last_err: Exception | None = None
    for attempt in range(Config.DB_WRITE_RETRIES):
        try:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as err:
            last_err = err
            if _is_busy(err):
                _backoff(attempt)
                continue
            raise
        try:
            yield db
        except BaseException:
            try:
                if db.in_transaction:
                    db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        try:
            db.execute("COMMIT")
        except sqlite3.OperationalError as err:
            last_err = err
            try:
                if db.in_transaction:
                    db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if _is_busy(err):
                _backoff(attempt)
                continue
            raise
        return
    raise last_err if last_err else sqlite3.OperationalError("write transaction failed")


def query(sql: str, params=()) -> list:
    return get_db().execute(sql, params).fetchall()


def query_one(sql: str, params=()):
    return get_db().execute(sql, params).fetchone()


def scalar(sql: str, params=(), default=0):
    row = get_db().execute(sql, params).fetchone()
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


# ---------------------------------------------------------------------------
# 建表 / 迁移
# ---------------------------------------------------------------------------

_MIGRATIONS = [
    ("exams", "start_at", "ALTER TABLE exams ADD COLUMN start_at INTEGER"),
    ("exams", "end_at", "ALTER TABLE exams ADD COLUMN end_at INTEGER"),
    ("exams", "show_result", "ALTER TABLE exams ADD COLUMN show_result INTEGER NOT NULL DEFAULT 1"),
    ("exams", "show_answer", "ALTER TABLE exams ADD COLUMN show_answer INTEGER NOT NULL DEFAULT 1"),
    ("exams", "show_explanation", "ALTER TABLE exams ADD COLUMN show_explanation INTEGER NOT NULL DEFAULT 1"),
    ("exams", "training_mode", "ALTER TABLE exams ADD COLUMN training_mode INTEGER NOT NULL DEFAULT 0"),
    ("exams", "show_rank", "ALTER TABLE exams ADD COLUMN show_rank INTEGER NOT NULL DEFAULT 1"),
    ("exams", "show_score_monitor", "ALTER TABLE exams ADD COLUMN show_score_monitor INTEGER NOT NULL DEFAULT 1"),
    ("exams", "violation_alert", "ALTER TABLE exams ADD COLUMN violation_alert INTEGER NOT NULL DEFAULT 10"),
    ("exams", "enable_egg", "ALTER TABLE exams ADD COLUMN enable_egg INTEGER NOT NULL DEFAULT 0"),
    ("exams", "show_original", "ALTER TABLE exams ADD COLUMN show_original INTEGER NOT NULL DEFAULT 1"),
    ("exams", "show_gongde", "ALTER TABLE exams ADD COLUMN show_gongde INTEGER NOT NULL DEFAULT 0"),
    ("exams", "enable_reflection", "ALTER TABLE exams ADD COLUMN enable_reflection INTEGER NOT NULL DEFAULT 0"),
    ("results", "notes", "ALTER TABLE results ADD COLUMN notes TEXT DEFAULT ''"),
    ("results", "reflection", "ALTER TABLE results ADD COLUMN reflection TEXT DEFAULT ''"),
    ("results", "reflection_at", "ALTER TABLE results ADD COLUMN reflection_at INTEGER"),
    ("results", "last_seen", "ALTER TABLE results ADD COLUMN last_seen INTEGER"),
    ("results", "question_scores", "ALTER TABLE results ADD COLUMN question_scores TEXT DEFAULT ''"),
    ("results", "time_offset", "ALTER TABLE results ADD COLUMN time_offset INTEGER DEFAULT 0"),
    ("results", "answered_count", "ALTER TABLE results ADD COLUMN answered_count INTEGER NOT NULL DEFAULT 0"),
    ("results", "overtime_seconds", "ALTER TABLE results ADD COLUMN overtime_seconds INTEGER NOT NULL DEFAULT 0"),
    ("questions", "explanation", "ALTER TABLE questions ADD COLUMN explanation TEXT DEFAULT ''"),
    ("questions", "parent_id", "ALTER TABLE questions ADD COLUMN parent_id INTEGER DEFAULT NULL"),
    ("students", "enabled", "ALTER TABLE students ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"),
    ("students", "last_ip", "ALTER TABLE students ADD COLUMN last_ip TEXT DEFAULT ''"),
    ("students", "last_login_at", "ALTER TABLE students ADD COLUMN last_login_at INTEGER"),
    ("students", "group_id", "ALTER TABLE students ADD COLUMN group_id INTEGER"),
    ("blocked_ips", "remark", "ALTER TABLE blocked_ips ADD COLUMN remark TEXT DEFAULT ''"),
]


def _has_column(db: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in db.execute("PRAGMA table_info(%s)" % table))


def _migrate(db: sqlite3.Connection) -> None:
    """为旧库补列 / 重建带旧 CHECK 约束的表。"""
    for table, col, ddl in _MIGRATIONS:
        if not _has_column(db, table, col):
            db.execute(ddl)

    # 旧版 questions.type 约束只允许 choice/blank，重建以支持 multi/judge
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='questions'"
    ).fetchone()
    if row and "choice','blank" in (row[0] or ""):
        db.executescript("""
            ALTER TABLE questions RENAME TO questions_old;
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                question TEXT NOT NULL,
                options TEXT DEFAULT '[]',
                answer TEXT NOT NULL,
                score INTEGER NOT NULL DEFAULT 5,
                explanation TEXT DEFAULT '',
                FOREIGN KEY (exam_id) REFERENCES exams(id)
            );
            INSERT INTO questions (id, exam_id, type, question, options, answer, score)
                SELECT id, exam_id, type, question, options, answer, score FROM questions_old;
            DROP TABLE questions_old;
        """)

    # 历史数据：补 answered_count，避免监控页对每条记录做 JSON 解析
    db.execute(
        "UPDATE results SET answered_count = "
        "  (SELECT COUNT(*) FROM json_each(results.answers)) "
        "WHERE answered_count = 0 AND answers IS NOT NULL AND answers != '{}' "
        "  AND json_valid(answers)"
    )


def _seed(db: sqlite3.Connection) -> None:
    """首次运行时播种演示数据。**必须在迁移之后调用**，否则 org_id 等新列还不存在。"""
    from werkzeug.security import generate_password_hash

    org_id = 1
    super_role = db.execute("SELECT id FROM roles WHERE code='super_admin'").fetchone()

    if db.execute("SELECT id FROM admins WHERE username='admin'").fetchone() is None:
        db.execute(
            "INSERT INTO admins (username, password_hash, org_id, role_id) VALUES (?, ?, ?, ?)",
            ("admin",
             generate_password_hash("admin123", method=Config.PASSWORD_HASH_METHOD),
             org_id, super_role[0] if super_role else None),
        )
    if db.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0:
        # 演示账号不强制改密（密码写在 README 里）；后台新建/导入的账号才强制
        db.execute(
            "INSERT INTO students (username, password_hash, name, student_no, org_id)"
            " VALUES (?, ?, ?, ?, ?)",
            ("student1", generate_password_hash("123456", method=Config.PASSWORD_HASH_METHOD),
             "张三", "1001", org_id),
        )
    if db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0:
        cur = db.execute(
            "INSERT INTO exams (title, description, duration_minutes, max_violations, org_id)"
            " VALUES (?, ?, ?, ?, ?)",
            ("计算机基础测验", "用于演示的试卷，可在后台删除或修改。", 30, 3, org_id),
        )
        exam_id = cur.lastrowid
        sample = [
            ("choice", "Python 中用于定义函数的关键字是？",
             '["class", "def", "function", "lambda"]', "B", 5),
            ("choice", "下列哪个是关系型数据库？",
             '["Redis", "MySQL", "MongoDB", "Elasticsearch"]', "B", 5),
            ("blank", "HTTP 协议默认端口号是 ____。", '[]', '["80"]', 5),
            ("blank", "HTML 的中文全称是 ____。", '[]', '["超文本标记语言", "超文本標記語言"]', 5),
        ]
        db.executemany(
            "INSERT INTO questions (exam_id, type, question, options, answer, score) VALUES (?, ?, ?, ?, ?, ?)",
            [(exam_id, t, q, o, a, s) for t, q, o, a, s in sample],
        )


def init_db() -> None:
    """建库 / 迁移 / 播种。幂等，启动时调用。

    三段式：
    1. ``schema.sql``：新库一次到位，老库靠 IF NOT EXISTS 无痛跳过；
    2. ``_migrate()``：版本化之前的历史补列逻辑（等价于 v0 基线），保留是为了
       让 2023 年那种只允许 choice/blank 的老库还能升上来；
    3. ``migrations.apply_pending()``：版本化迁移，有未执行的就先备份再跑。

    播种放在最后：种子数据要用到迁移新加的列（org_id / role_id），
    顺序反了会报 "no such column"。
    """
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = fh.read()

    conn = sqlite3.connect(Config.DB_PATH, timeout=Config.DB_TIMEOUT)
    try:
        # 事务边界由我们显式控制，迁移执行器依赖这一点
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(schema)

        # _migrate 内部有 executescript（重建 questions 表），不能包在事务里
        _migrate(conn)

        from . import migrations

        result = migrations.apply_pending(conn)
        for version in result["applied"]:
            print("[migrate] 已应用 v%d" % version)
        if result["backup"]:
            print("[migrate] 迁移前快照：%s" % result["backup"])
        for msg in result["warnings"]:
            print("[migrate] 警告：%s" % msg)

        conn.execute("BEGIN IMMEDIATE")
        try:
            _seed(conn)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

        # 让查询规划器拿到最新的索引统计（限制采样量，避免启动变慢）
        try:
            conn.execute("PRAGMA analysis_limit = 400")
            conn.execute("PRAGMA optimize")
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
