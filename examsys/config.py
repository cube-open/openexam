"""集中配置。

所有可调参数都在这里，视图层不再硬编码常量。
"""
import os
import secrets

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _load_secret_key() -> str:
    """优先环境变量，其次项目根目录的 .secret_key，最后随机生成并落盘。

    固定写死的 SECRET_KEY 等于把会话签名密钥公开，任何人都能伪造 admin_id。
    """
    env_key = (os.environ.get("EXAM_SECRET_KEY") or "").strip()
    if len(env_key) >= 16:
        return env_key

    path = os.path.join(BASE_DIR, ".secret_key")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass

    key = secrets.token_hex(32)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return key


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    # --- 基础 ---
    SECRET_KEY = _load_secret_key()
    BRAND_NAME = os.environ.get("EXAM_BRAND_NAME", "在线考试系统")
    BRAND_TAGLINE = os.environ.get("EXAM_BRAND_TAGLINE", "").strip()
    LOGO_URL = os.environ.get("EXAM_LOGO_URL", "").strip()
    BRAND_COLOR = os.environ.get("EXAM_BRAND_COLOR", "#2563eb").strip()
    UI_DENSITY = os.environ.get("EXAM_UI_DENSITY", "comfortable").strip().lower()
    SEND_FILE_MAX_AGE_DEFAULT = 86400  # 静态资源缓存 1 天
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 上传上限 16MB（Excel / 备份）

    # --- 会话 ---
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 8 * 3600
    # 生产默认不重载模板（省 IO）。开发时设 EXAM_TEMPLATES_RELOAD=1 或用 tools/dev.py。
    TEMPLATES_AUTO_RELOAD = (os.environ.get("EXAM_TEMPLATES_RELOAD", "0")
                              .strip().lower() in ("1", "true", "yes"))
    # 单点登录：同一账号只保留最新一次登录，旧设备上的会话立即失效。
    # 考试系统默认开启（防代考）；如果场景允许一人多端同时在线，设 EXAM_SINGLE_SESSION=0
    ENFORCE_SINGLE_SESSION = (os.environ.get("EXAM_SINGLE_SESSION", "1")
                              .strip().lower() not in ("0", "false", "no"))

    # --- 数据库 ---
    DB_PATH = os.environ.get("EXAM_DB") or os.path.join(BASE_DIR, "exam.db")
    DB_POOL_SIZE = _env_int("EXAM_DB_POOL", 32)
    DB_TIMEOUT = 30
    DB_BUSY_TIMEOUT_MS = 15000
    DB_WRITE_RETRIES = 6
    DB_CACHE_SIZE_KB = 16384  # 16MB page cache

    # --- 业务参数 ---
    ONLINE_WINDOW = 20  # 心跳 20 秒内算在线
    LAST_SEEN_FLUSH_INTERVAL = 3  # last_seen 最多每 3 秒落库一次
    BLOCKED_IP_TTL = 5  # 封禁名单缓存秒数
    QUESTION_CACHE_TTL = 120  # 题目缓存秒数（写操作会主动失效）
    LOGIN_RATE_LIMIT = 15  # 每窗口允许的登录尝试次数
    LOGIN_RATE_WINDOW = 60
    BACKUP_DIR = os.environ.get("EXAM_BACKUP_DIR") or os.path.join(BASE_DIR, "backups")
    BACKUP_INTERVAL_HOURS = _env_int("EXAM_BACKUP_HOURS", 24)  # 自动备份间隔
    BACKUP_KEEP = _env_int("EXAM_BACKUP_KEEP", 7)  # 保留份数
    MAX_ANSWERS_ITEMS = 2000  # 单次保存的作答条目上限
    MAX_ANSWER_LENGTH = 20000  # 单题答案长度上限
    # 服务端强制时限：超过截止时间后 /api/save 一律拒收，改 JS / 断网拖时间都没用。
    # 交卷请求允许的宽限秒数——网络抖动会让交卷比截止时间晚几十秒到达，
    # 这段时间内仍采信客户端提交的答案；超出宽限则只认截止前已落库的答案。
    EXAM_SUBMIT_GRACE_SECONDS = _env_int("EXAM_SUBMIT_GRACE", 30)
    # 口令哈希：pbkdf2 与 scrypt 在本机都约 66~72ms，但 scrypt 每次要占 32MB 内存。
    # 开考瞬间几十人同时登录时，scrypt 会瞬间吃掉几个 GB；pbkdf2 的常驻内存几乎为零。
    PASSWORD_HASH_METHOD = os.environ.get("EXAM_PASSWORD_HASH", "pbkdf2:sha256:260000")
