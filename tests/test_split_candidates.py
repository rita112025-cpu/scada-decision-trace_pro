"""同一句同時像決議與交辦：分成兩筆候選、共用原文、各自審核入帳，重新抽取不重複也不覆寫。只用虛構句子。"""
import base64
import pathlib
import tempfile
import unittest

from sdt import db, transcripts

BOTH = "決議：採用虛構甲案，請王假設於 2026-11-05 前更新虛構圖面。"
SPOKEN_BOTH = "那就這樣，你們先把虛構圖改一版。"
ONLY_DECISION = "決議：採用虛構乙案。"


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def one(text, method=None):
    return transcripts.extract_candidates(
        [{"source_segment_id": "1", "text": text, "location_label": "第 1 行"}], method=method)


class TestExtractSplit(unittest.TestCase):
    def test_explicit_sentence_gives_decision_and_todo_sharing_quote(self):
        found = one(BOTH)
        self.assertEqual([(c["kind"], c["rule_id"]) for c in found], [("決議", "D1"), ("待辦", "T2")])
        self.assertEqual({c["source_quote"] for c in found}, {BOTH})
        self.assertEqual({c["sentence_index"] for c in found}, {1})
        self.assertEqual({c["segment_key"] for c in found}, {"1"})
        for c in found:
            self.assertEqual((c["owner"], c["due_date"]), ("王假設", "2026-11-05"))
            self.assertEqual(c["extraction_method"], transcripts.RULE_V6)
            self.assertIn("兩筆共用同一句原文", c["review_gaps"])
            self.assertNotIn("只留決議候選", c["review_gaps"])

    def test_spoken_sentence_gives_decision_and_todo(self):
        found = one(SPOKEN_BOTH)
        self.assertEqual([(c["kind"], c["rule_id"], c["cue"]) for c in found],
                         [("決議", "D7", "那就這樣"), ("待辦", "T4", "你們先")])
        self.assertEqual({c["source_quote"] for c in found}, {SPOKEN_BOTH})

    def test_older_methods_keep_single_decision(self):
        for method in ("rule:v2", "rule:v3", "rule:v4", "rule:v5"):
            found = one(BOTH, method)
            self.assertEqual([c["kind"] for c in found], ["決議"], method)
            self.assertIn("只留決議候選", found[0]["review_gaps"], method)

    def test_single_kind_sentence_unchanged(self):
        found = one(ONLY_DECISION)
        self.assertEqual([c["kind"] for c in found], ["決議"])
        self.assertNotIn("兩筆共用", found[0]["review_gaps"])

    def test_context_refs_not_shared_between_pair(self):
        segs = [
            {"source_segment_id": "1", "text": "虛構前文。", "location_label": "第 1 行"},
            {"source_segment_id": "2", "text": "決議：這個就採用，請王假設更新虛構圖面。", "location_label": "第 2 行"},
        ]
        found = transcripts.extract_candidates(segs)
        self.assertEqual(len(found), 2)
        self.assertTrue(found[0]["context_refs"])
        self.assertEqual(found[0]["context_refs"], found[1]["context_refs"])
        self.assertIsNot(found[0]["context_refs"][0], found[1]["context_refs"][0])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(pathlib.Path(self.tmp.name) / "t.db")
        self.pid = db.create_project(self.conn, "合成專案甲")["project_id"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def import_text(self, lines):
        text = "\n".join(lines) + "\n"
        return transcripts.import_transcript(
            self.conn, project_id=self.pid, filename="fictional-split.txt",
            content_base64=b64(text), operator="測試員")

    def drafts(self, tid):
        return transcripts.list_drafts(self.conn, project_id=self.pid, transcript_id=tid)

    def pair(self, imported):
        decision = next(d for d in imported["drafts"] if d["kind"] == "決議" and d["source_quote"] == BOTH)
        todo = next(d for d in imported["drafts"] if d["kind"] == "待辦" and d["source_quote"] == BOTH)
        return decision, todo


class TestImportAndConfirm(Base):
    def test_pair_imported_and_confirmed_separately(self):
        imported = self.import_text([f"[00:00:01.00–00:00:03.00] {BOTH}"])
        self.assertEqual(len(imported["drafts"]), 2)
        decision, todo = self.pair(imported)
        self.assertEqual(decision["segment_id"], todo["segment_id"])
        self.assertEqual(decision["sentence_index"], todo["sentence_index"])
        self.assertNotEqual(decision["draft_id"], todo["draft_id"])

        transcripts.update_draft(self.conn, todo["draft_id"], {"summary": "王假設更新虛構圖面"})
        first = transcripts.confirm_draft(self.conn, decision["draft_id"], "測試員")
        self.assertEqual(transcripts.get_draft(self.conn, todo["draft_id"])["review_status"], "草稿")
        second = transcripts.confirm_draft(self.conn, todo["draft_id"], "測試員")
        a, b = first["issue"], second["issue"]
        self.assertNotEqual(a["issue_id"], b["issue_id"])
        self.assertEqual((a["issue_type"], b["issue_type"]), ("會議決議", "待辦"))
        self.assertEqual(a["source_quote"], BOTH)
        self.assertEqual(b["source_quote"], BOTH)
        self.assertEqual(b["title"], "王假設更新虛構圖面")
        again = transcripts.confirm_draft(self.conn, todo["draft_id"], "測試員")
        self.assertFalse(again["created"])
        self.assertEqual(len(db.list_issues(self.conn)), 2)

    def test_discard_one_confirm_other(self):
        imported = self.import_text([BOTH])
        decision, todo = self.pair(imported)
        transcripts.discard_draft(self.conn, decision["draft_id"])
        confirmed = transcripts.confirm_draft(self.conn, todo["draft_id"], "測試員")
        self.assertEqual(confirmed["issue"]["issue_type"], "待辦")
        self.assertEqual(transcripts.get_draft(self.conn, decision["draft_id"])["review_status"], "已捨棄")
        with self.assertRaises(db.ValidationError):
            transcripts.confirm_draft(self.conn, decision["draft_id"], "測試員")


class TestReextractPair(Base):
    def test_reextract_does_not_duplicate_or_overwrite(self):
        imported = self.import_text([BOTH, ONLY_DECISION, SPOKEN_BOTH])
        tid = imported["transcript_id"]
        decision, todo = self.pair(imported)
        transcripts.update_draft(self.conn, decision["draft_id"],
                                 {"summary": "人工決議摘要", "owner": "", "due_date": ""})
        transcripts.update_draft(self.conn, todo["draft_id"], {"summary": "人工待辦摘要", "owner": "林示範"})
        issue_id = transcripts.confirm_draft(self.conn, decision["draft_id"], "測試員")["issue"]["issue_id"]
        before = {d["draft_id"]: dict(d) for d in self.drafts(tid)}

        preview = transcripts.reextract_preview(self.conn, tid)
        self.assertEqual(preview["added_count"], 0)
        self.assertEqual(preview["skipped_count"], len(before))
        self.assertTrue(all(r["reason"] == "position" for r in preview["skipped"]))
        for _ in range(2):
            applied = transcripts.reextract_apply(self.conn, tid)
            self.assertEqual(applied["added_count"], 0)
        after = {d["draft_id"]: dict(d) for d in self.drafts(tid)}
        self.assertEqual(set(after), set(before))
        for draft_id, row in before.items():
            for field in ("summary", "owner", "due_date", "review_status", "source_quote", "kind", "issue_id"):
                self.assertEqual(after[draft_id][field], row[field], (draft_id, field))
        self.assertEqual(after[todo["draft_id"]]["summary"], "人工待辦摘要")
        self.assertEqual(after[todo["draft_id"]]["owner"], "林示範")
        self.assertEqual(db.get_issue(self.conn, issue_id)["source_quote"], BOTH)
        self.assertEqual(len(db.list_issues(self.conn)), 1)

    def test_old_single_decision_gets_todo_backfilled_once(self):
        # 舊版（v5）只留決議草稿：刪掉待辦，模擬升級前的資料
        imported = self.import_text([BOTH])
        tid = imported["transcript_id"]
        decision, todo = self.pair(imported)
        self.conn.execute("DELETE FROM drafts WHERE draft_id=?", (todo["draft_id"],))
        self.conn.execute("UPDATE drafts SET extraction_method='rule:v5' WHERE draft_id=?", (decision["draft_id"],))
        transcripts.update_draft(self.conn, decision["draft_id"], {"summary": "人工決議摘要"})
        issue_id = transcripts.confirm_draft(self.conn, decision["draft_id"], "測試員")["issue"]["issue_id"]

        preview = transcripts.reextract_preview(self.conn, tid)
        self.assertEqual([(r["kind"], r["rule_id"]) for r in preview["added"]], [("待辦", "T2")])
        self.assertEqual([(r["kind"], r["draft_id"], r["reason"]) for r in preview["skipped"]],
                         [("決議", decision["draft_id"], "position")])
        applied = transcripts.reextract_apply(self.conn, tid)
        self.assertEqual(applied["added_count"], 1)
        new = transcripts.get_draft(self.conn, applied["added_draft_ids"][0])
        self.assertEqual((new["kind"], new["review_status"], new["source_quote"]), ("待辦", "草稿", BOTH))
        self.assertEqual((new["segment_id"], new["sentence_index"]),
                         (decision["segment_id"], decision["sentence_index"]))
        kept = transcripts.get_draft(self.conn, decision["draft_id"])
        self.assertEqual((kept["summary"], kept["review_status"], kept["issue_id"]),
                         ("人工決議摘要", "已確認", issue_id))
        self.assertEqual(transcripts.reextract_apply(self.conn, tid)["added_count"], 0)
        self.assertEqual(len(self.drafts(tid)), 2)

    def test_discarded_todo_is_not_readded(self):
        imported = self.import_text([BOTH])
        tid = imported["transcript_id"]
        _, todo = self.pair(imported)
        transcripts.discard_draft(self.conn, todo["draft_id"])
        self.assertEqual(transcripts.reextract_apply(self.conn, tid)["added_count"], 0)
        self.assertEqual(len(self.drafts(tid)), 2)

    def test_legacy_decision_without_position_maps_and_todo_added(self):
        # 更早的草稿沒有句序：決議靠原文對位，待辦補一筆，之後不再重複
        imported = self.import_text([BOTH])
        tid = imported["transcript_id"]
        decision, todo = self.pair(imported)
        self.conn.execute("DELETE FROM drafts WHERE draft_id=?", (todo["draft_id"],))
        self.conn.execute("UPDATE drafts SET sentence_index='' WHERE draft_id=?", (decision["draft_id"],))
        preview = transcripts.reextract_preview(self.conn, tid)
        self.assertEqual([r["kind"] for r in preview["added"]], ["待辦"])
        self.assertEqual([(r["kind"], r["reason"]) for r in preview["skipped"]], [("決議", "legacy_quote")])
        self.assertEqual(preview["pending_review"], [])
        self.assertEqual(transcripts.reextract_apply(self.conn, tid)["added_count"], 1)
        self.assertEqual(transcripts.reextract_apply(self.conn, tid)["added_count"], 0)
        self.assertEqual(transcripts.get_draft(self.conn, decision["draft_id"])["sentence_index"], "")
        self.assertEqual(sorted(d["kind"] for d in self.drafts(tid)), ["待辦", "決議"])

    def test_legacy_ambiguous_decision_blocks_only_same_kind(self):
        # 同一段出現兩次相同句子，舊決議草稿沒有句序：無法唯一定位，列待核對，不新增決議
        imported = self.import_text([BOTH + BOTH])
        tid = imported["transcript_id"]
        legacy = [d for d in imported["drafts"] if d["kind"] == "決議"]
        self.assertEqual(len(legacy), 2)
        self.conn.execute("DELETE FROM drafts WHERE transcript_id=? AND kind='待辦'", (tid,))
        self.conn.execute("DELETE FROM drafts WHERE draft_id=?", (legacy[1]["draft_id"],))
        self.conn.execute("UPDATE drafts SET sentence_index='' WHERE draft_id=?", (legacy[0]["draft_id"],))
        preview = transcripts.reextract_preview(self.conn, tid)
        self.assertEqual([r["draft_id"] for r in preview["pending_review"]], [legacy[0]["draft_id"]])
        self.assertEqual(sorted((r["kind"], r["reason"]) for r in preview["skipped"]),
                         [("決議", "ambiguous"), ("決議", "ambiguous")])
        self.assertEqual([r["kind"] for r in preview["added"]], ["待辦", "待辦"])


if __name__ == "__main__":
    unittest.main()
