"""v2：考试归属组织。

有了 ``exams.org_id``，组织管理员的考试列表、监控、统计才能只看到本组织的数据。
存量考试统一归到默认机构，行为与升级前一致。
"""
VERSION = 2
NAME = "考试归属组织"

DEFAULT_ORG_ID = 1


def _has_column(db, table, col):
    return any(r[1] == col for r in db.execute("PRAGMA table_info(%s)" % table))


def up(db):
    if not _has_column(db, "exams", "org_id"):
        db.execute("ALTER TABLE exams ADD COLUMN org_id INTEGER")
    db.execute("UPDATE exams SET org_id=? WHERE org_id IS NULL", (DEFAULT_ORG_ID,))
    db.execute("CREATE INDEX IF NOT EXISTS idx_exams_org ON exams(org_id, id)")
