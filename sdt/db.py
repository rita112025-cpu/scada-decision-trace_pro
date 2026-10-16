"""SQLite 資料層：事項 CRUD、驗證規則、變更紀錄、視圖篩選。"""
import datetime as dt
import re
import sqlite3
from contextlib import contextmanager

ISSUE_TYPES = ("會議決議", "待辦", "規範要求", "圖面問題", "模型問題", "待確認")
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

# 既有 issues 表維持原欄位；下列欄位與資料表在 migrate() 補上，不刪除原資料。
TRACE_COLUMNS = (
    "project_id", "transcript_id", "segment_ref", "draft_id", "extraction_method", "source_quote",
)

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name ON projects(name);

CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    source_filename TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    duration_seconds TEXT NOT NULL DEFAULT '',
    export_status TEXT NOT NULL DEFAULT '',
    export_time TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    segment_count INTEGER NOT NULL,
    timestamp_status TEXT NOT NULL,
    timestamp_detail TEXT NOT NULL DEFAULT '',
    UNIQUE(project_id, content_hash)
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    segment_id TEXT PRIMARY KEY,
    transcript_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    source_segment_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    speaker TEXT NOT NULL DEFAULT '',
    start_seconds TEXT NOT NULL DEFAULT '',
    end_seconds TEXT NOT NULL DEFAULT '',
    timestamp_label TEXT NOT NULL,
    timestamp_source TEXT NOT NULL DEFAULT '',
    line_start INTEGER,
    line_end INTEGER,
    edited INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    transcript_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    location_label TEXT NOT NULL,
    timestamp_label TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    limitation TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    owner_origin TEXT NOT NULL DEFAULT '未指定',
    due_date TEXT NOT NULL DEFAULT '',
    due_origin TEXT NOT NULL DEFAULT '未指定',
    review_status TEXT NOT NULL,
    issue_id TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    review_gaps TEXT NOT NULL DEFAULT '',
    context_refs TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_links (
    link_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    notebook TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    page TEXT NOT NULL DEFAULT '',
    line_start TEXT NOT NULL DEFAULT '',
    line_end TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL DEFAULT '',
    endorsement TEXT NOT NULL DEFAULT '未主張支持',
    file_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(issue_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_segments_transcript ON transcript_segments(transcript_id, seq);
CREATE INDEX IF NOT EXISTS idx_drafts_project ON drafts(project_id, review_status);
CREATE INDEX IF NOT EXISTS idx_links_issue ON document_links(issue_id);
"""


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class NotFound(Exception):
    def __init__(self, key="", message="找不到此事項"):
        super().__init__(message)
        self.key = key
        self.message = message


def _table_columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn):
    """補上專案 ID、逐字稿與文件關聯。只新增欄位與資料表，不刪除、不改寫既有事項內容。"""
    conn.executescript(EXTRA_SCHEMA)
    cols = _table_columns(conn, "issues")
    for col in TRACE_COLUMNS:
        if col not in cols:
            conn.execute(f"ALTER TABLE issues ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    link_cols = _table_columns(conn, "document_links")
    if "file_hash" not in link_cols:
        conn.execute("ALTER TABLE document_links ADD COLUMN file_hash TEXT NOT NULL DEFAULT ''")
    draft_cols = _table_columns(conn, "drafts")
    for col in ("rule_id", "reason", "review_gaps", "context_refs"):
        if col not in draft_cols:
            conn.execute(f"ALTER TABLE drafts ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project_id)")
    # 依既有「專案」文字歸入穩定 ID。名稱相同的列共用一個專案；空白名稱也保留為一筆。
    names = [r["project"] for r in conn.execute("SELECT DISTINCT project FROM issues")]
    for name in names:
        pid = ensure_project(conn, name)
        conn.execute(
            "UPDATE issues SET project_id=? WHERE project=? AND (project_id IS NULL OR project_id='')",
            (pid, name))


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
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


def next_public_id(conn, table, column, prefix):
    """不可變編號。只掃描指定資料表的既有編號，不重用已發出的 ID。"""
    best = 0
    for row in conn.execute(f"SELECT {column} FROM {table}"):
        val = row[0]
        if isinstance(val, str) and val.startswith(prefix):
            tail = val[len(prefix):]
            if tail.isdigit():
                best = max(best, int(tail))
    return f"{prefix}{best + 1:04d}"


def list_projects(conn):
    return [dict(r) for r in conn.execute(
        "SELECT project_id, name, created_at FROM projects ORDER BY project_id")]


def get_project(conn, project_id):
    row = conn.execute(
        "SELECT project_id, name, created_at FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        raise NotFound(project_id, "找不到此專案")
    return dict(row)


def create_project(conn, name, *, now=None):
    name = _clean_text(name)
    if len(name) > 200:
        raise ValidationError(["專案名稱過長（上限 200 字）"])
    if conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        label = name or "（未命名）"
        raise ValidationError([f"專案名稱「{label}」已存在"])
    pid = next_public_id(conn, "projects", "project_id", "PRJ-")
    when = now or now_str()
    conn.execute("INSERT INTO projects(project_id, name, created_at) VALUES (?,?,?)", (pid, name, when))
    return get_project(conn, pid)


def ensure_project(conn, name, *, now=None):
    """沿用同名專案；沒有才建立。名稱是顯示用，project_id 建立後不變。"""
    name = _clean_text(name)
    row = conn.execute("SELECT project_id FROM projects WHERE name=?", (name,)).fetchone()
    if row:
        return row["project_id"]
    return create_project(conn, name, now=now)["project_id"]


def bind_project(conn, data, clean):
    """事項歸屬專案。有 project_id 時以 ID 為準，並把名稱改成該專案的現名（不會因此重新命名）。"""
    pid = _clean_text((data or {}).get("project_id"))
    if pid:
        proj = get_project(conn, pid)
        clean["project"] = proj["name"]
        return proj["project_id"]
    return ensure_project(conn, clean.get("project", ""))


def rename_project(conn, project_id, new_name, operator, *, now=None):
    """只改名稱。既有事項的 project_id 不變，台帳上的專案文字同步為新名稱並留下變更紀錄。"""
    proj = get_project(conn, project_id)
    new_name = _clean_text(new_name)
    if len(new_name) > 200:
        raise ValidationError(["專案名稱過長（上限 200 字）"])
    if new_name == proj["name"]:
        return proj
    other = conn.execute("SELECT project_id FROM projects WHERE name=?", (new_name,)).fetchone()
    if other:
        label = new_name or "（未命名）"
        raise ValidationError([f"專案名稱「{label}」已存在"])
    when = now or now_str()
    conn.execute("UPDATE projects SET name=? WHERE project_id=?", (new_name, project_id))
    ids = [r[0] for r in conn.execute("SELECT issue_id FROM issues WHERE project_id=?", (project_id,))]
    for iid in ids:
        update_issue(conn, iid, {"project": new_name}, operator, now=when, action="rename-project")
    return get_project(conn, project_id)


def _log(conn, issue_id, when, operator, action, field, old, new):
    conn.execute(
        "INSERT INTO issue_history(issue_id, changed_at, operator, action, field, old_value, new_value)"
        " VALUES (?,?,?,?,?,?,?)", (issue_id, when, operator, action, field, old, new))


def create_issue(conn, data, operator, *, now=None, action="create", is_sample=False,
                 issue_id=None, created_at=None):
    clean, warnings = normalize(data)
    pid = bind_project(conn, data, clean)
    when = now or now_str()
    issue_id = _clean_text(issue_id or data.get("issue_id")) or _next_issue_id(conn)
    if conn.execute("SELECT 1 FROM issues WHERE issue_id=?", (issue_id,)).fetchone():
        raise ValidationError([f"事項編號 {issue_id} 已存在"])
    cols = ["issue_id", *EDITABLE_FIELDS, "project_id", "created_at", "updated_at", "is_sample"]
    vals = [issue_id, *(clean[k] for k in EDITABLE_FIELDS), pid, created_at or when, when, int(is_sample)]
    conn.execute(f"INSERT INTO issues({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    for k in EDITABLE_FIELDS:
        if clean[k]:
            _log(conn, issue_id, when, operator, action, k, "", clean[k])
    if pid:
        _log(conn, issue_id, when, operator, action, "project_id", "", pid)
    return get_issue(conn, issue_id), warnings


PROJECT_MOVE_BLOCKED = (
    "此事項已有逐字稿來源或文件關聯，不能直接改掛到別的專案，以免追溯資料對不上。請另建事項。"
)


def project_move_blocked(conn, issue):
    """已有逐字稿來源或任一文件關聯時，不允許改掛專案。"""
    if not (issue.get("project_id") or ""):
        return False
    if issue.get("transcript_id") or issue.get("segment_ref") or issue.get("draft_id"):
        return True
    return conn.execute(
        "SELECT 1 FROM document_links WHERE issue_id=? LIMIT 1", (issue["issue_id"],)
    ).fetchone() is not None


def _intended_project_id(conn, changes, project_name):
    """算出這次儲存會落到哪個專案，尚不建立新專案。未知的 project_id 會丟 NotFound。"""
    requested = _clean_text((changes or {}).get("project_id"))
    if requested:
        return get_project(conn, requested)["project_id"]
    row = conn.execute("SELECT project_id FROM projects WHERE name=?", (project_name,)).fetchone()
    return row["project_id"] if row else ""


def update_issue(conn, issue_id, changes, operator, *, now=None, action="update"):
    """部分更新：只覆寫 changes 中出現的欄位，逐欄記錄修改前後值。

    project_id 不在可編輯欄位裡。若傳入 project_id，事項改掛到該專案，名稱跟著該專案的現名，
    不會把這次儲存變成重新命名。已有逐字稿來源或文件關聯時，拒絕改掛。
    """
    current = get_issue(conn, issue_id)
    merged = {k: current[k] for k in EDITABLE_FIELDS}
    merged.update({k: v for k, v in changes.items() if k in EDITABLE_FIELDS})
    clean, warnings = normalize(merged)
    current_pid = current.get("project_id") or ""
    intended = _intended_project_id(conn, changes, clean["project"])
    if intended != current_pid and project_move_blocked(conn, current):
        raise ValidationError([PROJECT_MOVE_BLOCKED])
    pid = bind_project(conn, changes, clean)
    diff = [k for k in EDITABLE_FIELDS if clean[k] != current[k]]
    when = now or now_str()
    if pid != current_pid:
        conn.execute("UPDATE issues SET project_id=? WHERE issue_id=?", (pid, issue_id))
        _log(conn, issue_id, when, operator, action, "project_id", current.get("project_id") or "", pid)
    if not diff:
        return get_issue(conn, issue_id), warnings, []
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
    filters = filters or {}
    project_id = filters.get("project_id") or ""
    if project_id:
        clauses.append("project_id=?")
        params.append(project_id)
    for key, value in filters.items():
        if key == "project_id":
            continue
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


def view_counts(conn, today=None, project_id=""):
    filters = {"project_id": project_id} if project_id else None
    return {v: len(list_issues(conn, view=v, today=today, filters=filters)) for v in VIEWS}


def clear_sample(conn, operator):
    ids = [r[0] for r in conn.execute("SELECT issue_id FROM issues WHERE is_sample=1")]
    when = now_str()
    for i in ids:
        _log(conn, i, when, operator, "clear-sample", "*", "範例資料", "")
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM document_links WHERE issue_id IN ({marks})", ids)
    conn.execute("DELETE FROM issues WHERE is_sample=1")
    return len(ids)
