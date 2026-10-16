"""第二階段：TXT 時間、口語候選、重新解析與重新抽取。只用虛構句子。"""
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from sdt import config, db, server, transcripts
from scripts.verify_local import evaluate_score, verify

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
        self.assertTrue(all(d["sentence_index"] == "1" for d in result["drafts"]))

    def test_same_wording_in_different_segments_keeps_each_context(self):
        segments = []
        texts = [
            "通訊模組就放在虛構盤的右側。",
            "就照這樣定案。",
            "端子改放在虛構盤的左側。",
            "就照這樣定案。就照這樣定案。",
        ]
        for i, text in enumerate(texts, start=1):
            segments.append({
                "source_segment_id": str(i),
                "text": text,
                "raw_text": text,
                "location_label": f"第 {i} 行",
                "timestamp_label": "無時間資訊",
            })
        found = transcripts.extract_candidates(segments)
        hits = [c for c in found if c["source_quote"] == "就照這樣定案。"]
        self.assertEqual([(c["segment_key"], c["sentence_index"]) for c in hits], [
            ("2", 1), ("4", 1), ("4", 2),
        ])
        self.assertIn("右側", hits[0]["context_refs"][0]["text"])
        self.assertIn("左側", hits[1]["context_refs"][0]["text"])
        self.assertIn("左側", hits[2]["context_refs"][0]["text"])
        self.assertNotIn("右側", hits[1]["source_quote"])
        self.assertNotIn("左側", hits[0]["context_refs"][0]["text"])

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
            result = verify(folder)
            text = result["text"]
            self.assertEqual(result["scored"], 1)
            self.assertEqual(result["refused"], 0)
            self.assertIn("待核對參考", text)
            self.assertIn("沒有讀取內文", text)
            self.assertIn("待確認", text)
            self.assertIn("人工判定的誤收", text)
            self.assertIn("一對一", text)
            self.assertIn("決議 precision 1/3", text)
            self.assertIn("決議 precision 1/3（分子是與標註一對一對上的候選數，分母是該類候選數）； recall 1/1", text)
            self.assertIn("待辦 precision 2/2", text)
            self.assertIn("recall 2/2", text)

    def test_cross_file_same_line_pairs_by_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            (folder / "fictional-a.txt").write_text("[00:01:00] 決議：採用虛構甲盤。\n", encoding="utf-8")
            (folder / "fictional-b.txt").write_text("[00:01:00] 決議：採用虛構乙盤。\n", encoding="utf-8")
            (folder / "annotation.json").write_text(json.dumps({
                "items": [{"kind": "決議", "file": "fictional-a.txt", "line": 1, "sentence": 1}],
            }), encoding="utf-8")
            result = verify(folder)
        decision = result["score"]["by_kind"]["決議"]
        self.assertEqual((decision["precision"], decision["recall"]), ("1/2", "1/1"))
        self.assertEqual(result["matched_total"] if "matched_total" in result else result["score"]["matched_total"], 1)

    def test_multiple_sentences_on_one_line_use_sentence_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            (folder / "fictional-line.txt").write_text(
                "[00:01:00] 決議：採用虛構甲盤。決議：採用虛構乙盤。\n", encoding="utf-8")
            (folder / "annotation.json").write_text(json.dumps({
                "items": [{"kind": "決議", "file": "fictional-line.txt", "line": 1, "sentence": 2}],
            }), encoding="utf-8")
            result = verify(folder)
        decision = result["score"]["by_kind"]["決議"]
        self.assertEqual((decision["precision"], decision["recall"]), ("1/2", "1/1"))

    def test_duplicate_or_unclear_annotation_refuses_score(self):
        locations = {"fictional-a.txt": {"1": 1}, "fictional-line.txt": {"1": None}}
        hit = {"kind": "決議", "file": "fictional-a.txt", "line": "1", "sentence": "1"}
        duplicate = evaluate_score([hit, dict(hit)], [hit], locations)
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["matched_total"], 0)
        self.assertTrue(any(p["code"] == "duplicate" for p in duplicate["problems"]))
        missing_sentence = evaluate_score(
            [{"kind": "決議", "file": "fictional-a.txt", "line": "1", "sentence": ""}], [hit], locations)
        self.assertEqual(missing_sentence["matched_total"], 0)
        self.assertTrue(any(p["code"] == "missing_sentence" for p in missing_sentence["problems"]))
        missing_file = evaluate_score(
            [{"kind": "決議", "file": "", "line": "1", "sentence": "1"}], [hit], locations)
        self.assertTrue(any(p["code"] == "missing_file" for p in missing_file["problems"]))
        self.assertEqual(missing_file["matched_total"], 0)
        out_of_range = evaluate_score(
            [{"kind": "決議", "file": "fictional-a.txt", "line": "1", "sentence": "9"}], [hit], locations)
        self.assertTrue(any(p["code"] == "not_found" for p in out_of_range["problems"]))
        self.assertEqual(out_of_range["matched_total"], 0)
        ambiguous = evaluate_score(
            [{"kind": "決議", "file": "fictional-line.txt", "line": "1", "sentence": "1"}], [hit], locations)
        self.assertTrue(any(p["code"] == "ambiguous_line" for p in ambiguous["problems"]))
        self.assertEqual(ambiguous["matched_total"], 0)
        dup_pred = evaluate_score([hit], [hit, dict(hit)], locations)
        self.assertTrue(any(p["code"] == "duplicate_prediction" for p in dup_pred["problems"]))
        self.assertEqual(dup_pred["matched_total"], 0)
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            (folder / "fictional-a.txt").write_text("[00:01:00] 決議：採用虛構甲盤。\n", encoding="utf-8")
            (folder / "annotation.json").write_text(json.dumps({"items": [
                {"kind": "決議", "file": "fictional-a.txt", "line": 1, "sentence": 1},
                {"kind": "決議", "file": "fictional-a.txt", "line": 1, "sentence": 1, "note": "重複的虛構標註"},
            ]}), encoding="utf-8")
            result = verify(folder)
            self.assertEqual(result["scored"], 0)
            self.assertGreater(result["refused"], 0)
            self.assertIn("拒絕評分", result["text"])
            self.assertIn("沒有算成命中", result["text"])
            self.assertIn("重複標註", result["text"])
            self.assertNotRegex(result["text"], r"precision \d+/\d+")
            (folder / "annotation.json").write_text(json.dumps({"items": [
                {"kind": "決議", "file": "fictional-a.txt", "line": 1, "note": "只有行號的虛構標註"},
            ]}), encoding="utf-8")
            unclear = verify(folder)
            self.assertEqual(unclear["scored"], 0)
            self.assertEqual(unclear["score"]["matched_total"], 0)
            self.assertIn("缺少句子位置", unclear["text"])
            self.assertIn("沒有算成命中", unclear["text"])
            self.assertNotRegex(unclear["text"], r"precision \d+/\d+")


