import unittest
import base64
import pathlib
import tempfile
from unittest.mock import patch, MagicMock

from sdt import transcripts


class TestTranscriptRanges(unittest.TestCase):
    def test_header_source_and_fractional_time_ranges_are_parsed(self):
        parsed = transcripts.parse_txt(
            "會議日期：2026-10-16\n會議名稱：匿名會議\n"
            "===== REC002.MP3｜SRC-002 =====\n"
            "[00:00:00.00–00:00:04.00] 第一段匿名內容\n"
            "[00:00:04.00—00:00:06.50] 第二段匿名內容\n"
            "===== REC003.MP3｜SRC-003 =====\n"
            "[00:00:00.00 - 00:00:01.00] 第三段匿名內容\n")
        self.assertEqual(len(parsed["segments"]), 3)
        first, second, third = parsed["segments"]
        self.assertEqual((first["source_id"], first["source_audio"]), ("SRC-002", "REC002.MP3"))
        self.assertEqual((first["start_seconds"], first["end_seconds"]), ("0.000", "4.000"))
        self.assertEqual(first["raw_timecode"], "[00:00:00.00–00:00:04.00]")
        self.assertEqual((second["start_seconds"], second["end_seconds"]), ("4.000", "6.500"))
        self.assertEqual((third["source_id"], third["source_audio"], third["start_seconds"]),
                         ("SRC-003", "REC003.MP3", "0.000"))
        self.assertEqual(parsed["time_diagnostics"]["timed"], 3)

    def test_malformed_and_missing_time_are_distinguished_and_preserved(self):
        parsed = transcripts.parse_txt("[bad–00:00:02] 保留的壞時間內容\n沒有時間的段落\n")
        self.assertEqual(len(parsed["segments"]), 2)
        self.assertEqual(parsed["segments"][0]["time_parse_status"], "failed")
        self.assertEqual(parsed["segments"][1]["time_parse_status"], "no_time")
        self.assertIn("時間格式錯誤", " ".join(parsed["warnings"]))


class TestConservativeCandidates(unittest.TestCase):
    def test_explicit_anonymous_decision_becomes_review_candidate(self):
        segments = [{"source_segment_id": "a", "text": "本次決定採用 A 方案。", "location_label": "第1行", "timestamp_label": "無時間資訊"}]
        candidates = transcripts.extract_candidates(segments)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["kind"], "決議")
        self.assertEqual(candidates[0]["owner"], "")
        self.assertEqual(candidates[0]["due_date"], "")

    def test_following_fragment_after_cross_segment_negation_is_not_decision(self):
        segments = [
            {"source_segment_id": "a", "text": "因為以我所知應該是我們還沒有一個已經送審", "location_label": "第1行", "timestamp_label": "00:33:10"},
            {"source_segment_id": "b", "text": "然後就是核准過的版本", "location_label": "第2行", "timestamp_label": "00:33:14"},
        ]
        candidates = transcripts.extract_candidates(segments)
        self.assertFalse(any(item["kind"] == "決議" for item in candidates))


