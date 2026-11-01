"""安全相关能力。

对外统一从这里导入，视图层不需要知道内部怎么拆的：

    from ..security import login_required_student, hash_password, login_limiter
"""
from .decorators import (admin_gate, current_student, login_required_admin,
                         login_required_student, student_gate)
from .ipban import blocked_ips, invalidate_blocked_ips, is_ip_blocked
from .passwords import hash_many, hash_password, needs_rehash, verify_password
from .ratelimit import RateLimiter, login_limiter

__all__ = [
    "current_student",
    "login_required_student",
    "login_required_admin",
    "student_gate",
    "admin_gate",
    "hash_password",
    "verify_password",
    "needs_rehash",
    "hash_many",
    "RateLimiter",
    "login_limiter",
    "blocked_ips",
    "invalidate_blocked_ips",
    "is_ip_blocked",
]
