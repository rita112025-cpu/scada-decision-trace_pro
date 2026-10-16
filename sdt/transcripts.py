"""逐字稿匯入與決議／待辦草稿。

只接受已核對過的 Transcript_translation JSON 匯出，以及 UTF-8 TXT。
沒有既有抽取器，也不呼叫外部服務；草稿用規則產生，確認前不進正式台帳。
"""
import base64
import datetime as dt
import hashlib
import json
import math
import pathlib
import re

from . import db

EXTRACTION_METHOD = "rule:v1"
LIMITATION = (
    "能力限制：此環境沒有可用的既有抽取器，也未呼叫外部 API 或下載模型。"
    "草稿由可解釋的規則產生：只有句子含明確的「決議、定案、核准、決定採用、同意採用、會議通過」等用語，"
    "且不是提議、疑問、否定或未定案時，才列為決議。"
    "只有明確交辦（「待辦」，或「請某人…」並寫出姓名，或「請於／應於某日之前」）才列為待辦。"
    "同一句若同時像決議又像交辦，只產生一筆決議草稿。"
    "負責人與期限只有原文直接寫明才填入，否則保持空白並標示未指定。"
    "規則可能漏掉比較口語的決議，但不會把尚未定案的討論直接當成已決議。"
    "草稿要經人工確認後才會寫進正式台帳。"
)

_JSON_KEYS = (
    "model", "language", "source_filename", "mode", "text", "raw_text",
    "segments", "duration_seconds", "status", "export_time",
)
_SEG_KEYS = ("id", "start", "end", "speaker", "text", "raw_text", "edited", "timestamp_source")
_MODES = {"cloud", "local"}

_TXT_TIME = re.compile(r"^\[(\d{2,}):(\d{2}):(\d{2})\]\s*(.*)$")
_DATE = re.compile(
    r"(?P<y>\d{4})\s*(?:[-/.]|年)\s*(?P<m>\d{1,2})\s*(?:[-/.]|月)\s*(?P<d>\d{1,2})\s*日?"
)
_DECISION_BLOCK = re.compile(
    r"尚未|還未|未決|未定|沒有決議|未決議|不決議|暫不|暫緩|先不|不予|不同意|否決|"
    r"不採用|不通過|不確定|無法確認|沒有結論|未有結論|再議|待議|待確認|待決定|"
    r"再討論|提議|提案|建議|擬議|考慮|或許|也許|好像|似乎|"
    r"是否|能不能|能否|要不要|是不是|有沒有|嗎|[？?]"
)
_DECISION_POS = re.compile(
    r"決議|(?:決定|確定|同意)(?:採用|使用)|定案|核准|(?:會議|審查|已)通過|予以確認"
)
_TODO_BLOCK = re.compile(r"不必|不用|無需|不需|請勿|不要|先不要|暫不|是否|能不能|能否|嗎|[？?]")
_NAME = r"[\u4e00-\u9fffA-Za-z]{2,4}"
_OWNER_RES = (
    re.compile(rf"負責人\s*[:：]\s*(?P<name>{_NAME})"),
    re.compile(rf"由(?P<name>{_NAME})負責"),
    re.compile(
        rf"請(?P<name>{_NAME})(?:於|負責|更新|完成|處理|補上|補充|提供|確認|追蹤|修改|修正|繪製)"
    ),
)
_NAME_BAD_PARTS = (
    "完成", "更新", "處理", "提供", "確認", "追蹤", "修改", "修正", "繪製", "補充", "補上",
    "盤", "圖", "於", "前", "是否", "不要", "相關", "我們", "大家", "他們", "這個", "那個",
)


def clean_filename(name):
    base = pathlib.PurePath(str(name or "").replace("\\", "/")).name.strip()
    if not base or base in {".", ".."}:
        raise db.ValidationError(["檔名無效"])
    return base[:200]


