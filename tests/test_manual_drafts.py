"""從原文補建候選：人工選句建立決議／待辦草稿，重抽不刪不覆寫，重複時提示，入帳可追溯。只用虛構句子。"""
import base64
import http.client
import json
import pathlib
import tempfile
import threading
import unittest

from sdt import config, db, server, transcripts

MISSED = "王假設下週把虛構圖面更新好。"
DECIDED = "決議：採用虛構甲案。"
LINES = [
    f"[00:00:01.00–00:00:03.00] 虛構開場閒聊。{MISSED}",
    f"[00:00:04.00–00:00:06.00] {DECIDED}",
]


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(pathlib.Path(self.tmp.name) / "t.db")
        self.pid = db.create_project(self.conn, "合成專案甲")["project_id"]
        imported = transcripts.import_transcript(
            self.conn, project_id=self.pid, filename="fictional-manual.txt",
            content_base64=b64("\n".join(LINES) + "\n"), operator="測試員")
        self.tid = imported["transcript_id"]
        self.rule_drafts = imported["drafts"]
        self.segments = transcripts.list_segments(self.conn, self.tid)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def manual(self, seg_no, index, kind, **extra):
        data = {"segment_id": self.segments[seg_no]["segment_id"], "sentence_index": str(index),
                "kind": kind, "summary": f"人工{kind}摘要"}
        data.update(extra)
        return transcripts.create_manual_draft(self.conn, self.tid, data)

    def drafts(self):
        return transcripts.list_drafts(self.conn, project_id=self.pid, transcript_id=self.tid)


class TestSourceSentences(Base):
    def test_lists_sentences_with_existing_candidates(self):
        data = transcripts.source_sentences(self.conn, self.tid)
        self.assertEqual(data["project_id"], self.pid)
        first, second = data["segments"]
        self.assertEqual([s["text"] for s in first["sentences"]], ["虛構開場閒聊。", MISSED])
        self.assertEqual([s["sentence_index"] for s in first["sentences"]], ["1", "2"])
        self.assertEqual([s["drafts"] for s in first["sentences"]], [[], []])
        self.assertEqual([(d["kind"], d["extraction_method"]) for d in second["sentences"][0]["drafts"]],
                         [("決議", transcripts.EXTRACTION_METHOD)])
        self.assertIn("第 1 行", first["location_label"])
        self.assertTrue(first["timestamp_label"])


class TestCreate(Base):
    def test_creates_draft_with_source_fields(self):
        res = self.manual(0, 2, "待辦", owner="王假設", due_date="2026-11-05")
        self.assertTrue(res["created"])
        d = res["draft"]
        seg = self.segments[0]
        self.assertEqual((d["project_id"], d["transcript_id"], d["segment_id"], d["sentence_index"]),
                         (self.pid, self.tid, seg["segment_id"], "2"))
        self.assertEqual(d["source_quote"], MISSED)
        self.assertEqual(d["timestamp_label"], seg["timestamp_label"])
        self.assertIn("第 1 行", d["location_label"])
        self.assertEqual((d["kind"], d["review_status"], d["extraction_method"]), ("待辦", "草稿", "manual"))
        self.assertEqual(d["summary"], "人工待辦摘要")
        self.assertEqual((d["owner"], d["owner_origin"], d["due_date"], d["due_origin"]),
                         ("王假設", "人工", "2026-11-05", "人工"))
        self.assertEqual((d["rule_id"], d["cue"], d["confidence"]), ("", "", ""))
        self.assertEqual(d["limitation"], transcripts.MANUAL_LIMITATION)
        self.assertEqual(db.list_issues(self.conn), [])

    def test_blank_owner_and_due_are_unassigned(self):
        d = self.manual(0, 2, "決議")["draft"]
        self.assertEqual((d["owner"], d["owner_origin"], d["due_date"], d["due_origin"]),
                         ("", "未指定", "", "未指定"))

    def test_validation(self):
        seg = self.segments[0]["segment_id"]
        bad = [
            {"segment_id": seg, "sentence_index": "2", "kind": "待辦", "summary": "  "},
            {"segment_id": seg, "sentence_index": "2", "kind": "其他", "summary": "x"},
            {"segment_id": seg, "sentence_index": "3", "kind": "待辦", "summary": "x"},
            {"segment_id": seg, "sentence_index": "0", "kind": "待辦", "summary": "x"},
            {"segment_id": seg, "sentence_index": "", "kind": "待辦", "summary": "x"},
            {"segment_id": seg, "sentence_index": "2", "kind": "待辦", "summary": "x", "due_date": "下週五"},
        ]
        for data in bad:
            with self.assertRaises(db.ValidationError, msg=data):
                transcripts.create_manual_draft(self.conn, self.tid, data)
        with self.assertRaises(db.NotFound):
            transcripts.create_manual_draft(self.conn, self.tid, {
                "segment_id": "TR-9999-S0001", "sentence_index": "1", "kind": "待辦", "summary": "x"})
        self.assertEqual(len(self.drafts()), len(self.rule_drafts))