def _run_verify(args, env):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_local.py"), *args],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )


class TestVerifierTerminal(unittest.TestCase):
    def test_success_and_failure_hide_sensitive_marker(self):
        marker = "虛構敏感標記甲"
        env = os.environ.copy()
        env.pop("SDT_VERIFY_DIR", None)
        env["PYTHONIOENCODING"] = "utf-8"
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp) / marker
            folder.mkdir()
            name = f"{marker}.txt"
            (folder / name).write_text(f"[00:01:00] {marker}就照這樣定案。\n", encoding="utf-8")
            (folder / "annotation.json").write_text(json.dumps({"items": [
                {"kind": "決議", "file": name, "line": 1, "sentence": 1, "note": marker},
            ]}), encoding="utf-8")
            (folder / "pending.json").write_text(json.dumps({
                "items": [{"kind": "待辦", "note": marker}],
            }), encoding="utf-8")
            (folder / "false-positives.json").write_text(json.dumps({
                "items": [{"kind": "決議", "file": name, "line": 1, "sentence": 1, "note": marker}],
            }), encoding="utf-8")
            proc = _run_verify(["--dir", str(folder)], env)
            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertEqual(proc.stderr, "")
            self.assertNotIn(marker, combined)
            self.assertNotIn(str(folder), combined)
            self.assertNotIn(name, combined)
            self.assertNotIn("定案", combined)
            self.assertNotIn("verify-report", combined)
            self.assertIn("status=0", proc.stdout)
            self.assertIn("files=1", proc.stdout)
            self.assertIn("segments=1", proc.stdout)
            self.assertIn("sentences=1", proc.stdout)
            self.assertIn("parse_warnings=0", proc.stdout)
            self.assertIn("scored=1", proc.stdout)
            self.assertIn("refused=0", proc.stdout)
            report = (folder / "reports" / "verify-report.txt").read_text(encoding="utf-8")
            self.assertIn(marker, report)
            self.assertIn(name, report)

            missing = _run_verify(["--dir", str(folder / marker)], env)
            missing_out = missing.stdout + missing.stderr
            self.assertEqual(missing.returncode, 2)
            self.assertIn("status=2", missing.stderr)
            self.assertIn("error=missing_dir", missing.stderr)
            self.assertNotIn(marker, missing_out)
            self.assertNotIn(str(folder), missing_out)

            (folder / name).write_bytes(marker.encode("utf-8") + b"\xff")
            bad_text = _run_verify(["--dir", str(folder)], env)
            bad_out = bad_text.stdout + bad_text.stderr
            self.assertEqual(bad_text.returncode, 2)
            self.assertIn("error=not_utf8", bad_text.stderr)
            self.assertNotIn(marker, bad_out)
            self.assertNotIn(name, bad_out)

            (folder / name).write_text("[00:01:00] 一句虛構。\n", encoding="utf-8")
            (folder / "annotation.json").write_text("{" + marker, encoding="utf-8")
            bad_json = _run_verify(["--dir", str(folder)], env)
            json_out = bad_json.stdout + bad_json.stderr
            self.assertEqual(bad_json.returncode, 2)
            self.assertIn("error=bad_annotation", bad_json.stderr)
            self.assertNotIn(marker, json_out)
            self.assertNotIn(str(folder), json_out)

            (folder / "annotation.json").write_text(json.dumps({"items": [
                {"kind": "決議", "file": name, "line": marker, "sentence": 1},
            ]}), encoding="utf-8")
            bad_line = _run_verify(["--dir", str(folder)], env)
            line_out = bad_line.stdout + bad_line.stderr
            self.assertEqual(bad_line.returncode, 2)
            self.assertIn("error=bad_annotation", bad_line.stderr)
            self.assertNotIn(marker, line_out)

            unknown = _run_verify([marker], env)
            unknown_out = unknown.stdout + unknown.stderr
            self.assertEqual(unknown.returncode, 2)
            self.assertIn("error=failed", unknown.stderr)
            self.assertNotIn(marker, unknown_out)

        # 沒有 --dir 時，腳本會用 SDT_VERIFY_DIR（預設 C:\Users\user\sdt-verify）。
        # 這裡指到保證不存在的隨機路徑，讓測試不依賴本機是否有真實預設目錄，
        # 並確認錯誤訊息不會把這個路徑印出來。
        env["SDT_VERIFY_DIR"] = os.path.join(
            tempfile.gettempdir(), "sdt-verify-does-not-exist-" + os.urandom(8).hex())
        bare = _run_verify([], env)
        bare_out = bare.stdout + bare.stderr
        self.assertEqual(bare.returncode, 2)
        self.assertIn("error=missing_dir", bare.stderr)
        self.assertNotIn("sdt-verify-does-not-exist", bare_out)
        self.assertNotIn("Users", bare_out)


