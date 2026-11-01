"""批量导入解析：学生账号、JSON 题目、Excel / CSV 题目。

分工约定：
- ``parse_*`` 只做解析、不写库（可传入 db 做查重 / 知识点名校验），
  产出规范化题目 dict 和错误列表；答案、题型都做了容错归一；
- ``insert_questions`` 统一落库。
后台导入走「上传 → 预览 → 确认」流程，确认之前不写库。
"""
import csv
import io
import json
import re

from ..utils import JUDGE_FALSE, JUDGE_TRUE, LETTERS, TYPE_ALIASES, TYPE_LABELS

# 单次导入行数上限，防手滑传了整个题库
MAX_IMPORT_ROWS = 2000

# 答案分隔符：中英文逗号、顿号、分号、斜杠、空白
_ANSWER_SPLIT_RE = re.compile(r"[\s、,，;；/|]+")
# 选项手写前缀："A." "A．" "A、" "A)" "A：" 等
_OPT_PREFIX_RE = re.compile(r"^[A-Ha-h][\.．、:：\)）]\s*")
# 表头首列的可能写法（用来判断表格第一行是不是表头）
_HEADER_TYPE_WORDS = ("题型", "类型", "题目类型", "type")
# 表头列名 → 字段
_HEADER_MAP = {
    "题型": "type", "类型": "type", "题目类型": "type",
    "题干": "question", "题目": "question", "题目内容": "question",
    "答案": "answer", "正确答案": "answer",
    "分值": "score", "分数": "score", "得分": "score",
    "解析": "explanation", "答案解析": "explanation", "解释": "explanation",
    "知识点": "kps",
}
_OPT_HEADER_RE = re.compile(r"^(?:选项|option)[-_]?([A-Ha-h])?$")


def parse_students(text: str):
    """解析学生账号文本。支持 CSV（逗号）或空白分隔，每行 username,password,name,student_no。"""
    students, errors = [], []
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return [], ["内容为空"]
    use_csv = any("," in line for line in lines)
    for i, line in enumerate(lines, 1):
        parts = next(csv.reader([line])) if use_csv else line.split()
        parts = [p.strip() for p in parts]
        if i == 1 and parts and parts[0].lower() in ("username", "用户名", "user"):
            continue
        if len(parts) < 4:
            errors.append("第%d行字段不足（需 用户名,密码,姓名,学号）" % i)
            continue
        username, password, name, student_no = parts[0], parts[1], parts[2], parts[3]
        if not all([username, password, name, student_no]):
            errors.append("第%d行存在空字段" % i)
            continue
        students.append((username, password, name, student_no))
    return students, errors


# ---------------------------------------------------------------------------
# 归一化：题型 / 选项 / 答案
# ---------------------------------------------------------------------------

def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value).strip()


def _decode_bytes(raw: bytes) -> str:
    """CSV 常见编码：带 BOM 的 UTF-8（Excel 另存）、GBK（老 Windows 导出）、UTF-8。"""
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _normalize_type(raw):
    text = _clean(raw).lower().replace(" ", "").replace("　", "")
    if not text:
        return None
    return TYPE_ALIASES.get(text)


def _split_blank(value) -> list:
    """填空/多答案文本 → 列表。支持 |、；、;、换行分隔。"""
    if isinstance(value, list):
        return [str(a).strip() for a in value if str(a).strip()]
    text = str(value or "")
    for ch in ("；", ";", "\n", "\r"):
        text = text.replace(ch, "|")
    return [a.strip() for a in text.split("|") if a.strip()]


def _normalize_options(raw) -> list:
    """选项列表：去空、去掉手写的 "A." 前缀。"""
    if isinstance(raw, list):
        items = [_clean(o) for o in raw]
    else:
        items = _split_blank(raw)
    out = []
    for t in items:
        t = _OPT_PREFIX_RE.sub("", t, count=1)
        if t:
            out.append(t)
    return out


def _normalize_judge_answer(value):
    """判断题答案 → 'A'（正确）/ 'B'（错误）；无法识别返回 None。"""
    if isinstance(value, bool):
        return "A" if value else "B"
    text = _clean(value).lower().rstrip("。.．,，;；!！?？")
    text = text.replace(" ", "")
    if not text:
        return None
    if text in JUDGE_TRUE:
        return "A"
    if text in JUDGE_FALSE:
        return "B"
    return None


