"""對真正的 local-notebook 網頁搜尋做端到端測試。

路徑取自環境變數 SDT_NOTEBOOK_ROOT，未設定時為 /tmp/source-repos/local-notebook。
缺少該目錄、starlette／uvicorn／pypdf，或 SQLite 沒有 FTS5 trigram 時：
一般執行會 skip；SDT_INTEGRATION_STRICT=1 時改為失敗，不略過。
套件版本見 requirements-integration.txt。這個檔案不把 mock 搜尋當成介接成功。
"""
import base64
import json
import os
import pathlib
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTEBOOK_ROOT = pathlib.Path(os.environ.get("SDT_NOTEBOOK_ROOT", "/tmp/source-repos/local-notebook"))
EXAMPLE = ROOT / "examples" / "synthetic" / "engineering-note.txt"
TRANSCRIPT = ROOT / "examples" / "synthetic" / "meeting.json"


def _strict():
    return os.environ.get("SDT_INTEGRATION_STRICT", "").strip() == "1"


def _deps_ready():
    if not NOTEBOOK_ROOT.is_dir():
        return f"找不到 local-notebook（SDT_NOTEBOOK_ROOT={NOTEBOOK_ROOT}）"
    try:
        import pypdf  # noqa: F401
        import starlette  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as e:
        return f"缺少套件：{e.name}（見 requirements-integration.txt）"
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='trigram')")
    except sqlite3.DatabaseError as e:
        return f"SQLite 沒有 FTS5 trigram：{e}"
    finally:
        conn.close()
    return ""


