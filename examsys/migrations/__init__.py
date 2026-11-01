"""数据库迁移。

启动时 ``db.init_db()`` 会自动调用 :func:`apply_pending` 补齐；
也可以在命令行手动看状态 / 手动升级::

    python -m examsys.migrate status
    python -m examsys.migrate up
"""
from .runner import (MigrationError, applied, apply_pending, ensure_table,  # noqa: F401
                     modules, pending, snapshot, snapshot_path, status)