def _match_option_token(token, options):
    """单个答案 token → 选项字母。支持：选项原文、单个字母、数字序号。"""
    t = _clean(token).strip(".)）】]（(").strip()
    if not t:
        return None
    for i, opt in enumerate(options):
        if _clean(opt) == t:
            return LETTERS[i]
    if len(t) == 1 and t.upper() in LETTERS[:len(options)]:
        return t.upper()
    if t.isdigit():
        n = int(t)
        if 1 <= n <= len(options):
            return LETTERS[n - 1]
    return None


def _match_choice_answer(text, options, multi):
    """单选/多选答案 → 字母串；无法识别返回 None。

    多选认：ABD / A,B,D / A、B、D / 选项原文（分隔符分开）。
    单选认：B / b / 2（序号）/ 选项原文。
    """
    text = _clean(text)
    if not text:
        return None
    if multi:
        compact = text.upper().replace(" ", "")
        if compact and all(c in LETTERS for c in compact):
            valid = [c for c in compact if c in LETTERS[:len(options)]]
            if len(valid) != len(compact):
                return None  # 有字母超出选项范围，宁可报错也不静默丢
            if valid:
                return "".join(sorted(set(valid)))
    tokens = [t for t in _ANSWER_SPLIT_RE.split(text) if t]
    if len(tokens) > 1:
        letters = []
        for tk in tokens:
            hit = _match_option_token(tk, options)
            if hit is None:
                return None
            letters.append(hit)
        return "".join(sorted(set(letters)))
    if tokens:
        return _match_option_token(tokens[0], options)
    return None


def _infer_type(options, answer_raw):
    """题型列留空时根据选项和答案猜一个；猜不出返回 None。"""
    text = _clean(answer_raw)
    if not options:
        if _normalize_judge_answer(text) is not None:
            return "judge"
        return "blank" if text else None
    if len(options) < 2:
        return None
    tokens = [t for t in _ANSWER_SPLIT_RE.split(text) if t]
    compact = text.upper().replace(" ", "")
    if len(tokens) > 1:
        return "multi"
    if len(compact) > 1 and all(c in LETTERS for c in compact):
        return "multi"
    return "choice"


# ---------------------------------------------------------------------------
# 行 → 题目（JSON / Excel / CSV 共用）
# ---------------------------------------------------------------------------

def _parse_row(row, pos, db=None):
    """row: dict(type/question/options/answer/score/explanation/kps)。

    返回 (item, error)；item 为规范化题目，blank 的 answer 是列表。
    """
    typ = _normalize_type(row.get("type"))
    question = _clean(row.get("question"))
    options = _normalize_options(row.get("options"))
    inferred = False
    if not typ:
        typ = _infer_type(options, row.get("answer"))
        inferred = typ is not None

    if not question:
        return None, "%s题干为空" % pos
    if not typ:
        return None, "%s题型无法识别：「%s」（支持 单选/多选/判断/填空）" % (pos, _clean(row.get("type")))

    # 分值：留空默认 5，填了但认不出要报错（静默改分比报错更糟）
    score_raw = row.get("score")
    if score_raw is None or _clean(score_raw) == "":
        score = 5
    else:
        try:
            score = int(_clean(score_raw))
        except (ValueError, TypeError):
            return None, "%s分值无法识别：「%s」" % (pos, _clean(score_raw))
        if not 1 <= score <= 100:
            return None, "%s分值应在 1-100 之间（当前 %d）" % (pos, score)

    explanation = _clean(row.get("explanation"))

    if typ == "judge":
        answer = _normalize_judge_answer(row.get("answer"))
        if answer is None:
            return None, "%s判断题答案无法识别（支持 A/B、正确/错误、对/错、T/F、√/×）" % pos
        options = ["正确", "错误"]
    elif typ in ("choice", "multi"):
        if len(options) < 2:
            return None, "%s%s至少需要两个选项" % (pos, TYPE_LABELS.get(typ, ""))
        answer = _match_choice_answer(row.get("answer"), options, multi=(typ == "multi"))
        if not answer:
            hint = "ABD、A,B,D 或选项原文" if typ == "multi" else "字母如 B、序号如 2 或选项原文"
            return None, "%s%s答案无法识别（可填 %s）" % (pos, TYPE_LABELS.get(typ, ""), hint)
        if typ == "choice" and len(answer) != 1:
            return None, "%s单选题答案只能对应一个选项（当前识别出 %s）" % (pos, answer)
    elif typ == "group":
        options = []
        answer = ""
    else:  # blank
        answers = _split_blank(row.get("answer"))
        if not answers:
            return None, "%s填空题答案为空（多个可接受答案用 | 分隔）" % pos
        answer = answers

    # 知识点：按名字链接，名字不存在则忽略并记一条警告（不因此丢掉整道题）
    kp_names = []
    raw_kps = row.get("kps")
    if isinstance(raw_kps, str):
        raw_kps = _split_blank(raw_kps)
    for k in (raw_kps or []):
        name = _clean(k)
        if name:
            kp_names.append(name)
    warnings = []
    if kp_names and db is not None:
        known = {r["name"] for r in db.execute("SELECT name FROM knowledge_points").fetchall()}
        unknown = [n for n in kp_names if n not in known]
        if unknown:
            warnings.append("%s知识点不存在已忽略：%s" % (pos, "、".join(unknown)))
        kp_names = [n for n in kp_names if n in known]

    return {
        "type": typ, "question": question, "options": options,
        "answer": answer, "score": score, "explanation": explanation,
        "kps": kp_names, "inferred": inferred, "subs": [], "warnings": warnings,
    }, None


