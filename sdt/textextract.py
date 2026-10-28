"""把會議紀錄檔（.docx .xlsx .csv .md）轉成逐行文字，之後走和 TXT 相同的解析。

只用標準函式庫：docx、xlsx 是 zip 內的 XML，直接讀取。轉出來的文字一行對應一個段落或一列。
"""
import csv
import io
import pathlib
import re
import zipfile
from xml.etree import ElementTree as ET

from . import csvio, db

TABULAR_SUFFIXES = {".docx", ".xlsx", ".csv", ".md", ".markdown"}
LEGACY_SUFFIXES = {".doc": ".docx", ".xls": ".xlsx"}
MAX_PART_BYTES = 30 * 1024 * 1024

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _open_zip(raw, kind):
    try:
        return zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise db.ValidationError([f"檔案不是有效的 {kind}，請用 Word／Excel 重新另存後再匯入"])


def _read_xml(zf, name):
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise db.ValidationError(["檔案內容過大，無法解析"])
    try:
        return ET.fromstring(zf.read(info))
    except ET.ParseError:
        raise db.ValidationError(["檔案內部 XML 損毀，請重新另存後再匯入"])


def _para_text(p):
    parts = []
    for node in p.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag in (f"{_W}tab", f"{_W}br", f"{_W}cr"):
            parts.append(" ")
    return "".join(parts).strip()


def _cell_text(tc):
    return " ".join(t for t in (_para_text(p) for p in tc.findall(f"{_W}p")) if t)


def _docx_lines(raw):
    zf = _open_zip(raw, "Word .docx")
    root = _read_xml(zf, "word/document.xml")
    if root is None:
        raise db.ValidationError(["找不到 Word 內文，請確認是 .docx 檔"])
    body = root.find(f"{_W}body")
    lines = []
    for child in (body if body is not None else []):
        if child.tag == f"{_W}p":
            lines.append(_para_text(child))
        elif child.tag == f"{_W}tbl":
            for tr in child.iter(f"{_W}tr"):
                cells = [_cell_text(tc) for tc in tr.findall(f"{_W}tc")]
                lines.append(" ".join(c for c in cells if c))
    return lines


def _shared_strings(zf):
    root = _read_xml(zf, "xl/sharedStrings.xml")
    if root is None:
        return []
    return ["".join(t.text or "" for t in si.iter(f"{_S}t")) for si in root.findall(f"{_S}si")]


def _sheet_parts(zf):
    """依活頁簿順序回傳 [(工作表名, 檔案路徑)]；讀不到對應時退回檔名順序。"""
    wb = _read_xml(zf, "xl/workbook.xml")
    rels = _read_xml(zf, "xl/_rels/workbook.xml.rels")
    targets = {}
    if rels is not None:
        for rel in rels.findall(f"{_PR}Relationship"):
            target = rel.get("Target", "")
            targets[rel.get("Id")] = target.lstrip("/") if target.startswith("/") else "xl/" + target
    out = []
    if wb is not None:
        for sheet in wb.iter(f"{_S}sheet"):
            path = targets.get(sheet.get(f"{_R}id"))
            if path and path in zf.namelist():
                out.append((sheet.get("name") or "", path))
    if out:
        return out
    names = sorted(n for n in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    return [("", n) for n in names]


def _xlsx_lines(raw):
    zf = _open_zip(raw, "Excel .xlsx")
    strings = _shared_strings(zf)
    sheets = _sheet_parts(zf)
    if not sheets:
        raise db.ValidationError(["找不到 Excel 工作表，請確認是 .xlsx 檔"])
    lines = []
    for name, path in sheets:
        root = _read_xml(zf, path)
        if root is None:
            continue
        rows = []
        for row in root.iter(f"{_S}row"):
            cells = []
            for c in row.findall(f"{_S}c"):
                kind = c.get("t")
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(f"{_S}t"))
                else:
                    v = c.find(f"{_S}v")
                    value = v.text if v is not None and v.text else ""
                    if kind == "s" and value.isdigit() and int(value) < len(strings):
                        value = strings[int(value)]
                value = value.strip()
                if value:
                    cells.append(value)
            if cells:
                rows.append(" ".join(cells))
        if rows and len(sheets) > 1:
            lines.append(f"【工作表：{name}】")
        lines.extend(rows)
    return lines


def _csv_lines(raw):
    text, _ = csvio.decode(raw)
    try:
        return [" ".join(c.strip() for c in row if c.strip()) for row in csv.reader(io.StringIO(text, newline=""))]
    except csv.Error:
        raise db.ValidationError(["CSV 格式錯誤，請檢查引號與欄位"])


_MD_LEAD = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s+)?)")
_MD_RULE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _md_lines(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise db.ValidationError(["Markdown 不是有效的 UTF-8 編碼，請另存為 UTF-8 後再匯入"])
    lines = []
    for line in text.splitlines():
        if "|" in line and _MD_RULE.match(line) and "-" in line:
            continue
        line = _MD_LEAD.sub("", line)
        if line.lstrip().startswith("|"):
            line = " ".join(c.strip() for c in line.strip().strip("|").split("|") if c.strip())
        lines.append(line.replace("**", "").replace("__", "").strip())
    return lines


_EXTRACTORS = {
    ".docx": _docx_lines,
    ".xlsx": _xlsx_lines,
    ".csv": _csv_lines,
    ".md": _md_lines,
    ".markdown": _md_lines,
}


def suffix_of(filename):
    return pathlib.PurePath(filename or "").suffix.lower()


def extract_text(filename, raw):
    """回傳逐行文字；沒有任何文字時回報空檔。"""
    suffix = suffix_of(filename)
    if suffix in LEGACY_SUFFIXES:
        raise db.ValidationError([f"不支援舊版 {suffix}，請在 Office 另存為 {LEGACY_SUFFIXES[suffix]} 後再匯入"])
    lines = [ln for ln in _EXTRACTORS[suffix](raw) if ln.strip()]
    if not lines:
        raise db.ValidationError(["檔案是空的，沒有可匯入的文字"])
    return "\n".join(lines)
