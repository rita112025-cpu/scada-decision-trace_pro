"""事項編號永久保留，以及議程／CSV 的專案範圍。"""
import http.client
import json
import pathlib
import tempfile
import threading
import unittest

from sdt import agenda, config, csvio, db, server

TODAY = "2026-10-14"


def issue(**kw):
    base = {"title": "測試事項", "issue_type": "會議決議", "priority": "高", "status": "待處理"}
    base.update(kw)
    return base


class TestIssueIdNeverReused(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "t.db"
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, conn=None, **kw):
        return db.create_issue(conn or self.conn, issue(**kw), "u")[0]["issue_id"]

    def test_delete_last_then_add_gets_new_id(self):
        ids = [self.add() for _ in range(3)]
        db.delete_issue(self.conn, ids[-1], "u")
        self.assertEqual(self.add(), "SDT-0004")

    def test_consecutive_tail_deletes_do_not_reuse(self):
        ids = [self.add() for _ in range(5)]
        for i in ids[2:]:
            db.delete_issue(self.conn, i, "u")
        self.assertEqual(self.add(), "SDT-0006")

    def test_restart_keeps_counter(self):
        ids = [self.add() for _ in range(3)]
        db.delete_issue(self.conn, ids[-1], "u")
        self.conn.commit()
        self.conn.close()
        self.conn = db.connect(self.path)
        self.assertEqual(self.add(), "SDT-0004")

    def test_clear_sample_does_not_reuse(self):
        self.add()
        db.create_issue(self.conn, issue(), "u", is_sample=True, action="sample")
        db.clear_sample(self.conn, "u")
        self.assertEqual(self.add(), "SDT-0003")

    def test_csv_cannot_recreate_deleted_id(self):
        ids = [self.add() for _ in range(2)]
        db.delete_issue(self.conn, ids[-1], "u")
        data = csvio.export_issues([dict(issue(), issue_id="SDT-0002")])
        with self.assertRaises(db.ValidationError) as ctx:
            csvio.import_issues(self.conn, data, "u")
        self.assertIn("曾經使用過", " ".join(ctx.exception.errors))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0], 1)

    def test_explicit_new_id_advances_counter(self):
        db.create_issue(self.conn, issue(), "u", issue_id="SDT-0050")
        self.assertEqual(self.add(), "SDT-0051")

    def test_migration_counts_history_of_deleted_ids(self):
        self.add()
        self.conn.execute("INSERT INTO issue_history(issue_id, changed_at, operator, action, field,"
                          " old_value, new_value) VALUES ('SDT-0009','2026-01-01 00:00:00','u','delete','title','x','')")
        self.conn.execute("DROP TABLE id_counters")
        self.conn.execute("PRAGMA user_version=1")
        self.conn.commit()
        self.conn.close()
        self.conn = db.connect(self.path)
        self.assertEqual(self.add(), "SDT-0010")

    def test_upgrade_from_version_2_without_retired_table(self):
        # 版本 2（81814e6）的資料庫：有 id_counters，沒有 retired_issue_ids
        ids = [self.add() for _ in range(3)]
        db.delete_issue(self.conn, ids[-1], "u")
        self.conn.execute("DROP TABLE retired_issue_ids")
        self.conn.execute("PRAGMA user_version=2")
        self.conn.commit()
        issues_before = [dict(r) for r in self.conn.execute("SELECT * FROM issues ORDER BY issue_id")]
        history_before = db.get_history(self.conn)
        self.conn.close()
        self.conn = db.connect(self.path)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(db.SCHEMA_VERSION, 3)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='retired_issue_ids'").fetchone())
        self.assertEqual([dict(r) for r in self.conn.execute("SELECT * FROM issues ORDER BY issue_id")],
                         issues_before)
        self.assertEqual(db.get_history(self.conn), history_before)
        self.assertEqual(self.add(), "SDT-0004")
        db.wipe_all(self.conn)
        self.assertEqual(self.add(), "SDT-0005")

    def test_reused_history_is_reported_not_rewritten(self):
        self.add()
        self.add()
        db.delete_issue(self.conn, "SDT-0002", "u")
        before = db.get_history(self.conn)
        # 模擬舊版行為造成的重用：刪除後又出現同編號的建立紀錄
        db._log(self.conn, "SDT-0002", "2026-01-02 00:00:00", "u", "create", "title", "", "新事項")
        self.assertEqual(db.reused_issue_ids(self.conn), ["SDT-0002"])
        self.assertEqual(db.get_history(self.conn)[:len(before)], before)

    def test_normal_history_not_reported(self):
        self.add()
        db.delete_issue(self.conn, "SDT-0001", "u")
        self.add()
        self.assertEqual(db.reused_issue_ids(self.conn), [])

    def _cli_cfg(self):
        cfg = config.Config(operator="u", port=0, data_dir=pathlib.Path(self.tmp.name) / "cli")
        cfg.data_dir.mkdir(exist_ok=True)
        return cfg

    def _run_check_ids(self, cfg):
        import contextlib
        import io
        from sdt import __main__ as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.cmd_check_ids(cfg, None)
        return code, out.getvalue()

    def _seed_reused(self, cfg, action):
        with db.session(cfg.db_path) as conn:
            db.create_issue(conn, issue(), "u")
            db.create_issue(conn, issue(), "u")
            db.delete_issue(conn, "SDT-0002", "u")
            # 模擬舊版行為：刪除後同一編號又被配給新事項
            db._log(conn, "SDT-0002", "2026-01-02 00:00:00", "u", action, "title", "", "新事項")

    def test_check_ids_cli_reports_reuse(self):
        cfg = self._cli_cfg()
        self._seed_reused(cfg, "create")
        code, out = self._run_check_ids(cfg)
        self.assertEqual(code, 0)
        self.assertIn("SDT-0002", out)

    def test_check_ids_reports_confirm_draft_reuse(self):
        cfg = self._cli_cfg()
        self._seed_reused(cfg, "confirm-draft")
        self.assertIn("SDT-0002", self._run_check_ids(cfg)[1])

    def test_check_ids_does_not_touch_old_database(self):
        import hashlib
        cfg = self._cli_cfg()
        self._seed_reused(cfg, "create")
        with db.session(cfg.db_path) as conn:
            conn.execute("DROP TABLE id_counters")
            conn.execute("DROP TABLE retired_issue_ids")
            conn.execute("PRAGMA user_version=1")
        digest = lambda: hashlib.sha256(cfg.db_path.read_bytes()).hexdigest()
        before = digest()
        code, out = self._run_check_ids(cfg)
        self.assertEqual(code, 0)
        self.assertIn("SDT-0002", out)
        self.assertEqual(digest(), before)
        # 本測試沒有其他連線：SQLite 唯讀開啟可能建立 -wal／-shm 輔助檔，此時新建的 -wal 應為空
        wal = cfg.db_path.with_name(cfg.db_path.name + "-wal")
        if wal.exists():
            self.assertEqual(wal.stat().st_size, 0)
        ro = db.connect_readonly(cfg.db_path)
        try:
            self.assertEqual(ro.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(ro.execute(
                "SELECT 1 FROM sqlite_master WHERE name='id_counters'").fetchone())
        finally:
            ro.close()

    def test_check_ids_missing_database_is_error_and_not_created(self):
        cfg = self._cli_cfg()
        code, out = self._run_check_ids(cfg)
        self.assertEqual(code, 1)
        self.assertIn("找不到資料庫", out)
        self.assertFalse(cfg.db_path.exists())

    def test_wipe_all_keeps_counter(self):
        self.add()
        self.add()
        db.wipe_all(self.conn)
        self.assertEqual(self.add(), "SDT-0003")

    def test_wipe_all_blocks_csv_old_ids(self):
        self.add()
        db.wipe_all(self.conn)
        data = csvio.export_issues([dict(issue(), issue_id="SDT-0001")])
        with self.assertRaises(db.ValidationError) as ctx:
            csvio.import_issues(self.conn, data, "u")
        self.assertIn("曾經使用過", " ".join(ctx.exception.errors))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0], 0)


class TestProjectScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.Config(operator="測試員", port=0, data_dir=pathlib.Path(self.tmp.name))
        self.srv = server.make_server(cfg, port=0, today_fn=lambda: TODAY)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        _, a = self.req("POST", "/api/issues", issue(title="甲案事項", project="甲案"))
        _, b = self.req("POST", "/api/issues", issue(title="乙案事項", project="乙案"))
        self.pa = a["issue"]["project_id"]
        self.pb = b["issue"]["project_id"]

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

    def test_agenda_limited_to_project(self):
        _, r = self.req("GET", f"/api/agenda?project_id={self.pa}")
        md = r["markdown"]
        self.assertIn("甲案事項", md)
        self.assertNotIn("乙案事項", md)
        self.assertIn(f"範圍：甲案（{self.pa}）", md)

    def test_agenda_without_project_is_all_and_labelled(self):
        _, r = self.req("GET", "/api/agenda")
        self.assertIn("甲案事項", r["markdown"])
        self.assertIn("乙案事項", r["markdown"])
        self.assertIn("範圍：全部專案", r["markdown"])

    def test_export_limited_to_project(self):
        s, body, h = self.req("GET", f"/api/export.csv?project_id={self.pb}", raw=True)
        self.assertEqual(s, 200)
        self.assertIn("乙案事項", body)
        self.assertNotIn("甲案事項", body)
        self.assertIn(self.pb, h["Content-Disposition"])

    def test_export_without_project_is_all_and_named(self):
        s, body, h = self.req("GET", "/api/export.csv", raw=True)
        self.assertIn("甲案事項", body)
        self.assertIn("乙案事項", body)
        self.assertIn("-all.csv", h["Content-Disposition"])

    def test_unknown_project_is_404(self):
        self.assertEqual(self.req("GET", "/api/export.csv?project_id=PRJ-9999", raw=True)[0], 404)
        self.assertEqual(self.req("GET", "/api/agenda?project_id=PRJ-9999")[0], 404)


if __name__ == "__main__":
    unittest.main()
