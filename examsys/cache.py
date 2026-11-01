"""极简进程内缓存。

只缓存「读多写少、写操作可明确失效」的数据：题目列表、封禁 IP 集合。
多进程部署时缓存互不可见，所以 TTL 保持较短，写路径一律主动失效。
"""
import threading
import time


class TTLCache:
    def __init__(self, ttl: float = 60.0, maxsize: int = 512):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expire_at, value = item
            if expire_at < time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key, value):
        with self._lock:
            if len(self._data) >= self.maxsize:
                # 简单淘汰：清掉已经过期的，仍然满就清一半
                now = time.monotonic()
                expired = [k for k, (exp, _) in self._data.items() if exp < now]
                for k in expired:
                    self._data.pop(k, None)
                if len(self._data) >= self.maxsize:
                    for k in list(self._data)[: self.maxsize // 2]:
                        self._data.pop(k, None)
            self._data[key] = (time.monotonic() + self.ttl, value)

    def get_or_load(self, key, loader):
        value = self.get(key)
        if value is not None:
            return value
        value = loader()
        self.set(key, value)
        return value

    def invalidate(self, key) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
