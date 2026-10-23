"""回歸測試：錯誤輸入不可截斷資料、造成 500 或等待負長度讀取。"""
import io
import json
import pathlib
import tempfile
import unittest
from email.message import Message
from types import SimpleNamespace

from sdt import csvio, db, server


class TestCsvStructure(unittest.TestCase):
    def test_invalid_structure_rejects_whole_import(self):
        cases = (
            '標題,title\n第一欄,被覆蓋的標題\n',
            '標題,描述\n正常,保留\n錯誤,描述,多出的內容\n',
            '標題,描述\n正常,保留\n錯誤\n',
            '標題,描述\n正常,保留\n錯誤,"未結束\n',
        )
        with tempfile.TemporaryDirectory() as tmp:
            with db.session(pathlib.Path(tmp) / 'test.db') as conn:
                original, _ = db.create_issue(conn, {'title': '原有事項'}, '測試')
                history = db.get_history(conn)
                for text in cases:
                    with self.subTest(text=text):
                        with self.assertRaises(db.ValidationError):
                            csvio.import_issues(conn, text.encode('utf-8'), '測試')
                        self.assertEqual([r['issue_id'] for r in db.list_issues(conn)],
                                         [original['issue_id']])
                        self.assertEqual(db.get_history(conn), history)


class TestHttpInput(unittest.TestCase):
    def handler(self, body=b'', length=None):
        handler_class = server.make_handler(SimpleNamespace(today=lambda: '2026-10-22'))
        handler = handler_class.__new__(handler_class)
        handler.headers = Message()
        handler.headers['Content-Length'] = str(len(body)) if length is None else length
        handler.rfile = io.BytesIO(body)
        return handler

    def test_invalid_json_returns_400_through_dispatch(self):
        for body in (b'null', b'[]', b'"text"', b'123', b'{invalid'):
            with self.subTest(body=body):
                handler = self.handler(body)
                handler.headers['Host'] = '127.0.0.1:8765'
                handler.headers['X-SDT'] = '1'
                handler.server = SimpleNamespace(server_address=('127.0.0.1', 8765))
                handler.path = '/api/issues'
                responses = []
                handler._json = lambda status, payload: responses.append((status, payload))
                handler._dispatch('POST')
                self.assertEqual(responses[0][0], 400)
                self.assertTrue(responses[0][1]['errors'])

    def test_json_requires_object(self):
        for value in (None, [], '文字', 12, True):
            with self.subTest(value=value):
                handler = self.handler(json.dumps(value).encode('utf-8'))
                with self.assertRaises(db.ValidationError):
                    handler._json_body()
        self.assertEqual(self.handler(b'{"title":"ok"}')._json_body(), {'title': 'ok'})
        self.assertEqual(self.handler()._json_body(), {})

    def test_invalid_lengths_are_rejected_before_read(self):
        class Unreadable:
            def read(self, size):
                raise AssertionError('不應讀取無效長度')
        for length in ('-1', 'abc', '1.0', '+2', '20971521', '9' * 5000):
            with self.subTest(length=length):
                handler = self.handler(length=length)
                handler.rfile = Unreadable()
                with self.assertRaises(db.ValidationError):
                    handler._body()

    def test_ambiguous_framing_rejected(self):
        handler = self.handler(b'{}')
        handler.headers['Content-Length'] = '2'
        with self.assertRaises(db.ValidationError):
            handler._body()
        handler = self.handler(b'{}')
        handler.headers['Transfer-Encoding'] = 'chunked'
        with self.assertRaises(db.ValidationError):
            handler._body()


if __name__ == '__main__':
    unittest.main()
