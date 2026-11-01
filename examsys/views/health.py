"""健康检查（给压测脚本、负载均衡、运维探活用）。"""
from flask import jsonify

from .. import deps
from ..db import get_db
from ..utils import now_epoch


def healthz():
    try:
        get_db().execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:  # noqa: BLE001 - 探活接口不该因为异常返回 500
        db_ok = False
    # 缺 openpyxl / reportlab 不影响考试，只是导不出 Excel/PDF，所以不算不健康
    missing = deps.missing()
    return jsonify({
        "ok": db_ok,
        "ts": now_epoch(),
        "optional_missing": missing,
        "optional_hint": {n: deps.hint(n) for n in missing},
    })


def register(app):
    app.add_url_rule("/healthz", "healthz", healthz)