class TestReextract(Base):
    def test_reextract_keeps_existing_rows_and_adds_by_position(self):
        proj = self.project()
        imported = self.import_example("meeting-colloquial.txt", proj["project_id"])
        tid = imported["transcript_id"]
        draft = next(d for d in imported["drafts"] if d["rule_id"] == "D1")
        other = next(d for d in imported["drafts"] if d["draft_id"] != draft["draft_id"])
        transcripts.update_draft(self.conn, other["draft_id"], {"summary": "人工改寫的虛構摘要"})
        confirmed = transcripts.confirm_draft(self.conn, draft["draft_id"], "測試員")
        issue_id = confirmed["issue"]["issue_id"]
        quote = draft["source_quote"]
        raw = transcripts.get_transcript(self.conn, tid)["raw_text"]
        before_ids = sorted(d["draft_id"] for d in imported["drafts"])
        preview = transcripts.reextract_preview(self.conn, tid)
        self.assertFalse(preview["applied"])
        self.assertEqual(preview["added_count"], 0)
        self.assertEqual(preview["skipped_count"], len(before_ids))
        self.assertEqual(preview["extraction_method"], "rule:v5")
        self.assertEqual(preview["runs"], [])
        self.assertIn("尚未寫入", preview["message"])
        self.assertEqual(sorted(d["draft_id"] for d in transcripts.list_drafts(
            self.conn, project_id=proj["project_id"], transcript_id=tid)), before_ids)
        first = transcripts.reextract_apply(self.conn, tid)
        self.assertTrue(first["applied"])
        self.assertEqual(first["added_count"], 0)
        self.assertEqual(first["extraction_method"], "rule:v5")
        self.assertEqual(transcripts.get_draft(self.conn, other["draft_id"])["summary"], "人工改寫的虛構摘要")
        self.assertEqual(transcripts.get_draft(self.conn, draft["draft_id"])["review_status"], "已確認")
        self.assertEqual(transcripts.get_draft(self.conn, draft["draft_id"])["source_quote"], quote)
        self.assertEqual(db.get_issue(self.conn, issue_id)["source_quote"], quote)
        self.assertEqual(transcripts.get_transcript(self.conn, tid)["raw_text"], raw)
        self.assertEqual(len(db.list_issues(self.conn)), 1)
        segment = next(s for s in transcripts.list_segments(self.conn, tid) if s["text"] == "就照這樣定案。")
        self.conn.execute(
            "UPDATE transcript_segments SET text=? WHERE segment_id=?",
            (segment["text"] + "決議：另定虛構項目。", segment["segment_id"]))
        self.conn.commit()
        again = transcripts.reextract_preview(self.conn, tid)
        self.assertEqual(again["added_count"], 1)
        self.assertEqual(again["added"][0]["rule_id"], "D1")
        self.assertEqual(again["added"][0]["sentence_index"], "2")
        self.assertIn("另定虛構項目", again["added"][0]["source_quote"])
        applied = transcripts.reextract_apply(self.conn, tid)
        self.assertEqual(applied["added_count"], 1)
        created = transcripts.get_draft(self.conn, applied["added_draft_ids"][0])
        self.assertEqual(created["review_status"], "草稿")
        self.assertEqual(created["sentence_index"], "2")
        self.assertEqual(created["extraction_method"], "rule:v5")
        self.assertEqual(transcripts.get_draft(self.conn, other["draft_id"])["summary"], "人工改寫的虛構摘要")
        self.assertEqual(transcripts.get_draft(self.conn, other["draft_id"])["sentence_index"], "1")
        self.assertEqual(transcripts.get_draft(self.conn, draft["draft_id"])["source_quote"], quote)
        self.assertEqual(db.get_issue(self.conn, issue_id)["source_quote"], quote)
        self.assertEqual(transcripts.get_transcript(self.conn, tid)["raw_text"], raw)
        self.assertEqual(len(db.list_issues(self.conn)), 1)
        third = transcripts.reextract_apply(self.conn, tid)
        self.assertEqual(third["added_count"], 0)
        runs = self.conn.execute(
            "SELECT extraction_method, added_count, skipped_count FROM extraction_runs WHERE transcript_id=? ORDER BY run_id",
            (tid,)).fetchall()
        self.assertEqual([tuple(r) for r in runs], [
            ("rule:v5", 0, len(before_ids)),
            ("rule:v5", 1, len(before_ids)),
            ("rule:v5", 0, len(before_ids) + 1),
        ])
        draft_count = len(transcripts.list_drafts(self.conn, project_id=proj["project_id"], transcript_id=tid))
        transcripts.reparse_apply(self.conn, tid)
        self.assertEqual(len(transcripts.list_drafts(
            self.conn, project_id=proj["project_id"], transcript_id=tid)), draft_count)

    def test_legacy_quote_match_does_not_backfill_or_duplicate(self):
        proj = self.project()
        imported = self.import_example("meeting-colloquial.txt", proj["project_id"])
        tid = imported["transcript_id"]
        self.conn.execute("UPDATE drafts SET sentence_index='' WHERE transcript_id=?", (tid,))
        self.conn.commit()
        preview = transcripts.reextract_preview(self.conn, tid)
        self.assertEqual(preview["added_count"], 0)
        self.assertTrue(all(row["reason"] == "legacy_quote" for row in preview["skipped"]))
        applied = transcripts.reextract_apply(self.conn, tid)
        self.assertEqual(applied["added_count"], 0)
        stored = transcripts.list_drafts(self.conn, project_id=proj["project_id"], transcript_id=tid)
        self.assertTrue(all(d["sentence_index"] == "" for d in stored))
        self.assertEqual(len(stored), len(imported["drafts"]))

    def test_legacy_timestamp_prefix_maps_by_position_after_reparse(self):
        proj = self.project()
        text = "\n".join([
            "[00:00:01.00–00:00:02.00] 決議：採用虛構甲案。",
            "[00:00:03-00:00:04] 待辦：更新虛構盤面。",
            "[00:00:05.5–00:00:06] 決議：採用虛構乙案。待辦：請林示範於 2026-12-01 前補上端子對照。",
            "[00:00:09.00–00:00:10.00] 就照這樣定案。就照這樣定案。",
        ]) + "\n"
        imported = transcripts.import_transcript(
            self.conn, project_id=proj["project_id"], filename="fictional-legacy.txt",
            content_base64=b64(text), operator="測試員")
        tid = imported["transcript_id"]
        drafts = list(imported["drafts"])
        confirmed_src = next(d for d in drafts if d["source_quote"] == "決議：採用虛構甲案。")
        discarded_src = next(d for d in drafts if d["source_quote"] == "待辦：更新虛構盤面。")
        confirmed = transcripts.confirm_draft(self.conn, confirmed_src["draft_id"], "測試員")
        transcripts.discard_draft(self.conn, discarded_src["draft_id"])
        issue_id = confirmed["issue"]["issue_id"]
        segments = transcripts.list_segments(self.conn, tid)

        def prefixed(draft):
            raw = next(s["raw_text"] for s in segments if s["segment_id"] == draft["segment_id"])
            return raw.split("]", 1)[0] + "] " + draft["source_quote"]

        ambiguous = [d for d in drafts if d["source_quote"] == "就照這樣定案。"]
        self.assertEqual(len(ambiguous), 2)
        self.conn.execute("DELETE FROM drafts WHERE draft_id=?", (ambiguous[1]["draft_id"],))
        kept = [d for d in drafts if d["draft_id"] != ambiguous[1]["draft_id"]]
        quotes = {}
        for draft in kept:
            quote = prefixed(draft)
            quotes[draft["draft_id"]] = quote
            self.conn.execute(
                """UPDATE drafts
                   SET source_quote=?, summary=?, sentence_index='', extraction_method='rule:v1'
                   WHERE draft_id=?""",
                (quote, "舊摘要：" + quote, draft["draft_id"]))
        self.conn.execute(
            "UPDATE issues SET source_quote=? WHERE issue_id=?",
            (quotes[confirmed_src["draft_id"]], issue_id))
        self.conn.execute("UPDATE transcript_segments SET text=raw_text WHERE transcript_id=?", (tid,))
        self.conn.commit()
        reparsed = transcripts.reparse_apply(self.conn, tid)
        self.assertTrue(reparsed["applied"])
        after_parse = {s["line_start"]: s for s in transcripts.list_segments(self.conn, tid)}
        self.assertEqual(after_parse[1]["text"], "決議：採用虛構甲案。")
        self.assertTrue(after_parse[1]["raw_text"].startswith("[00:00:01.00–00:00:02.00]"))
        self.assertEqual(after_parse[2]["text"], "待辦：更新虛構盤面。")
        self.assertTrue(after_parse[2]["raw_text"].startswith("[00:00:03-00:00:04]"))
        self.assertEqual(
            after_parse[3]["text"],
            "決議：採用虛構乙案。待辦：請林示範於 2026-12-01 前補上端子對照。")
        self.assertEqual(after_parse[4]["text"], "就照這樣定案。就照這樣定案。")
        self.assertEqual(len(transcripts.list_drafts(
            self.conn, project_id=proj["project_id"], transcript_id=tid)), 5)

        preview = transcripts.reextract_preview(self.conn, tid)
        self.assertFalse(preview["applied"])
        self.assertEqual(preview["added_count"], 0)
        self.assertEqual(preview["pending_count"], 1)
        self.assertEqual(preview["pending_review"][0]["draft_id"], ambiguous[0]["draft_id"])
        self.assertEqual(preview["pending_review"][0]["review"], "待核對")
        self.assertEqual(preview["pending_review"][0]["source_quote"], quotes[ambiguous[0]["draft_id"]])
        self.assertIn("待核對", preview["message"])
        self.assertEqual(len(transcripts.list_drafts(
            self.conn, project_id=proj["project_id"], transcript_id=tid)), 5)
        positioned = [row for row in preview["skipped"] if row["reason"] == "legacy_position"]
        self.assertEqual(
            {row["draft_id"] for row in positioned},
            {confirmed_src["draft_id"], discarded_src["draft_id"]} | {
                d["draft_id"] for d in kept
                if d["draft_id"] not in {confirmed_src["draft_id"], discarded_src["draft_id"], ambiguous[0]["draft_id"]}
            },
        )
        self.assertTrue(any(row["reason"] == "ambiguous" for row in preview["skipped"]))

        applied = transcripts.reextract_apply(self.conn, tid)
        self.assertEqual(applied["added_count"], 0)
        self.assertEqual(applied["added_draft_ids"], [])
        self.assertEqual(applied["pending_count"], 1)
        self.assertEqual(applied["pending_review"][0]["review"], "待核對")
        self.assertIn("沒有確認入帳", applied["message"])
        stored = transcripts.list_drafts(self.conn, project_id=proj["project_id"], transcript_id=tid)
        self.assertEqual(len(stored), 5)
        by_id = {d["draft_id"]: d for d in stored}
        self.assertEqual(by_id[confirmed_src["draft_id"]]["review_status"], "已確認")
        self.assertEqual(by_id[confirmed_src["draft_id"]]["source_quote"], quotes[confirmed_src["draft_id"]])
        self.assertEqual(by_id[confirmed_src["draft_id"]]["summary"], "舊摘要：" + quotes[confirmed_src["draft_id"]])
        self.assertEqual(by_id[confirmed_src["draft_id"]]["extraction_method"], "rule:v1")
        self.assertEqual(by_id[discarded_src["draft_id"]]["review_status"], "已捨棄")
        self.assertTrue(by_id[discarded_src["draft_id"]]["source_quote"].startswith("[00:00:03-00:00:04] "))
        self.assertEqual(db.get_issue(self.conn, issue_id)["source_quote"], quotes[confirmed_src["draft_id"]])
        self.assertEqual(len(db.list_issues(self.conn)), 1)
        self.assertTrue(all(d["sentence_index"] == "" and d["extraction_method"] == "rule:v1" for d in stored))

        second = transcripts.reextract_apply(self.conn, tid)
        self.assertEqual(second["added_count"], 0)
        self.assertEqual(second["pending_count"], 1)
        stored_again = transcripts.list_drafts(self.conn, project_id=proj["project_id"], transcript_id=tid)
        self.assertEqual(
            [(d["draft_id"], d["source_quote"], d["review_status"], d["summary"]) for d in stored_again],
            [(d["draft_id"], d["source_quote"], d["review_status"], d["summary"]) for d in stored],
        )
        self.assertEqual(len(db.list_issues(self.conn)), 1)

    def test_http_reextract_preview_does_not_apply_without_confirm(self):
        http_dir = self.dir / "http"
        http_dir.mkdir()
        cfg = config.Config(operator="測試員", port=0, data_dir=http_dir)
        srv = server.make_server(cfg, port=0)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            def req(method, path, body):
                import http.client
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                data = json.dumps(body).encode("utf-8")
                conn.request(method, path, body=data, headers={
                    "Host": f"127.0.0.1:{port}",
                    "X-SDT": "1",
                    "Content-Type": "application/json",
                })
                response = conn.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                status = response.status
                conn.close()
                return status, payload

            status, proj = req("POST", "/api/projects", {"name": "合成專案甲"})
            self.assertEqual(status, 201)
            status, imported = req("POST", "/api/transcripts/import", {
                "project_id": proj["project"]["project_id"],
                "filename": "meeting-colloquial.txt",
                "content_base64": b64((EXAMPLES / "meeting-colloquial.txt").read_bytes()),
                "confirm": True,
            })
            self.assertEqual(status, 200)
            tid = imported["transcript_id"]
            status, preview = req("POST", f"/api/transcripts/{tid}/reextract/preview", {})
            self.assertEqual(status, 200)
            self.assertFalse(preview["applied"])
            self.assertIn("尚未寫入", preview["message"])
            status, denied = req("POST", f"/api/transcripts/{tid}/reextract", {})
            self.assertEqual(status, 400)
            self.assertTrue(any("預覽重新抽取" in err for err in denied["errors"]))
            status, time_preview = req("POST", f"/api/transcripts/{tid}/reparse/preview", {})
            self.assertEqual(status, 200)
            self.assertNotIn("added_count", time_preview)
        finally:
            srv.shutdown()
            srv.server_close()


