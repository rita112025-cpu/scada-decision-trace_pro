"""對 local-notebook 的本機介接。

搜尋使用它實際提供的網頁 API（GET /api/notebooks、GET /api/search）。
該 API 沒有依 chunk 取回檔案路徑的端點；若設定了資料庫路徑，定位時改以唯讀方式
讀取 sources.original_path（程式實際寫入的是複製到 data/originals 的路徑）。
連線失敗只回報錯誤，不改台帳。
"""
import json
import pathlib
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from . import db

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_SEARCH_KEYS = (
    "chunk_id", "seq", "page", "line_start", "line_end", "timestamp", "text",
    "source_file", "source_id", "notebook", "snippet", "score",
)


class NotebookUnavailable(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.errors = [message]


def _base(url):
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise NotebookUnavailable("尚未設定 local_notebook_url，台帳功能不受影響。")
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme != "http" or host not in _LOOPBACK:
        raise NotebookUnavailable("local_notebook_url 只允許本機 http://127.0.0.1 或 http://localhost，不會把查詢送到其他位址。")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise NotebookUnavailable("local_notebook_url 不可包含帳號、查詢字串或片段。")
    port = parts.port or 80
    return parts, f"{parts.scheme}://{host}:{port}"


def _get_json(url, path, query=None):
    parts, base = _base(url)
    target = path
    if query:
        target += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    req = urllib.request.Request(
        base + target,
        headers={"Host": parts.netloc or f"{parts.hostname}:{parts.port or 80}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read(2_000_000)
            status = resp.status
    except urllib.error.HTTPError as e:
        detail = e.read(4000).decode("utf-8", errors="replace")
        message = detail
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict) and parsed.get("error"):
                message = str(parsed["error"])
        except json.JSONDecodeError:
            message = f"HTTP {e.code}"
        raise NotebookUnavailable(f"local-notebook 回應錯誤：{message}")
    except (urllib.error.URLError, TimeoutError, OSError):
        host = parts.netloc or parts.hostname
        raise NotebookUnavailable(f"搜尋服務未啟動或連線失敗（{host}）。正式台帳沒有被修改。")
    if status != 200:
        raise NotebookUnavailable(f"local-notebook 回應 HTTP {status}")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NotebookUnavailable("local-notebook 的回應不是 JSON")


def list_notebooks(url):
    data = _get_json(url, "/api/notebooks")
    if not isinstance(data, list):
        raise NotebookUnavailable("local-notebook 的筆記本清單不是預期的陣列")
    books = []
    for item in data:
        if not isinstance(item, dict):
            continue
        books.append({
            "id": item.get("id"),
            "name": item.get("name") or "",
            "tags": item.get("tags") or "",
            "source_count": item.get("source_count"),
            "chunk_count": item.get("chunk_count"),
        })
    return books


def _blank(value):
    return "" if value is None else str(value)


def _hit(row):
    """只保留搜尋 API 實際回傳的欄位，不補版本或章節。"""
    if not isinstance(row, dict):
        return None
    hit = {k: row.get(k) for k in _SEARCH_KEYS if k in row}
    for key in ("source_file", "source_id", "chunk_id"):
        if key not in hit or hit[key] in (None, ""):
            return None
    text = hit.get("text") if isinstance(hit.get("text"), str) else ""
    snippet = hit.get("snippet") if isinstance(hit.get("snippet"), str) else ""
    preview = snippet.strip() or text.strip()
    if len(preview) > 200:
        preview = preview[:200] + "…"
    return {
        "notebook": _blank(hit.get("notebook")),
        "source_id": _blank(hit.get("source_id")),
        "chunk_id": _blank(hit.get("chunk_id")),
        "filename": _blank(hit.get("source_file")),
        "snippet": preview,
        "page": _blank(hit.get("page")) if "page" in hit and hit.get("page") is not None else "",
        "line_start": _blank(hit.get("line_start")) if hit.get("line_start") is not None else "",
        "line_end": _blank(hit.get("line_end")) if hit.get("line_end") is not None else "",
        "timestamp": _blank(hit.get("timestamp")) if hit.get("timestamp") else "",
        "seq": hit.get("seq"),
        "score": hit.get("score"),
    }


def search(url, notebook, query, limit=20):
    notebook = (notebook or "").strip()
    query = (query or "").strip()
    if not notebook:
        raise db.ValidationError(["請選擇 local-notebook 的 notebook"])
    if not query:
        raise db.ValidationError(["請輸入搜尋關鍵字"])
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise db.ValidationError(["limit 必須是整數"])
    if not 1 <= limit <= 50:
        raise db.ValidationError(["limit 必須介於 1 到 50"])
    data = _get_json(url, "/api/search", {"notebook": notebook, "q": query, "limit": str(limit)})
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise NotebookUnavailable("local-notebook 的搜尋結果不是預期格式")
    hits = []
    for row in data["results"]:
        item = _hit(row)
        if item:
            if not item["notebook"]:
                item["notebook"] = notebook
            hits.append(item)
    return {"notebook": data.get("notebook") or notebook, "query": data.get("query") or query, "results": hits}


def status(url, db_path=""):
    if not (url or "").strip():
        return {
            "configured": False,
            "reachable": False,
            "db_configured": bool((db_path or "").strip()),
            "message": "尚未設定 local_notebook_url。事項台帳仍可使用。",
        }
    try:
        books = list_notebooks(url)
    except NotebookUnavailable as e:
        return {
            "configured": True,
            "reachable": False,
            "db_configured": bool((db_path or "").strip()),
            "message": str(e),
        }
    return {
        "configured": True,
        "reachable": True,
        "db_configured": bool((db_path or "").strip()),
        "notebook_count": len(books),
        "message": "已連上 local-notebook 搜尋。",
    }


def _link_row(row):
    item = dict(row)
    item["endorsement"] = item.get("endorsement") or "未主張支持"
    item["supports_decision"] = False
    return item


def list_links(conn, issue_id):
    issue = db.get_issue(conn, issue_id)
    rows = conn.execute(
        "SELECT * FROM document_links WHERE issue_id=? AND project_id=? ORDER BY link_id",
        (issue_id, issue["project_id"]))
    return [_link_row(r) for r in rows]


def create_link(conn, issue_id, hit, *, now=None):
    """儲存使用者選取的搜尋結果。不複製整份文件，也不把關聯標成「文件支持此決議」。"""
    issue = db.get_issue(conn, issue_id)
    if not isinstance(hit, dict):
        raise db.ValidationError(["請選擇一筆搜尋結果"])
    # 用戶端若夾帶版本、章節或「支持」旗標，一律不採用。
    filename = db._clean_text(hit.get("filename") or hit.get("source_file"))
    notebook = db._clean_text(hit.get("notebook"))
    source_id = db._clean_text(hit.get("source_id"))
    chunk_id = db._clean_text(hit.get("chunk_id"))
    if not filename or not notebook or not source_id or not chunk_id:
        raise db.ValidationError(["搜尋結果缺少檔名、notebook、source_id 或 chunk_id，無法建立關聯"])
    snippet = db._clean_text(hit.get("snippet") or "")
    if not snippet:
        text = hit.get("text") if isinstance(hit.get("text"), str) else ""
        snippet = text.strip()
    if len(snippet) > 200:
        snippet = snippet[:200] + "…"
    page = "" if hit.get("page") in (None, "") else str(hit.get("page"))
    line_start = "" if hit.get("line_start") in (None, "") else str(hit.get("line_start"))
    line_end = "" if hit.get("line_end") in (None, "") else str(hit.get("line_end"))
    timestamp = "" if not hit.get("timestamp") else str(hit.get("timestamp"))
    existing = conn.execute(
        "SELECT * FROM document_links WHERE issue_id=? AND chunk_id=?",
        (issue_id, chunk_id)).fetchone()
    if existing:
        return {"created": False, "link": _link_row(existing),
                "message": "此事項已關聯這個段落，未重複建立。"}
    when = now or db.now_str()
    link_id = db.next_public_id(conn, "document_links", "link_id", "LNK-")
    conn.execute(
        """INSERT INTO document_links(
            link_id, project_id, issue_id, notebook, source_id, chunk_id, filename, snippet,
            page, line_start, line_end, timestamp, endorsement, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (link_id, issue["project_id"], issue_id, notebook, source_id, chunk_id, filename, snippet,
         page, line_start, line_end, timestamp, "未主張支持", when))
    return {"created": True, "link": _link_row(conn.execute(
        "SELECT * FROM document_links WHERE link_id=?", (link_id,)).fetchone()),
            "message": "已建立文件關聯。這只表示選取了文件出處，不代表文件支持此決議。"}


def locate_link(db_path, link):
    """檢查關聯是否還對得到 local-notebook 裡的段落與複製檔。"""
    base = {
        "link_id": link["link_id"],
        "filename": link["filename"],
        "notebook": link["notebook"],
        "source_id": link["source_id"],
        "chunk_id": link["chunk_id"],
        "page": link["page"],
        "line_start": link["line_start"],
        "line_end": link["line_end"],
        "timestamp": link["timestamp"],
        "endorsement": "未主張支持",
        "supports_decision": False,
    }
    path_text = (db_path or "").strip()
    if not path_text:
        return dict(base, status="無法驗證", message=(
            "未設定 local_notebook_db。網頁搜尋沒有依段落編號取回檔案路徑的介面，"
            "所以無法從這裡開啟檔案；已保存的關聯仍然在。"
        ))
    path = pathlib.Path(path_text)
    if not path.is_file():
        return dict(base, status="無法驗證", message="找不到 local-notebook 資料庫檔，已保存的關聯仍然在。")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.DatabaseError:
        return dict(base, status="無法驗證", message="無法讀取 local-notebook 資料庫，已保存的關聯仍然在。")
    try:
        try:
            source_id = int(link["source_id"])
            chunk_id = int(link["chunk_id"])
        except (TypeError, ValueError):
            return dict(base, status="來源失效", message="關聯裡的來源編號無法對應到 local-notebook。")
        row = conn.execute(
            """SELECT c.id AS chunk_id, c.page, c.line_start, c.line_end, c.timestamp,
                      s.id AS source_id, s.filename, s.original_path, s.file_hash, n.name AS notebook
               FROM chunks c
               JOIN sources s ON s.id = c.source_id
               JOIN notebooks n ON n.id = c.notebook_id
               WHERE c.id=? AND s.id=?""",
            (chunk_id, source_id)).fetchone()
    except sqlite3.DatabaseError:
        return dict(base, status="無法驗證", message="local-notebook 資料庫沒有預期的段落資料表，已保存的關聯仍然在。")
    finally:
        conn.close()
    if row is None:
        return dict(base, status="來源失效", message="local-notebook 已找不到此段落或來源檔。關聯紀錄仍保留，但無法開啟。")
    original = row["original_path"] or ""
    exists = bool(original) and pathlib.Path(original).is_file()
    located = dict(base)
    located.update({
        "filename": row["filename"] or link["filename"],
        "notebook": row["notebook"] or link["notebook"],
        "page": "" if row["page"] is None else str(row["page"]),
        "line_start": "" if row["line_start"] is None else str(row["line_start"]),
        "line_end": "" if row["line_end"] is None else str(row["line_end"]),
        "timestamp": row["timestamp"] or "",
        "file_hash": row["file_hash"] or "",
        "original_path": original,
    })
    if not exists:
        located["status"] = "來源失效"
        located["message"] = "資料庫仍有此段落，但複製檔已不在 sources.original_path 記錄的路徑。關聯紀錄仍保留。"
        return located
    located["status"] = "可定位"
    located["message"] = (
        "可定位。路徑是 local-notebook 複製到 data/originals 的檔案，不是台帳裡的副本。"
        "頁碼、行號、時間戳只顯示資料庫裡實際有的欄位。"
    )
    return located
