"""本機 HTTP 伺服器（只綁 127.0.0.1）。只用標準函式庫，不呼叫任何外部服務。"""
import json
import mimetypes
import pathlib
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import agenda, backup, csvio, db, notebook, sample, transcripts

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
CLEAR_SAMPLE_PHRASE = "清除範例資料"
CLEAR_ALL_PHRASE = "清空"


class App:
    def __init__(self, cfg, today_fn=db.today_str):
        self.cfg = cfg
        self.today = today_fn
        self.lock = threading.Lock()  # SQLite 單一寫入者，序列化所有寫入

    def conn(self):
        return db.session(self.cfg.db_path)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sdt"

        def log_message(self, fmt, *args):  # 不把請求內容寫進 log
            pass

        # ---------- 回應工具 ----------
        def _send(self, status, body, ctype, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; connect-src 'self'; img-src 'self' data:")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 20 * 1024 * 1024:
                raise db.ValidationError(["上傳內容過大（上限 20MB）"])
            return self.rfile.read(length) if length else b""

        def _json_body(self):
            raw = self._body()
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise db.ValidationError(["請求內容不是有效的 JSON"])

        def _same_origin(self):
            """擋掉其他網站對本機服務發出的寫入請求（CSRF / DNS rebinding）。"""
            port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in allowed:
                return False
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlsplit(origin).netloc not in allowed:
                return False
            return self.headers.get("X-SDT") == "1"

        # ---------- 路由 ----------
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def _dispatch(self, method):
            url = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in url.path.strip("/").split("/") if p]
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                if self.headers.get("Host") not in {f"127.0.0.1:{self.server.server_address[1]}",
                                                    f"localhost:{self.server.server_address[1]}"}:
                    return self._json(403, {"errors": ["拒絕非本機 Host 的請求"]})
                if method != "GET" and not self._same_origin():
                    return self._json(403, {"errors": ["拒絕跨來源請求"]})
                if not parts or parts[0] == "static":
                    return self._static(parts[1:] if parts else ["index.html"]) if method == "GET" else self._json(405, {})
                if parts[0] != "api":
                    return self._json(404, {"errors": ["找不到頁面"]})
                return self._api(method, parts[1:], query)
            except db.ValidationError as e:
                self._json(400, {"errors": e.errors})
            except transcripts.NeedsChoice as e:
                self._json(409, dict(e.plan, errors=[e.plan["message"]], deleted=False))
            except notebook.NotebookUnavailable as e:
                self._json(503, {"errors": e.errors})
            except ValueError as e:  # 備份／還原的已知錯誤，訊息不含路徑
                self._json(400, {"errors": [str(e)]})
            except db.NotFound as e:
                self._json(404, {"errors": [e.message]})
            except Exception:
                # 詳細堆疊只印在本機主控台，不回傳給瀏覽器
                traceback.print_exc()
                self._json(500, {"errors": ["伺服器內部錯誤，請查看啟動視窗訊息"]})

        def _static(self, parts):
            target = (STATIC_DIR / "/".join(parts or ["index.html"])).resolve()
            if STATIC_DIR not in target.parents or not target.is_file():
                return self._json(404, {"errors": ["找不到檔案"]})
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            # Linux 的 mimetypes 常把 .ico 標成 image/vnd.microsoft.icon；分頁圖示要用 image/x-icon。
            if target.suffix.lower() == ".ico":
                ctype = "image/x-icon"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def _api(self, method, parts, query):
            today = app.today()
            route = (method, parts[0] if parts else "")
            if route == ("GET", "meta"):
                return self._json(200, {
                    "operator": app.cfg.operator, "today": today, "fields": db.FIELDS,
                    "issue_types": db.ISSUE_TYPES, "source_statuses": db.SOURCE_STATUSES,
                    "priorities": db.PRIORITIES, "statuses": db.STATUSES,
                    "acceptance_results": db.ACCEPTANCE_RESULTS, "views": db.VIEWS,
                    "clear_sample_phrase": CLEAR_SAMPLE_PHRASE,
                    "clear_all_phrase": CLEAR_ALL_PHRASE,
                    "extraction_method": transcripts.EXTRACTION_METHOD,
                    "extraction_limitation": transcripts.LIMITATION,
                    "draft_statuses": ["草稿", "已確認", "已捨棄"],
                    "link_endorsement": "未主張支持"})
            if route == ("GET", "issues") and len(parts) == 1:
                with app.conn() as conn:
                    filters = {k: query.get(k, "") for k in
                               ("issue_type", "source_status", "priority", "status", "owner", "project")}
                    if query.get("project_id"):
                        filters["project_id"] = query["project_id"]
                    items = db.list_issues(conn, view=query.get("view", "all"), q=query.get("q", ""),
                                           filters=filters, sort=query.get("sort", "due_date"),
                                           order=query.get("order", "asc"), today=today)
                    counts = db.view_counts(conn, today, project_id=query.get("project_id", ""))
                    has_sample = bool(conn.execute("SELECT 1 FROM issues WHERE is_sample=1").fetchone())
                return self._json(200, {"items": items, "counts": counts, "has_sample": has_sample})
            if route == ("GET", "issues") and len(parts) == 3 and parts[2] == "history":
                with app.conn() as conn:
                    db.get_issue(conn, parts[1])
                    return self._json(200, {"items": db.get_history(conn, parts[1])})
            if route == ("GET", "issues") and len(parts) == 2:
                with app.conn() as conn:
                    issue = db.get_issue(conn, parts[1])
                    issue["project_locked"] = db.project_move_blocked(conn, issue)
                    return self._json(200, issue)
            if route == ("POST", "issues") and len(parts) == 1:
                data = self._json_body()
                with app.lock, app.conn() as conn:
                    issue, warnings = db.create_issue(conn, data, app.cfg.operator)
                return self._json(201, {"issue": issue, "warnings": warnings})
            if route == ("POST", "issues") and len(parts) == 3 and parts[2] == "delete":
                data = self._json_body()
                if data.get("confirm") is not True:
                    raise db.ValidationError(["請確認後再刪除事項。這不會刪磁碟上的檔案。"])
                with app.lock, app.conn() as conn:
                    result = db.delete_issue(conn, parts[1], app.cfg.operator)
                return self._json(200, result)
            if route == ("PUT", "issues") and len(parts) == 2:
                data = self._json_body()
                with app.lock, app.conn() as conn:
                    issue, warnings, diff = db.update_issue(conn, parts[1], data, app.cfg.operator)
                return self._json(200, {"issue": issue, "warnings": warnings, "changed": diff})
            if route == ("GET", "export.csv"):
                project_id = query.get("project_id", "")
                with app.conn() as conn:
                    if project_id:
                        db.get_project(conn, project_id)
                    filters = {"project_id": project_id} if project_id else None
                    body = csvio.export_issues(
                        db.list_issues(conn, sort="issue_id", today=today, filters=filters))
                suffix = f"-{project_id}" if project_id else "-all"
                return self._send(200, body, "text/csv; charset=utf-8", {
                    "Content-Disposition": f"attachment; filename=\"issues-{today}{suffix}.csv\""})
            if route == ("GET", "history.csv"):
                with app.conn() as conn:
                    body = csvio.export_history(db.get_history(conn))
                return self._send(200, body, "text/csv; charset=utf-8", {
                    "Content-Disposition": f"attachment; filename=\"history-{today}.csv\""})
            if route == ("POST", "import"):
                data = self._body()
                with app.lock:
                    pre = backup.backup(app.cfg.db_path, app.cfg.backup_dir, label="pre-import")
                    with app.conn() as conn:
                        result = csvio.import_issues(conn, data, app.cfg.operator)
                result["backup"] = pre.name
                return self._json(200, result)
            if route == ("GET", "agenda"):
                project_id = query.get("project_id", "")
                with app.conn() as conn:
                    return self._json(200, {"markdown": agenda.build_agenda(
                        conn, today, project_id=project_id)})
            if route == ("POST", "backup"):
                with app.lock:
                    path = backup.backup(app.cfg.db_path, app.cfg.backup_dir)
                return self._json(200, {"file": path.name})
            if route == ("POST", "sample") and parts[1:] == ["load"]:
                with app.lock, app.conn() as conn:
                    n = sample.load_sample(conn, app.cfg.operator, today)
                return self._json(200, {"loaded": n})
            if route == ("POST", "data") and parts[1:] == ["clear"]:
                if self._json_body().get("confirm") != CLEAR_ALL_PHRASE:
                    raise db.ValidationError([
                        f"請輸入「{CLEAR_ALL_PHRASE}」確認。這會刪除台帳、逐字稿與草稿，不會刪磁碟上的原始 TXT。"])
                with app.lock:
                    pre = backup.backup(app.cfg.db_path, app.cfg.backup_dir, label="pre-clear-all")
                    with app.conn() as conn:
                        counts = db.wipe_all(conn)
                return self._json(200, {
                    "cleared": True, "backup": pre.name, "counts": counts,
                    "message": "已先備份，再清空台帳、逐字稿與草稿。沒有刪磁碟上的原始 TXT，也沒有刪既有備份檔。",
                })
            if route == ("POST", "sample") and parts[1:] == ["clear"]:
                if self._json_body().get("confirm") != CLEAR_SAMPLE_PHRASE:
                    raise db.ValidationError([f"請輸入「{CLEAR_SAMPLE_PHRASE}」確認"])
                with app.lock:
                    pre = backup.backup(app.cfg.db_path, app.cfg.backup_dir, label="pre-clear-sample")
                    with app.conn() as conn:
                        n = db.clear_sample(conn, app.cfg.operator)
                return self._json(200, {"cleared": n, "backup": pre.name})
            if route == ("GET", "projects") and len(parts) == 1:
                with app.conn() as conn:
                    return self._json(200, {"items": db.list_projects(conn)})
            if route == ("POST", "projects") and len(parts) == 1:
                data = self._json_body()
                with app.lock, app.conn() as conn:
                    proj = db.create_project(conn, data.get("name", ""))
                return self._json(201, {"project": proj})
            if route == ("PUT", "projects") and len(parts) == 2:
                data = self._json_body()
                with app.lock, app.conn() as conn:
                    proj = db.rename_project(conn, parts[1], data.get("name", ""), app.cfg.operator)
                return self._json(200, {"project": proj})
            if route == ("POST", "transcripts") and parts[1:] == ["preview"]:
                data = self._json_body()
                with app.conn() as conn:
                    return self._json(200, transcripts.preview_payload(
                        conn, project_id=data.get("project_id", ""), filename=data.get("filename", ""),
                        content_base64=data.get("content_base64", "")))
            if route == ("POST", "transcripts") and parts[1:] == ["import"]:
                data = self._json_body()
                if data.get("confirm") is not True:
                    raise db.ValidationError(["請先預覽，確認後再匯入"])
                with app.lock, app.conn() as conn:
                    result = transcripts.import_transcript(
                        conn, project_id=data.get("project_id", ""), filename=data.get("filename", ""),
                        content_base64=data.get("content_base64", ""), operator=app.cfg.operator)
                return self._json(200, result)
            if route == ("POST", "transcripts") and len(parts) == 3 and parts[2] == "delete":
                data = self._json_body()
                with app.lock, app.conn() as conn:
                    if data.get("preview") is True or data.get("confirm") is not True:
                        plan = transcripts.delete_transcript_preview(conn, parts[1])
                        if data.get("confirm") is not True and data.get("preview") is not True:
                            plan = dict(plan, message="請確認後再刪除逐字稿。" + plan["message"])
                    else:
                        plan = transcripts.delete_transcript(
                            conn, parts[1], app.cfg.operator, ledger=str(data.get("ledger") or ""))
                return self._json(200, plan)
            if route == ("POST", "transcripts") and parts[1:2] != ["preview"] and parts[2:] == ["reparse", "preview"]:
                with app.conn() as conn:
                    return self._json(200, transcripts.reparse_preview(conn, parts[1]))
            if route == ("POST", "transcripts") and parts[2:] == ["reparse"]:
                data = self._json_body()
                if data.get("confirm") is not True:
                    raise db.ValidationError(["請先預覽重新解析，確認後再套用"])
                with app.lock, app.conn() as conn:
                    return self._json(200, transcripts.reparse_apply(conn, parts[1]))
            if route == ("POST", "transcripts") and parts[1:2] != ["preview"] and parts[2:] == ["reextract", "preview"]:
                with app.conn() as conn:
                    return self._json(200, transcripts.reextract_preview(conn, parts[1]))
            if route == ("POST", "transcripts") and parts[2:] == ["reextract"]:
                data = self._json_body()
                if data.get("confirm") is not True:
                    raise db.ValidationError(["請先預覽重新抽取，確認後再套用。套用不會自動入帳"])
                with app.lock, app.conn() as conn:
                    return self._json(200, transcripts.reextract_apply(conn, parts[1]))
            if route == ("GET", "transcripts") and len(parts) == 1:
                with app.conn() as conn:
                    return self._json(200, {"items": transcripts.list_transcripts(conn, query.get("project_id", ""))})
            if route == ("GET", "transcripts") and len(parts) == 3 and parts[2] == "sentences":
                with app.conn() as conn:
                    return self._json(200, transcripts.source_sentences(conn, parts[1]))
            if route == ("POST", "transcripts") and len(parts) == 3 and parts[2] == "manual-drafts":
                data = self._json_body()
                with app.lock, app.conn() as conn:
                    return self._json(201, transcripts.create_manual_draft(conn, parts[1], data))
            if route == ("GET", "transcripts") and len(parts) == 3 and parts[2] == "segments":
                with app.conn() as conn:
                    return self._json(200, {"items": transcripts.list_segments(conn, parts[1])})
            if route == ("GET", "transcripts") and len(parts) == 4 and parts[2] == "segments":
                with app.conn() as conn:
                    return self._json(200, transcripts.get_segment(conn, parts[1], parts[3]))
            if route == ("GET", "transcripts") and len(parts) == 2:
                with app.conn() as conn:
                    item = transcripts.get_transcript(conn, parts[1])
                    item.pop("raw_text", None)
                    return self._json(200, item)
            if route == ("GET", "drafts") and len(parts) == 1:
                with app.conn() as conn:
                    return self._json(200, {"items": transcripts.list_drafts(
                        conn, project_id=query.get("project_id", ""), status=query.get("status", ""),
                        transcript_id=query.get("transcript_id", ""))})
            if route == ("GET", "drafts") and len(parts) == 2:
                with app.conn() as conn:
                    return self._json(200, transcripts.get_draft(conn, parts[1]))
            if route == ("PUT", "drafts") and len(parts) == 2:
                data = self._json_body()
                with app.lock, app.conn() as conn:
                    return self._json(200, {"draft": transcripts.update_draft(conn, parts[1], data)})
            if route == ("POST", "drafts") and len(parts) == 3 and parts[2] == "confirm":
                with app.lock, app.conn() as conn:
                    return self._json(200, transcripts.confirm_draft(conn, parts[1], app.cfg.operator))
            if route == ("POST", "drafts") and len(parts) == 3 and parts[2] == "delete":
                data = self._json_body()
                if data.get("confirm") is not True:
                    raise db.ValidationError(["請確認後再刪除草稿。刪除不會留下捨棄紀錄，也不會刪台帳。"])
                with app.lock, app.conn() as conn:
                    result = transcripts.delete_draft(conn, parts[1])
                return self._json(200, result)
            if route == ("POST", "drafts") and len(parts) == 3 and parts[2] == "discard":
                with app.lock, app.conn() as conn:
                    return self._json(200, transcripts.discard_draft(conn, parts[1]))
            if route == ("GET", "notebook") and parts[1:] == ["status"]:
                return self._json(200, notebook.status(app.cfg.local_notebook_url, app.cfg.local_notebook_db))
            if route == ("GET", "notebook") and parts[1:] == ["notebooks"]:
                return self._json(200, {"items": notebook.list_notebooks(app.cfg.local_notebook_url)})
            if route == ("GET", "notebook") and parts[1:] == ["search"]:
                return self._json(200, notebook.search(
                    app.cfg.local_notebook_url, query.get("notebook", ""), query.get("q", ""),
                    query.get("limit", 20)))
            if route == ("GET", "issues") and len(parts) == 3 and parts[2] == "links":
                with app.conn() as conn:
                    return self._json(200, {"items": notebook.list_links(conn, parts[1]),
                                            "endorsement": "未主張支持"})
            if route == ("POST", "issues") and len(parts) == 3 and parts[2] == "links":
                data = self._json_body()
                with app.lock, app.conn() as conn:
                    result = notebook.create_link(
                        conn, parts[1], data.get("hit") or data,
                        notebook_db=app.cfg.local_notebook_db)
                return self._json(201 if result["created"] else 200, result)
            if route == ("POST", "links") and len(parts) == 3 and parts[2] == "locate":
                with app.conn() as conn:
                    row = conn.execute(
                        "SELECT * FROM document_links WHERE link_id=?", (parts[1],)).fetchone()
                    if row is None:
                        raise db.NotFound(parts[1], "找不到此文件關聯")
                    link = dict(row)
                return self._json(200, notebook.locate_link(app.cfg.local_notebook_db, link))
            if route == ("POST", "shutdown"):
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return None
            return self._json(404, {"errors": ["找不到 API"]})

    return Handler


def make_server(cfg, port=None, today_fn=db.today_str):
    app = App(cfg, today_fn)
    db.connect(cfg.db_path).close()  # 建立資料表
    return ThreadingHTTPServer(("127.0.0.1", cfg.port if port is None else port), make_handler(app))
