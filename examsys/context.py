"""模板全局变量。

所有模板都能直接用的变量（logo、当前 IP、显示名、当前管理员、权限判断）在这里注入，
不用每个视图都手动传一遍。
"""
from flask import current_app, request, session


def inject_globals():
    from . import rbac

    admin = None
    if session.get("admin_id"):
        # 模板里要按权限藏导航；判定以服务端为准，藏起来只是减少误点
        try:
            admin = rbac.current_admin()
        except Exception:  # noqa: BLE001 - 渲染页面不该因为取不到管理员而崩
            admin = None

    return {
        "logo_url": current_app.config["LOGO_URL"],
        "brand_name": current_app.config["BRAND_NAME"],
        "brand_tagline": current_app.config["BRAND_TAGLINE"],
        "brand_color": current_app.config["BRAND_COLOR"],
        "ui_density": current_app.config["UI_DENSITY"],
        "client_ip": request.remote_addr,
        "display_name": session.get("student_name") or "",
        "current_admin": admin,
        "can": lambda perm: rbac.has_perm(admin, perm),
    }


def install(app) -> None:
    app.context_processor(inject_globals)
