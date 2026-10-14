"""SQLite 資料層：事項 CRUD、驗證規則、變更紀錄、視圖篩選。"""
import datetime as dt
import re
import sqlite3
from contextlib import contextmanager

ISSUE_TYPES = ("會議決議", "規範要求", "圖面問題", "模型問題", "待確認")
SOURCE_STATUSES = ("已確認", "有爭議", "缺乏證據", "無法驗證")
PRIORITIES = ("高", "中", "低")
STATUSES = ("待處理", "處理中", "等待他人回覆", "待驗收", "已驗收", "已關閉")
CLOSED_STATUSES = ("已驗收", "已關閉")
ACCEPTANCE_RESULTS = ("", "通過", "不通過")

# (欄位, 中文標題)；順序即 CSV 欄位順序
FIELDS = [
    ("issue_id", "事項編號"),
    ("project", "專案"),
    ("system", "系統"),
    ("issue_type", "事項類型"),
    ("title", "標題"),
    ("description", "描述"),
    ("source_file", "來源檔名"),
    ("source_location", "來源頁碼/段落"),
    ("source_excerpt", "原文摘錄"),
    ("source_status", "來源狀態"),
    ("owner", "負責人"),
    ("due_date", "期限"),
    ("priority", "優先級"),
    ("status", "目前狀態"),
    ("drawing_no", "關聯圖號"),
    ("drawing_rev", "圖紙版次"),
    ("model_file", "模型檔名"),
    ("model_element_id", "模型元素識別碼"),
    ("acceptance_criteria", "驗收標準"),
    ("acceptance_result", "驗收結果"),
    ("acceptance_evidence", "驗收證據路徑"),
    ("created_at", "建立時間"),
    ("updated_at", "更新時間"),
]
FIELD_LABELS = dict(FIELDS)
SYSTEM_FIELDS = ("issue_id", "created_at", "updated_at")
EDITABLE_FIELDS = [k for k, _ in FIELDS if k not in SYSTEM_FIELDS]