class TestLiveNotebook(unittest.TestCase):
    def setUp(self):
        reason = _deps_ready()
        if reason:
            if _strict():
                self.fail("整合驗收模式缺少必要環境，不得略過：" + reason)
            self.skipTest(reason)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.nb_db = self.dir / "notebook.db"
        self.old_env = os.environ.get("LOCAL_NOTEBOOK_DB")
        os.environ["LOCAL_NOTEBOOK_DB"] = str(self.nb_db)
        if str(NOTEBOOK_ROOT) not in sys.path:
            sys.path.insert(0, str(NOTEBOOK_ROOT))
        from src import db as nbdb
        from src import ingest
        from src.ui.app import create_app
        import uvicorn

        conn = nbdb.connect(self.nb_db)
        notebook_id = nbdb.get_or_create_notebook(conn, "合成筆記", "synthetic")
        ingest.ingest_file(conn, notebook_id, EXAMPLE)
        conn.close()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.nb_port = sock.getsockname()[1]
        self.nb_server = uvicorn.Server(uvicorn.Config(create_app(port=self.nb_port), log_level="warning"))
        self.nb_thread = threading.Thread(target=lambda: self.nb_server.run(sockets=[sock]), daemon=True)
        self.nb_thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.nb_server.started:
            time.sleep(0.05)
        if not self.nb_server.started:
            self.fail("local-notebook UI 沒有在時限內啟動")

        from sdt import config, server
        (self.dir / "sdt").mkdir()
        cfg = config.Config(
            operator="測試員", port=0, data_dir=self.dir / "sdt",
            local_notebook_url=f"http://127.0.0.1:{self.nb_port}",
            local_notebook_db=str(self.nb_db),
        )
        self.sdt = server.make_server(cfg, port=0, today_fn=lambda: "2026-10-16")
        self.sdt_port = self.sdt.server_address[1]
        self.sdt_thread = threading.Thread(target=self.sdt.serve_forever, daemon=True)
        self.sdt_thread.start()

    def tearDown(self):
        if getattr(self, "sdt", None):
            self.sdt.shutdown()
            self.sdt.server_close()
        if getattr(self, "nb_server", None):
            self.nb_server.should_exit = True
        if self.old_env is None:
            os.environ.pop("LOCAL_NOTEBOOK_DB", None)
        else:
            os.environ["LOCAL_NOTEBOOK_DB"] = self.old_env
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    def sdt_req(self, method, path, body=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.sdt_port, timeout=5)
        data = None if body is None else json.dumps(body).encode("utf-8")
        c.request(method, path, body=data, headers={
            "Host": f"127.0.0.1:{self.sdt_port}", "X-SDT": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        payload = json.loads(r.read() or b"{}")
        c.close()
        return r.status, payload

    def test_search_link_restart_and_missing_source(self):
        # 先直接打 local-notebook，確認不是本專案自己的假伺服器。
        with urllib.request.urlopen(f"http://127.0.0.1:{self.nb_port}/api/notebooks", timeout=3) as resp:
            books = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(books[0]["name"], "合成筆記")

        s, proj = self.sdt_req("POST", "/api/projects", {"name": "合成專案甲"})
        self.assertEqual(s, 201)
        payload = {
            "project_id": proj["project"]["project_id"],
            "filename": "meeting.json",
            "content_base64": base64.b64encode(TRANSCRIPT.read_bytes()).decode("ascii"),
            "confirm": True,
        }
        s, imported = self.sdt_req("POST", "/api/transcripts/import", payload)
        draft = next(d for d in imported["drafts"] if d["kind"] == "決議")
        s, confirmed = self.sdt_req("POST", f"/api/drafts/{draft['draft_id']}/confirm", {})
        issue_id = confirmed["issue"]["issue_id"]

        query = urllib.parse.urlencode({"notebook": "合成筆記", "q": "通訊模組預留"})
        s, found = self.sdt_req("GET", "/api/notebook/search?" + query)
        self.assertEqual(s, 200, found)
        self.assertTrue(found["results"])
        hit = found["results"][0]
        self.assertEqual(hit["filename"], "engineering-note.txt")
        self.assertNotIn("version", hit)
        self.assertNotIn("chapter", hit)
        s, linked = self.sdt_req("POST", f"/api/issues/{issue_id}/links", {"hit": hit})
        self.assertTrue(linked["created"])
        self.assertEqual(linked["link"]["endorsement"], "未主張支持")

        s, located = self.sdt_req("POST", f"/api/links/{linked['link']['link_id']}/locate", {})
        self.assertEqual(located["status"], "可定位", located)
        self.assertTrue(located["original_path"])
        self.assertTrue(pathlib.Path(located["original_path"]).is_file())

        # 重新建立本服務，模擬重新啟動後仍查得到。
        self.sdt.shutdown()
        self.sdt.server_close()
        from sdt import config, server
        cfg = config.Config(
            operator="測試員", port=0, data_dir=self.dir / "sdt",
            local_notebook_url=f"http://127.0.0.1:{self.nb_port}",
            local_notebook_db=str(self.nb_db),
        )
        self.sdt = server.make_server(cfg, port=0, today_fn=lambda: "2026-10-16")
        self.sdt_port = self.sdt.server_address[1]
        self.sdt_thread = threading.Thread(target=self.sdt.serve_forever, daemon=True)
        self.sdt_thread.start()
        s, issue = self.sdt_req("GET", f"/api/issues/{issue_id}")
        self.assertEqual(issue["source_quote"], draft["source_quote"])
        s, links = self.sdt_req("GET", f"/api/issues/{issue_id}/links")
        self.assertEqual(len(links["items"]), 1)
        self.assertEqual(links["items"][0]["filename"], "engineering-note.txt")

        pathlib.Path(located["original_path"]).unlink()
        s, missing = self.sdt_req("POST", f"/api/links/{linked['link']['link_id']}/locate", {})
        self.assertEqual(missing["status"], "來源失效")
        s, issue = self.sdt_req("GET", f"/api/issues/{issue_id}")
        self.assertEqual(issue["title"], confirmed["issue"]["title"])
        self.assertEqual(issue["source_quote"], draft["source_quote"])

        self.nb_server.should_exit = True
        stopped = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.nb_port}/api/notebooks", timeout=0.3) as resp:
                    resp.read()
            except Exception:
                stopped = True
                break
            time.sleep(0.05)
        self.assertTrue(stopped, "local-notebook 沒有在時限內停止")
        s, offline = self.sdt_req("GET", "/api/notebook/search?" + query)
        self.assertEqual(s, 503, offline)
        s, issue = self.sdt_req("GET", f"/api/issues/{issue_id}")
        self.assertEqual(issue["issue_id"], issue_id)


if __name__ == "__main__":
    unittest.main()
