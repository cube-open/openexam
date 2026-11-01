"""v1：组织 / 角色 / 审计日志，以及账号表上的权限字段。

这一步只加东西，不改也不删：所有存量账号都被归到「默认机构」，
原来的管理员全部升为超级管理员——升级完功能与升级前完全一致，
不会出现「更新之后某个管理员突然没权限了」。
"""
VERSION = 1
NAME = "组织、角色、审计日志与账号权限字段"

DEFAULT_ORG_ID = 1

# code, 名称, 作用域, 说明, 排序
ROLES = [
    ("super_admin", "超级管理员", "global", "全局配置、组织与权限管理", 10),
    ("org_admin", "组织管理员", "org", "管理本组织的教师、学生、班级与考试", 20),
    ("teacher", "教师", "self", "管理题库、组卷、发布考试、阅卷与分析", 30),
    ("student", "学生", "self", "参加考试、查看已公布成绩", 40),
]


def _has_column(db, table, col):
    return any(r[1] == col for r in db.execute("PRAGMA table_info(%s)" % table))


def _add(db, table, col, ddl):
    if not _has_column(db, table, col):
        db.execute("ALTER TABLE %s ADD COLUMN %s" % (table, ddl))


def up(db):
    # --- 组织 ---
    db.execute(
        "CREATE TABLE IF NOT EXISTS organizations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL,"
        " code TEXT NOT NULL UNIQUE,"
        " remark TEXT NOT NULL DEFAULT '',"
        " enabled INTEGER NOT NULL DEFAULT 1,"
        " created_at INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute(
        "INSERT OR IGNORE INTO organizations (id, name, code, remark, enabled, created_at)"
        " VALUES (?, ?, ?, ?, 1, ?)",
        (DEFAULT_ORG_ID, "默认机构", "default", "系统初始化时创建，可改名", 0),
    )

    # --- 角色 ---
    db.execute(
        "CREATE TABLE IF NOT EXISTS roles ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " code TEXT NOT NULL UNIQUE,"
        " name TEXT NOT NULL,"
        " scope TEXT NOT NULL DEFAULT 'self',"
        " description TEXT NOT NULL DEFAULT '',"
        " is_system INTEGER NOT NULL DEFAULT 1,"
        " sort_order INTEGER NOT NULL DEFAULT 100)"
    )
    for code, name, scope, desc, order in ROLES:
        db.execute(
            "INSERT OR IGNORE INTO roles (code, name, scope, description, is_system, sort_order)"
            " VALUES (?, ?, ?, ?, 1, ?)",
            (code, name, scope, desc, order),
        )

    # --- 审计日志 ---
    db.execute(
        "CREATE TABLE IF NOT EXISTS audit_logs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " org_id INTEGER,"
        " actor_type TEXT NOT NULL DEFAULT 'admin',"
        " actor_id INTEGER,"
        " actor_name TEXT NOT NULL DEFAULT '',"
        " action TEXT NOT NULL,"
        " target_type TEXT NOT NULL DEFAULT '',"
        " target_id INTEGER,"
        " detail TEXT NOT NULL DEFAULT '',"
        " ip TEXT NOT NULL DEFAULT '',"
        " created_at INTEGER NOT NULL DEFAULT 0)"
    )

    # --- 账号表补字段 ---
    # admins：原来只有 id / username / password_hash
    _add(db, "admins", "org_id", "org_id INTEGER")
    _add(db, "admins", "role_id", "role_id INTEGER")
    _add(db, "admins", "enabled", "enabled INTEGER NOT NULL DEFAULT 1")
    _add(db, "admins", "must_change_password",
         "must_change_password INTEGER NOT NULL DEFAULT 0")
    _add(db, "admins", "session_version", "session_version INTEGER NOT NULL DEFAULT 0")
    _add(db, "admins", "last_login_at", "last_login_at INTEGER")

    # students：已有 enabled 等字段，只补权限相关的
    _add(db, "students", "org_id", "org_id INTEGER")
    _add(db, "students", "must_change_password",
         "must_change_password INTEGER NOT NULL DEFAULT 0")
    _add(db, "students", "session_version", "session_version INTEGER NOT NULL DEFAULT 0")

    # groups：班级归属组织
    _add(db, "groups", "org_id", "org_id INTEGER")

    # --- 存量数据回填 ---
    db.execute("UPDATE admins SET org_id=? WHERE org_id IS NULL", (DEFAULT_ORG_ID,))
    db.execute("UPDATE students SET org_id=? WHERE org_id IS NULL", (DEFAULT_ORG_ID,))
    db.execute("UPDATE groups SET org_id=? WHERE org_id IS NULL", (DEFAULT_ORG_ID,))

    # 存量管理员一律升为超级管理员：升级前后权限不变
    super_id = db.execute(
        "SELECT id FROM roles WHERE code='super_admin'").fetchone()[0]
    db.execute("UPDATE admins SET role_id=? WHERE role_id IS NULL", (super_id,))
    db.execute("UPDATE admins SET enabled=1 WHERE enabled IS NULL")

    # 存量账号不强制改密码：他们的密码是用户自己设过的，不是默认密码
    db.execute("UPDATE admins SET must_change_password=0 WHERE must_change_password IS NULL")
    db.execute("UPDATE students SET must_change_password=0 WHERE must_change_password IS NULL")

    # --- 索引 ---
    db.execute("CREATE INDEX IF NOT EXISTS idx_admins_org ON admins(org_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_admins_role ON admins(role_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_students_org ON students(org_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_groups_org ON groups(org_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_org ON audit_logs(org_id, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor"
               " ON audit_logs(actor_type, actor_id, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action, created_at)")
