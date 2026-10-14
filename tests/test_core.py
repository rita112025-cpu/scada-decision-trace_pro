"""資料層、CSV、議程、備份的自動化測試（只用標準函式庫 unittest）。"""
import pathlib
import sqlite3
import tempfile
import unittest

from sdt import agenda, backup, csvio, db, sample

TODAY = "2026-10-14"


def issue(**kw):
    base = {"title": "測試事項", "issue_type": "會議決議", "source_status": "缺乏證據",
            "priority": "中", "status": "待處理"}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.db_path = self.dir / "t.db"
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, **kw):
        return db.create_issue(self.conn, issue(**kw), "測試員")[0]


class TestValidation(Base):
    def test_confirmed_without_evidence_is_downgraded(self):
        it, warnings = db.create_issue(self.conn, issue(source_status="已確認", source_file="a.pdf"), "u")
        self.assertEqual(it["source_status"], "缺乏證據")
        self.assertTrue(any("缺乏證據" in w for w in warnings))

    def test_confirmed_with_full_evidence_kept(self):
        it, warnings = db.create_issue(self.conn, issue(
            source_status="已確認", source_file="a.pdf", source_location="p.3", source_excerpt="原文"), "u")
        self.assertEqual(it["source_status"], "已確認")
        self.assertEqual(warnings, [])

    def test_defaults_do_not_claim_confirmation(self):
        it, _ = db.create_issue(self.conn, {"title": "只有標題"}, "u")
        self.assertEqual(it["source_status"], "缺乏證據")
        self.assertEqual(it["issue_type"], "待確認")

    def test_accepted_requires_pass_and_evidence(self):
        with self.assertRaises(db.ValidationError):
            self.add(status="已驗收")
        with self.assertRaises(db.ValidationError):
            self.add(status="已驗收", acceptance_result="通過")
        it = self.add(status="已驗收", acceptance_result="通過", acceptance_evidence="ev/x.pdf")
        self.assertEqual(it["status"], "已驗收")

    def test_invalid_enum_and_date(self):
        with self.assertRaises(db.ValidationError) as cm:
            self.add(status="亂填", due_date="明天")
        self.assertEqual(len(cm.exception.errors), 2)

    def test_excel_style_date_normalized(self):
        self.assertEqual(self.add(due_date="2026/3/5")["due_date"], "2026-03-05")


class TestUpdateAndHistory(Base):
    def test_status_update_records_before_after(self):
        it = self.add()
        updated, _, diff = db.update_issue(self.conn, it["issue_id"], {"status": "處理中"}, "王", now="2026-10-14 10:00:00")
        self.assertEqual(updated["status"], "處理中")
        self.assertEqual(diff, ["status"])
        last = db.get_history(self.conn, it["issue_id"])[-1]
        self.assertEqual((last["field"], last["old_value"], last["new_value"], last["operator"], last["changed_at"]),
                         ("status", "待處理", "處理中", "王", "2026-10-14 10:00:00"))
        self.assertEqual(updated["updated_at"], "2026-10-14 10:00:00")

    def test_no_change_writes_no_history(self):
        it = self.add()
        before = len(db.get_history(self.conn))
        _, _, diff = db.update_issue(self.conn, it["issue_id"], {"status": "待處理"}, "u")
        self.assertEqual(diff, [])
        self.assertEqual(len(db.get_history(self.conn)), before)

    def test_content_update_multiple_fields(self):
        it = self.add(description="舊")
        db.update_issue(self.conn, it["issue_id"], {"description": "新\n第二行", "owner": "林"}, "u")
        fields = {h["field"]: h for h in db.get_history(self.conn, it["issue_id"]) if h["action"] == "update"}
        self.assertEqual(fields["description"]["old_value"], "舊")
        self.assertEqual(fields["description"]["new_value"], "新\n第二行")
        self.assertIn("owner", fields)

    def test_failed_update_changes_nothing(self):
        it = self.add()
        with self.assertRaises(db.ValidationError):
            db.update_issue(self.conn, it["issue_id"], {"status": "已驗收"}, "u")
        self.assertEqual(db.get_issue(self.conn, it["issue_id"])["status"], "待處理")


