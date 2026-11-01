"""IP 封禁名单。

名单放在 TTL 缓存里，封禁 / 解封时主动失效，
避免每个请求（尤其 3 秒一次的心跳）都去查一次表。
"""
from ..cache import TTLCache
from ..config import Config
from ..db import get_db

_cache = TTLCache(ttl=Config.BLOCKED_IP_TTL, maxsize=4)


def blocked_ips() -> set:
    def load():
        rows = get_db().execute("SELECT ip FROM blocked_ips").fetchall()
        return {r["ip"] for r in rows}

    return _cache.get_or_load("all", load)


def invalidate_blocked_ips() -> None:
    _cache.invalidate("all")


def is_ip_blocked(ip: str) -> bool:
    if not ip:
        return False
    return ip in blocked_ips()