def decode_upload(content_base64):
    """把上傳內容還原成位元組並要求是 UTF-8 文字。不嘗試其他編碼，避免誤讀。"""
    if not isinstance(content_base64, str) or not content_base64.strip():
        raise db.ValidationError(["沒有收到檔案內容"])
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except Exception:
        raise db.ValidationError(["檔案內容不是有效的 Base64"])
    if not raw or not raw.strip():
        raise db.ValidationError(["檔案是空的，沒有可匯入的文字"])
    if b"\x00" in raw:
        raise db.ValidationError(["檔案含有二進位內容，請改用 UTF-8 文字或 Transcript_translation 的 JSON"])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise db.ValidationError(["檔案不是有效的 UTF-8 編碼，請另存為 UTF-8 後再匯入"])
    if not text.strip():
        raise db.ValidationError(["檔案是空的，沒有可匯入的文字"])
    return text


def content_hash(text):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), normalized


def _num(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise db.ValidationError([f"{label}不是有效數字"])
    return float(value)


def _seg_id(value, label):
    if isinstance(value, bool):
        raise db.ValidationError([f"{label}不是整數"])
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise db.ValidationError([f"{label}不是整數"])


def _fmt_seconds(seconds):
    ms_total = int(round(float(seconds) * 1000))
    if ms_total < 0:
        raise db.ValidationError(["時間戳不可為負數"])
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, milli = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milli:03d}"


def _require_str(obj, key, label):
    value = obj.get(key)
    if not isinstance(value, str):
        raise db.ValidationError([f"{label}必須是字串"])
    return value


