"""逐字稿匯入、草稿入帳、專案隔離與文件關聯。只用合成資料。"""
import base64
import json
import pathlib
import sqlite3
import tempfile
import unittest

from sdt import db, notebook, transcripts

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "synthetic"


def b64(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def _write_notebook_db(path, *, filename, file_hash, notebook, original_path, source_id=1, chunk_id=2):
    """最小的 local-notebook 資料表，只含定位會讀的欄位。雜湊值由測試給定，不假裝是檔案內容算出來的。"""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE notebooks (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            notebook_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_path TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_hash TEXT NOT NULL
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            notebook_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            page INTEGER,
            line_start INTEGER,
            line_end INTEGER,
            timestamp TEXT,
            text TEXT NOT NULL
        );
    """)
    conn.execute("INSERT INTO notebooks(id, name) VALUES (1, ?)", (notebook,))
    conn.execute(
        """INSERT INTO sources(id, notebook_id, filename, original_path, file_type, file_hash)
           VALUES (?,?,?,?,?,?)""",
        (source_id, 1, filename, original_path, "txt", file_hash))
    conn.execute(
        """INSERT INTO chunks(id, source_id, notebook_id, seq, page, line_start, line_end, timestamp, text)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (chunk_id, source_id, 1, 1, None, 3, 3, None, "合成段落"))
    conn.commit()
    conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.db_path = self.dir / "t.db"
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def project(self, name="合成專案甲"):
        return db.create_project(self.conn, name)

    def load(self, name):
        return (EXAMPLES / name).read_bytes()


class TestParse(Base):
    def test_json_preview_has_project_filename_and_complete_times(self):
        proj = self.project()
        preview = transcripts.preview_payload(
            self.conn, project_id=proj["project_id"], filename="meeting.json",
            content_base64=b64(self.load("meeting.json")))
        self.assertEqual(preview["project_id"], proj["project_id"])
        self.assertEqual(preview["filename"], "meeting.json")
        self.assertEqual(preview["segment_count"], 6)
        self.assertEqual(preview["timestamp_status"], "齊全")
        self.assertFalse(preview["duplicate"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0], 0)

    def test_plain_txt_uses_line_numbers_and_no_fabricated_time(self):
        proj = self.project()
        result = transcripts.import_transcript(
            self.conn, project_id=proj["project_id"], filename="meeting-notime.txt",
            content_base64=b64(self.load("meeting-notime.txt")), operator="測試員")
        segments = transcripts.list_segments(self.conn, result["transcript_id"])
        decision = next(s for s in segments if s["text"].startswith("決議："))
        self.assertEqual(decision["line_start"], 3)
        self.assertEqual(decision["timestamp_label"], "無時間資訊")
        self.assertEqual(decision["start_seconds"], "")
        self.assertEqual(decision["end_seconds"], "")
        self.assertNotIn("00:", decision["timestamp_label"])
        stored = transcripts.get_transcript(self.conn, result["transcript_id"])
        self.assertEqual(stored["timestamp_status"], "無時間資訊")
        self.assertIn("無時間資訊", stored["timestamp_detail"])
        self.assertNotIn("00:", stored["timestamp_detail"])

    def test_timed_txt_keeps_start_and_does_not_invent_end(self):
        proj = self.project()
        result = transcripts.import_transcript(
            self.conn, project_id=proj["project_id"], filename="meeting-timed.txt",
            content_base64=b64(self.load("meeting-timed.txt")), operator="測試員")
        segments = transcripts.list_segments(self.conn, result["transcript_id"])
        self.assertEqual(segments[0]["start_seconds"], "12.000")
        self.assertEqual(segments[0]["end_seconds"], "")
        self.assertIn("無結束時間", segments[0]["timestamp_label"])
        self.assertEqual(segments[0]["speaker"], "講者未識別")
        self.assertEqual(result["timestamp_status"], "不齊全")

    def test_errors_are_explicit_and_write_nothing(self):
        proj = self.project()
        cases = [
            ("empty.txt", b64(b"   \n"), "空的"),
            ("bad.txt", base64.b64encode(b"\xff\xfe not utf8").decode("ascii"), "UTF-8"),
            ("notes.json", b64("{"), "JSON"),
            ("notes.json", b64(json.dumps({"segments": []})), "缺少欄位"),
            ("notes.srt", b64("1\n00:00:00,000 --> 00:00:01,000\n你好\n"), "不接受 .srt"),
        ]
        for filename, payload, needle in cases:
            with self.assertRaises(db.ValidationError) as cm:
                transcripts.import_transcript(
                    self.conn, project_id=proj["project_id"], filename=filename,
                    content_base64=payload, operator="測試員")
            self.assertTrue(any(needle in err for err in cm.exception.errors), cm.exception.errors)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)


