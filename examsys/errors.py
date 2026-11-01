"""统一错误页。

接口请求返回 JSON，页面请求返回模板，避免前端拿到一整页 HTML。
"""
from flask import current_app, jsonify, render_template, request


def _wants_json() -> bool:
    return request.path.startswith("/api/")


def not_found(_err):
    if _wants_json():
        return jsonify({"ok": False, "msg": "not found"}), 404
    return render_template("error.html", code=404, message="页面不存在"), 404


def forbidden(_err):
    if _wants_json():
        return jsonify({"ok": False, "msg": "forbidden"}), 403
    return render_template("error.html", code=403, message="没有权限访问该页面"), 403


def server_error(err):
    current_app.logger.exception("unhandled error: %s", err)
    if _wants_json():
        return jsonify({"ok": False, "msg": "服务器内部错误"}), 500
    return render_template("error.html", code=500,
                          message="服务器开小差了，请稍后重试"), 500


def install(app) -> None:
    app.register_error_handler(404, not_found)
    app.register_error_handler(403, forbidden)
    app.register_error_handler(500, server_error)
