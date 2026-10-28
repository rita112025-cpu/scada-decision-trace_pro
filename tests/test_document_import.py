"""會議紀錄檔（docx／xlsx／csv／md）轉成逐行文字後，和 TXT 走同一條解析。"""
import base64
import io
import unittest
import zipfile

from sdt import db, textextract, transcripts

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def make_docx():
    xml = (f'<w:document {W}><w:body>'
           '<w:p><w:r><w:t>BIM 會議紀錄</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>[00:01:05 --> 00:01:20] 王小明：決議採用方案A。</w:t></w:r></w:p>'
           '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>00:02:00</w:t></w:r></w:p></w:tc>'
           '<w:tc><w:p><w:r><w:t>李大華請於2026-11-05前補圖。</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
           '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_xlsx():
    wb = (f'<workbook {S} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="紀錄" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
    shared = f'<sst {S}><si><t>00:03:00</t></si><si><t>張三確認管線路徑。</t></si></sst>'
    sheet = (f'<worksheet {S}><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
             '<row r="2"><c r="A2" t="inlineStr"><is><t>備註</t></is></c></row></sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", wb)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


class TestDocumentImport(unittest.TestCase):
    def test_docx_paragraphs_and_table_rows(self):
        text = textextract.extract_text("a.docx", make_docx())
        self.assertEqual(text.splitlines(), [
            "BIM 會議紀錄",
            "[00:01:05 --> 00:01:20] 王小明：決議採用方案A。",
            "00:02:00 李大華請於2026-11-05前補圖。",
        ])

    def test_xlsx_rows_use_shared_and_inline_strings(self):
        text = textextract.extract_text("a.xlsx", make_xlsx())
        self.assertEqual(text.splitlines(), ["00:03:00 張三確認管線路徑。", "備註"])

    def test_csv_cp950_and_utf8(self):
        raw = "時間,內容\n00:01,決議採用方案B。\n".encode("cp950")
        self.assertEqual(textextract.extract_text("a.csv", raw).splitlines(),
                         ["時間 內容", "00:01 決議採用方案B。"])

    def test_markdown_markers_removed(self):
        raw = "# 標題\n- 00:01 **王小明**：決議採用方案C。\n| 00:02 | 內容 |\n|---|---|\n".encode("utf-8")
        self.assertEqual(textextract.extract_text("a.md", raw).splitlines(),
                         ["標題", "00:01 王小明：決議採用方案C。", "00:02 內容"])

    def test_bad_inputs_rejected(self):
        for name, raw in (("a.docx", b"not a zip"), ("a.xlsx", b"not a zip"), ("a.doc", b"x"), ("a.csv", b"\n\n")):
            with self.assertRaises(db.ValidationError, msg=name):
                textextract.extract_text(name, raw)

    def test_decode_and_parse_pipeline(self):
        b64 = base64.b64encode(make_docx()).decode()
        text = transcripts.decode_upload(b64, "紀錄.docx")
        _, normalized = transcripts.content_hash(text)
        parsed = transcripts.parse_upload("紀錄.docx", normalized)
        self.assertEqual(parsed["format"], "txt")
        self.assertEqual(len(parsed["segments"]), 3)
        self.assertEqual(parsed["segments"][1]["timestamp_label"] != "無時間資訊", True)


if __name__ == "__main__":
    unittest.main()
