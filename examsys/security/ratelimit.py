"""登录限流：进程内滑动窗口。

只统计「失败」事件，成功登录会清零。按 IP+用户名 计数，
避免同一 NAT 出口下整个班级互相拖累。
"""
import threading
import time
from collections import deque

from ..config import Config


class RateLimiter:
    def __init__(self, limit: int, window: int, max_keys: int = 8192):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: dict = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def _prune(self, bucket: deque, now: float) -> None:
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def exceeded(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.get(key)
            if not bucket:
                return False
            self._prune(bucket, now)
            return len(bucket) >= self.limit

    def hit(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep > self.window:
                self._sweep(now)
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self.max_keys:
                    self._sweep(now)
                bucket = deque()
                self._hits[key] = bucket
            self._prune(bucket, now)
            bucket.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window
        for k in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            self._hits.pop(k, None)
        self._last_sweep = now


login_limiter = RateLimiter(Config.LOGIN_RATE_LIMIT, Config.LOGIN_RATE_WINDOW)
