"""第二階段：TXT 時間、口語候選與重新解析。只用虛構句子。"""
import base64
import pathlib
import shutil
import tempfile
import unittest

from sdt import db, transcripts
from scripts.verify_local import score_kind, verify

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "synthetic"


def b64(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.conn = db.connect(self.dir / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def project(self, name="合成專案甲"):
        return db.create_project(self.conn, name)

    def import_example(self, filename, project_id):
        return transcripts.import_transcript(
            self.conn, project_id=project_id, filename=filename,
            content_base64=b64((EXAMPLES / filename).read_bytes()), operator="測試員")


class TestTxtTimes(Base):
    def test_mixed_formats_keep_each_line(self):
        proj = self.project()
        preview = transcripts.preview_payload(
            self.conn, project_id=proj["project_id"], filename="meeting-range.txt",
            content_base64=b64((EXAMPLES / "meeting-range.txt").read_bytes()))
        counts = preview["time_counts"]
        self.assertEqual(counts, {"complete": 3, "start_only": 1, "no_time": 1, "time_error": 4})
        self.assertEqual(preview["timestamp_status"], "有時間錯誤")
        result = self.import_example("meeting-range.txt", proj["project_id"])
        segments = transcripts.list_segments(self.conn, result["transcript_id"])
        by_line = {s["line_start"]: s for s in segments}
        self.assertEqual(by_line[1]["start_seconds"], "1994.140")
        self.assertEqual(by_line[1]["end_seconds"], "1996.780")
        self.assertEqual(by_line[1]["text"], "然後就是核准過的版本。")
        self.assertTrue(by_line[1]["raw_text"].startswith("[00:33:14.14"))
        self.assertEqual(by_line[2]["start_seconds"], "1996.780")
        self.assertEqual(by_line[2]["end_seconds"], "2000.500")
        self.assertEqual(by_line[3]["start_seconds"], "2041.100")
        self.assertEqual(by_line[3]["end_seconds"], "2045.000")
        self.assertEqual(by_line[4]["end_seconds"], "")
        self.assertIn("無結束時間", by_line[4]["timestamp_label"])
        self.assertEqual(by_line[4]["speaker"], "講者甲")
        self.assertEqual(by_line[5]["timestamp_label"], "無時間資訊")
        self.assertEqual(by_line[5]["start_seconds"], "")
        for line in (6, 7, 8, 9):
            self.assertEqual(by_line[line]["timestamp_label"], "時間錯誤")
            self.assertEqual(by_line[line]["start_seconds"], "")
            self.assertEqual(by_line[line]["end_seconds"], "")
            self.assertIn(f"第 {line} 行", by_line[line]["timestamp_source"])
        self.assertEqual(by_line[6]["raw_text"], "[00:41:70] 這一秒超出範圍。")
        joined = "\n".join(s["timestamp_source"] for s in segments)
        self.assertIn("分或秒超出", joined)
        self.assertIn("結束時間早於", joined)
        self.assertIn("時間不可為負", joined)
        self.assertIn("小數秒", joined)

    def test_no_blank_line_required_between_segments(self):
        text = "[00:01:00] 講者甲：第一句。\n[00:01:02.5–00:01:03] 第二句。\n"
        parsed = transcripts.parse_txt(text)
        self.assertEqual(len(parsed["segments"]), 2)
        self.assertEqual(parsed["segments"][0]["time_kind"], "start_only")
        self.assertEqual(parsed["segments"][1]["time_kind"], "complete")
        self.assertEqual(parsed["segments"][1]["text"], "第二句。")


class TestColloquialRules(Base):
    def test_historical_and_bare_words_are_not_decisions(self):
        proj = self.project()
        result = self.import_example("meeting-colloquial.txt", proj["project_id"])
        quotes = [d["source_quote"] for d in result["drafts"]]
        for blocked in (
            "然後就是核准過的版本。",
            "我們核准這個做法。",
            "請確認一下就好。",
            "建議採用新的合成命名，是否同意？",
            "本案未核准。",
            "這不是決議。",
            "目前沒有待辦。",
            "待辦已取消。",
        ):
            self.assertNotIn(blocked, quotes)
        decision = next(d for d in result["drafts"] if d["source_quote"] == "決議：本次採用合成備援盤。")
        self.assertEqual(decision["rule_id"], "D1")
        self.assertTrue(decision["reason"])
        self.assertIn("缺負責人", decision["review_gaps"])
        self.assertIn("缺期限", decision["review_gaps"])
        self.assertEqual(decision["owner"], "")
        self.assertEqual(decision["due_date"], "")
        self.assertEqual(decision["context_refs"], [])
        bare_decision = next(d for d in result["drafts"] if d["source_quote"] == "決定採用合成備援盤。")
        self.assertEqual(bare_decision["rule_id"], "D3")
        anaphora = next(d for d in result["drafts"] if d["source_quote"] == "就照這樣定案。")
        self.assertEqual(anaphora["rule_id"], "D2")
        self.assertEqual(len(anaphora["context_refs"]), 1)
        self.assertIn("通訊模組就放在虛構盤的右側。", anaphora["context_refs"][0]["text"])
        self.assertNotIn("通訊模組就放在虛構盤的右側。", anaphora["source_quote"])
        todo = next(d for d in result["drafts"] if d["source_quote"] == "待辦：更新合成盤面配置。")
        self.assertEqual(todo["rule_id"], "T1")
        self.assertEqual(todo["owner"], "")
        self.assertEqual(todo["due_date"], "")
        assigned = next(d for d in result["drafts"] if "林示範" in d["source_quote"])
        self.assertEqual((assigned["owner"], assigned["due_date"]), ("林示範", "2026-12-01"))
        self.assertEqual(db.list_issues(self.conn), [])

    def test_summary_edit_does_not_replace_quote(self):
        proj = self.project()
        result = self.import_example("meeting-colloquial.txt", proj["project_id"])
        draft = next(d for d in result["drafts"] if d["rule_id"] == "D1")
        updated = transcripts.update_draft(self.conn, draft["draft_id"], {"summary": "人工改寫的虛構摘要"})
        self.assertEqual(updated["source_quote"], draft["source_quote"])
        self.assertEqual(updated["summary"], "人工改寫的虛構摘要")


class TestReparse(Base):
    def test_reparse_keeps_ids_status_quote_and_survives_reopen(self):
        proj = self.project()
        imported = self.import_example("meeting-range.txt", proj["project_id"])
        tid = imported["transcript_id"]
        draft = next(d for d in imported["drafts"] if d["kind"] == "決議")
        confirmed = transcripts.confirm_draft(self.conn, draft["draft_id"], "測試員")
        issue_id = confirmed["issue"]["issue_id"]
        quote = draft["source_quote"]
        raw_before = transcripts.get_transcript(self.conn, tid)["raw_text"]
        segments = transcripts.list_segments(self.conn, tid)
        target = next(s for s in segments if s["text"].startswith("決議："))
        self.conn.execute(
            """UPDATE transcript_segments
               SET text=?, start_seconds='', end_seconds='', timestamp_label='無時間資訊', speaker=''
               WHERE segment_id=?""",
            (target["raw_text"], target["segment_id"]))
        self.conn.execute(
            "UPDATE transcripts SET timestamp_status='無時間資訊', timestamp_detail='舊資料尚未解析時間' WHERE transcript_id=?",
            (tid,))
        self.conn.commit()
        preview = transcripts.reparse_preview(self.conn, tid)
        self.assertFalse(preview["applied"])
        changed = next(c for c in preview["changes"] if c["segment_id"] == target["segment_id"])
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["before"]["timestamp_label"], "無時間資訊")
        self.assertIn("–", changed["after"]["timestamp_label"])
        self.assertEqual(transcripts.get_transcript(self.conn, tid)["timestamp_status"], "無時間資訊")
        applied = transcripts.reparse_apply(self.conn, tid)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["transcript_id"], tid)
        again = transcripts.get_segment(self.conn, tid, target["segment_id"])
        self.assertEqual(again["segment_id"], target["segment_id"])
        self.assertEqual(again["raw_text"], target["raw_text"])
        self.assertNotEqual(again["start_seconds"], "")
        self.assertEqual(again["text"], "決議：本次採用合成備援盤。")
        stored_draft = transcripts.get_draft(self.conn, draft["draft_id"])
        self.assertEqual(stored_draft["review_status"], "已確認")
        self.assertEqual(stored_draft["source_quote"], quote)
        issue = db.get_issue(self.conn, issue_id)
        self.assertEqual(issue["source_quote"], quote)
        self.assertEqual(issue["issue_id"], issue_id)
        self.assertEqual(transcripts.get_transcript(self.conn, tid)["raw_text"], raw_before)
        duplicate = self.import_example("meeting-range.txt", proj["project_id"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["transcript_id"], tid)
        self.conn.commit()
        self.conn.close()
        reopened = db.connect(self.dir / "t.db")
        try:
            self.assertEqual(transcripts.get_transcript(reopened, tid)["raw_text"], raw_before)
            self.assertEqual(transcripts.get_draft(reopened, draft["draft_id"])["source_quote"], quote)
            self.assertEqual(db.get_issue(reopened, issue_id)["source_quote"], quote)
        finally:
            reopened.close()
            self.conn = db.connect(self.dir / "t.db")


class TestLocalVerifier(unittest.TestCase):
    def test_report_without_annotation_does_not_claim_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            src = EXAMPLES / "meeting-colloquial.txt"
            dest = folder / "meeting-colloquial.txt"
            shutil.copyfile(src, dest)
            before = dest.stat().st_mtime_ns
            result = verify(folder)
            self.assertEqual(dest.stat().st_mtime_ns, before)
            text = result["text"]
            self.assertIn("候選數：", text)
            self.assertIn("不計算 precision 或 recall", text)
            self.assertIn("不聲稱召回率", text)
            self.assertNotIn("SCADA會議紀錄.docx 的內文", text)
            self.assertTrue((folder / "reports" / "verify-report.txt").is_file())

    def test_annotation_reports_precision_and_recall_fractions(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            shutil.copyfile(EXAMPLES / "meeting-colloquial.txt", folder / "meeting-colloquial.txt")
            shutil.copyfile(EXAMPLES / "annotation-template.json", folder / "annotation.json")
            (folder / "pending.json").write_text(
                '{"items":[{"kind":"決議","note":"虛構參考有一項還沒對到行號"}]}', encoding="utf-8")
            (folder / "false-positives.json").write_text(
                '{"items":[{"kind":"決議","line":2,"note":"虛構：人工判定這句不是這次的決議"}]}',
                encoding="utf-8")
            (folder / "SCADA會議紀錄.docx").write_bytes(b"not a real document")
            text = verify(folder)["text"]
            self.assertIn("待核對參考", text)
            self.assertIn("沒有讀取內文", text)
            self.assertIn("待確認", text)
            self.assertIn("人工判定的誤收", text)
            self.assertIn("precision", text)
            self.assertIn("recall", text)
            self.assertRegex(text, r"決議 precision \d+/\d+")
            self.assertRegex(text, r"待辦 precision \d+/\d+")
            self.assertIn("recall 2/2", text)
        predicted = [
            {"kind": "決議", "line": "11"},
            {"kind": "決議", "line": "2"},
            {"kind": "待辦", "line": "13"},
        ]
        gold = [
            {"kind": "決議", "line": "11"},
            {"kind": "待辦", "line": "12"},
            {"kind": "待辦", "line": "13"},
        ]
        decision = score_kind("決議", gold, predicted)
        self.assertEqual((decision["precision"], decision["recall"]), ("1/2", "1/1"))
        todo = score_kind("待辦", gold, predicted)
        self.assertEqual((todo["precision"], todo["recall"]), ("1/1", "1/2"))
