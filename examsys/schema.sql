-- 完整表结构（幂等）。旧库由 db._migrate() 补齐差异。

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    student_no TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_ip TEXT DEFAULT '',
    last_login_at INTEGER,
    group_id INTEGER
);

CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    duration_minutes INTEGER NOT NULL DEFAULT 30,
    max_violations INTEGER NOT NULL DEFAULT 3,
    start_at INTEGER,
    end_at INTEGER,
    show_result INTEGER NOT NULL DEFAULT 1,
    show_answer INTEGER NOT NULL DEFAULT 1,
    show_explanation INTEGER NOT NULL DEFAULT 1,
    training_mode INTEGER NOT NULL DEFAULT 0,
    show_rank INTEGER NOT NULL DEFAULT 1,
    show_score_monitor INTEGER NOT NULL DEFAULT 1,
    violation_alert INTEGER NOT NULL DEFAULT 10,
    enable_egg INTEGER NOT NULL DEFAULT 0,
    show_original INTEGER NOT NULL DEFAULT 1,
    show_gongde INTEGER NOT NULL DEFAULT 0,
    enable_reflection INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL,
    parent_id INTEGER DEFAULT NULL,
    type TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT DEFAULT '[]',
    answer TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 5,
    explanation TEXT DEFAULT '',
    FOREIGN KEY (exam_id) REFERENCES exams(id),
    FOREIGN KEY (parent_id) REFERENCES questions(id)
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    exam_id INTEGER NOT NULL,
    answers TEXT DEFAULT '{}',
    score INTEGER,
    violations INTEGER NOT NULL DEFAULT 0,
    auto_submitted INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    reflection TEXT DEFAULT '',
    reflection_at INTEGER,
    question_scores TEXT DEFAULT '',
    started_at INTEGER,
    submitted_at INTEGER,
    last_seen INTEGER,
    time_offset INTEGER DEFAULT 0,
    answered_count INTEGER NOT NULL DEFAULT 0,
    overtime_seconds INTEGER NOT NULL DEFAULT 0,
    UNIQUE(student_id, exam_id),
    FOREIGN KEY (student_id) REFERENCES students(id),
    FOREIGN KEY (exam_id) REFERENCES exams(id)
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocked_ips (
    ip TEXT PRIMARY KEY,
    remark TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS exam_groups (
    exam_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    PRIMARY KEY (exam_id, group_id)
);

CREATE TABLE IF NOT EXISTS login_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER,
    username TEXT DEFAULT '',
    name TEXT DEFAULT '',
    ip TEXT DEFAULT '',
    login_at INTEGER
);

CREATE TABLE IF NOT EXISTS knowledge_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS question_kp (
    question_id INTEGER NOT NULL,
    kp_id INTEGER NOT NULL,
    PRIMARY KEY (question_id, kp_id),
    FOREIGN KEY (question_id) REFERENCES questions(id),
    FOREIGN KEY (kp_id) REFERENCES knowledge_points(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    read_at INTEGER DEFAULT NULL
);

-- 索引：覆盖学生端高频查询与后台统计
CREATE INDEX IF NOT EXISTS idx_questions_exam      ON questions(exam_id, id);
CREATE INDEX IF NOT EXISTS idx_questions_parent    ON questions(parent_id);
CREATE INDEX IF NOT EXISTS idx_results_exam_sub    ON results(exam_id, submitted_at);
CREATE INDEX IF NOT EXISTS idx_results_exam_score  ON results(exam_id, submitted_at, score);
CREATE INDEX IF NOT EXISTS idx_results_student     ON results(student_id, submitted_at);
CREATE INDEX IF NOT EXISTS idx_results_student_exam ON results(student_id, exam_id);
CREATE INDEX IF NOT EXISTS idx_results_live        ON results(exam_id, last_seen);
CREATE INDEX IF NOT EXISTS idx_students_group      ON students(group_id);
CREATE INDEX IF NOT EXISTS idx_messages_unread     ON messages(exam_id, student_id, read_at);
CREATE INDEX IF NOT EXISTS idx_login_logs_student  ON login_logs(student_id, login_at);
CREATE INDEX IF NOT EXISTS idx_login_logs_at       ON login_logs(login_at);
CREATE INDEX IF NOT EXISTS idx_question_kp_kp      ON question_kp(kp_id);
CREATE INDEX IF NOT EXISTS idx_question_kp_q       ON question_kp(question_id);
CREATE INDEX IF NOT EXISTS idx_exam_groups_group   ON exam_groups(group_id);