class TestDuplicatePrompt(Base):
    def test_same_segment_same_kind_needs_confirm(self):
        with self.assertRaises(transcripts.NeedsChoice) as ctx:
            self.manual(1, 1, "決議")
        plan = ctx.exception.plan
        self.assertTrue(plan["needs_confirm"])
        self.assertEqual([d["draft_id"] for d in plan["existing"]], [self.rule_drafts[0]["draft_id"]])
        self.assertEqual(len(self.drafts()), len(self.rule_drafts))
        res = self.manual(1, 1, "決議", confirm_duplicate=True)
        self.assertTrue(res["created"])
        self.assertEqual(len(res["existing"]), 1)

    def test_other_kind_or_other_segment_no_prompt(self):
        self.assertTrue(self.manual(1, 1, "待辦")["created"])
        self.assertTrue(self.manual(0, 2, "決議")["created"])

    def test_discarded_and_confirmed_still_prompt(self):
        first = self.manual(0, 2, "待辦")["draft"]
        transcripts.discard_draft(self.conn, first["draft_id"])
        with self.assertRaises(transcripts.NeedsChoice) as ctx:
            self.manual(0, 1, "待辦")
        self.assertEqual([d["review_status"] for d in ctx.exception.plan["existing"]], ["已捨棄"])
        transcripts.confirm_draft(self.conn, self.rule_drafts[0]["draft_id"], "測試員")
        with self.assertRaises(transcripts.NeedsChoice) as ctx:
            self.manual(1, 1, "決議")
        self.assertEqual([d["review_status"] for d in ctx.exception.plan["existing"]], ["已確認"])


class TestEditConfirmTrace(Base):
    def test_edit_then_confirm_traces_back_to_source(self):
        d = self.manual(0, 2, "待辦")["draft"]
        transcripts.update_draft(self.conn, d["draft_id"], {"summary": "王假設更新虛構圖面", "due_date": "2026-11-05"})
        edited = transcripts.get_draft(self.conn, d["draft_id"])
        self.assertEqual(edited["source_quote"], MISSED)
        res = transcripts.confirm_draft(self.conn, d["draft_id"], "測試員")
        issue = res["issue"]
        self.assertEqual(issue["issue_type"], "待辦")
        self.assertEqual(issue["title"], "王假設更新虛構圖面")
        self.assertEqual(issue["due_date"], "2026-11-05")
        self.assertEqual((issue["transcript_id"], issue["segment_ref"], issue["draft_id"], issue["extraction_method"]),
                         (self.tid, d["segment_id"], d["draft_id"], "manual"))
        self.assertEqual((issue["source_quote"], issue["source_excerpt"]), (MISSED, MISSED))
        self.assertEqual(issue["source_file"], "fictional-manual.txt")
        self.assertEqual(issue["source_location"], d["location_label"])
        self.assertEqual(issue["source_status"], "已確認")
        self.assertIn("人工從逐字稿原文建立", issue["description"])
        self.assertNotIn("規則抽取，不是模型", issue["description"])
        history = {(h["field"], h["new_value"]) for h in db.get_history(self.conn, issue["issue_id"])
                   if h["action"] == "confirm-draft"}
        self.assertIn(("draft_id", d["draft_id"]), history)
        self.assertIn(("source_quote", MISSED), history)
        self.assertEqual(transcripts.get_draft(self.conn, d["draft_id"])["review_status"], "已確認")


