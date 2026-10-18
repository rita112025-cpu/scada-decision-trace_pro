"""HTTP 層測試：在隨機埠啟動真實伺服器，經 127.0.0.1 呼叫 API。"""
import base64
import http.client
import json
import pathlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse

from sdt import config, server

FICTIONAL_TXT = (
    "[00:00:01.00\u201300:00:03.00] 決議：虛構刪除測試採用備援盤。\n"
    "[00:00:04.00\u201300:00:06.00] 待辦：請林示範確認虛構點位。\n"
)

TODAY = "2026-10-14"


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.Config(operator="測試員", port=0, data_dir=pathlib.Path(self.tmp.name))
        self.srv = server.make_server(cfg, port=0, today_fn=lambda: TODAY)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}", "X-SDT": "1"}
        h.update(headers or {})
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode("utf-8")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, (payload if raw else json.loads(payload or b"{}"))

    def test_index_served(self):
        status, body = self.req("GET", "/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("決議".encode("utf-8"), body)
        self.assertIn(b'<link rel="icon" href="/static/app.ico"', body)

    def test_favicon_is_icon(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/static/app.ico", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        payload = r.read()
        ctype = r.getheader("Content-Type")
        c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(ctype, "image/x-icon")
        self.assertTrue(payload.startswith(b"\x00\x00\x01\x00"))

    def test_create_update_history_flow(self):
        s, r = self.req("POST", "/api/issues", {"title": "HTTP 建立", "source_status": "已確認"})
        self.assertEqual(s, 201)
        self.assertEqual(r["issue"]["source_status"], "缺乏證據")
        iid = r["issue"]["issue_id"]
        s, r = self.req("PUT", f"/api/issues/{iid}", {"status": "處理中"})
        self.assertEqual((s, r["changed"]), (200, ["status"]))
        s, r = self.req("GET", f"/api/issues/{iid}/history")
        last = r["items"][-1]
        self.assertEqual((last["operator"], last["old_value"], last["new_value"]), ("測試員", "待處理", "處理中"))

    def test_linked_issue_cannot_change_project(self):
        _, origin = self.req("POST", "/api/projects", {"name": "合成專案甲"})
        _, other = self.req("POST", "/api/projects", {"name": "合成專案乙"})
        _, created = self.req("POST", "/api/issues", {
            "title": "有文件關聯", "project_id": origin["project"]["project_id"],
        })
        issue_id = created["issue"]["issue_id"]
        status, linked = self.req("POST", f"/api/issues/{issue_id}/links", {"hit": {
            "notebook": "合成筆記", "source_id": "1", "chunk_id": "2",
            "filename": "engineering-note.txt", "snippet": "通訊模組預留",
        }})
        self.assertEqual(status, 201)
        status, issue = self.req("GET", f"/api/issues/{issue_id}")
        self.assertTrue(issue["project_locked"])
        status, denied = self.req("PUT", f"/api/issues/{issue_id}", {
            "project_id": other["project"]["project_id"], "title": "有文件關聯",
        })
        self.assertEqual(status, 400)
        self.assertTrue(any("不能直接改掛" in err for err in denied["errors"]))
        status, links = self.req("GET", f"/api/issues/{issue_id}/links")
        self.assertEqual(len(links["items"]), 1)
        self.assertEqual(links["items"][0]["filename"], "engineering-note.txt")
        status, issue = self.req("GET", f"/api/issues/{issue_id}")
        self.assertEqual(issue["project_id"], origin["project"]["project_id"])
        _, plain = self.req("POST", "/api/issues", {
            "title": "沒有追溯", "project_id": origin["project"]["project_id"],
        })
        status, unlocked = self.req("GET", f"/api/issues/{plain['issue']['issue_id']}")
        self.assertFalse(unlocked["project_locked"])

    def test_validation_error_is_400(self):
        s, r = self.req("POST", "/api/issues", {"title": ""})
        self.assertEqual(s, 400)
        self.assertTrue(r["errors"])

    def test_views_and_counts(self):
        self.req("POST", "/api/issues", {"title": "逾期", "due_date": "2026-01-01"})
        self.req("POST", "/api/issues", {"title": "未到期", "due_date": "2027-01-01"})
        s, r = self.req("GET", "/api/issues?view=overdue")
        self.assertEqual([i["title"] for i in r["items"]], ["逾期"])
        self.assertEqual(r["counts"]["open"], 2)
        s, r = self.req("GET", "/api/issues?q=" + urllib.parse.quote("未到期"))
        self.assertEqual([i["title"] for i in r["items"]], ["未到期"])
        s, r = self.req("GET", "/api/issues?view=open&sort=due_date&order=desc")
        self.assertEqual([i["title"] for i in r["items"]], ["未到期", "逾期"])

    def test_cross_origin_write_rejected(self):
        s, _ = self.req("POST", "/api/issues", {"title": "x"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(s, 403)
        s, _ = self.req("POST", "/api/issues", {"title": "x"}, headers={"X-SDT": ""})
        self.assertEqual(s, 403)
        s, _ = self.req("GET", "/api/meta", headers={"Host": "evil.example"})
        self.assertEqual(s, 403)

    def test_csv_export_import_roundtrip_via_http(self):
        self.req("POST", "/api/issues", {"title": "中文,逗號", "description": "a\nb", "due_date": "2026-05-06"})
        s, data = self.req("GET", "/api/export.csv", raw=True)
        self.assertEqual(s, 200)
        s, r = self.req("POST", "/api/import", data, headers={"Content-Type": "text/csv"})
        self.assertEqual((s, r["unchanged"]), (200, 1))
        self.assertTrue(r["backup"].startswith("sdt-pre-import"))

    def test_clear_sample_requires_phrase(self):
        self.req("POST", "/api/sample/load", {})
        s, _ = self.req("POST", "/api/sample/clear", {"confirm": "yes"})
        self.assertEqual(s, 400)
        s, r = self.req("GET", "/api/issues")
        self.assertTrue(r["has_sample"])
        s, r = self.req("POST", "/api/sample/clear", {"confirm": server.CLEAR_SAMPLE_PHRASE})
        self.assertEqual(s, 200)
        self.assertGreater(r["cleared"], 0)
        s, r = self.req("GET", "/api/issues")
        self.assertEqual((r["items"], r["has_sample"]), ([], False))

    def test_agenda_endpoint(self):
        self.req("POST", "/api/issues", {"title": "高優先", "priority": "高"})
        s, r = self.req("GET", "/api/agenda")
        self.assertEqual(s, 200)
        self.assertIn("高優先", r["markdown"])

    def test_static_path_traversal_blocked(self):
        s, _ = self.req("GET", "/static/../server.py", raw=True)
        self.assertEqual(s, 404)

    def _import_fictional(self):
        source = pathlib.Path(self.tmp.name) / "do-not-touch.txt"
        source.write_text("虛構原始檔不該被刪\n", encoding="utf-8")
        status, proj = self.req("POST", "/api/projects", {"name": "虛構刪除專案"})
        self.assertEqual(status, 201)
        status, imported = self.req("POST", "/api/transcripts/import", {
            "project_id": proj["project"]["project_id"],
            "filename": "fictional-delete.txt",
            "content_base64": base64.b64encode(FICTIONAL_TXT.encode("utf-8")).decode("ascii"),
            "confirm": True,
        })
        self.assertEqual(status, 200, imported)
        self.assertTrue(imported["created"])
        return proj["project"]["project_id"], imported["transcript_id"], source

    def _drafts(self, project_id, transcript_id):
        status, data = self.req(
            "GET", "/api/drafts?" + urllib.parse.urlencode({
                "project_id": project_id, "transcript_id": transcript_id, "status": "",
            }))
        self.assertEqual(status, 200)
        return data["items"]

    def test_delete_transcript_removes_only_unconfirmed_rows(self):
        project_id, tid, source = self._import_fictional()
        drafts = self._drafts(project_id, tid)
        self.assertGreaterEqual(len(drafts), 2)
        kept = source.read_text(encoding="utf-8")
        status, preview = self.req("POST", f"/api/transcripts/{tid}/delete", {"preview": True})
        self.assertEqual(status, 200)
        self.assertFalse(preview["deleted"])
        self.assertEqual(preview["confirmed_count"], 0)
        self.assertGreater(preview["segment_count"], 0)
        status, held = self.req("POST", f"/api/transcripts/{tid}/delete", {})
        self.assertEqual(status, 200)
        self.assertFalse(held["deleted"])
        status, blocked = self.req(
            "POST", f"/api/transcripts/{tid}/delete", {"confirm": True}, headers={"X-SDT": ""})
        self.assertEqual(status, 403)
        self.assertTrue(blocked["errors"])
        self.assertEqual(source.read_text(encoding="utf-8"), kept)
        status, gone = self.req("POST", f"/api/transcripts/{tid}/delete", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(gone["deleted"])
        self.assertIn("沒有刪除台帳", gone["message"])
        status, missing = self.req("GET", f"/api/transcripts/{tid}")
        self.assertEqual(status, 404)
        self.assertEqual(self._drafts(project_id, tid), [])
        status, issues = self.req("GET", "/api/issues")
        self.assertEqual(issues["items"], [])
        self.assertEqual(source.read_text(encoding="utf-8"), kept)

    def test_confirmed_transcript_requires_explicit_detach(self):
        project_id, tid, source = self._import_fictional()
        drafts = self._drafts(project_id, tid)
        decision = next(d for d in drafts if d["kind"] == "決議")
        other = next(d for d in drafts if d["draft_id"] != decision["draft_id"])
        status, confirmed = self.req("POST", f"/api/drafts/{decision['draft_id']}/confirm", {})
        self.assertEqual(status, 200)
        issue_id = confirmed["issue"]["issue_id"]
        quote = confirmed["issue"]["source_quote"]
        status, denied = self.req("POST", f"/api/transcripts/{tid}/delete", {"confirm": True})
        self.assertEqual(status, 409)
        self.assertFalse(denied["deleted"])
        self.assertEqual(denied["issue_ids"], [issue_id])
        self.assertGreaterEqual(denied["confirmed_count"], 1)
        status, issue = self.req("GET", f"/api/issues/{issue_id}")
        self.assertEqual(status, 200)
        self.assertEqual(issue["transcript_id"], tid)
        status, blocked = self.req(
            "POST", f"/api/transcripts/{tid}/delete", {"confirm": True, "ledger": "block"})
        self.assertEqual(status, 200)
        self.assertFalse(blocked["deleted"])
        self.assertEqual(self.req("GET", f"/api/transcripts/{tid}")[0], 200)
        status, detached = self.req(
            "POST", f"/api/transcripts/{tid}/delete", {"confirm": True, "ledger": "detach"})
        self.assertEqual(status, 200, detached)
        self.assertTrue(detached["deleted"])
        self.assertEqual(self.req("GET", f"/api/transcripts/{tid}")[0], 404)
        self.assertEqual(self._drafts(project_id, tid), [])
        status, issue = self.req("GET", f"/api/issues/{issue_id}")
        self.assertEqual(issue["transcript_id"], "")
        self.assertEqual(issue["segment_ref"], "")
        self.assertEqual(issue["draft_id"], "")
        self.assertEqual(issue["source_quote"], quote)
        self.assertFalse(issue["project_locked"])
        self.assertEqual(source.read_text(encoding="utf-8"), "虛構原始檔不該被刪\n")
        self.assertNotEqual(other["draft_id"], decision["draft_id"])

    def test_delete_draft_does_not_keep_discard_row(self):
        project_id, tid, source = self._import_fictional()
        drafts = self._drafts(project_id, tid)
        first, second = drafts[0], drafts[1]
        status, discarded = self.req("POST", f"/api/drafts/{first['draft_id']}/discard", {})
        self.assertEqual(status, 200)
        self.assertEqual(discarded["draft"]["review_status"], "已捨棄")
        status, denied = self.req("POST", f"/api/drafts/{second['draft_id']}/delete", {})
        self.assertEqual(status, 400)
        self.assertEqual(
            self.req("GET", f"/api/drafts/{second['draft_id']}")[1]["review_status"], "草稿")
        status, blocked = self.req(
            "POST", f"/api/drafts/{second['draft_id']}/delete", {"confirm": True}, headers={"X-SDT": ""})
        self.assertEqual(status, 403)
        self.assertTrue(blocked["errors"])
        status, removed = self.req(
            "POST", f"/api/drafts/{second['draft_id']}/delete", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(removed["deleted"])
        self.assertIn("沒有留下捨棄紀錄", removed["message"])
        self.assertEqual(self.req("GET", f"/api/drafts/{second['draft_id']}")[0], 404)
        self.assertEqual(self.req("GET", f"/api/drafts/{first['draft_id']}")[1]["review_status"], "已捨棄")
        status, removed_discarded = self.req(
            "POST", f"/api/drafts/{first['draft_id']}/delete", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.req("GET", f"/api/drafts/{first['draft_id']}")[0], 404)
        status, issues = self.req("GET", "/api/issues")
        self.assertEqual(issues["items"], [])
        self.assertEqual(source.read_text(encoding="utf-8"), "虛構原始檔不該被刪\n")
        status, confirmed = self.req("POST", "/api/transcripts/import", {
            "project_id": project_id,
            "filename": "fictional-delete-again.txt",
            "content_base64": base64.b64encode(FICTIONAL_TXT.replace("備援盤", "另一盤").encode("utf-8")).decode("ascii"),
            "confirm": True,
        })
        self.assertEqual(status, 200)
        again = self._drafts(project_id, confirmed["transcript_id"])
        posted = self.req("POST", f"/api/drafts/{again[0]['draft_id']}/confirm", {})[1]
        status, refused = self.req(
            "POST", f"/api/drafts/{again[0]['draft_id']}/delete", {"confirm": True})
        self.assertEqual(status, 400)
        self.assertTrue(any("不會默默刪除" in err for err in refused["errors"]))
        self.assertEqual(self.req("GET", f"/api/issues/{posted['issue']['issue_id']}")[0], 200)
        self.assertEqual(
            self.req("GET", f"/api/drafts/{again[0]['draft_id']}")[1]["review_status"], "已確認")

    def test_delete_issue_stays_in_history_and_reverts_draft(self):
        project_id, tid, source = self._import_fictional()
        draft = self._drafts(project_id, tid)[0]
        status, confirmed = self.req("POST", f"/api/drafts/{draft['draft_id']}/confirm", {})
        issue_id = confirmed["issue"]["issue_id"]
        status, linked = self.req("POST", f"/api/issues/{issue_id}/links", {"hit": {
            "notebook": "虛構筆記", "source_id": "9", "chunk_id": "9",
            "filename": "fictional-note.txt", "snippet": "只是連結",
        }})
        self.assertEqual(status, 201)
        status, early = self.req("POST", f"/api/issues/{issue_id}/delete", {})
        self.assertEqual(status, 400)
        self.assertEqual(self.req("GET", f"/api/issues/{issue_id}")[0], 200)
        status, blocked = self.req(
            "POST", f"/api/issues/{issue_id}/delete", {"confirm": True}, headers={"X-SDT": ""})
        self.assertEqual(status, 403)
        self.assertTrue(blocked["errors"])
        status, removed = self.req("POST", f"/api/issues/{issue_id}/delete", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(removed["draft_reverted"])
        self.assertEqual(removed["links_removed"], 1)
        self.assertEqual(self.req("GET", f"/api/issues/{issue_id}")[0], 404)
        self.assertEqual(self.req("GET", f"/api/issues/{issue_id}/links")[0], 404)
        self.assertEqual(self.req("GET", f"/api/drafts/{draft['draft_id']}")[1]["review_status"], "草稿")
        self.assertEqual(self.req("GET", f"/api/drafts/{draft['draft_id']}")[1]["issue_id"], "")
        status, csv_body = self.req("GET", "/api/history.csv", raw=True)
        text = csv_body.decode("utf-8-sig")
        self.assertIn(issue_id, text)
        self.assertIn("delete", text)
        self.assertEqual(source.read_text(encoding="utf-8"), "虛構原始檔不該被刪\n")

    def test_wipe_all_backs_up_first_and_leaves_source_file(self):
        project_id, tid, source = self._import_fictional()
        self.req("POST", f"/api/drafts/{self._drafts(project_id, tid)[0]['draft_id']}/confirm", {})
        before = source.read_text(encoding="utf-8")
        status, denied = self.req("POST", "/api/data/clear", {"confirm": "不是"})
        self.assertEqual(status, 400)
        self.assertEqual(self.req("GET", f"/api/transcripts/{tid}")[0], 200)
        self.assertFalse(list(pathlib.Path(self.tmp.name, "backups").glob("sdt-pre-clear-all-*")))
        status, blocked = self.req("POST", "/api/data/clear", {"confirm": server.CLEAR_ALL_PHRASE}, headers={"X-SDT": ""})
        self.assertEqual(status, 403)
        self.assertTrue(blocked["errors"])
        self.assertEqual(self.req("GET", "/api/issues")[1]["counts"]["all"], 1)
        status, cleared = self.req("POST", "/api/data/clear", {"confirm": server.CLEAR_ALL_PHRASE})
        self.assertEqual(status, 200)
        self.assertTrue(cleared["backup"].startswith("sdt-pre-clear-all-"))
        self.assertEqual(self.req("GET", "/api/issues")[1]["items"], [])
        self.assertEqual(self.req("GET", "/api/projects")[1]["items"], [])
        self.assertEqual(self.req("GET", f"/api/transcripts/{tid}")[0], 404)
        self.assertEqual(source.read_text(encoding="utf-8"), before)
        backup = pathlib.Path(self.tmp.name, "backups", cleared["backup"])
        self.assertTrue(backup.is_file())
        conn = sqlite3.connect(backup)
        try:
            stored = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
            transcripts = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(stored, 1)
        self.assertEqual(transcripts, 1)
        self.assertTrue(backup.is_file())


if __name__ == "__main__":
    unittest.main()