def parse_questions_json(raw: str, db=None):
    """解析 JSON 数组文本。返回 (items, errors)；JSON 语法错误抛 ValueError。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("JSON 解析失败：%s（位置 %s）" % (exc.msg, exc.pos))
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("格式应为 JSON 数组")
    if len(data) > MAX_IMPORT_ROWS:
        raise ValueError("一次最多导入 %d 条（当前 %d）" % (MAX_IMPORT_ROWS, len(data)))

    items, errors = [], []

    def row_of(obj):
        return {
            "type": obj.get("type"), "question": obj.get("question"),
            "options": obj.get("options"), "answer": obj.get("answer"),
            "score": obj.get("score"), "explanation": obj.get("explanation"),
            "kps": obj.get("kp") or obj.get("kps") or obj.get("knowledge_points") or [],
        }

    for i, obj in enumerate(data, 1):
        pos = "第%d条" % i
        if not isinstance(obj, dict):
            errors.append("%s不是对象" % pos)
            continue
        item, err = _parse_row(row_of(obj), pos, db)
        if err:
            errors.append(err)
            continue
        errors.extend(item.pop("warnings", []))
        subs = obj.get("sub") or obj.get("subs") or obj.get("children") or []
        if isinstance(subs, list):
            for j, sub in enumerate(subs, 1):
                if not isinstance(sub, dict):
                    errors.append("%s.%d 不是对象" % (i, j))
                    continue
                sub_item, sub_err = _parse_row(row_of(sub), "%d.%d" % (i, j), db)
                if sub_err:
                    errors.append(sub_err)
                else:
                    errors.extend(sub_item.pop("warnings", []))
                    item["subs"].append(sub_item)
        items.append(item)
    return items, errors


def _looks_like_header(cells):
    first = _clean(cells[0]).lower() if cells else ""
    return first in _HEADER_TYPE_WORDS


def parse_questions_excel(stream, db=None):
    """解析 .xlsx 文件流。列：0题型 1题干 2~9选项 10答案 11分值 12解析 13知识点(可选)。"""
    from openpyxl import load_workbook

    wb = load_workbook(stream, data_only=True, read_only=True)
    ws = wb.active
    rows = []
    seen_content = False
    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        cells = list(row)
        if not cells or all(_clean(c) == "" for c in cells):
            continue
        if not seen_content:
            seen_content = True
            if _looks_like_header(cells):
                continue  # 第一行是表头，跳过
        rows.append((i, cells))
    wb.close()
    return _parse_tabular(rows, db)


def parse_questions_csv(raw: bytes, db=None):
    """解析 CSV 字节流（列同 Excel）。自动识别 utf-8-sig / gbk。"""
    text = _decode_bytes(raw)
    rows = []
    seen_content = False
    for i, cells in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not cells or all(_clean(c) == "" for c in cells):
            continue
        if not seen_content:
            seen_content = True
            if _looks_like_header(cells):
                continue
        rows.append((i, cells))
    return _parse_tabular(rows, db)


def _parse_tabular(rows, db=None):
    """rows: [(行号, cells)] → (items, errors)。"""
    if len(rows) > MAX_IMPORT_ROWS:
        raise ValueError("一次最多导入 %d 行（当前 %d）" % (MAX_IMPORT_ROWS, len(rows)))
    items, errors = [], []
    for lineno, cells in rows:
        pos = "第%d行" % lineno

        def col(idx):
            return cells[idx] if len(cells) > idx else None

        row = {
            "type": col(0),
            "question": col(1),
            "options": [col(j) for j in range(2, 10)],
            "answer": col(10),
            "score": col(11),
            "explanation": col(12),
            "kps": _split_blank(col(13)) if _clean(col(13)) else [],
        }
        item, err = _parse_row(row, pos, db)
        if err:
            errors.append(err)
        else:
            errors.extend(item.pop("warnings", []))
            items.append(item)
    return items, errors


# ---------------------------------------------------------------------------
# 查重 + 落库
# ---------------------------------------------------------------------------

def find_duplicates(db, exam_id: int, items) -> int:
    """标记题干在同一试卷下已存在的题目（含文件内重复），返回重复条数。"""
    existing = set()
    for r in db.execute("SELECT type, question FROM questions WHERE exam_id=?", (exam_id,)).fetchall():
        existing.add((r["type"], _clean(r["question"])))

    dups = 0

    def walk(it):
        nonlocal dups
        key = (it["type"], it["question"])
        if key in existing:
            it["dup"] = True
            dups += 1
        else:
            it["dup"] = False
            existing.add(key)
        for sub in it.get("subs") or []:
            walk(sub)

    for it in items:
        walk(it)
    return dups


def clear_dup_flags(items) -> None:
    for it in items:
        it["dup"] = False
        for sub in it.get("subs") or []:
            sub["dup"] = False


def _link_kps(db, question_id: int, kp_names) -> None:
    if not kp_names:
        return
    ids = []
    for name in kp_names:
        r = db.execute("SELECT id FROM knowledge_points WHERE name=?", (name,)).fetchone()
        if r:
            ids.append(r["id"])
    if ids:
        db.executemany(
            "INSERT OR IGNORE INTO question_kp (question_id, kp_id) VALUES (?, ?)",
            [(question_id, i) for i in ids],
        )


def _answer_column(item) -> str:
    if item["type"] == "blank":
        return json.dumps(item["answer"], ensure_ascii=False)
    return item["answer"]


def insert_questions(db, exam_id: int, items) -> tuple:
    """落库。返回 (插入数, 跳过数)。带 dup 标记的条目（含其子题）跳过。"""
    inserted = 0
    skipped = 0
    for it in items:
        subs = [s for s in (it.get("subs") or []) if not s.get("dup")]
        if it.get("dup"):
            skipped += 1 + len(subs)
            continue
        cur = db.execute(
            "INSERT INTO questions (exam_id, type, question, options, answer, score, explanation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (exam_id, it["type"], it["question"],
             json.dumps(it["options"], ensure_ascii=False),
             _answer_column(it), it["score"], it["explanation"]),
        )
        inserted += 1
        qid = cur.lastrowid
        _link_kps(db, qid, it.get("kps") or [])
        if subs:
            db.executemany(
                "INSERT INTO questions (exam_id, parent_id, type, question, options, answer, score, explanation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(exam_id, qid, s["type"], s["question"],
                  json.dumps(s["options"], ensure_ascii=False),
                  _answer_column(s), s["score"], s["explanation"]) for s in subs],
            )
            inserted += len(subs)
            sub_ids = [r["id"] for r in db.execute(
                "SELECT id FROM questions WHERE parent_id=? ORDER BY id DESC LIMIT ?",
                (qid, len(subs)),
            ).fetchall()]
            sub_ids.reverse()
            for s, sid in zip(subs, sub_ids):
                _link_kps(db, sid, s.get("kps") or [])
    return inserted, skipped
