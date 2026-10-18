"""Transcript deletion keeps ledger evidence and never touches original files."""
import base64
import http.client
import json
import pathlib
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

from sdt import config, db, server, transcripts


class TestTranscriptDeletion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.cfg = config.Config("測試員", 0, self.root)
        self.srv = server.make_server(self.cfg, port=0)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        _, result = self.req("POST", "/api/projects", {"name": "刪除測試甲"})
        self.project_id = result["project"]["project_id"]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"X-SDT": "1", "Content-Type": "application/json"}
        request_headers.update(headers or {})
        conn.request(method, path, body=None if body is None else json.dumps(body).encode("utf-8"),
                     headers=request_headers)
        response = conn.getresponse()
        status, payload = response.status, json.loads(response.read())
        conn.close()
        return status, payload

    def import_file(self, project_id=None):
        original = self.root / "meeting.txt"
        original.write_text("決議：採用新版圖紙。\n請林示範負責更新端子對照。\n決議：保留盤面空間。", encoding="utf-8")
        payload = {"project_id": project_id or self.project_id, "filename": original.name,
                   "content_base64": base64.b64encode(original.read_bytes()).decode("ascii"), "confirm": True}
        status, result = self.req("POST", "/api/transcripts/import", payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["drafts"]), 3)
        return result

    def preview(self, transcript_id):
        status, result = self.req("GET", f"/api/transcripts/{transcript_id}/delete-preview")
        self.assertEqual(status, 200)
        return result

    def delete(self, transcript_id, preview):
        return self.req("DELETE", f"/api/transcripts/{transcript_id}",
                        {"confirm": True, "preview_token": preview["preview_token"]})

    def test_delete_preserves_issues_evidence_links_other_projects_and_original(self):
        imported = self.import_file()
        tid = imported["transcript_id"]
        _, confirmed = self.req("POST", f"/api/drafts/{imported['drafts'][0]['draft_id']}/confirm", {})
        before = confirmed["issue"]
        iid = before["issue_id"]
        self.req("POST", f"/api/drafts/{imported['drafts'][1]['draft_id']}/discard", {})
        status, _ = self.req("POST", f"/api/issues/{iid}/links", {"hit": {
            "notebook": "合成筆記", "source_id": "1", "chunk_id": "2",
            "filename": "engineering-note.txt", "snippet": "保留文件關聯"}})
        self.assertEqual(status, 201)
        _, other = self.req("POST", "/api/projects", {"name": "刪除測試乙"})
        other_id = other["project"]["project_id"]
        other_import = self.import_file(other_id)
        _, listing = self.req("GET", f"/api/transcripts?project_id={self.project_id}")
        self.assertEqual([item["transcript_id"] for item in listing["items"]], [tid])
        self.assertTrue(listing["items"][0]["imported_at"])
        impact = self.preview(tid)
        self.assertEqual((impact["draft_count"], impact["pending_draft_count"],
                          impact["confirmed_draft_count"], impact["discarded_draft_count"],
                          impact["issue_count"]), (3, 1, 1, 1, 1))
        status, deleted = self.delete(tid, impact)
        self.assertEqual(status, 200)
        self.assertEqual((deleted["drafts_deleted"], deleted["issues_preserved"]), (3, 1))
        status, after = self.req("GET", f"/api/issues/{iid}")
        self.assertEqual(status, 200)
        for key in ("transcript_id", "segment_ref", "draft_id"):
            self.assertEqual(after[key], "")
        for key in (*db.EDITABLE_FIELDS, "project_id", "source_quote", "extraction_method", "created_at"):
            self.assertEqual(after[key], before[key], key)
        self.assertTrue(after["project_locked"])
        _, links = self.req("GET", f"/api/issues/{iid}/links")
        self.assertEqual(len(links["items"]), 1)
        _, history = self.req("GET", f"/api/issues/{iid}/history")
        events = [row for row in history["items"] if row["action"] == "delete-transcript"]
        self.assertEqual({row["field"] for row in events}, {"transcript_id", "segment_ref", "draft_id"})
        for table in ("transcripts", "transcript_segments", "drafts"):
            with db.session(self.cfg.db_path) as conn:
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE transcript_id=?", (tid,)).fetchone()[0], 0)
        self.assertEqual(self.req("GET", f"/api/transcripts/{tid}")[0], 404)
        self.assertEqual(self.req("GET", f"/api/transcripts/{tid}/segments")[0], 404)
        self.assertEqual(self.req("GET", f"/api/drafts/{imported['drafts'][0]['draft_id']}")[0], 404)
        _, others = self.req("GET", f"/api/transcripts?project_id={other_id}")
        self.assertEqual(others["items"][0]["transcript_id"], other_import["transcript_id"])
        self.assertTrue((self.root / "meeting.txt").is_file())
        backup_path = self.cfg.backup_dir / deleted["backup"]
        with closing(sqlite3.connect(backup_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transcripts WHERE transcript_id=?", (tid,)).fetchone()[0], 1)

    def test_delete_without_confirmed_issues_and_reimport(self):
        imported = self.import_file()
        tid = imported["transcript_id"]
        preview = self.preview(tid)
        self.assertEqual(preview["issue_count"], 0)
        status, _ = self.delete(tid, preview)
        self.assertEqual(status, 200)
        _, issues = self.req("GET", "/api/issues")
        self.assertEqual(issues["items"], [])
        again = self.import_file()
        self.assertTrue(again["created"])
        self.assertFalse(again["duplicate"])

    def test_requires_confirmation_and_same_origin(self):
        tid = self.import_file()["transcript_id"]
        token = self.preview(tid)["preview_token"]
        for payload in ({}, {"confirm": True}, {"confirm": False, "preview_token": token}):
            self.assertEqual(self.req("DELETE", f"/api/transcripts/{tid}", payload)[0], 400)
        payload = {"confirm": True, "preview_token": token}
        self.assertEqual(self.req("DELETE", f"/api/transcripts/{tid}", payload, {"X-SDT": ""})[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/transcripts/{tid}", payload,
                                  {"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.req("GET", f"/api/transcripts/{tid}")[0], 200)

    def test_stale_preview_after_draft_confirmation_changes_nothing(self):
        imported = self.import_file()
        tid = imported["transcript_id"]
        preview = self.preview(tid)
        _, result = self.req("POST", f"/api/drafts/{imported['drafts'][0]['draft_id']}/confirm", {})
        status, error = self.delete(tid, preview)
        self.assertEqual(status, 400)
        self.assertIn("資料已改變", error["errors"][0])
        self.assertEqual(self.req("GET", f"/api/transcripts/{tid}")[0], 200)
        _, issue = self.req("GET", f"/api/issues/{result['issue']['issue_id']}")
        self.assertEqual(issue["transcript_id"], tid)
        self.assertEqual(self.preview(tid)["draft_count"], 3)

    def test_issue_edit_also_invalidates_preview(self):
        imported = self.import_file()
        _, confirmed = self.req("POST", f"/api/drafts/{imported['drafts'][0]['draft_id']}/confirm", {})
        tid = imported["transcript_id"]
        preview = self.preview(tid)
        self.req("PUT", f"/api/issues/{confirmed['issue']['issue_id']}", {"title": "編輯後標題"})
        self.assertEqual(self.delete(tid, preview)[0], 400)
        self.assertEqual(self.delete(tid, self.preview(tid))[0], 200)

    def test_failure_rolls_back_detachment_and_history(self):
        imported = self.import_file()
        tid = imported["transcript_id"]
        _, confirmed = self.req("POST", f"/api/drafts/{imported['drafts'][0]['draft_id']}/confirm", {})
        with db.session(self.cfg.db_path) as conn:
            conn.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON transcripts BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.session(self.cfg.db_path) as conn:
                transcripts.delete_transcript(conn, tid, preview_token=self.preview(tid)["preview_token"], operator="測試員")
        _, issue = self.req("GET", f"/api/issues/{confirmed['issue']['issue_id']}")
        self.assertEqual(issue["transcript_id"], tid)
        self.assertEqual(self.preview(tid)["draft_count"], 3)
        _, history = self.req("GET", f"/api/issues/{issue['issue_id']}/history")
        self.assertFalse(any(row["action"] == "delete-transcript" for row in history["items"]))

    def test_unknown_transcript_returns_404(self):
        self.assertEqual(self.req("GET", "/api/transcripts/TR-missing/delete-preview")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/transcripts/TR-missing",
                                  {"confirm": True, "preview_token": "missing"})[0], 404)

    def test_backup_failure_prevents_deletion(self):
        imported = self.import_file()
        tid = imported["transcript_id"]
        preview = self.preview(tid)
        with patch("sdt.server.backup.backup", side_effect=ValueError("備份失敗")):
            status, result = self.delete(tid, preview)
        self.assertEqual(status, 400)
        self.assertEqual(result["errors"], ["備份失敗"])
        self.assertEqual(self.preview(tid)["draft_count"], 3)

    def test_transcript_without_candidates_can_be_deleted(self):
        status, imported = self.req("POST", "/api/transcripts/import", {
            "project_id": self.project_id, "filename": "discussion.txt", "confirm": True,
            "content_base64": base64.b64encode("這只是討論，尚未定案。".encode("utf-8")).decode("ascii")})
        self.assertEqual(status, 200)
        tid = imported["transcript_id"]
        preview = self.preview(tid)
        self.assertEqual((preview["draft_count"], preview["issue_count"]), (0, 0))
        self.assertEqual(self.delete(tid, preview)[0], 200)
        self.assertEqual(self.req("GET", f"/api/transcripts/{tid}")[0], 404)