class TestFiltersAndViews(Base):
    def setUp(self):
        super().setUp()
        self.a = self.add(title="RTU 通訊", priority="高", due_date="2026-10-01", owner="王")
        self.b = self.add(title="光纖路由", priority="低", due_date="2026-12-01", status="等待他人回覆",
                          issue_type="規範要求", source_status="有爭議")
        self.c = self.add(title="已完成", due_date="2026-01-01", status="已驗收",
                          acceptance_result="通過", acceptance_evidence="e.pdf")
        self.d = self.add(title="已關閉逾期", due_date="2026-01-01", status="已關閉")

    def ids(self, **kw):
        return [i["issue_id"] for i in db.list_issues(self.conn, today=TODAY, **kw)]

    def test_overdue_excludes_closed(self):
        self.assertEqual(self.ids(view="overdue"), [self.a["issue_id"]])
        self.assertTrue(db.is_overdue(self.a, TODAY))
        self.assertFalse(db.is_overdue(self.c, TODAY))
        self.assertFalse(db.is_overdue(dict(self.a, due_date=TODAY), TODAY))  # 當天到期不算逾期
        self.assertFalse(db.is_overdue(dict(self.a, due_date=""), TODAY))

    def test_views(self):
        self.assertEqual(set(self.ids(view="open")), {self.a["issue_id"], self.b["issue_id"]})
        self.assertEqual(self.ids(view="accepted"), [self.c["issue_id"]])
        self.assertIn(self.b["issue_id"], self.ids(view="disputed"))

    def test_search_and_filters(self):
        self.assertEqual(self.ids(q="光纖"), [self.b["issue_id"]])
        self.assertEqual(self.ids(filters={"priority": "高"}), [self.a["issue_id"]])
        self.assertEqual(self.ids(filters={"owner": "王"}), [self.a["issue_id"]])
        self.assertEqual(self.ids(q="%"), [])  # LIKE 萬用字元已跳脫

    def test_sort(self):
        ids = self.ids(view="open", sort="priority")
        self.assertEqual(ids[0], self.a["issue_id"])
        ids = self.ids(view="open", sort="due_date", order="desc")
        self.assertEqual(ids[0], self.b["issue_id"])
        self.assertEqual(self.ids(sort="DROP TABLE"), self.ids(sort="due_date"))  # 非白名單欄位退回預設


class TestCsv(Base):
    TRICKY = {
        "title": "含,逗號與\"引號\"",
        "description": "第一行\n第二行，中文全形逗號\n第三行",
        "source_excerpt": "=SUM(A1:A3) 不應被 Excel 執行",
        "owner": "",
        "due_date": "2026-02-28",
        "drawing_no": "DEMO-E-001",
        "drawing_rev": "",
        "model_element_id": "-0042",
    }

    def test_roundtrip_into_new_db(self):
        src = self.add(**self.TRICKY)
        data = csvio.export_issues(db.list_issues(self.conn))
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"), "需有 UTF-8 BOM 才能被中文 Excel 正確辨識")
        other = db.connect(self.dir / "other.db")
        result = csvio.import_issues(other, data, "u")
        self.assertEqual(result["created"], 1)
        dst = db.get_issue(other, src["issue_id"])
        for k in db.EDITABLE_FIELDS + ["created_at"]:
            self.assertEqual(dst[k], src[k], k)
        other.close()

    def test_reimport_same_db_is_noop(self):
        self.add(**self.TRICKY)
        before = len(db.get_history(self.conn))
        r = csvio.import_issues(self.conn, csvio.export_issues(db.list_issues(self.conn)), "u")
        self.assertEqual((r["created"], r["updated"], r["unchanged"]), (0, 0, 1))
        self.assertEqual(len(db.get_history(self.conn)), before)

    def test_formula_cells_are_escaped_in_file(self):
        self.add(**self.TRICKY)
        text = csvio.export_issues(db.list_issues(self.conn)).decode("utf-8-sig")
        self.assertIn("'=SUM", text)

    def test_import_cp950_excel_file(self):
        data = "標題,事項類型,期限\r\n中文測試,圖面問題,2026/1/9\r\n".encode("cp950")
        r = csvio.import_issues(self.conn, data, "u")
        self.assertEqual(r["created"], 1)
        it = db.list_issues(self.conn)[0]
        self.assertEqual((it["title"], it["due_date"]), ("中文測試", "2026-01-09"))

    def test_bad_row_rejects_whole_import(self):
        data = "標題,目前狀態\r\n好的一列,待處理\r\n壞的一列,亂填\r\n".encode("utf-8-sig")
        with self.assertRaises(db.ValidationError) as cm:
            csvio.import_issues(self.conn, data, "u")
        self.assertIn("第 3 列", cm.exception.errors[0])
        self.assertEqual(db.list_issues(self.conn), [])

    def test_import_updates_existing_and_logs(self):
        it = self.add()
        data = f"事項編號,標題,目前狀態\r\n{it['issue_id']},測試事項,處理中\r\n".encode("utf-8-sig")
        r = csvio.import_issues(self.conn, data, "u")
        self.assertEqual(r["updated"], 1)
        self.assertEqual(db.get_history(self.conn, it["issue_id"])[-1]["action"], "import")

    def test_unknown_header_rejected(self):
        with self.assertRaises(db.ValidationError):
            csvio.import_issues(self.conn, "標題,奇怪欄位\r\nx,y\r\n".encode("utf-8"), "u")