SORTABLE = {"issue_id", "project", "system", "issue_type", "title", "source_status",
            "owner", "due_date", "priority", "status", "drawing_no", "updated_at", "created_at"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    {", ".join(f"{k} TEXT NOT NULL DEFAULT ''" for k in EDITABLE_FIELDS)},
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_sample INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS issue_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    operator TEXT NOT NULL,
    action TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_issue ON issue_history(issue_id);
"""


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class NotFound(Exception):
    pass


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path):
    """開連線 → 成功 commit／失敗 rollback → 一定關閉。"""
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return dt.date.today().isoformat()


_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")


def parse_date(value):
    """接受 YYYY-MM-DD 或 Excel 常見的 YYYY/M/D，回傳 ISO 字串；空字串回傳空字串。"""
    value = (value or "").strip()
    if not value:
        return ""
    m = _DATE_RE.match(value)
    if not m:
        raise ValueError(value)
    return dt.date(int(m[1]), int(m[2]), int(m[3])).isoformat()


def _clean_text(value):
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize(data):
    """驗證並正規化一筆完整事項資料。回傳 (clean, warnings)；錯誤時丟 ValidationError。"""
    clean = {k: _clean_text(data.get(k, "")) for k in EDITABLE_FIELDS}
    errors, warnings = [], []

    clean["issue_type"] = clean["issue_type"] or "待確認"
    clean["source_status"] = clean["source_status"] or "缺乏證據"
    clean["priority"] = clean["priority"] or "中"
    clean["status"] = clean["status"] or "待處理"

    if not clean["title"]:
        errors.append("標題不可空白")
    for key, allowed in (("issue_type", ISSUE_TYPES), ("source_status", SOURCE_STATUSES),
                         ("priority", PRIORITIES), ("status", STATUSES),
                         ("acceptance_result", ACCEPTANCE_RESULTS)):
        if clean[key] not in allowed:
            errors.append(f"{FIELD_LABELS[key]}「{clean[key]}」不是有效值")
    try:
        clean["due_date"] = parse_date(clean["due_date"])
    except ValueError:
        errors.append(f"期限「{clean['due_date']}」格式錯誤，請用 YYYY-MM-DD")

    # 證據規則：沒有來源檔名、頁碼/段落與原文摘錄，不得標為「已確認」
    if clean["source_status"] == "已確認":
        missing = [FIELD_LABELS[k] for k in ("source_file", "source_location", "source_excerpt")
                   if not clean[k]]
        if missing:
            clean["source_status"] = "缺乏證據"
            warnings.append(f"缺少{'、'.join(missing)}，來源狀態改為「缺乏證據」")

    # 驗收規則：標為已驗收必須有通過結果與證據路徑；系統不會自動推定
    if clean["status"] == "已驗收":
        if clean["acceptance_result"] != "通過":
            errors.append("狀態為「已驗收」時，驗收結果必須為「通過」")
        if not clean["acceptance_evidence"]:
            errors.append("狀態為「已驗收」時，必須填寫驗收證據路徑")

    if errors:
        raise ValidationError(errors)
    return clean, warnings


def _row_to_dict(row):
    d = dict(row)
    d["is_sample"] = bool(d.get("is_sample"))
    return d


def get_issue(conn, issue_id):
    row = conn.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
    if row is None:
        raise NotFound(issue_id)
    return _row_to_dict(row)


def _next_issue_id(conn):
    n = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] + 1
    while conn.execute("SELECT 1 FROM issues WHERE issue_id=?", (f"SDT-{n:04d}",)).fetchone():
        n += 1
    return f"SDT-{n:04d}"


def _log(conn, issue_id, when, operator, action, field, old, new):
    conn.execute(
        "INSERT INTO issue_history(issue_id, changed_at, operator, action, field, old_value, new_value)"
        " VALUES (?,?,?,?,?,?,?)", (issue_id, when, operator, action, field, old, new))


def create_issue(conn, data, operator, *, now=None, action="create", is_sample=False,
                 issue_id=None, created_at=None):
    clean, warnings = normalize(data)
    when = now or now_str()
    issue_id = _clean_text(issue_id or data.get("issue_id")) or _next_issue_id(conn)
    if conn.execute("SELECT 1 FROM issues WHERE issue_id=?", (issue_id,)).fetchone():
        raise ValidationError([f"事項編號 {issue_id} 已存在"])
    cols = ["issue_id", *EDITABLE_FIELDS, "created_at", "updated_at", "is_sample"]
    vals = [issue_id, *(clean[k] for k in EDITABLE_FIELDS), created_at or when, when, int(is_sample)]
    conn.execute(f"INSERT INTO issues({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    for k in EDITABLE_FIELDS:
        if clean[k]:
            _log(conn, issue_id, when, operator, action, k, "", clean[k])
    return get_issue(conn, issue_id), warnings


def update_issue(conn, issue_id, changes, operator, *, now=None, action="update"):
    """部分更新：只覆寫 changes 中出現的欄位，逐欄記錄修改前後值。"""
    current = get_issue(conn, issue_id)
    merged = {k: current[k] for k in EDITABLE_FIELDS}
    merged.update({k: v for k, v in changes.items() if k in EDITABLE_FIELDS})
    clean, warnings = normalize(merged)
    diff = [k for k in EDITABLE_FIELDS if clean[k] != current[k]]
    if not diff:
        return current, warnings, []
    when = now or now_str()
    sets = ", ".join(f"{k}=?" for k in diff)
    conn.execute(f"UPDATE issues SET {sets}, updated_at=? WHERE issue_id=?",
                 [*(clean[k] for k in diff), when, issue_id])
    for k in diff:
        _log(conn, issue_id, when, operator, action, k, current[k], clean[k])
    return get_issue(conn, issue_id), warnings, diff


def get_history(conn, issue_id=None):
    sql = "SELECT * FROM issue_history"
    args = ()
    if issue_id:
        sql += " WHERE issue_id=?"
        args = (issue_id,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]


def is_overdue(issue, today):
    return bool(issue["due_date"]) and issue["due_date"] < today and issue["status"] not in CLOSED_STATUSES


VIEWS = ("all", "open", "overdue", "disputed", "accepted")


def _view_clause(view, today):
    closed = ",".join(f"'{s}'" for s in CLOSED_STATUSES)
    if view == "open":
        return f"status NOT IN ({closed})", []
    if view == "overdue":
        return f"status NOT IN ({closed}) AND due_date <> '' AND due_date < ?", [today]
    if view == "disputed":
        return ("(issue_type='待確認' OR source_status IN ('有爭議','缺乏證據','無法驗證'))"
                f" AND status NOT IN ({closed})"), []
    if view == "accepted":
        return "status='已驗收'", []
    return "1=1", []


def list_issues(conn, *, view="all", q="", filters=None, sort="due_date", order="asc", today=None):
    today = today or today_str()
    if view not in VIEWS:
        raise ValidationError([f"未知視圖 {view}"])
    where, args = _view_clause(view, today)
    clauses, params = [where], list(args)
    for key, value in (filters or {}).items():
        if key in EDITABLE_FIELDS and value:
            clauses.append(f"{key}=?")
            params.append(value)
    if q:
        like_cols = ["issue_id", "project", "system", "title", "description", "source_file",
                     "source_excerpt", "owner", "drawing_no", "model_file", "model_element_id"]
        clauses.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in like_cols) + ")")
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.extend([pattern] * len(like_cols))
    if sort not in SORTABLE:
        sort = "due_date"
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    if sort == "priority":
        order_by = f"CASE priority WHEN '高' THEN 0 WHEN '中' THEN 1 ELSE 2 END {direction}"
    elif sort == "due_date":
        # 空期限一律排最後
        order_by = f"due_date = '' ASC, due_date {direction}"
    else:
        order_by = f"{sort} {direction}"
    rows = conn.execute(
        f"SELECT * FROM issues WHERE {' AND '.join(clauses)} ORDER BY {order_by}, issue_id ASC", params)
    result = []
    for r in rows:
        d = _row_to_dict(r)
        d["overdue"] = is_overdue(d, today)
        result.append(d)
    return result


def view_counts(conn, today=None):
    return {v: len(list_issues(conn, view=v, today=today)) for v in VIEWS}


def clear_sample(conn, operator):
    ids = [r[0] for r in conn.execute("SELECT issue_id FROM issues WHERE is_sample=1")]
    when = now_str()
    for i in ids:
        _log(conn, i, when, operator, "clear-sample", "*", "範例資料", "")
    conn.execute("DELETE FROM issues WHERE is_sample=1")
    return len(ids)
