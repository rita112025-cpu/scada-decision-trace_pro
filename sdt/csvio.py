"""CSV 匯入／匯出。匯出用 UTF-8 BOM，讓中文 Windows Excel 直接開啟不亂碼。"""
import csv
import datetime as dt
import io
import re

from . import db

# Excel 會把這些開頭的儲存格當公式執行；匯出時加 ' 前綴，匯入時移除
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t")

_HEADER_TO_KEY = {}
for _key, _label in db.FIELDS:
    _HEADER_TO_KEY[_key] = _key
    _HEADER_TO_KEY[_label] = _key


def _escape(value):
    value = value or ""
    if value.startswith(_FORMULA_PREFIXES) or value.startswith("'"):
        return "'" + value
    return value


def _unescape(value):
    if value.startswith("'") and len(value) > 1 and (value[1:].startswith(_FORMULA_PREFIXES) or value[1] == "'"):
        return value[1:]
    return value


def export_issues(issues):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow([label for _, label in db.FIELDS])
    for issue in issues:
        writer.writerow([_escape(issue.get(k, "")) for k, _ in db.FIELDS])
    return buf.getvalue().encode("utf-8-sig")


def export_history(rows):
    cols = [("issue_id", "事項編號"), ("changed_at", "時間"), ("operator", "操作者"),
            ("action", "動作"), ("field", "欄位"), ("old_value", "修改前"), ("new_value", "修改後")]
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow([label for _, label in cols])
    for r in rows:
        r = dict(r, field=db.FIELD_LABELS.get(r["field"], r["field"]))
        writer.writerow([_escape(str(r.get(k, ""))) for k, _ in cols])
    return buf.getvalue().encode("utf-8-sig")


def decode(data):
    """先試 UTF-8（含 BOM），失敗再試 cp950（Excel「另存 CSV」在繁中 Windows 的預設編碼）。"""
    for enc in ("utf-8-sig", "cp950"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise db.ValidationError(["無法辨識檔案編碼，請另存為「CSV UTF-8」後再匯入"])


def parse(data):
    text, _ = decode(data)
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise db.ValidationError(["CSV 是空的"])
    keys = [_HEADER_TO_KEY.get(h.strip()) for h in header]
    unknown = [h for h, k in zip(header, keys) if k is None and h.strip()]
    if unknown:
        raise db.ValidationError([f"無法辨識的欄位：{'、'.join(unknown)}"])
    if "title" not in keys:
        raise db.ValidationError(["缺少「標題」欄位"])
    rows = []
    for line_no, raw in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw):
            continue
        row = {k: _unescape(v) for k, v in zip(keys, raw) if k}
        rows.append((line_no, row))
    return rows


_CREATED_AT = re.compile(
    r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)


def _normalize_created_at(value):
    """空的用現在時間。寫進台帳前先擋下不是日期的值。"""
    text = (value or "").strip()
    if not text:
        return None
    matched = _CREATED_AT.match(text)
    if not matched:
        raise db.ValidationError([f"建立時間「{text}」格式錯誤，請用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"])
    try:
        day = dt.date(int(matched[1]), int(matched[2]), int(matched[3]))
    except ValueError:
        raise db.ValidationError([f"建立時間「{text}」不是有效日期，請用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"])
    if matched[4] is None:
        return f"{day.isoformat()} 00:00:00"
    hour, minute, second = int(matched[4]), int(matched[5]), int(matched[6] or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise db.ValidationError([f"建立時間「{text}」不是有效時間，請用 YYYY-MM-DD HH:MM:SS"])
    return f"{day.isoformat()} {hour:02d}:{minute:02d}:{second:02d}"


def import_issues(conn, data, operator, now=None):
    """全部驗證通過才寫入；任一列錯誤就整批不匯入。issue_id 已存在 → 更新，否則新增。"""
    rows = parse(data)
    created, updated, warnings, errors = 0, 0, [], []
    seen = set()
    conn.execute("SAVEPOINT import_csv")
    try:
        for line_no, row in rows:
            issue_id = (row.get("issue_id") or "").strip()
            if issue_id:
                if issue_id in seen:
                    errors.append(f"第 {line_no} 列：事項編號 {issue_id} 重複")
                    continue
                seen.add(issue_id)
            try:
                created_at = _normalize_created_at(row.get("created_at"))
                exists = issue_id and conn.execute(
                    "SELECT 1 FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
                if exists:
                    changes = {k: v for k, v in row.items() if k in db.EDITABLE_FIELDS}
                    _, w, diff = db.update_issue(conn, issue_id, changes, operator, now=now, action="import")
                    updated += bool(diff)
                else:
                    _, w = db.create_issue(conn, row, operator, now=now, action="import",
                                           issue_id=issue_id or None, created_at=created_at)
                    created += 1
                warnings.extend(f"第 {line_no} 列：{x}" for x in w)
            except db.ValidationError as e:
                errors.extend(f"第 {line_no} 列：{x}" for x in e.errors)
        if errors:
            raise db.ValidationError(errors)
    except Exception:
        conn.execute("ROLLBACK TO import_csv")
        conn.execute("RELEASE import_csv")
        raise
    conn.execute("RELEASE import_csv")
    return {"created": created, "updated": updated, "unchanged": len(rows) - created - updated,
            "warnings": warnings}