class TestAgenda(Base):
    def test_sections_and_no_inferred_decisions(self):
        self.add(title="逾期項", due_date="2026-10-01", status="處理中")
        self.add(title="高優先項", priority="高", due_date="2026-11-01")
        self.add(title="等回覆項", status="等待他人回覆")
        self.add(title="待驗收項", status="待驗收")
        self.add(title="已驗收項", status="已驗收", acceptance_result="通過", acceptance_evidence="e")
        count_before = self.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        hist_before = len(db.get_history(self.conn))
        _, sections = agenda.build_sections(self.conn, TODAY)
        titles = {h: [i["title"] for i in items] for h, items in sections}
        self.assertEqual(titles["一、逾期事項"], ["逾期項"])
        self.assertEqual(titles["二、高優先事項"], ["高優先項"])
        self.assertEqual(titles["三、等待他人回覆"], ["等回覆項"])
        self.assertEqual(titles["四、尚未驗收（待驗收）"], ["待驗收項"])
        md = agenda.build_agenda(self.conn, TODAY)
        self.assertNotIn("已驗收項", md)
        self.assertIn("未推定任何決議", md)
        # 產生議程不得寫入任何資料
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0], count_before)
        self.assertEqual(len(db.get_history(self.conn)), hist_before)


class TestSampleAndBackup(Base):
    def test_sample_load_and_clear_only_samples(self):
        mine = self.add(title="我的真實事項")
        n = sample.load_sample(self.conn, "u", TODAY)
        self.assertGreater(n, 0)
        self.assertEqual(sample.load_sample(self.conn, "u", TODAY), 0)  # 不重複載入
        self.assertEqual(db.clear_sample(self.conn, "u"), n)
        remaining = [i["issue_id"] for i in db.list_issues(self.conn)]
        self.assertEqual(remaining, [mine["issue_id"]])

    def test_sample_rows_valid_and_fictional(self):
        sample.load_sample(self.conn, "u", TODAY)
        for it in db.list_issues(self.conn):
            if it["source_status"] == "已確認":
                self.assertTrue(it["source_file"] and it["source_location"] and it["source_excerpt"])
            for f in ("source_file", "drawing_no", "model_file"):
                if it[f]:
                    self.assertIn("DEMO", it[f])

    def test_restore_backs_up_current_first(self):
        self.add(title="版本一")
        self.conn.commit()
        bdir = self.dir / "backups"
        snap = backup.backup(self.db_path, bdir)
        self.add(title="版本二（還原後應消失，但留在還原前備份）")
        self.conn.commit()
        self.conn.close()
        pre = backup.restore(self.db_path, snap, bdir)
        self.conn = db.connect(self.db_path)
        self.assertEqual([i["title"] for i in db.list_issues(self.conn)], ["版本一"])
        pre_conn = sqlite3.connect(pre)
        self.assertEqual(pre_conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0], 2)
        pre_conn.close()

    def test_restore_rejects_non_backup(self):
        bogus = self.dir / "bogus.db"
        bogus.write_bytes(b"not a database at all" * 100)
        with self.assertRaises(ValueError):
            backup.restore(self.db_path, bogus, self.dir / "backups")


if __name__ == "__main__":
    unittest.main()
