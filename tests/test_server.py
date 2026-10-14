"""HTTP 層測試：在隨機埠啟動真實伺服器，經 127.0.0.1 呼叫 API。"""
import http.client
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.parse

from sdt import config, server

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


if __name__ == "__main__":
    unittest.main()