def parse_json(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise db.ValidationError([f"JSON 格式錯誤：{e.msg}（第 {e.lineno} 行）"])
    if not isinstance(data, dict):
        raise db.ValidationError(["不是 Transcript_translation 的 JSON 匯出（最外層必須是物件）"])
    missing = [k for k in _JSON_KEYS if k not in data]
    if missing:
        raise db.ValidationError([
            "不是 Transcript_translation 的 JSON 匯出，缺少欄位：" + "、".join(missing)
        ])
    if data.get("status") != "completed":
        raise db.ValidationError(["逐字稿狀態不是 completed，無法匯入"])
    if data.get("mode") not in _MODES:
        raise db.ValidationError(["mode 必須是 Transcript_translation 的 cloud 或 local"])
    for key in ("model", "language", "source_filename", "text", "raw_text", "export_time"):
        _require_str(data, key, key)
    duration = data.get("duration_seconds")
    if duration is not None:
        _num(duration, "duration_seconds")
    segments = data.get("segments")
    if not isinstance(segments, list):
        raise db.ValidationError(["segments 必須是陣列"])
    if not segments:
        raise db.ValidationError(["沒有任何逐字稿段落可匯入"])
    parsed = []
    seen = set()
    for index, seg in enumerate(segments, start=1):
        if not isinstance(seg, dict):
            raise db.ValidationError([f"第 {index} 段不是物件"])
        missing = [k for k in _SEG_KEYS if k not in seg]
        if missing:
            raise db.ValidationError([f"第 {index} 段缺少欄位：" + "、".join(missing)])
        sid = _seg_id(seg.get("id"), f"第 {index} 段的 id")
        if sid in seen:
            raise db.ValidationError([f"段落 id {sid} 重複"])
        seen.add(sid)
        start = _num(seg.get("start"), f"第 {sid} 段的 start")
        end = _num(seg.get("end"), f"第 {sid} 段的 end")
        if start < 0 or end <= start:
            raise db.ValidationError([f"第 {sid} 段的結束時間必須晚於開始時間"])
        if not isinstance(seg.get("edited"), bool):
            raise db.ValidationError([f"第 {sid} 段的 edited 必須是布林值"])
        text_value = seg.get("text")
        raw_value = seg.get("raw_text")
        speaker = seg.get("speaker")
        source = seg.get("timestamp_source")
        if not all(isinstance(v, str) for v in (text_value, raw_value, speaker, source)):
            raise db.ValidationError([f"第 {sid} 段的 speaker／text／raw_text／timestamp_source 必須是字串"])
        label = f"{_fmt_seconds(start)}–{_fmt_seconds(end)}"
        parsed.append({
            "source_segment_id": str(sid),
            "seq": len(parsed) + 1,
            "text": text_value,
            "raw_text": raw_value,
            "speaker": speaker,
            "start_seconds": f"{start:.3f}",
            "end_seconds": f"{end:.3f}",
            "timestamp_label": label,
            "timestamp_source": source,
            "line_start": None,
            "line_end": None,
            "edited": seg["edited"],
            "location_label": f"段落 {sid}　{label}",
        })
    if not any(s["text"].strip() or s["raw_text"].strip() for s in parsed):
        raise db.ValidationError(["逐字稿段落都是空的"])
    return {
        "format": "json",
        "segments": parsed,
        "timestamp_status": "齊全",
        "timestamp_detail": "每一段都有匯入檔提供的開始與結束時間（秒），沒有另外估算。",
        "model": data["model"],
        "language": data["language"],
        "source_filename": data["source_filename"],
        "mode": data["mode"],
        "duration_seconds": "" if duration is None else str(duration),
        "export_status": data["status"],
        "export_time": data["export_time"],
        "notes": ["JSON 欄位依 Transcript_translation 的匯出格式讀取；檔案裡多出來的欄位會略過，不會寫進台帳。"],
    }


def _blocks(text):
    blocks = []
    buf = []
    start = None
    lines = text.split("\n")
    for i, line in enumerate(lines, start=1):
        if line.strip() == "":
            if buf:
                blocks.append((start, i - 1, buf))
                buf = []
                start = None
            continue
        if start is None:
            start = i
        buf.append(line)
    if buf:
        blocks.append((start, len(lines), buf))
    return blocks


def _parse_speaker(rest):
    matched = re.match(r"^(.*?)[:：](.*)$", rest)
    if not matched:
        return "", rest.strip()
    return matched.group(1).strip(), matched.group(2).strip()


def parse_txt(text):
    blocks = _blocks(text)
    if not blocks:
        raise db.ValidationError(["檔案是空的，沒有可匯入的文字"])
    timed = all(len(buf) == 1 and _TXT_TIME.match(buf[0]) for _, _, buf in blocks)
    segments = []
    if timed:
        for start_line, end_line, buf in blocks:
            matched = _TXT_TIME.match(buf[0])
            clock = f"{int(matched.group(1)):02d}:{matched.group(2)}:{matched.group(3)}"
            speaker, spoken = _parse_speaker(matched.group(4).strip())
            label = f"{clock}（無結束時間）"
            segments.append({
                "source_segment_id": str(start_line),
                "seq": len(segments) + 1,
                "text": spoken,
                "raw_text": buf[0],
                "speaker": speaker,
                "start_seconds": _clock_to_seconds(matched),
                "end_seconds": "",
                "timestamp_label": label,
                "timestamp_source": "",
                "line_start": start_line,
                "line_end": end_line,
                "edited": False,
                "location_label": f"第 {start_line} 行　{label}",
            })
        status = "不齊全"
        detail = "這是 Transcript_translation 的 TXT 匯出，只有開始時間 [HH:MM:SS]，沒有結束時間，因此沒有補造結束時間。"
        notes = [detail]
    else:
        lines = text.split("\n")
        for i, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            label = "無時間資訊"
            segments.append({
                "source_segment_id": str(i),
                "seq": len(segments) + 1,
                "text": line.strip(),
                "raw_text": line,
                "speaker": "",
                "start_seconds": "",
                "end_seconds": "",
                "timestamp_label": label,
                "timestamp_source": "",
                "line_start": i,
                "line_end": i,
                "edited": False,
                "location_label": f"第 {i} 行　{label}",
            })
        status = "無時間資訊"
        detail = "此 TXT 不是 Transcript_translation 的 [HH:MM:SS] 分段格式，已依行號定位，時間標為「無時間資訊」，沒有補造時間。"
        notes = [detail]
    if not segments:
        raise db.ValidationError(["檔案是空的，沒有可匯入的文字"])
    return {
        "format": "txt",
        "segments": segments,
        "timestamp_status": status,
        "timestamp_detail": detail,
        "model": "",
        "language": "",
        "source_filename": "",
        "mode": "",
        "duration_seconds": "",
        "export_status": "",
        "export_time": "",
        "notes": notes,
    }


def _clock_to_seconds(matched):
    hours = int(matched.group(1))
    minutes = int(matched.group(2))
    seconds = int(matched.group(3))
    if minutes > 59 or seconds > 59:
        raise db.ValidationError([f"時間戳 [{matched.group(1)}:{matched.group(2)}:{matched.group(3)}] 不正確"])
    return f"{hours * 3600 + minutes * 60 + seconds:.3f}"


def parse_upload(filename, text):
    name = clean_filename(filename)
    suffix = pathlib.PurePath(name).suffix.lower()
    if suffix == ".json":
        parsed = parse_json(text)
    elif suffix == ".txt" or suffix == "":
        if suffix == "" and text.lstrip().startswith("{"):
            parsed = parse_json(text)
        else:
            parsed = parse_txt(text)
    else:
        raise db.ValidationError([
            f"目前只接受 Transcript_translation 的 JSON，以及 UTF-8 TXT，不接受 {suffix}"
        ])
    parsed["filename"] = name
    return parsed


def preview_payload(conn, *, project_id, filename, content_base64):
    project = db.get_project(conn, project_id)
    text = decode_upload(content_base64)
    digest, normalized = content_hash(text)
    parsed = parse_upload(filename, normalized)
    existing = conn.execute(
        "SELECT transcript_id FROM transcripts WHERE project_id=? AND content_hash=?",
        (project["project_id"], digest)).fetchone()
    return {
        "project_id": project["project_id"],
        "project_name": project["name"],
        "filename": parsed["filename"],
        "format": parsed["format"],
        "segment_count": len(parsed["segments"]),
        "timestamp_status": parsed["timestamp_status"],
        "timestamp_detail": parsed["timestamp_detail"],
        "duplicate": existing is not None,
        "existing_transcript_id": existing["transcript_id"] if existing else "",
        "content_hash": digest,
        "notes": parsed["notes"],
        "limitation": LIMITATION,
        "segments_preview": [
            {
                "source_segment_id": s["source_segment_id"],
                "location_label": s["location_label"],
                "timestamp_label": s["timestamp_label"],
                "text": s["text"][:120],
            }
            for s in parsed["segments"][:8]
        ],
    }


def _sentences(text):
    parts = re.split(r"(?<=[。！？!?])", text or "")
    out = [p.strip() for p in parts if p.strip()]
    return out or ([])


def extract_owner(sentence):
    for cre in _OWNER_RES:
        matched = cre.search(sentence)
        if not matched:
            continue
        name = matched.group("name")
        if any(part in name for part in _NAME_BAD_PARTS):
            continue
        if name not in sentence:
            continue
        return name
    return ""


def extract_due(sentence):
    found = ""
    for matched in _DATE.finditer(sentence):
        before = sentence[max(0, matched.start() - 12):matched.start()]
        after = sentence[matched.end():matched.end() + 4].lstrip()
        explicit = (
            any(key in before for key in ("期限", "截止", "請於", "應於"))
            or after.startswith(("前", "以前", "之前"))
        )
        if not explicit:
            continue
        try:
            iso = dt.date(int(matched["y"]), int(matched["m"]), int(matched["d"])).isoformat()
        except ValueError:
            continue
        if found and found != iso:
            return ""
        found = iso
    return found


def _is_decision(sentence):
    if _DECISION_BLOCK.search(sentence):
        return False
    return _DECISION_POS.search(sentence) is not None


def _is_todo(sentence):
    if _TODO_BLOCK.search(sentence):
        return False
    if "待辦" in sentence:
        return True
    if extract_owner(sentence):
        return True
    if re.search(r"請於|應於", sentence) and extract_due(sentence):
        return True
    return False


def extract_candidates(segments):
    """由段落產生候選草稿。同一句最多一筆；不會把草稿寫進正式台帳。"""
    found = []
    seen = set()
    for seg in segments:
        for sentence in _sentences(seg.get("text") or ""):
            if _is_decision(sentence):
                kind = "決議"
            elif _is_todo(sentence):
                kind = "待辦"
            else:
                continue
            key = (kind, sentence)
            if key in seen:
                continue
            seen.add(key)
            owner = extract_owner(sentence)
            due = extract_due(sentence)
            found.append({
                "segment_key": seg["source_segment_id"],
                "kind": kind,
                "summary": sentence,
                "source_quote": sentence,
                "location_label": seg["location_label"],
                "timestamp_label": seg["timestamp_label"],
                "owner": owner,
                "owner_origin": "原文" if owner else "未指定",
                "due_date": due,
                "due_origin": "原文" if due else "未指定",
                "extraction_method": EXTRACTION_METHOD,
                "limitation": LIMITATION,
            })
    return found


def _public_segment(row):
    item = dict(row)
    item["edited"] = bool(item.get("edited"))
    return item


def _public_draft(row):
    return dict(row)


def import_transcript(conn, *, project_id, filename, content_base64, operator, now=None):
    project = db.get_project(conn, project_id)
    text = decode_upload(content_base64)
    digest, normalized = content_hash(text)
    parsed = parse_upload(filename, normalized)
    existing = conn.execute(
        "SELECT transcript_id FROM transcripts WHERE project_id=? AND content_hash=?",
        (project["project_id"], digest)).fetchone()
    if existing:
        tid = existing["transcript_id"]
        return {
            "created": False,
            "duplicate": True,
            "transcript_id": tid,
            "drafts": list_drafts(conn, project_id=project["project_id"], transcript_id=tid),
            "message": f"此專案已匯入過相同內容（逐字稿 {tid}），未重複建立來源或草稿。",
        }
    when = now or db.now_str()
    tid = db.next_public_id(conn, "transcripts", "transcript_id", "TR-")
    conn.execute(
        """INSERT INTO transcripts(
            transcript_id, project_id, filename, content_hash, format, raw_text,
            source_filename, model, language, mode, duration_seconds, export_status,
            export_time, imported_at, segment_count, timestamp_status, timestamp_detail
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, project["project_id"], parsed["filename"], digest, parsed["format"], normalized,
         parsed["source_filename"], parsed["model"], parsed["language"], parsed["mode"],
         parsed["duration_seconds"], parsed["export_status"], parsed["export_time"], when,
         len(parsed["segments"]), parsed["timestamp_status"], parsed["timestamp_detail"]))
    saved_segments = []
    for seg in parsed["segments"]:
        sid = f"{tid}-S{seg['seq']:04d}"
        conn.execute(
            """INSERT INTO transcript_segments(
                segment_id, transcript_id, project_id, source_segment_id, seq, text, raw_text,
                speaker, start_seconds, end_seconds, timestamp_label, timestamp_source,
                line_start, line_end, edited
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, tid, project["project_id"], seg["source_segment_id"], seg["seq"], seg["text"],
             seg["raw_text"], seg["speaker"], seg["start_seconds"], seg["end_seconds"],
             seg["timestamp_label"], seg["timestamp_source"], seg["line_start"], seg["line_end"],
             int(seg["edited"])))
        saved = dict(seg, segment_id=sid)
        saved_segments.append(saved)
    drafts = []
    for cand in extract_candidates(saved_segments):
        seg = next(s for s in saved_segments if s["source_segment_id"] == cand["segment_key"])
        did = db.next_public_id(conn, "drafts", "draft_id", "DR-")
        conn.execute(
            """INSERT INTO drafts(
                draft_id, project_id, transcript_id, segment_id, kind, summary, source_quote,
                location_label, timestamp_label, extraction_method, limitation, owner, owner_origin,
                due_date, due_origin, review_status, issue_id, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (did, project["project_id"], tid, seg["segment_id"], cand["kind"], cand["summary"],
             cand["source_quote"], cand["location_label"], cand["timestamp_label"],
             cand["extraction_method"], cand["limitation"], cand["owner"], cand["owner_origin"],
             cand["due_date"], cand["due_origin"], "草稿", "", when, when))
        drafts.append(get_draft(conn, did))
    message = f"已匯入 {tid}，產生 {len(drafts)} 筆草稿。草稿尚未進入正式台帳。"
    if not drafts:
        message += "這份逐字稿沒有符合規則的決議或待辦候選。"
    return {
        "created": True,
        "duplicate": False,
        "transcript_id": tid,
        "drafts": drafts,
        "message": message,
        "timestamp_status": parsed["timestamp_status"],
        "timestamp_detail": parsed["timestamp_detail"],
    }


def list_transcripts(conn, project_id):
    db.get_project(conn, project_id)
    rows = conn.execute(
        """SELECT transcript_id, project_id, filename, content_hash, format, segment_count,
                  timestamp_status, timestamp_detail, imported_at, source_filename
           FROM transcripts WHERE project_id=? ORDER BY imported_at, transcript_id""",
        (project_id,)).fetchall()
    return [dict(r) for r in rows]


def get_transcript(conn, transcript_id):
    row = conn.execute("SELECT * FROM transcripts WHERE transcript_id=?", (transcript_id,)).fetchone()
    if row is None:
        raise db.NotFound(transcript_id, "找不到此逐字稿")
    return dict(row)


def list_segments(conn, transcript_id):
    get_transcript(conn, transcript_id)
    rows = conn.execute(
        "SELECT * FROM transcript_segments WHERE transcript_id=? ORDER BY seq", (transcript_id,))
    return [_public_segment(r) for r in rows]


def get_segment(conn, transcript_id, segment_id):
    row = conn.execute(
        "SELECT * FROM transcript_segments WHERE transcript_id=? AND segment_id=?",
        (transcript_id, segment_id)).fetchone()
    if row is None:
        raise db.NotFound(segment_id, "找不到此逐字稿段落")
    return _public_segment(row)


def list_drafts(conn, *, project_id, status="", transcript_id=""):
    db.get_project(conn, project_id)
    clauses = ["project_id=?"]
    params = [project_id]
    if status:
        if status not in {"草稿", "已確認", "已捨棄"}:
            raise db.ValidationError(["審核狀態必須是草稿、已確認或已捨棄"])
        clauses.append("review_status=?")
        params.append(status)
    if transcript_id:
        clauses.append("transcript_id=?")
        params.append(transcript_id)
    rows = conn.execute(
        f"SELECT * FROM drafts WHERE {' AND '.join(clauses)} ORDER BY draft_id", params)
    return [_public_draft(r) for r in rows]


def get_draft(conn, draft_id):
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if row is None:
        raise db.NotFound(draft_id, "找不到此草稿")
    return _public_draft(row)


def update_draft(conn, draft_id, changes, *, now=None):
    draft = get_draft(conn, draft_id)
    if draft["review_status"] != "草稿":
        raise db.ValidationError(["只有尚未入帳的草稿可以修改"])
    summary = draft["summary"]
    owner = draft["owner"]
    due = draft["due_date"]
    owner_origin = draft["owner_origin"]
    due_origin = draft["due_origin"]
    if "summary" in changes:
        summary = db._clean_text(changes.get("summary"))
        if not summary:
            raise db.ValidationError(["摘要不可空白"])
    if "owner" in changes:
        owner = db._clean_text(changes.get("owner"))
        if owner == draft["owner"]:
            owner_origin = draft["owner_origin"]
        else:
            owner_origin = "人工" if owner else "未指定"
    if "due_date" in changes:
        try:
            due = db.parse_date(changes.get("due_date"))
        except ValueError:
            raise db.ValidationError([f"期限「{changes.get('due_date')}」格式錯誤，請用 YYYY-MM-DD"])
        if due == draft["due_date"]:
            due_origin = draft["due_origin"]
        else:
            due_origin = "人工" if due else "未指定"
    if (summary, owner, due, owner_origin, due_origin) == (
            draft["summary"], draft["owner"], draft["due_date"], draft["owner_origin"], draft["due_origin"]):
        return draft
    when = now or db.now_str()
    conn.execute(
        """UPDATE drafts SET summary=?, owner=?, owner_origin=?, due_date=?, due_origin=?, updated_at=?
           WHERE draft_id=?""",
        (summary, owner, owner_origin, due, due_origin, when, draft_id))
    updated = get_draft(conn, draft_id)
    if updated["source_quote"] != draft["source_quote"]:
        raise db.ValidationError(["原始引用被改寫，已中止"])
    return updated


def _issue_title(summary):
    text = summary.strip()
    if len(text) <= 80:
        return text
    return text[:79] + "…"


def confirm_draft(conn, draft_id, operator, *, now=None):
    draft = get_draft(conn, draft_id)
    if draft["review_status"] == "已確認" and draft["issue_id"]:
        return {
            "created": False,
            "issue": db.get_issue(conn, draft["issue_id"]),
            "draft": draft,
            "message": f"此草稿已入帳為 {draft['issue_id']}，未重複建立事項。",
        }
    if draft["review_status"] != "草稿":
        raise db.ValidationError(["此草稿已捨棄，不能入帳"])
    project = db.get_project(conn, draft["project_id"])
    transcript = get_transcript(conn, draft["transcript_id"])
    kind = draft["kind"]
    issue_type = {"決議": "會議決議", "待辦": "待辦"}.get(kind)
    if not issue_type:
        raise db.ValidationError([f"未知的草稿類型 {kind}"])
    description = (
        f"【{kind}】由逐字稿草稿確認入帳。抽取方式：{draft['extraction_method']}。\n"
        "規則抽取，不是模型。提議、疑問、否定與未定案不會直接入帳；"
        "負責人與期限留空表示原文沒有寫明。\n\n"
        f"{draft['summary']}"
    )
    data = {
        "project_id": project["project_id"],
        "project": project["name"],
        "issue_type": issue_type,
        "title": _issue_title(draft["summary"]),
        "description": description,
        "source_file": transcript["filename"],
        "source_location": draft["location_label"],
        "source_excerpt": draft["source_quote"],
        "source_status": "已確認",
        "owner": draft["owner"],
        "due_date": draft["due_date"],
        "priority": "中",
        "status": "待處理",
    }
    when = now or db.now_str()
    issue, warnings = db.create_issue(conn, data, operator, now=when, action="confirm-draft")
    conn.execute(
        """UPDATE issues SET transcript_id=?, segment_ref=?, draft_id=?, extraction_method=?, source_quote=?
           WHERE issue_id=?""",
        (draft["transcript_id"], draft["segment_id"], draft["draft_id"], draft["extraction_method"],
         draft["source_quote"], issue["issue_id"]))
    for field, value in (
        ("transcript_id", draft["transcript_id"]),
        ("segment_ref", draft["segment_id"]),
        ("draft_id", draft["draft_id"]),
        ("extraction_method", draft["extraction_method"]),
        ("source_quote", draft["source_quote"]),
    ):
        db._log(conn, issue["issue_id"], when, operator, "confirm-draft", field, "", value)
    conn.execute(
        "UPDATE drafts SET review_status='已確認', issue_id=?, updated_at=? WHERE draft_id=?",
        (issue["issue_id"], when, draft_id))
    return {
        "created": True,
        "issue": db.get_issue(conn, issue["issue_id"]),
        "draft": get_draft(conn, draft_id),
        "warnings": warnings,
        "message": f"已入帳為 {issue['issue_id']}。原始引用保留在來源摘錄，摘要修改不會覆寫它。",
    }


def discard_draft(conn, draft_id, *, now=None):
    draft = get_draft(conn, draft_id)
    if draft["review_status"] == "已捨棄":
        return {"draft": draft, "message": "此草稿已經捨棄，未另外建立事項。"}
    if draft["review_status"] != "草稿":
        raise db.ValidationError(["已入帳的草稿不能捨棄；若不需要該事項，請把台帳狀態改為已關閉"])
    when = now or db.now_str()
    conn.execute(
        "UPDATE drafts SET review_status='已捨棄', updated_at=? WHERE draft_id=?", (when, draft_id))
    return {"draft": get_draft(conn, draft_id), "message": "已捨棄，未寫入正式台帳。"}