class TestMeetingReviewBundle(unittest.TestCase):
    def test_selected_local_outcome_persists_multiple_validated_sources_and_confirms_once(self):
        from sdt import csvio, db
        with tempfile.TemporaryDirectory() as directory:
            conn = db.connect(pathlib.Path(directory) / "multi-source.db")
            try:
                project = db.create_project(conn, "匿名來源測試")
                text = ("===== track-a.mp3｜SRC-A =====\n[00:00:00.00–00:00:02.00] 同意採用 A 版\n"
                        "===== track-b.mp3｜SRC-B =====\n[00:00:00.00–00:00:01.00] 交付成果為更新後清單\n")
                imported = transcripts.import_transcript(conn, project_id=project["project_id"],
                    filename="anonymous.txt", content_base64=base64.b64encode(text.encode()).decode(), operator="test")
                segments = transcripts.list_segments(conn, imported["transcript_id"])
                outcome = {"topic": "版本選用", "summary": "採用 A 版並更新清單", "kind": "待辦",
                    "action": "更新清單", "deliverable": "更新後清單", "owner": "", "due_date": "",
                    "model": "test-local", "evidence": [{"segment_id": segment["segment_id"], "quote": segment["text"]} for segment in segments]}
                saved = transcripts.save_local_outcome(conn, imported["transcript_id"], outcome)
                self.assertTrue(saved["created"])
                edited = transcripts.update_draft(conn, saved["draft"]["draft_id"],
                    {"summary": "人工核對後的版本選用", "kind": "待辦", "action": "修訂清單", "deliverable": "最終清單"})
                self.assertEqual(edited["outcome"]["action"], "修訂清單")
                confirmed = transcripts.confirm_draft(conn, edited["draft_id"], "test")
                self.assertEqual(len(__import__("json").loads(confirmed["issue"]["source_evidence_json"])), 2)
                self.assertIn("SRC-A", confirmed["issue"]["source_location"])
                self.assertIn("SRC-B", confirmed["issue"]["source_location"])
                self.assertEqual(confirmed["issue"]["source_excerpt"], "\n".join(segment["text"] for segment in segments))
                self.assertFalse(transcripts.confirm_draft(conn, edited["draft_id"], "test")["created"])
                exported = csvio.export_issues(db.list_issues(conn)).decode("utf-8-sig")
                self.assertIn("track-a.mp3", exported)
                self.assertIn("track-b.mp3", exported)
            finally:
                conn.close()

    def test_import_bundle_is_traceable_and_quality_preview_is_read_only(self):
        from sdt import db
        with tempfile.TemporaryDirectory() as directory:
            conn = db.connect(pathlib.Path(directory) / "review.db")
            try:
                project = db.create_project(conn, "匿名測試專案")
                text = ("會議日期：2026-10-16\n會議名稱：匿名會議\n"
                        "===== anonymous.mp3｜SRC-A =====\n"
                        "[00:00:00.00–00:00:04.00] 本次決定採用 A 方案。\n")
                imported = transcripts.import_transcript(
                    conn, project_id=project["project_id"], filename="anonymous.txt",
                    content_base64=base64.b64encode(text.encode()).decode(), operator="測試")
                self.assertEqual(imported["diagnostics"]["time"]["timed"], 1)
                segment = transcripts.list_segments(conn, imported["transcript_id"])[0]
                self.assertEqual((segment["source_audio"], segment["source_id"]), ("anonymous.mp3", "SRC-A"))
                result = transcripts.meeting_results(conn, imported["transcript_id"])
                self.assertEqual(result["items"][0]["evidence"]["raw_text"], segment["raw_text"])
                self.assertIn("anonymous.mp3", transcripts.meeting_results_markdown(result))
                preview = transcripts.quality_repair_preview(conn, imported["transcript_id"])
                self.assertTrue(preview["read_only"])
                self.assertFalse(preview["ledger_modified"])
                self.assertTrue(preview["reparse_preview"]["available"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0], 0)
                invalid = MagicMock()
                invalid.__enter__.return_value = invalid
                invalid.read.return_value = __import__("json").dumps({"message": {"content": __import__("json").dumps({"items": [
                    {"topic": "匿名議題", "summary": "有效候選", "kind": "明確決議", "action": "", "deliverable": "",
                     "owner": "", "due_date": "", "evidence": [{"segment_id": segment["segment_id"], "quote": segment["text"]}]},
                    {"topic": "虛構議題", "summary": "無效候選", "kind": "明確決議", "action": "", "deliverable": "",
                     "owner": "", "due_date": "", "evidence": [{"segment_id": "invented", "quote": "不存在的原文"}]},
                ]})}}).encode()
                with patch("sdt.transcripts.urllib.request.urlopen", return_value=invalid):
                    analysis = transcripts.analyze_local_ollama(
                        conn, imported["transcript_id"], "http://127.0.0.1:11434", "test-model")
                self.assertFalse(analysis["complete"])
                self.assertEqual(analysis["items"], [])
                self.assertIn("引用不存在", analysis["coverage"]["failed_chunks"][0]["error"])
                with patch("sdt.transcripts.urllib.request.urlopen", side_effect=TimeoutError("timeout")):
                    timeout_result = transcripts.analyze_local_ollama(
                        conn, imported["transcript_id"], "http://127.0.0.1:11434", "test-model")
                self.assertFalse(timeout_result["complete"])
                self.assertIn("timeout", timeout_result["coverage"]["failed_chunks"][0]["error"])
                malformed = MagicMock()
                malformed.__enter__.return_value = malformed
                malformed.read.return_value = __import__("json").dumps(
                    {"message": {"content": "not-json"}}).encode()
                with patch("sdt.transcripts.urllib.request.urlopen", return_value=malformed):
                    malformed_result = transcripts.analyze_local_ollama(
                        conn, imported["transcript_id"], "http://127.0.0.1:11434", "test-model")
                self.assertFalse(malformed_result["complete"])
                self.assertTrue(malformed_result["coverage"]["failed_chunks"])
                with self.assertRaises(db.ValidationError):
                    transcripts.analyze_local_ollama(
                        conn, imported["transcript_id"], "https://example.com:11434", "test-model")
            finally:
                conn.close()

    def test_exact_quote_substring_from_raw_text_is_accepted(self):
        from sdt import db
        with tempfile.TemporaryDirectory() as directory:
            conn = db.connect(pathlib.Path(directory) / "quote-slice.db")
            try:
                project = db.create_project(conn, "anonymous quote validation")
                text = ("===== anonymous.mp3|SRC-A =====\n"
                        "[00:00:00.00–00:00:02.00] \u6703\u8b70\u6c7a\u8b70\uff1a\u672c\u6b21\u63a1\u7528 A \u7248\u3002\n")
                imported = transcripts.import_transcript(
                    conn, project_id=project["project_id"], filename="anonymous.txt",
                    content_base64=base64.b64encode(text.encode()).decode(), operator="test")
                segment = transcripts.list_segments(conn, imported["transcript_id"])[0]
                marker = "\u6703\u8b70\u6c7a\u8b70\uff1a"
                self.assertNotIn(marker, segment["text"])
                self.assertIn(marker, segment["raw_text"])
                outcome = {"topic": "version", "summary": "explicit decision", "kind": "\u660e\u78ba\u6c7a\u8b70",
                    "action": "", "deliverable": "", "owner": "", "due_date": "",
                    "evidence": [{"segment_id": segment["segment_id"], "quote": marker}]}
                saved = transcripts.save_local_outcome(conn, imported["transcript_id"], outcome)
                self.assertTrue(saved["created"])
                self.assertIn(marker, saved["draft"]["source_quote"])
            finally:
                conn.close()

    def test_question_proposal_and_past_approval_are_not_current_decisions(self):
        segments = [{"source_segment_id": str(i), "text": text, "location_label": "line", "timestamp_label": "no time"}
                    for i, text in enumerate(("是否核准這個版本？", "我提議核准這個版本", "之前已經核准過這個版本"))]
        candidates = transcripts.extract_candidates(segments)
        self.assertFalse(any(item["kind"] == "決議" for item in candidates))


if __name__ == "__main__":
    unittest.main()
