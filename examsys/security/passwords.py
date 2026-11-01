"""口令哈希。

只做一件事：把明文口令变成可存库的哈希、校验、以及批量并发哈希。
算法由 ``Config.PASSWORD_HASH_METHOD`` 决定，旧哈希会在登录成功时自动升级。
"""
from concurrent.futures import ThreadPoolExecutor
import os

from werkzeug.security import check_password_hash, generate_password_hash

from ..config import Config


def hash_password(raw: str) -> str:
    return generate_password_hash(raw, method=Config.PASSWORD_HASH_METHOD)


def verify_password(stored: str, raw: str) -> bool:
    try:
        return check_password_hash(stored or "", raw or "")
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    """旧库里的 scrypt 哈希在登录成功时顺手升级成当前算法。"""
    prefix = Config.PASSWORD_HASH_METHOD + "$"
    return bool(stored) and not stored.startswith(prefix)


def hash_many(raw_passwords) -> dict:
    """批量哈希。

    单次约 70ms，导入 500 个学生就是 35 秒的同步阻塞。
    hashlib 的 KDF 实现会释放 GIL，用线程池并发能把这段压到几秒。
    """
    unique = list({p for p in raw_passwords if p})
    if not unique:
        return {}
    if len(unique) == 1:
        return {unique[0]: hash_password(unique[0])}
    workers = min(8, max(2, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(unique, pool.map(hash_password, unique)))