class TestReextractKeepsManual(Base):
    def test_reextract_keeps_manual_and_adds_nothing(self):
        d = self.manual(0, 2, "待辦", owner="王假設")["draft"]
        transcripts.update_draft(self.conn, d["draft_id"], {"summary": "人工改寫"})
        for _ in range(2):
            self.assertEqual(transcripts.reextract_preview(self.conn, self.tid)["added_count"], 0)
            self.assertEqual(transcripts.reextract_apply(self.conn, self.tid)["added_count"], 0)
        kept = transcripts.get_draft(self.conn, d["draft_id"])
        self.assertEqual((kept["summary"], kept["owner"], kept["source_quote"], kept["review_status"],
                          kept["extraction_method"]), ("人工改寫", "王假設", MISSED, "草稿", "manual"))

    def test_manual_occupies_rule_position_same_kind_only(self):
        # 規則候選被刪掉後，人工在同一句建立決議：重抽不再補同類型的規則決議
        rule = self.rule_drafts[0]
        transcripts.delete_draft(self.conn, rule["draft_id"])
        manual = self.manual(1, 1, "決議")["draft"]
        preview = transcripts.reextract_preview(self.conn, self.tid)
        self.assertEqual(preview["added_count"], 0)
        self.assertEqual([(r["kind"], r["draft_id"], r["reason"]) for r in preview["skipped"]],
                         [("決議", manual["draft_id"], "manual")])
        self.assertEqual(transcripts.reextract_apply(self.conn, self.tid)["added_count"], 0)
        self.assertEqual([d["draft_id"] for d in self.drafts()], [manual["draft_id"]])

    def test_manual_todo_does_not_block_rule_decision(self):
        rule = self.rule_drafts[0]
        transcripts.delete_draft(self.conn, rule["draft_id"])
        self.manual(1, 1, "待辦")
        preview = transcripts.reextract_preview(self.conn, self.tid)
        self.assertEqual([(r["kind"], r["rule_id"]) for r in preview["added"]], [("決議", "D1")])

    def test_reparse_keeps_manual(self):
        d = self.manual(0, 2, "待辦")["draft"]
        transcripts.reparse_apply(self.conn, self.tid)
        kept = transcripts.get_draft(self.conn, d["draft_id"])
        self.assertEqual((kept["source_quote"], kept["review_status"], kept["sentence_index"]), (MISSED, "草稿", "2"))


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.Config(operator="測試員", port=0, data_dir=pathlib.Path(self.tmp.name))
        self.srv = server.make_server(cfg, port=0, today_fn=lambda: "2026-10-21")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        _, proj = self.req("POST", "/api/projects", {"name": "虛構專案"})
        _, imported = self.req("POST", "/api/transcripts/import", {
            "confirm": True, "project_id": proj["project"]["project_id"], "filename": "fictional.txt",
            "content_base64": b64("\n".join(LINES) + "\n")})
        self.tid = imported["transcript_id"]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=json.dumps(body).encode("utf-8") if body is not None else None,
                  headers={"Host": f"127.0.0.1:{self.port}", "X-SDT": "1"})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        c.close()
        return r.status, data

    def test_sentences_create_and_duplicate_confirm(self):
        s, data = self.req("GET", f"/api/transcripts/{self.tid}/sentences")
        self.assertEqual(s, 200)
        seg = data["segments"][1]
        body = {"segment_id": seg["segment_id"], "sentence_index": "1", "kind": "決議", "summary": "人工決議"}
        s, res = self.req("POST", f"/api/transcripts/{self.tid}/manual-drafts", body)
        self.assertEqual(s, 409)
        self.assertTrue(res["needs_confirm"])
        self.assertEqual(len(res["existing"]), 1)
        self.assertTrue(res["errors"])
        s, res = self.req("POST", f"/api/transcripts/{self.tid}/manual-drafts", dict(body, confirm_duplicate=True))
        self.assertEqual(s, 201)
        self.assertEqual(res["draft"]["extraction_method"], "manual")
        s, res = self.req("POST", f"/api/transcripts/{self.tid}/manual-drafts", dict(body, summary=""))
        self.assertEqual(s, 400)
        s, res = self.req("GET", "/api/transcripts/TR-9999/sentences")
        self.assertEqual(s, 404)


if __name__ == "__main__":
    unittest.main()