class TestDraftsAndLedger(Base):
    def import_json(self, project_id):
        return transcripts.import_transcript(
            self.conn, project_id=project_id, filename="meeting.json",
            content_base64=b64(self.load("meeting.json")), operator="測試員")

    def test_rules_do_not_promote_proposals_questions_or_negations(self):
        proj = self.project()
        result = self.import_json(proj["project_id"])
        kinds = {(d["kind"], d["source_quote"]) for d in result["drafts"]}
        self.assertIn(("決議", "決議：示範用 RTU 盤預留一組通訊模組空間。"), kinds)
        self.assertIn(("待辦", "請林示範於 2026-12-01 前補上端子對照。"), kinds)
        joined = "\n".join(d["source_quote"] for d in result["drafts"])
        for blocked in ("建議採用", "是否同意", "尚未決定", "不採用", "也許不夠"):
            self.assertNotIn(blocked, joined)
        self.assertEqual(db.list_issues(self.conn), [])
        decision = next(d for d in result["drafts"] if d["kind"] == "決議")
        self.assertEqual(decision["owner"], "")
        self.assertEqual(decision["owner_origin"], "未指定")
        self.assertEqual(decision["due_date"], "")
        self.assertEqual(decision["due_origin"], "未指定")
        todo = next(d for d in result["drafts"] if d["kind"] == "待辦")
        self.assertEqual((todo["owner"], todo["owner_origin"]), ("林示範", "原文"))
        self.assertEqual((todo["due_date"], todo["due_origin"]), ("2026-12-01", "原文"))

    def test_confirm_keeps_quote_and_duplicate_confirm_is_noop(self):
        proj = self.project()
        result = self.import_json(proj["project_id"])
        draft = next(d for d in result["drafts"] if d["kind"] == "決議")
        edited = transcripts.update_draft(self.conn, draft["draft_id"], {"summary": "人工改寫的摘要"})
        self.assertEqual(edited["source_quote"], draft["source_quote"])
        self.assertEqual(edited["summary"], "人工改寫的摘要")
        first = transcripts.confirm_draft(self.conn, draft["draft_id"], "測試員")
        self.assertTrue(first["created"])
        issue = first["issue"]
        self.assertEqual(issue["title"], "人工改寫的摘要")
        self.assertEqual(issue["source_excerpt"], draft["source_quote"])
        self.assertEqual(issue["source_quote"], draft["source_quote"])
        self.assertEqual(issue["source_file"], "meeting.json")
        self.assertIn("段落 1", issue["source_location"])
        self.assertIn("00:00:00.000", issue["source_location"])
        self.assertEqual(issue["project_id"], proj["project_id"])
        self.assertEqual(issue["owner"], "")
        self.assertEqual(issue["due_date"], "")
        segment = transcripts.get_segment(self.conn, issue["transcript_id"], issue["segment_ref"])
        self.assertEqual(segment["raw_text"], draft["source_quote"])
        updated, _, diff = db.update_issue(self.conn, issue["issue_id"], {"title": "再改標題"}, "測試員")
        self.assertEqual(diff, ["title"])
        self.assertEqual(updated["source_quote"], draft["source_quote"])
        self.assertEqual(updated["source_excerpt"], draft["source_quote"])
        second = transcripts.confirm_draft(self.conn, draft["draft_id"], "測試員")
        self.assertFalse(second["created"])
        self.assertEqual(len(db.list_issues(self.conn)), 1)

    def test_discard_and_unconfirmed_stay_out_of_the_ledger(self):
        proj = self.project()
        result = self.import_json(proj["project_id"])
        todo = next(d for d in result["drafts"] if d["kind"] == "待辦")
        transcripts.discard_draft(self.conn, todo["draft_id"])
        again = transcripts.discard_draft(self.conn, todo["draft_id"])
        self.assertEqual(again["draft"]["review_status"], "已捨棄")
        self.assertEqual(db.list_issues(self.conn), [])
        with self.assertRaises(db.ValidationError):
            transcripts.confirm_draft(self.conn, todo["draft_id"], "測試員")
        self.assertEqual(db.list_issues(self.conn), [])

    def test_same_content_is_not_imported_twice(self):
        proj = self.project()
        first = self.import_json(proj["project_id"])
        second = self.import_json(proj["project_id"])
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["created"])
        self.assertEqual(second["transcript_id"], first["transcript_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], len(first["drafts"]))

    def test_projects_do_not_share_issues_or_links(self):
        a = self.project("合成專案甲")
        b = self.project("合成專案乙")
        imported = self.import_json(a["project_id"])
        other = transcripts.import_transcript(
            self.conn, project_id=b["project_id"], filename="meeting.json",
            content_base64=b64(self.load("meeting.json")), operator="測試員")
        self.assertNotEqual(imported["transcript_id"], other["transcript_id"])
        draft = next(d for d in imported["drafts"] if d["kind"] == "決議")
        issue = transcripts.confirm_draft(self.conn, draft["draft_id"], "測試員")["issue"]
        notebook.create_link(self.conn, issue["issue_id"], {
            "notebook": "合成筆記", "source_id": "3", "chunk_id": "9",
            "filename": "engineering-note.txt", "snippet": "通訊模組預留",
            "line_start": 3, "line_end": 3, "version": "不該被保存", "chapter": "也不該被保存",
            "endorsement": "支持",
        })
        links_a = notebook.list_links(self.conn, issue["issue_id"])
        self.assertEqual(len(links_a), 1)
        self.assertEqual(links_a[0]["endorsement"], "未主張支持")
        self.assertFalse(links_a[0]["supports_decision"])
        self.assertNotIn("version", links_a[0])
        self.assertEqual(links_a[0]["project_id"], a["project_id"])
        b_draft = next(d for d in other["drafts"] if d["kind"] == "決議")
        issue_b = transcripts.confirm_draft(self.conn, b_draft["draft_id"], "測試員")["issue"]
        self.assertEqual(notebook.list_links(self.conn, issue_b["issue_id"]), [])
        self.assertEqual(
            [i["issue_id"] for i in db.list_issues(self.conn, filters={"project_id": a["project_id"]})],
            [issue["issue_id"]])
        self.assertEqual(
            [i["issue_id"] for i in db.list_issues(self.conn, filters={"project_id": b["project_id"]})],
            [issue_b["issue_id"]])
        again = notebook.create_link(self.conn, issue["issue_id"], {
            "notebook": "合成筆記", "source_id": "3", "chunk_id": "9", "filename": "engineering-note.txt",
        })
        self.assertFalse(again["created"])
        self.assertEqual(len(notebook.list_links(self.conn, issue["issue_id"])), 1)

    def test_negation_and_cancellation_do_not_become_drafts(self):
        segments = []
        for i, text in enumerate((
            "本案未核准。",
            "這不是決議。",
            "目前沒有待辦。",
            "待辦已取消。",
            "決議：示範用通訊模組維持原方案。",
            "待辦：請林示範於 2026-12-01 前補上端子對照。",
        ), start=1):
            segments.append({
                "source_segment_id": str(i),
                "text": text,
                "location_label": f"段落 {i}",
                "timestamp_label": "無時間資訊",
            })
        found = transcripts.extract_candidates(segments)
        quotes = [item["source_quote"] for item in found]
        for blocked in ("本案未核准。", "這不是決議。", "目前沒有待辦。", "待辦已取消。"):
            self.assertNotIn(blocked, quotes)
        self.assertEqual(
            [(item["kind"], item["source_quote"]) for item in found],
            [
                ("決議", "決議：示範用通訊模組維持原方案。"),
                ("待辦", "待辦：請林示範於 2026-12-01 前補上端子對照。"),
            ],
        )

    def test_traced_issue_cannot_change_project_and_links_stay(self):
        origin = self.project("合成專案甲")
        other = self.project("合成專案乙")
        imported = self.import_json(origin["project_id"])
        draft = next(d for d in imported["drafts"] if d["kind"] == "決議")
        issue = transcripts.confirm_draft(self.conn, draft["draft_id"], "測試員")["issue"]
        notebook.create_link(self.conn, issue["issue_id"], {
            "notebook": "合成筆記", "source_id": "3", "chunk_id": "9",
            "filename": "engineering-note.txt", "snippet": "通訊模組預留",
        })
        self.assertTrue(db.project_move_blocked(self.conn, issue))
        before_projects = db.list_projects(self.conn)
        with self.assertRaises(db.ValidationError) as cm:
            db.update_issue(self.conn, issue["issue_id"], {"project_id": other["project_id"]}, "測試員")
        self.assertIn("不能直接改掛", cm.exception.errors[0])
        with self.assertRaises(db.ValidationError):
            db.update_issue(self.conn, issue["issue_id"], {"project": "合成專案丙"}, "測試員")
        self.assertEqual(
            [p["name"] for p in db.list_projects(self.conn)],
            [p["name"] for p in before_projects],
        )
        stayed = db.get_issue(self.conn, issue["issue_id"])
        self.assertEqual(stayed["project_id"], origin["project_id"])
        self.assertEqual(stayed["project"], origin["name"])
        links = notebook.list_links(self.conn, issue["issue_id"])
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["filename"], "engineering-note.txt")
        self.assertEqual(links[0]["project_id"], origin["project_id"])
        renamed = db.rename_project(self.conn, origin["project_id"], "合成專案甲改名", "測試員")
        stayed = db.get_issue(self.conn, issue["issue_id"])
        self.assertEqual(stayed["project_id"], origin["project_id"])
        self.assertEqual(stayed["project"], renamed["name"])
        self.assertEqual(notebook.list_links(self.conn, issue["issue_id"])[0]["filename"], "engineering-note.txt")
        editable, _, diff = db.update_issue(self.conn, issue["issue_id"], {"title": "只改標題"}, "測試員")
        self.assertEqual(diff, ["title"])
        self.assertEqual(editable["project_id"], origin["project_id"])

        plain = db.create_issue(
            self.conn, {"title": "沒有追溯", "project_id": origin["project_id"]}, "測試員")[0]
        self.assertFalse(db.project_move_blocked(self.conn, plain))
        moved, _, diff = db.update_issue(
            self.conn, plain["issue_id"], {"project_id": other["project_id"]}, "測試員")
        self.assertIn("project", diff)
        self.assertEqual(moved["project_id"], other["project_id"])

    def test_locate_rejects_other_database_and_keeps_filename(self):
        proj = self.project()
        issue = db.create_issue(self.conn, {"title": "要關聯的事項", "project_id": proj["project_id"]}, "測試員")[0]
        original = self.dir / "copy.txt"
        original.write_text("合成原文", encoding="utf-8")
        decoy = self.dir / "decoy.txt"
        decoy.write_text("另一份文件", encoding="utf-8")
        good = self.dir / "good.db"
        wrong = self.dir / "wrong.db"
        same_name = self.dir / "same-name.db"
        _write_notebook_db(
            good, filename="engineering-note.txt", file_hash="a" * 64,
            original_path=str(original), notebook="合成筆記",
        )
        _write_notebook_db(
            wrong, filename="other-note.txt", file_hash="b" * 64,
            original_path=str(decoy), notebook="另一本筆記",
        )
        _write_notebook_db(
            same_name, filename="engineering-note.txt", file_hash="c" * 64,
            original_path=str(decoy), notebook="合成筆記",
        )
        created = notebook.create_link(self.conn, issue["issue_id"], {
            "notebook": "合成筆記", "source_id": "1", "chunk_id": "2",
            "filename": "engineering-note.txt", "snippet": "通訊模組預留",
            "file_hash": "client-supplied-fake", "line_start": 3, "line_end": 3,
        }, notebook_db=str(good))
        self.assertTrue(created["created"])
        self.assertEqual(created["link"]["file_hash"], "a" * 64)
        self.assertEqual(created["link"]["filename"], "engineering-note.txt")
        link = created["link"]
        matched = notebook.locate_link(str(good), link)
        self.assertEqual(matched["status"], "可定位")
        self.assertEqual(matched["filename"], "engineering-note.txt")
        self.assertEqual(matched["original_path"], str(original))
        mismatched = notebook.locate_link(str(wrong), link)
        self.assertEqual(mismatched["status"], "無法驗證")
        self.assertEqual(mismatched["filename"], "engineering-note.txt")
        self.assertNotIn("original_path", mismatched)
        self.assertNotIn("other-note.txt", mismatched["message"])
        self.assertEqual(mismatched["notebook"], "合成筆記")
        same_filename = notebook.locate_link(str(same_name), link)
        self.assertEqual(same_filename["status"], "無法驗證")
        self.assertEqual(same_filename["filename"], "engineering-note.txt")
        self.assertNotIn("original_path", same_filename)
        stored = notebook.list_links(self.conn, issue["issue_id"])[0]
        self.assertEqual(stored["filename"], "engineering-note.txt")
        self.assertEqual(stored["file_hash"], "a" * 64)
        with self.assertRaises(db.ValidationError):
            notebook.create_link(self.conn, issue["issue_id"], {
                "notebook": "合成筆記", "source_id": "1", "chunk_id": "2",
                "filename": "other-note.txt",
            }, notebook_db=str(good))
        self.assertEqual(len(notebook.list_links(self.conn, issue["issue_id"])), 1)

    def test_search_outage_and_bad_import_keep_existing_rows(self):
        proj = self.project()
        issue = db.create_issue(self.conn, {"title": "既有事項", "project_id": proj["project_id"]}, "測試員")[0]
        before = db.get_issue(self.conn, issue["issue_id"])
        with self.assertRaises(notebook.NotebookUnavailable):
            notebook.search("http://127.0.0.1:9", "合成筆記", "通訊模組")
        with self.assertRaises(notebook.NotebookUnavailable):
            notebook.list_notebooks("http://example.com/search")
        self.assertEqual(db.get_issue(self.conn, issue["issue_id"])["title"], before["title"])
        self.assertEqual(db.get_issue(self.conn, issue["issue_id"])["updated_at"], before["updated_at"])
        located = notebook.locate_link("", {
            "link_id": "LNK-0000", "filename": "x.txt", "notebook": "n", "source_id": "1",
            "chunk_id": "1", "page": "", "line_start": "", "line_end": "", "timestamp": "",
        })
        self.assertEqual(located["status"], "無法驗證")
        self.assertEqual(db.get_issue(self.conn, issue["issue_id"])["title"], "既有事項")


class TestMigration(Base):
    def test_legacy_rows_gain_project_id_without_losing_fields(self):
        self.conn.close()
        self.db_path.unlink()
        legacy = sqlite3.connect(self.db_path)
        legacy.executescript("""
            CREATE TABLE issues (
                issue_id TEXT PRIMARY KEY,
                project TEXT NOT NULL DEFAULT '',
                system TEXT NOT NULL DEFAULT '',
                issue_type TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                source_location TEXT NOT NULL DEFAULT '',
                source_excerpt TEXT NOT NULL DEFAULT '',
                source_status TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '',
                due_date TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                drawing_no TEXT NOT NULL DEFAULT '',
                drawing_rev TEXT NOT NULL DEFAULT '',
                model_file TEXT NOT NULL DEFAULT '',
                model_element_id TEXT NOT NULL DEFAULT '',
                acceptance_criteria TEXT NOT NULL DEFAULT '',
                acceptance_result TEXT NOT NULL DEFAULT '',
                acceptance_evidence TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_sample INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE issue_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                operator TEXT NOT NULL,
                action TEXT NOT NULL,
                field TEXT NOT NULL,
                old_value TEXT NOT NULL,
                new_value TEXT NOT NULL
            );
        """)
        legacy.execute(
            """INSERT INTO issues(
                issue_id, project, issue_type, title, source_status, priority, status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("SDT-0007", "舊專案", "待確認", "遷移前事項", "缺乏證據", "中", "待處理",
             "2026-01-01 00:00:00", "2026-01-02 00:00:00"))
        legacy.execute(
            """INSERT INTO issues(
                issue_id, project, issue_type, title, source_status, priority, status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("SDT-0008", "舊專案", "待確認", "同一專案的另一筆", "缺乏證據", "中", "待處理",
             "2026-01-01 00:00:00", "2026-01-02 00:00:00"))
        legacy.execute(
            """INSERT INTO issue_history(issue_id, changed_at, operator, action, field, old_value, new_value)
               VALUES ('SDT-0007','2026-01-01 00:00:00','舊操作者','create','title','','遷移前事項')""")
        legacy.commit()
        legacy.close()
        self.conn = db.connect(self.db_path)
        kept = db.get_issue(self.conn, "SDT-0007")
        other = db.get_issue(self.conn, "SDT-0008")
        self.assertEqual(kept["title"], "遷移前事項")
        self.assertEqual(kept["project"], "舊專案")
        self.assertTrue(kept["project_id"].startswith("PRJ-"))
        self.assertEqual(kept["project_id"], other["project_id"])
        self.assertEqual(db.get_history(self.conn, "SDT-0007")[0]["operator"], "舊操作者")
        renamed = db.rename_project(self.conn, kept["project_id"], "新顯示名稱", "測試員")
        self.assertEqual(renamed["project_id"], kept["project_id"])
        self.assertEqual(db.get_issue(self.conn, "SDT-0007")["project"], "新顯示名稱")
        self.assertEqual(db.get_issue(self.conn, "SDT-0007")["project_id"], kept["project_id"])
        self.assertEqual(db.get_issue(self.conn, "SDT-0008")["project_id"], kept["project_id"])


class TestClientShape(unittest.TestCase):
    def test_search_result_drops_unknown_fields(self):
        hit = notebook._hit({
            "chunk_id": 4, "seq": 1, "page": None, "line_start": 3, "line_end": 5,
            "timestamp": None, "text": "通訊模組預留" * 40, "source_file": "engineering-note.txt",
            "source_id": 2, "notebook": "合成筆記", "snippet": "通訊模組預留", "score": -0.2,
            "version": "Z", "chapter": "第 9 章",
        })
        self.assertEqual(hit["page"], "")
        self.assertEqual(hit["timestamp"], "")
        self.assertEqual(hit["line_start"], "3")
        self.assertNotIn("version", hit)
        self.assertNotIn("chapter", hit)
        self.assertEqual(hit["snippet"], "通訊模組預留")


class TestHttpFlow(unittest.TestCase):
    def setUp(self):
        import threading
        from sdt import config, server
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.Config(operator="測試員", port=0, data_dir=pathlib.Path(self.tmp.name))
        self.srv = server.make_server(cfg, port=0, today_fn=lambda: "2026-10-16")
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = None if body is None else json.dumps(body).encode("utf-8")
        c.request(method, path, body=data, headers={
            "Host": f"127.0.0.1:{self.port}", "X-SDT": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        payload = json.loads(r.read() or b"{}")
        c.close()
        return r.status, payload

    def test_preview_import_confirm_and_reread(self):
        s, proj = self.req("POST", "/api/projects", {"name": "合成專案甲"})
        self.assertEqual(s, 201)
        pid = proj["project"]["project_id"]
        payload = {"project_id": pid, "filename": "meeting.json",
                   "content_base64": b64((EXAMPLES / "meeting.json").read_bytes())}
        s, preview = self.req("POST", "/api/transcripts/preview", payload)
        self.assertEqual((s, preview["segment_count"], preview["timestamp_status"]), (200, 6, "齊全"))
        s, denied = self.req("POST", "/api/transcripts/import", payload)
        self.assertEqual(s, 400)
        self.assertIn("預覽", denied["errors"][0])
        payload["confirm"] = True
        s, imported = self.req("POST", "/api/transcripts/import", payload)
        self.assertEqual(s, 200)
        self.assertTrue(imported["created"])
        s, issues = self.req("GET", "/api/issues")
        self.assertEqual(issues["items"], [])
        draft = next(d for d in imported["drafts"] if d["kind"] == "決議")
        s, confirmed = self.req("POST", f"/api/drafts/{draft['draft_id']}/confirm", {})
        self.assertEqual(s, 200)
        issue_id = confirmed["issue"]["issue_id"]
        s, segment = self.req(
            "GET",
            f"/api/transcripts/{confirmed['issue']['transcript_id']}/segments/{confirmed['issue']['segment_ref']}")
        self.assertEqual(segment["raw_text"], draft["source_quote"])
        s, again = self.req("POST", f"/api/drafts/{draft['draft_id']}/confirm", {})
        self.assertFalse(again["created"])
        s, issues = self.req("GET", f"/api/issues?project_id={pid}")
        self.assertEqual([i["issue_id"] for i in issues["items"]], [issue_id])
        s, dup = self.req("POST", "/api/transcripts/import", payload)
        self.assertTrue(dup["duplicate"])
        s, offline = self.req("GET", "/api/notebook/search?notebook=x&q=abc")
        self.assertEqual(s, 503)
        self.assertTrue(offline["errors"])
        s, still = self.req("GET", f"/api/issues/{issue_id}")
        self.assertEqual(still["title"], confirmed["issue"]["title"])


if __name__ == "__main__":
    unittest.main()