_DECISION = "決議：本次採用合成備援盤。"
_TODO = "待辦：更新合成盤面配置。"
_ASSIGNED = "請林示範於 2026-12-01 前補上端子對照。"
_HISTORICAL = "然後就是核准過的版本。"
_FILLER = "然後就是一般討論。"
_TITLE = "虛構會議標題甲。"


def _clock(total_seconds):
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.00"


def _en_dash_line(index, body):
    start = index * 3
    return f"[{_clock(start)}\u2013{_clock(start + 2)}] {body}"


def _header_range_lines():
    """虛構檔：中文標題在前，接著大量半形方括號與 en dash 的時間列。"""
    headers = [
        _TITLE,
        "合成專案紀錄是否僅供測試？",
        "地點在虛構會議室。",
        "日期是二零二六年十一月二日。",
        "出席有林示範與陳假設。",
        "以下為合成逐字稿。",
        "沒有真實會議內容。",
        "時間標記僅供解析測試。",
    ]
    special = {10: _DECISION, 100: _TODO, 500: _ASSIGNED, 2000: _HISTORICAL}
    stamps = [_en_dash_line(i, special.get(i, _FILLER)) for i in range(2442)]
    return headers + [""] + stamps


class TestHeaderRangeParse(unittest.TestCase):
    def test_crlf_header_and_en_dash_lines_parse_and_extract(self):
        lines = _header_range_lines()
        self.assertEqual(len(lines), 2451)
        self.assertEqual(sum(1 for line in lines if line.startswith("[")), 2442)
        raw = "\r\n".join(lines)
        data = raw.encode("utf-8")
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(data.startswith(_TITLE.encode("utf-8")))
        name = "fictional-header-range.txt"
        parsed = transcripts.parse_upload(name, raw)
        counts = parsed["time_counts"]
        self.assertEqual(len(parsed["segments"]), 2450)
        self.assertEqual(counts, {"complete": 2442, "start_only": 0, "no_time": 8, "time_error": 0})
        self.assertEqual(parsed["segments"][0]["text"], _TITLE)
        self.assertEqual(parsed["segments"][0]["time_kind"], "no_time")
        first_stamp = next(seg for seg in parsed["segments"] if seg["time_kind"] == "complete")
        self.assertIn("\u2013", first_stamp["raw_text"])
        self.assertEqual(first_stamp["start_seconds"], "0.000")
        self.assertEqual(first_stamp["end_seconds"], "2.000")
        self.assertFalse(first_stamp["text"].startswith("["))
        _, normalized = transcripts.content_hash(raw)
        again = transcripts.parse_upload(name, normalized)
        self.assertEqual(len(again["segments"]), 2450)
        self.assertEqual(again["time_counts"], counts)
        found = transcripts.extract_candidates(parsed["segments"])
        quotes = [item["source_quote"] for item in found]
        self.assertEqual(quotes, [_DECISION, _TODO, _ASSIGNED])
        self.assertTrue(all(not quote.startswith("[") for quote in quotes))
        self.assertNotIn(_HISTORICAL, quotes)
        self.assertEqual([item["rule_id"] for item in found], ["D1", "T1", "T2"])
        assigned = found[2]
        self.assertEqual((assigned["owner"], assigned["due_date"]), ("林示範", "2026-12-01"))
        by_line = {item["segment_key"]: item["source_quote"] for item in found}
        self.assertEqual(by_line, {"20": _DECISION, "110": _TODO, "510": _ASSIGNED})

        env = os.environ.copy()
        env.pop("SDT_VERIFY_DIR", None)
        env["PYTHONIOENCODING"] = "utf-8"
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            (folder / name).write_bytes(data)
            proc = _run_verify(["--dir", str(folder)], env)
            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertEqual(proc.stderr, "")
            self.assertEqual(proc.stdout, "\n".join([
                "status=0",
                "files=1",
                "segments=2450",
                "sentences=2450",
                "parse_warnings=0",
                "candidates=3",
                "decisions=1",
                "todos=2",
                "gold=0",
                "pending=0",
                "false_hits=0",
                "scored=0",
                "refused=0",
                "",
            ]))
            for hidden in (_TITLE, _DECISION, _TODO, _ASSIGNED, _HISTORICAL, name, str(folder), "虛構"):
                self.assertNotIn(hidden, combined)
            report = (folder / "reports" / "verify-report.txt").read_text(encoding="utf-8")
            self.assertIn(name, report)
            self.assertIn(_DECISION, report)
            self.assertIn("段落 2450", report)

    def test_other_line_separators_and_one_bad_stamp_stay_per_line(self):
        samples = {
            "fictional-cr.txt": "虛構標題乙。\r[00:00:01.00\u201300:00:02.00] " + _DECISION + "\r",
            "fictional-u2028.txt": (
                "虛構標題丙？\u2028[00:00:01.14\u201300:00:03.78] " + _DECISION
                + "\u2028[00:00:04.00\u201300:00:05.00] " + _FILLER
            ),
            "fictional-u2029.txt": "虛構標題丁。\u2029[00:00:01.5\u201300:00:02.5] " + _DECISION,
            "fictional-nel.txt": "虛構標題戊。\x85[00:00:01.00\u201300:00:02.00] " + _TODO,
            "fictional-bom.txt": "\ufeff虛構標題庚。\r\n[00:00:01.00\u201300:00:02.00] " + _DECISION + "\r\n",
        }
        expected = {
            "fictional-cr.txt": (2, 1, _DECISION),
            "fictional-u2028.txt": (3, 2, _DECISION),
            "fictional-u2029.txt": (2, 1, _DECISION),
            "fictional-nel.txt": (2, 1, _TODO),
            "fictional-bom.txt": (2, 1, _DECISION),
        }
        env = os.environ.copy()
        env.pop("SDT_VERIFY_DIR", None)
        env["PYTHONIOENCODING"] = "utf-8"
        for name, text in samples.items():
            parsed = transcripts.parse_upload(name, text)
            segments, complete, quote = expected[name]
            self.assertEqual(len(parsed["segments"]), segments, name)
            self.assertEqual(parsed["time_counts"]["complete"], complete, name)
            self.assertEqual(parsed["time_counts"]["time_error"], 0, name)
            self.assertNotIn("\ufeff", parsed["segments"][0]["text"])
            found = transcripts.extract_candidates(parsed["segments"])
            self.assertEqual([item["source_quote"] for item in found], [quote], name)
            self.assertFalse(found[0]["source_quote"].startswith("["), name)
            with tempfile.TemporaryDirectory() as tmp:
                folder = pathlib.Path(tmp)
                (folder / name).write_bytes(text.encode("utf-8"))
                proc = _run_verify(["--dir", str(folder)], env)
                combined = proc.stdout + proc.stderr
                self.assertEqual(proc.returncode, 0, combined)
                self.assertIn(f"segments={segments}", proc.stdout)
                self.assertIn("candidates=1", proc.stdout)
                self.assertIn("parse_warnings=0", proc.stdout)
                self.assertNotIn(quote, combined)
                self.assertNotIn(name, combined)
                self.assertNotIn(str(folder), combined)

        warning = "\r\n".join([
            "虛構標題己。",
            "[00:00:01.00\u201300:00:02.00] " + _DECISION,
            "[00:41:70\u201300:42:10] 這一秒超出範圍。",
            "[00:00:03.00\u201300:00:04.00] " + _TODO,
        ])
        parsed = transcripts.parse_upload("fictional-warning.txt", warning)
        self.assertEqual(len(parsed["segments"]), 4)
        self.assertEqual(parsed["time_counts"], {
            "complete": 2, "start_only": 0, "no_time": 1, "time_error": 1,
        })
        self.assertEqual(parsed["segments"][2]["text"], "這一秒超出範圍。")
        self.assertEqual(parsed["segments"][3]["time_kind"], "complete")
        quotes = [item["source_quote"] for item in transcripts.extract_candidates(parsed["segments"])]
        self.assertEqual(quotes, [_DECISION, _TODO])
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            (folder / "fictional-warning.txt").write_bytes(warning.encode("utf-8"))
            proc = _run_verify(["--dir", str(folder)], env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("segments=4", proc.stdout)
            self.assertIn("parse_warnings=1", proc.stdout)
            self.assertIn("candidates=2", proc.stdout)
            self.assertNotIn("超出範圍", proc.stdout + proc.stderr)
            self.assertNotIn("fictional-warning", proc.stdout + proc.stderr)


class TestSpokenRules(Base):
    def test_v2_match_set_stays_reproducible(self):
        text = (EXAMPLES / "meeting-colloquial.txt").read_text(encoding="utf-8")
        parsed = transcripts.parse_upload("meeting-colloquial.txt", text)
        v2 = transcripts.extract_candidates(parsed["segments"], method="rule:v2")
        v3 = transcripts.extract_candidates(parsed["segments"], method="rule:v3")
        self.assertEqual(
            [(c["rule_id"], c["source_quote"]) for c in v2],
            [(c["rule_id"], c["source_quote"]) for c in v3],
        )
        self.assertTrue(all(c["confidence"] == "高" and c["extraction_method"] == "rule:v2" for c in v2))
        spoken = [{"source_segment_id": "1", "text": "我們就採用合成備援盤。", "location_label": "第 1 行"}]
        self.assertEqual(transcripts.extract_candidates(spoken, method="rule:v2"), [])
        hit = transcripts.extract_candidates(spoken, method="rule:v3")[0]
        self.assertEqual((hit["rule_id"], hit["cue"], hit["confidence"]), ("D7", "我們就採用", "中"))

    def test_colloquial_positives_and_guards(self):
        text = (EXAMPLES / "meeting-spoken.txt").read_text(encoding="utf-8")
        parsed = transcripts.parse_upload("meeting-spoken.txt", text)
        found = transcripts.extract_candidates(parsed["segments"])
        by_pos = {(c["segment_key"], c["sentence_index"]): c for c in found}
        expected = {
            ("1", 1): ("決議", "D7", "那就這樣", "中", "", ""),
            ("2", 1): ("決議", "D7", "就這樣定", "中", "", ""),
            ("3", 1): ("決議", "D7", "就照這個做", "中", "", ""),
            ("4", 1): ("決議", "D7", "我們就採用", "中", "", ""),
            ("5", 1): ("決議", "D7", "確定用", "中", "", ""),
            ("6", 1): ("決議", "D7", "OK那就", "中", "", ""),
            ("7", 1): ("決議", "D7", "不用改了", "中", "", ""),
            ("8", 1): ("決議", "D7", "先維持原案", "中", "", ""),
            ("9", 1): ("待辦", "T4", "回去確認", "中", "林示範", "2026-11-20"),
            ("10", 1): ("待辦", "T4", "回去確認", "中", "陳假設", ""),
            ("11", 1): ("待辦", "T4", "要再提供", "中", "", ""),
            ("12", 1): ("待辦", "T4", "下次會議前", "中", "", ""),
            ("13", 1): ("待辦", "T4", "我們這邊會", "中", "", ""),
            ("14", 1): ("待辦", "T4", "你們先", "中", "", ""),
            ("15", 1): ("待辦", "T4", "再回覆", "中", "", ""),
            ("16", 1): ("待辦", "T4", "補給我們", "中", "", ""),
            ("17", 1): ("決議", "D1", "決議：", "高", "", ""),
            ("18", 1): ("決議", "D7", "那就這樣", "中", "", ""),
            ("44", 2): ("決議", "D7", "我們就採用", "中", "", ""),
        }
        self.assertEqual(set(by_pos), set(expected))
        for key, spec in expected.items():
            cand = by_pos[key]
            self.assertEqual(
                (cand["kind"], cand["rule_id"], cand["cue"], cand["confidence"], cand["owner"], cand["due_date"]),
                spec,
                key,
            )
            self.assertFalse(cand["source_quote"].startswith("["))
            self.assertEqual(cand["extraction_method"], "rule:v5")
            if cand["confidence"] != "高":
                self.assertIn(cand["cue"], cand["reason"])
        self.assertEqual(by_pos[("18", 1)]["source_quote"], "那就這樣。")
        self.assertNotIn("要不要", by_pos[("18", 1)]["source_quote"])
        adopted = by_pos[("44", 2)]
        self.assertEqual(adopted["source_quote"], "我們就採用這個位置。")
        self.assertTrue(adopted["context_refs"])
        self.assertNotIn("比較麻煩", adopted["source_quote"])
        v2 = transcripts.extract_candidates(parsed["segments"], method="rule:v2")
        self.assertEqual([c["source_quote"] for c in v2], ["決議：本次採用合成備援盤。"])
        for quote in (
            "我可以幫你弄，但是麻煩你多給一點預算。",
            "麻煩你多給一點費用。",
            "你們先報個價就好。",
            "預算那就這樣算了。",
        ):
            self.assertNotIn(quote, [c["source_quote"] for c in found])

    def test_v4_drops_negotiation_and_keeps_real_assignments(self):
        def one(text, method=None):
            return transcripts.extract_candidates(
                [{"source_segment_id": "1", "text": text, "location_label": "第 1 行"}],
                method=method,
            )
        budget = "我可以幫你弄，但是麻煩你多給一點預算。"
        old = one(budget, "rule:v3")
        self.assertEqual([c["cue"] for c in old], ["麻煩"])
        self.assertEqual(one(budget, "rule:v4"), [])
        self.assertEqual(one("麻煩你多給一點費用。"), [])
        self.assertEqual(one("你們先報個價就好。"), [])
        self.assertEqual([c["cue"] for c in one("你們先報個價就好。", "rule:v3")], ["你們先"])
        self.assertEqual(one("預算那就這樣算了。"), [])
        self.assertEqual([c["cue"] for c in one("預算那就這樣算了。", "rule:v3")], ["那就這樣"])
        self.assertEqual(one("那就這樣，但是畫面再改。"), [])
        asked = one("麻煩你們確認虛構點位。")
        self.assertEqual((asked[0]["kind"], asked[0]["cue"], asked[0]["owner"], asked[0]["confidence"]),
                         ("待辦", "麻煩", "", "中"))
        named = one("麻煩陳假設回覆端子對照。")
        self.assertEqual((named[0]["owner"], named[0]["due_date"], named[0]["cue"]), ("陳假設", "", "麻煩"))
        kept = one("你們先把連鎖條件列出來。")
        self.assertEqual(kept[0]["cue"], "你們先")
        decided = one("嗯那就這樣，通訊模組就放在虛構盤右側。")
        self.assertEqual((decided[0]["cue"], decided[0]["confidence"]), ("那就這樣", "中"))
        blocked = "然後就是核准過的版本。"
        self.assertEqual(one(blocked), [])
        still = transcripts.extract_candidates(
            [{"source_segment_id": "1", "text": blocked, "location_label": "第 1 行"}],
            extra_cues={"decisions": ["核准過的版本"], "todos": []},
        )
        self.assertEqual(still, [])

    def test_v5_loosens_you_first_and_keeps_price_and_past_out(self):
        def one(text, method=None):
            return transcripts.extract_candidates(
                [{"source_segment_id": "1", "text": text, "location_label": "第 1 行"}],
                method=method,
            )

        missed_by_v4 = [
            "你們先看一下虛構點位。",
            "你先檢查這面虛構盤。",
            "就你們先把圖改一版。",
            "你們這邊先出圖。",
            "你先跟廠商對一下連鎖。",
            "你們先寄給我們虛構的點表。",
            "你先寫上這次的虛構修改。",
            "你先想一下告警文字。",
            "你們這邊先規劃虛構備援。",
        ]
        also_v5 = [
            "那你們先幫我們確認端子。",
            "嗯，你們先就幫我們把這個點補上。",
            "你們先看看告警文字，這個虛構盤的通訊位置跟線槽都還要再對過一遍才能收。",
        ]
        narrow = "你們先把連鎖條件列出來。"
        for text in missed_by_v4 + also_v5 + [narrow]:
            hit = one(text)
            self.assertEqual(len(hit), 1, text)
            self.assertEqual(
                (hit[0]["kind"], hit[0]["cue"], hit[0]["confidence"], hit[0]["owner"], hit[0]["due_date"],
                 hit[0]["extraction_method"]),
                ("待辦", "你們先", "中", "", "", "rule:v5"),
                text,
            )
            self.assertEqual(one(text, "rule:v2"), [], text)
        for text in missed_by_v4:
            self.assertEqual(one(text, "rule:v4"), [], text)

        self.assertEqual([c["cue"] for c in one(narrow, "rule:v3")], ["你們先"])
        self.assertEqual([c["cue"] for c in one(narrow, "rule:v4")], ["你們先"])
        planned = "你們先規劃虛構備援。"
        self.assertEqual([c["cue"] for c in one(planned, "rule:v4")], ["你們先"])
        self.assertEqual([c["cue"] for c in one(planned)], ["你們先"])
        self.assertEqual(one("你先想一下告警文字。", "rule:v4"), [])

        negatives = [
            "你們先報個價就好。",
            "你們先給個報價。",
            "你們先看一下好嗎？",
            "你們先不用改。",
            "你們先前做過的圖就留著。",
            "你先前做過的先別動。",
            "如果你們先看一下呢？",
            "你們先可能再改。",
            "你們先把報價單整理一下。",
            "我可以幫你弄，但是麻煩你多給一點預算。",
            "麻煩你多給一點費用。",
        ]
        for text in negatives:
            self.assertEqual(one(text), [], text)
            self.assertEqual(one(text, "rule:v5"), [], text)

        self.assertEqual([c["cue"] for c in one("你們先報個價就好。", "rule:v3")], ["你們先"])
        self.assertEqual(one("你們先報個價就好。", "rule:v4"), [])
        self.assertEqual([c["cue"] for c in one("我可以幫你弄，但是麻煩你多給一點預算。", "rule:v3")], ["麻煩"])
        self.assertEqual(one("我可以幫你弄，但是麻煩你多給一點預算。", "rule:v4"), [])
        asked = one("麻煩你們確認虛構點位。", "rule:v5")
        self.assertEqual((asked[0]["cue"], asked[0]["owner"]), ("麻煩", ""))

    def test_local_cue_file_stays_off_the_terminal(self):
        phrase = "虛構收口講法甲"
        todo = "虛構補件講法甲"
        cues = {"decisions": [phrase], "todos": [todo]}
        bare = [{"source_segment_id": "1", "text": f"對，這段就用{phrase}處理。", "location_label": "第 1 行"}]
        self.assertEqual(transcripts.extract_candidates(bare), [])
        self.assertEqual(transcripts.extract_candidates(bare, method="rule:v2", extra_cues=cues), [])
        hit = transcripts.extract_candidates(bare, extra_cues=cues)[0]
        self.assertEqual((hit["rule_id"], hit["cue"], hit["confidence"], hit["owner"], hit["due_date"]),
                         ("D8", phrase, "低", "", ""))
        asked = [{"source_segment_id": "1", "text": f"{phrase}嗎？", "location_label": "第 1 行"}]
        self.assertEqual(transcripts.extract_candidates(asked, extra_cues=cues), [])
        assigned = [{"source_segment_id": "1", "text": f"請把{todo}補進清單。", "location_label": "第 1 行"}]
        todo_hit = transcripts.extract_candidates(assigned, extra_cues=cues)[0]
        self.assertEqual((todo_hit["kind"], todo_hit["rule_id"], todo_hit["confidence"]), ("待辦", "T5", "低"))
        with self.assertRaises(transcripts.CueError):
            transcripts.normalize_cues({"decisions": ["太短"]})
        soft = transcripts.extract_candidates(
            [{"source_segment_id": "1", "text": "ok那就維持虛構告警。", "location_label": "第 1 行"}])
        self.assertEqual(soft[0]["cue"], "OK那就")

        env = os.environ.copy()
        env.pop("SDT_VERIFY_DIR", None)
        env["PYTHONIOENCODING"] = "utf-8"
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            (folder / "fictional-cue.txt").write_text(f"對，這段就用{phrase}處理。\n", encoding="utf-8")
            (folder / "cues.json").write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
            proc = _run_verify(["--dir", str(folder)], env)
            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertIn("candidates=1", proc.stdout)
            self.assertIn("decisions=1", proc.stdout)
            for hidden in (phrase, todo, "cues.json", "fictional-cue", str(folder)):
                self.assertNotIn(hidden, combined)
            report = (folder / "reports" / "verify-report.txt").read_text(encoding="utf-8")
            self.assertIn(phrase, report)
            self.assertIn("信心 低", report)
            self.assertNotIn(todo, report)

            (folder / "cues.json").write_text('{"decisions": ["' + phrase + '"', encoding="utf-8")
            bad = _run_verify(["--dir", str(folder)], env)
            bad_out = bad.stdout + bad.stderr
            self.assertEqual(bad.returncode, 2)
            self.assertIn("error=bad_cues", bad.stderr)
            self.assertNotIn(phrase, bad_out)
            self.assertNotIn(str(folder), bad_out)

    def test_synthetic_precision_recall_is_not_real_accuracy(self):
        text = (EXAMPLES / "meeting-spoken.txt").read_text(encoding="utf-8")
        gold_text = (EXAMPLES / "annotation-spoken.json").read_text(encoding="utf-8")
        self.assertIn("不是真實會議的準確率", gold_text)
        env = os.environ.copy()
        env.pop("SDT_VERIFY_DIR", None)
        env["PYTHONIOENCODING"] = "utf-8"
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            (folder / "meeting-spoken.txt").write_text(text, encoding="utf-8")
            (folder / "annotation.json").write_text(gold_text, encoding="utf-8")
            result = verify(folder)
            self.assertEqual(result["candidates"], 19)
            self.assertEqual(result["gold"], 19)
            self.assertEqual(result["scored"], 1)
            self.assertEqual(result["refused"], 0)
            self.assertEqual(result["score"]["by_kind"]["決議"]["precision"], "11/11")
            self.assertEqual(result["score"]["by_kind"]["決議"]["recall"], "11/11")
            self.assertEqual(result["score"]["by_kind"]["待辦"]["precision"], "8/8")
            self.assertEqual(result["score"]["by_kind"]["待辦"]["recall"], "8/8")
            self.assertIn("不是真實會議的準確率", result["text"])
            proc = _run_verify(["--dir", str(folder)], env)
            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertEqual(proc.stdout, "\n".join([
                "status=0",
                "files=1",
                "segments=48",
                "sentences=50",
                "parse_warnings=0",
                "candidates=19",
                "decisions=11",
                "todos=8",
                "gold=19",
                "pending=0",
                "false_hits=0",
                "scored=1",
                "refused=0",
                "",
            ]))
            for hidden in ("那就這樣", "核准過", "meeting-spoken", "準確率", str(folder)):
                self.assertNotIn(hidden, combined)
        proj = self.project()
        imported = self.import_example("meeting-spoken.txt", proj["project_id"])
        self.assertEqual(db.list_issues(self.conn), [])
        self.assertTrue(all(d["review_status"] == "草稿" for d in imported["drafts"]))
        sample = next(d for d in imported["drafts"] if d["cue"] == "那就這樣")
        self.assertEqual(sample["confidence"], "中")
        self.assertIn("請人工確認", sample["reason"])
