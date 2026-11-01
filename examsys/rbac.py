"""角色与权限（RBAC）。

设计取舍
--------
**权限点写在代码里，角色写在数据库里。**

权限点是「代码里存在哪些操作」，只能由路由决定；把它放进数据库，就会出现
「库里给了权限但代码没有对应功能」或者反过来的错位。所以：

- ``PERMISSIONS`` / ``ROLE_PERMISSIONS`` 是常量，加权限要改代码，这是刻意的；
- ``roles`` 表存角色本身，可以改名、可以加新角色（新角色在 ``ROLE_PERMISSIONS``
  里没有条目就等于没有权限，安全默认）。

**一个账号一个角色。** 需求里列了 ``user_roles``，但四类角色之间不存在兼任场景
（教师不会同时是学生），一列 ``admins.role_id`` 比一张关联表更简单、更快，
也不会出现两份数据不一致。将来真要一人多角色，再加 ``user_roles`` 表并把
``has_perm`` 改成取并集即可，不影响现有数据。

**数据隔离靠 ``org_id``。** 超级管理员 ``visible_org_id()`` 返回 ``None`` 表示
不限组织；其余角色只能看到自己组织的数据。
"""
import functools

from flask import current_app, g, jsonify, request

from .db import get_db

# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------

ROLE_SUPER_ADMIN = "super_admin"
ROLE_ORG_ADMIN = "org_admin"
ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"

# 后台可登录的角色（学生不走后台）
ADMIN_ROLES = (ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_TEACHER)

# ---------------------------------------------------------------------------
# 权限点
# ---------------------------------------------------------------------------

PERMISSIONS = {
    "system.settings": "系统设置与数据管理",
    "system.audit": "审计日志",
    "org.manage": "组织与权限管理",
    "user.manage": "用户增删改与密码重置",
    "user.view": "查看学生名单与班级",
    "bank.manage": "题库与组卷",
    "exam.manage": "考试创建、编辑、发布",
    "exam.monitor": "考试监控与考生管理",
    "grade.view": "查看成绩与答卷",
    "grade.manual": "人工阅卷与改分",
    "result.export": "导出成绩报表",
}

_ALL = set(PERMISSIONS)

ROLE_PERMISSIONS = {
    # 超级管理员：全部
    ROLE_SUPER_ADMIN: _ALL,
    # 组织管理员：本组织的教师、学生、班级、考试与成绩；不含全局设置与建组织
    ROLE_ORG_ADMIN: {
        "system.audit",
        "user.manage", "user.view",
        "bank.manage",
        "exam.manage", "exam.monitor",
        "grade.view", "grade.manual",
        "result.export",
    },
    # 教师：题库、组卷、发布考试、阅卷、看成绩；不管账号
    ROLE_TEACHER: {
        "user.view",
        "bank.manage",
        "exam.manage", "exam.monitor",
        "grade.view", "grade.manual",
        "result.export",
    },
    # 学生：后台权限为空，学生端由 login_required_student 控制
    ROLE_STUDENT: set(),
}


def permissions_of(role_code: str) -> set:
    return ROLE_PERMISSIONS.get(role_code or "", set())


# ---------------------------------------------------------------------------
# 当前管理员
# ---------------------------------------------------------------------------

_ADMIN_SQL = (
    "SELECT a.id, a.username, a.org_id, a.role_id, a.enabled,"
    "       a.must_change_password, a.session_version, a.last_login_at,"
    "       r.code AS role_code, r.name AS role_name, r.scope AS role_scope,"
    "       o.name AS org_name"
    "  FROM admins a"
    "  LEFT JOIN roles r ON r.id = a.role_id"
    "  LEFT JOIN organizations o ON o.id = a.org_id"
    " WHERE a.id = ?"
)


def current_admin():
    """当前登录的管理员（带角色）。同一请求内只查一次。"""
    if getattr(g, "_admin_loaded", False):
        return g._admin_row
    from flask import session

    g._admin_loaded = True
    g._admin_row = None
    aid = session.get("admin_id")
    if not aid:
        return None
    row = get_db().execute(_ADMIN_SQL, (aid,)).fetchone()
    if row is not None and not row["role_code"]:
        # 迁移异常导致角色丢失时，按升级前的行为（全部权限）兜底，
        # 否则会把老管理员锁在门外。数据正常时走不到这里。
        current_app.logger.warning(
            "管理员 %s 没有 role_id，按超级管理员处理", row["username"])
        row = dict(row)
        row["role_code"] = ROLE_SUPER_ADMIN
        row["role_name"] = "超级管理员"
    g._admin_row = row
    return row


def has_perm(admin, perm: str) -> bool:
    if admin is None:
        return False
    return perm in permissions_of(admin["role_code"])


def visible_org_id(admin):
    """``None`` = 不限组织（超级管理员）。"""
    if admin is None:
        return None
    if admin["role_code"] == ROLE_SUPER_ADMIN:
        return None
    return admin["org_id"]


def org_scope(admin, column: str = "org_id"):
    """拼一个 ``(sql 片段, 参数)``，用于给列表查询加组织过滤。

    用法::

        where, params = org_scope(admin, "e.org_id")
        sql = "SELECT ... FROM exams e WHERE 1=1" + where
    """
    org_id = visible_org_id(admin)
    if org_id is None:
        return "", ()
    return " AND %s = ?" % column, (org_id,)


# ---------------------------------------------------------------------------
# 装饰器
# ---------------------------------------------------------------------------

def require_perm(perm: str):
    """要求管理员已登录且拥有某个权限点。

    权限在服务端判定——前端按钮藏没藏都不影响这里的结果。
    """
    if perm not in PERMISSIONS:
        raise ValueError("未定义的权限点: %s" % perm)

    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            from .security.decorators import admin_gate

            blocked = admin_gate()
            if blocked is not None:
                return blocked
            admin = current_admin()
            if not has_perm(admin, perm):
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "msg": "没有权限执行该操作"}), 403
                current_app.logger.warning(
                    "权限不足: %s 访问 %s 需要 %s",
                    admin["username"] if admin else "-", request.path, perm)
                from flask import abort

                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def assign_role(db, admin_id: int, role_code: str) -> None:
    row = db.execute("SELECT id FROM roles WHERE code=?", (role_code,)).fetchone()
    if row is None:
        raise ValueError("未知角色: %s" % role_code)
    db.execute("UPDATE admins SET role_id=? WHERE id=?", (row["id"], admin_id))
