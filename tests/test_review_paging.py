"""草稿計數 API、草稿分頁、變更紀錄依專案匯出、409 回應格式。只用虛構資料。"""
import base64
import csv
import http.client
import io
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.parse

from sdt import config, db, server, transcripts


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def lines(n, prefix):
    return "\n".join(f"[00:00:{i:02d}.00–00:00:{i:02d}.50] 決議：採用{prefix}虛構方案{i}。" for i in range(1, n + 1)) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(pathlib.Path(self.tmp.name) / "t.db")
        self.pa = db.create_project(self.conn, "甲案")["project_id"]
        self.pb = db.create_project(self.conn, "乙案")["project_id"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def import_n(self, pid, n, prefix):
        return transcripts.import_transcript(
            self.conn, project_id=pid, filename=f"{prefix}.txt", content_base64=b64(lines(n, prefix)), operator="u")


class TestDraftCounts(Base):
    def test_counts_by_project_without_loading_drafts(self):
        a = self.import_n(self.pa, 3, "甲")
        self.import_n(self.pb, 2, "乙")
        transcripts.discard_draft(self.conn, a["drafts"][0]["draft_id"])
        self.assertEqual(transcripts.draft_counts(self.conn),
                         {"status": "草稿", "total": 4, "by_project": {self.pa: 2, self.pb: 2}})
        self.assertEqual(transcripts.draft_counts(self.conn, status="已捨棄")["by_project"], {self.pa: 1})
        with self.assertRaises(db.ValidationError):
            transcripts.draft_counts(self.conn, status="其他")

    def test_empty(self):
        self.assertEqual(transcripts.draft_counts(self.conn)["total"], 0)


class TestDraftPaging(Base):
    def test_limit_offset_and_total(self):
        imported = self.import_n(self.pa, 7, "甲")
        ids = [d["draft_id"] for d in imported["drafts"]]
        page = transcripts.list_drafts(self.conn, project_id=self.pa, limit=3, offset=0)
        self.assertEqual([d["draft_id"] for d in page], ids[:3])
        page = transcripts.list_drafts(self.conn, project_id=self.pa, limit=3, offset=6)
        self.assertEqual([d["draft_id"] for d in page], ids[6:])
        self.assertEqual(transcripts.list_drafts(self.conn, project_id=self.pa, limit=3, offset=9), [])
        self.assertEqual(len(transcripts.list_drafts(self.conn, project_id=self.pa)), 7)
        self.assertEqual(transcripts.count_drafts(self.conn, project_id=self.pa), 7)
        self.assertEqual(transcripts.count_drafts(self.conn, project_id=self.pa, status="已確認"), 0)


class TestProjectHistory(Base):
    def test_includes_current_deleted_and_moved_out_issues(self):
        stay = db.create_issue(self.conn, {"title": "甲留", "project_id": self.pa}, "u")[0]["issue_id"]
        gone = db.create_issue(self.conn, {"title": "甲刪", "project_id": self.pa}, "u")[0]["issue_id"]
        moved = db.create_issue(self.conn, {"title": "甲移乙", "project_id": self.pa}, "u")[0]["issue_id"]
        other = db.create_issue(self.conn, {"title": "乙", "project_id": self.pb}, "u")[0]["issue_id"]
        db.delete_issue(self.conn, gone, "u")
        db.update_issue(self.conn, moved, {"project_id": self.pb}, "u")
        rows = db.get_project_history(self.conn, self.pa)
        self.assertEqual({r["issue_id"] for r in rows}, {stay, gone, moved})
        self.assertEqual([r["id"] for r in rows], sorted(r["id"] for r in rows))
        self.assertIn(("delete", gone), {(r["action"], r["issue_id"]) for r in rows})
        b_rows = db.get_project_history(self.conn, self.pb)
        self.assertEqual({r["issue_id"] for r in b_rows}, {moved, other})
        moved_all = [r for r in db.get_history(self.conn) if r["issue_id"] == moved]
        self.assertEqual([r for r in rows if r["issue_id"] == moved], moved_all)
        with self.assertRaises(db.NotFound):
            db.get_project_history(self.conn, "PRJ-9999")


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.Config(operator="測試員", port=0, data_dir=pathlib.Path(self.tmp.name))
        self.srv = server.make_server(cfg, port=0, today_fn=lambda: "2026-10-29")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.pa = self.req("POST", "/api/projects", {"name": "甲案"})[1]["project"]["project_id"]
        self.pb = self.req("POST", "/api/projects", {"name": "乙案"})[1]["project"]["project_id"]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=json.dumps(body).encode("utf-8") if body is not None else None,
                  headers={"Host": f"127.0.0.1:{self.port}", "X-SDT": "1"})
        r = c.getresponse()
        payload = r.read()
        headers = dict(r.getheaders())
        c.close()
        if raw:
            return r.status, payload.decode("utf-8-sig"), headers
        return r.status, json.loads(payload or b"{}")

    def import_n(self, pid, n, prefix):
        return self.req("POST", "/api/transcripts/import", {
            "confirm": True, "project_id": pid, "filename": f"{prefix}.txt", "content_base64": b64(lines(n, prefix))})[1]

    def test_count_endpoint(self):
        self.import_n(self.pa, 3, "甲")
        self.import_n(self.pb, 2, "乙")
        s, data = self.req("GET", "/api/drafts/count")
        self.assertEqual(s, 200)
        self.assertEqual((data["total"], data["by_project"]), (5, {self.pa: 3, self.pb: 2}))
        self.assertEqual(self.req("GET", "/api/drafts/count?" + urllib.parse.urlencode({"status": "其他"}))[0], 400)

    def test_paged_list(self):
        self.import_n(self.pa, 5, "甲")
        s, data = self.req("GET", f"/api/drafts?project_id={self.pa}&limit=2&offset=4")
        self.assertEqual(s, 200)
        self.assertEqual((len(data["items"]), data["total"], data["limit"], data["offset"]), (1, 5, 2, 4))
        s, data = self.req("GET", f"/api/drafts?project_id={self.pa}")
        self.assertEqual((len(data["items"]), data["total"], data["limit"]), (5, 5, None))
        for bad in ("limit=0", "limit=501", "limit=x", "offset=-1", "limit=1.5"):
            self.assertEqual(self.req("GET", f"/api/drafts?project_id={self.pa}&{bad}")[0], 400, bad)

    def test_history_csv_by_project(self):
        a = self.req("POST", "/api/issues", {"title": "甲事項", "project_id": self.pa})[1]["issue"]["issue_id"]
        b = self.req("POST", "/api/issues", {"title": "乙事項", "project_id": self.pb})[1]["issue"]["issue_id"]
        s, body, h = self.req("GET", f"/api/history.csv?project_id={self.pa}", raw=True)
        self.assertEqual(s, 200)
        ids = {row[0] for row in list(csv.reader(io.StringIO(body)))[1:]}
        self.assertEqual(ids, {a})
        self.assertIn(f"-{self.pa}.csv", h["Content-Disposition"])
        s, body, h = self.req("GET", "/api/history.csv", raw=True)
        self.assertEqual({row[0] for row in list(csv.reader(io.StringIO(body)))[1:]}, {a, b})
        self.assertIn("-all.csv", h["Content-Disposition"])
        self.assertEqual(self.req("GET", "/api/history.csv?project_id=PRJ-9999", raw=True)[0], 404)

    def test_manual_duplicate_409_has_no_deleted_field(self):
        imported = self.import_n(self.pa, 1, "甲")
        seg = imported["drafts"][0]["segment_id"]
        s, data = self.req("POST", f"/api/transcripts/{imported['transcript_id']}/manual-drafts", {
            "segment_id": seg, "sentence_index": "1", "kind": "決議", "summary": "人工"})
        self.assertEqual(s, 409)
        self.assertTrue(data["needs_confirm"])
        self.assertNotIn("deleted", data)


if __name__ == "__main__":
    unittest.main()
