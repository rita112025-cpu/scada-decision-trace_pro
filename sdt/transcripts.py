"""逐字稿匯入與決議／待辦草稿。

只接受已核對過的 Transcript_translation JSON 匯出，以及 UTF-8 TXT。
沒有既有抽取器，也不呼叫外部服務；草稿用規則產生，確認前不進正式台帳。
"""
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import math
import pathlib
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from . import db

EXTRACTION_METHOD = "rule:candidate-v2"
LIMITATION = (
    "能力限制：此環境沒有可用的既有抽取器，也未呼叫外部 API 或下載模型。"
    "草稿由可解釋的規則產生，只認得列出的用語，以及一部分否定與取消句式"
    "（例如「未核准」「不是決議」「沒有待辦」「待辦已取消」）。"
    "同一句若同時像決議又像交辦，只產生一筆決議草稿。"
    "負責人與期限只有原文直接寫明才填入，否則保持空白並標示未指定。"
    "規則不完整：其他講法仍可能漏判或誤判，不能當成不會誤判。"
    "草稿必須經人工確認後才會寫進正式台帳。"
)

_JSON_KEYS = (
    "model", "language", "source_filename", "mode", "text", "raw_text",
    "segments", "duration_seconds", "status", "export_time",
)
_SEG_KEYS = ("id", "start", "end", "speaker", "text", "raw_text", "edited", "timestamp_source")
_MODES = {"cloud", "local"}

_DATE = re.compile(
    r"(?P<y>\d{4})\s*(?:[-/.]|年)\s*(?P<m>\d{1,2})\s*(?:[-/.]|月)\s*(?P<d>\d{1,2})\s*日?"
)
_TODO_BLOCK = re.compile(
    r"不必|不用|無需|不需|請勿|不要|先不要|暫不|是否|能不能|能否|嗎|[？?]|"
    r"沒有待辦|無待辦|不是待辦|並非待辦|待辦已取消|取消待辦|待辦取消|已取消"
)
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
            "raw_timecode": f"[{seg['start']}–{seg['end']}]",
            "time_parse_status": "timed",
            "source_id": "",
            "source_audio": data["source_filename"],
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


def _parse_speaker(rest):
    matched = re.match(r"^(.*?)[:：](.*)$", rest)
    if not matched:
        return "", rest.strip()
    return matched.group(1).strip(), matched.group(2).strip()


_RANGE_TIME = re.compile(r"^\s*\[(\d+:\d{2}:\d{2}(?:[.,]\d+)?)(?:\s*[\u2013\u2014-]\s*(\d+:\d{2}:\d{2}(?:[.,]\d+)?))?\s*\](.*)$")
_SOURCE_ROW = re.compile(r"^\s*={3,}\s*(.*?)\s*[\uFF5C|]\s*([A-Za-z0-9_-]+)\s*={0,}\s*$")
_HEADER_ROW = re.compile(r"^(\u6703\u8b70\u65e5\u671f|\u6703\u8b70\u540d\u7a31|\u6848\u540d\uff0f\u6848\u865f|\u6ce8\u610f)\uff1a")
_CLOCK = re.compile(r"^(\d+):(\d{2}):(\d{2})(?:[.,](\d+))?$")


def _time_seconds(value):
    match = _CLOCK.fullmatch(value.strip())
    if not match:
        raise ValueError("\u6642\u9593\u683c\u5f0f\u932f\u8aa4")
    hours, minutes, seconds = map(int, match.group(1, 2, 3))
    if minutes > 59 or seconds > 59:
        raise ValueError("\u6642\u9593\u6b04\u4f4d\u8d85\u51fa\u7bc4\u570d")
    fraction = float("0." + (match.group(4) or "0"))
    return hours * 3600 + minutes * 60 + seconds + fraction


def parse_txt(text):
    segments, warnings, metadata = [], [], {}
    source_id = source_audio = ""
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        source = _SOURCE_ROW.match(stripped)
        if source:
            source_audio, source_id = source.group(1).strip(), source.group(2).strip()
            continue
        if _HEADER_ROW.match(stripped):
            key, value = stripped.split("\uff1a", 1)
            metadata[key] = value.strip()
            continue
        if stripped.startswith("=====") and stripped.endswith("====="):
            continue
        raw_timecode, status, start, end, spoken = "", "no_time", "", "", stripped
        matched = _RANGE_TIME.match(stripped)
        if matched:
            raw_timecode = stripped[:stripped.find("]") + 1]
            spoken = matched.group(3).strip()
            try:
                begin = _time_seconds(matched.group(1))
                finish = _time_seconds(matched.group(2)) if matched.group(2) else None
                if finish is not None and finish < begin:
                    raise ValueError("\u7d50\u675f\u6642\u9593\u65e9\u65bc\u958b\u59cb\u6642\u9593")
                start, end, status = f"{begin:.3f}", "" if finish is None else f"{finish:.3f}", "timed"
            except ValueError as error:
                status = "failed"
                warnings.append(f"\u7b2c {line_no} \u884c\u6642\u9593\u89e3\u6790\u5931\u6557\uff1a{error}\uff1b\u539f\u6587\u5df2\u4fdd\u7559")
        elif stripped.startswith("[") and "]" in stripped:
            raw_timecode = stripped[:stripped.find("]") + 1]
            status = "failed"
            warnings.append(f"\u7b2c {line_no} \u884c\u6642\u9593\u683c\u5f0f\u932f\u8aa4\uff1b\u539f\u6587\u5df2\u4fdd\u7559")
        speaker, spoken = _parse_speaker(spoken) if status == "timed" else ("", spoken)
        label = f"{start}\u2013{end}" if end else (f"{start}\uff08\u50c5\u63d0\u4f9b\u958b\u59cb\u6642\u9593\uff0c\u7121\u7d50\u675f\u6642\u9593\uff09" if start else ("\u6642\u9593\u89e3\u6790\u5931\u6557" if status == "failed" else "\u7121\u6642\u9593\u8cc7\u8a0a"))
        sid = source_id or "TXT"
        segments.append({
            "source_segment_id": f"{sid}:{line_no}", "seq": len(segments) + 1,
            "text": spoken, "raw_text": line, "speaker": speaker,
            "start_seconds": start, "end_seconds": end, "timestamp_label": label,
            "timestamp_source": "txt-range" if status == "timed" else "",
            "raw_timecode": raw_timecode, "time_parse_status": status,
            "source_id": source_id, "source_audio": source_audio,
            "line_start": line_no, "line_end": line_no, "edited": False,
            "location_label": f"{source_audio or 'TXT'}\uff5c{sid}\uff5c\u7b2c {line_no} \u884c\uff5c{label}",
        })
    if not segments:
        raise db.ValidationError(["\u9010\u5b57\u7a3f\u6c92\u6709\u53ef\u8b80\u53d6\u7684\u6bb5\u843d"])
    counts = {key: sum(seg["time_parse_status"] == key for seg in segments) for key in ("timed", "no_time", "failed")}
    all_ranges_complete = all(seg["time_parse_status"] == "timed" and bool(seg["end_seconds"]) for seg in segments)
    status = "\u90e8\u5206\u6642\u9593\u89e3\u6790\u5931\u6557" if counts["failed"] else ("\u5df2\u89e3\u6790\u6642\u9593" if all_ranges_complete else ("\u4e0d\u9f4a\u5168" if counts["timed"] == len(segments) else ("\u90e8\u5206\u6709\u6642\u9593" if counts["timed"] else "\u7121\u6642\u9593\u8cc7\u8a0a")))
    detail = f"\u6bb5\u843d {len(segments)}\uff1b\u6642\u9593\u89e3\u6790\u6210\u529f {counts['timed']}\u3001\u7121\u6642\u9593\u8cc7\u8a0a {counts['no_time']}\u3001\u89e3\u6790\u5931\u6557 {counts['failed']}\u3002"
    return {"format": "txt", "segments": segments, "timestamp_status": status,
            "timestamp_detail": detail, "time_diagnostics": counts, "warnings": warnings,
            "metadata": metadata, "model": "", "language": "", "source_filename": "",
            "mode": "", "duration_seconds": "", "export_status": "", "export_time": "",
            "notes": warnings or [detail]}


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
        "diagnostics": {
            "source_audio_count": len({s.get("source_audio") for s in parsed["segments"] if s.get("source_audio")}),
            "segment_count": len(parsed["segments"]),
            "time": parsed.get("time_diagnostics", {
                "timed": sum(bool(s.get("start_seconds")) for s in parsed["segments"]),
                "no_time": sum(not s.get("start_seconds") and s.get("time_parse_status", "no_time") == "no_time" for s in parsed["segments"]),
                "failed": sum(s.get("time_parse_status") == "failed" for s in parsed["segments"]),
            }),
            "parser": "txt-range-v2" if parsed["format"] == "txt" else "transcript-json",
            "extractor": EXTRACTION_METHOD,
            "run_mode": "rule-candidates-only",
            "semantic_meeting_processing": False,
            "warnings": parsed.get("warnings", []),
        },
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


_DECISION_CONTEXT_BLOCKS = (
    "還沒有", "尚未", "未曾", "沒有核准", "尚未核准", "未核准", "不會核准",
    "是否", "嗎？", "嗎?", "？", "?", "可能", "建議", "提議", "之前", "過去",
    "已經核准", "核准過", "如果", "假設", "有人說", "引用", "更正", "撤回",
)
_DECISION_PATTERNS = (
    "會議決議", "決議採用", "決議通過", "決議核准", "決定採用", "決定核准",
    "同意採用", "同意核准", "本次核准", "正式核准", "最終採用", "決議：", "決議:",
)


def _context_safe_decision(sentence, context):
    return (any(pattern in sentence for pattern in _DECISION_PATTERNS)
            and not any(term in context for term in _DECISION_CONTEXT_BLOCKS))


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
    for index, seg in enumerate(segments):
        source = seg.get("source_id") or seg.get("source_audio") or ""
        context = ""
        following = ""
        if index:
            previous = segments[index - 1]
            previous_source = previous.get("source_id") or previous.get("source_audio") or ""
            if source == previous_source:
                context = previous.get("text") or ""
        if index + 1 < len(segments):
            next_segment = segments[index + 1]
            next_source = next_segment.get("source_id") or next_segment.get("source_audio") or ""
            if source == next_source:
                following = next_segment.get("text") or ""
        for sentence in _sentences(seg.get("text") or ""):
            if _context_safe_decision(sentence, f"{context} {sentence} {following}"):
                kind = "決議"
            elif _is_todo(sentence):
                kind = "待辦"
            else:
                continue
            key = (kind, source, seg.get("source_segment_id") or seg.get("segment_id"), sentence)
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
    result = dict(row)
    result["evidence"] = json.loads(result.get("evidence_json") or "[]")
    result["outcome"] = json.loads(result.get("outcome_json") or "{}")
    return result


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
            export_time, imported_at, segment_count, timestamp_status, timestamp_detail, metadata
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, project["project_id"], parsed["filename"], digest, parsed["format"], normalized,
         parsed["source_filename"], parsed["model"], parsed["language"], parsed["mode"],
         parsed["duration_seconds"], parsed["export_status"], parsed["export_time"], when,
         len(parsed["segments"]), parsed["timestamp_status"], parsed["timestamp_detail"],
         json.dumps(parsed.get("metadata", {}), ensure_ascii=False)))
    saved_segments = []
    for seg in parsed["segments"]:
        sid = f"{tid}-S{seg['seq']:04d}"
        conn.execute(
            """INSERT INTO transcript_segments(
                segment_id, transcript_id, project_id, source_segment_id, seq, text, raw_text,
                speaker, start_seconds, end_seconds, timestamp_label, timestamp_source,
                line_start, line_end, edited, source_id, source_audio, raw_timecode, time_parse_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, tid, project["project_id"], seg["source_segment_id"], seg["seq"], seg["text"],
             seg["raw_text"], seg["speaker"], seg["start_seconds"], seg["end_seconds"],
             seg["timestamp_label"], seg["timestamp_source"], seg["line_start"], seg["line_end"],
             int(seg["edited"]), seg.get("source_id", ""), seg.get("source_audio", ""),
             seg.get("raw_timecode", ""), seg.get("time_parse_status", "no_time")))
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
        "diagnostics": {
            "source_audio_count": len({s.get("source_audio") for s in saved_segments if s.get("source_audio")}),
            "segment_count": len(saved_segments),
            "time": parsed.get("time_diagnostics", {
                "timed": sum(bool(s.get("start_seconds")) for s in saved_segments),
                "no_time": sum(not s.get("start_seconds") and s.get("time_parse_status", "no_time") == "no_time" for s in saved_segments),
                "failed": sum(s.get("time_parse_status") == "failed" for s in saved_segments),
            }),
            "coverage": "not_semantically_processed",
            "candidate_count": len(drafts),
            "pending_review": sum(d["review_status"] == "?阮" for d in drafts),
            "extraction_method": EXTRACTION_METHOD,
            "warnings": parsed.get("warnings", []),
        },
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


def _linked_issues(conn: sqlite3.Connection, transcript_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM issues WHERE transcript_id=?
           OR draft_id IN (SELECT draft_id FROM drafts WHERE transcript_id=?)
           OR segment_ref IN (SELECT segment_id FROM transcript_segments WHERE transcript_id=?)
           ORDER BY issue_id""", (transcript_id, transcript_id, transcript_id))
    return [dict(row) for row in rows]


def deletion_preview(conn: sqlite3.Connection, transcript_id: str) -> dict:
    """Return counts and a snapshot token without exposing transcript text."""
    transcript = get_transcript(conn, transcript_id)
    drafts = [dict(row) for row in conn.execute(
        "SELECT * FROM drafts WHERE transcript_id=? ORDER BY draft_id", (transcript_id,))]
    issues = _linked_issues(conn, transcript_id)
    snapshot = json.dumps([transcript, drafts, issues], sort_keys=True, ensure_ascii=False)
    return {
        "transcript_id": transcript_id, "project_id": transcript["project_id"],
        "filename": transcript["filename"], "draft_count": len(drafts),
        "pending_draft_count": sum(d["review_status"] == "草稿" for d in drafts),
        "confirmed_draft_count": sum(d["review_status"] == "已確認" for d in drafts),
        "discarded_draft_count": sum(d["review_status"] == "已捨棄" for d in drafts),
        "issue_count": len(issues),
        "preview_token": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
    }


def delete_transcript(conn: sqlite3.Connection, transcript_id: str, *,
                      preview_token: str, operator: str) -> dict:
    """Remove imported data atomically; keep ledger entries and copied evidence."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    impact = deletion_preview(conn, transcript_id)
    if not preview_token or preview_token != impact["preview_token"]:
        raise db.ValidationError(["受影響資料已改變，請取消後重新按刪除，核對最新數量再確認。"])
    when = db.now_str()
    for issue in _linked_issues(conn, transcript_id):
        for field in ("transcript_id", "segment_ref", "draft_id"):
            if issue[field]:
                db._log(conn, issue["issue_id"], when, operator, "delete-transcript", field, issue[field], "")
        conn.execute(
            "UPDATE issues SET transcript_id='', segment_ref='', draft_id='', updated_at=? WHERE issue_id=?",
            (when, issue["issue_id"]))
    conn.execute("DELETE FROM drafts WHERE transcript_id=?", (transcript_id,))
    conn.execute("DELETE FROM transcript_segments WHERE transcript_id=?", (transcript_id,))
    conn.execute("DELETE FROM transcripts WHERE transcript_id=?", (transcript_id,))
    return {"deleted": True, "drafts_deleted": impact["draft_count"],
            "issues_preserved": impact["issue_count"], "transcript_id": transcript_id}


def list_segments(conn, transcript_id):
    get_transcript(conn, transcript_id)
    rows = conn.execute(
        "SELECT * FROM transcript_segments WHERE transcript_id=? ORDER BY seq", (transcript_id,))
    return [_public_segment(r) for r in rows]


def meeting_results(conn, transcript_id):
    """Read-only review bundle; rule candidates are never represented as completed minutes."""
    transcript = get_transcript(conn, transcript_id)
    segments = list_segments(conn, transcript_id)
    drafts = list_drafts(conn, project_id=transcript["project_id"], transcript_id=transcript_id)
    by_id = {segment["segment_id"]: segment for segment in segments}
    reviewed = []
    warnings = []
    seen = set()
    for draft in drafts:
        segment = by_id.get(draft["segment_id"])
        if not segment:
            warnings.append(f"{draft['draft_id']} 找不到原始段落，需檢查資料遷移")
            continue
        index = segments.index(segment)
        context = []
        for neighbor_index in (index - 1, index + 1):
            if 0 <= neighbor_index < len(segments):
                neighbor = segments[neighbor_index]
                same_source = (neighbor.get("source_id") or neighbor.get("source_audio") or "") == (
                    segment.get("source_id") or segment.get("source_audio") or "")
                if same_source:
                    context.append({"text": neighbor["raw_text"], "source_audio": neighbor.get("source_audio", ""),
                                    "timestamp_label": neighbor["timestamp_label"]})
        combined = " ".join([draft["source_quote"], *(item["text"] for item in context)])
        reasons = []
        if draft["extraction_method"] in ("rule:v1", "rule:candidate-v2"):
            reasons.append("規則候選，尚未完成語意核對")
        if segment.get("time_parse_status") == "failed":
            reasons.append("時間碼解析失敗")
        elif segment.get("time_parse_status") == "no_time":
            reasons.append("原文沒有時間定位")
        if any(term in combined for term in _DECISION_CONTEXT_BLOCKS):
            reasons.append("上下文包含否定、問句、提議、過去狀態或修正詞，請人工檢查")
        row = dict(draft)
        row["evidence"] = {"raw_text": segment["raw_text"], "text": segment["text"],
                           "source_audio": segment.get("source_audio") or transcript["source_filename"],
                           "source_id": segment.get("source_id", ""),
                           "timestamp_label": segment["timestamp_label"],
                           "line_start": segment.get("line_start"), "line_end": segment.get("line_end"),
                           "context": context}
        row["additional_evidence"] = []
        for cite in json.loads(draft.get("evidence_json") or "[]")[1:]:
            cited_segment = by_id.get(cite.get("segment_id"))
            if cited_segment:
                row["additional_evidence"].append({
                    "raw_text": cited_segment["raw_text"], "quote": cite.get("quote", ""),
                    "source_audio": cited_segment.get("source_audio", ""),
                    "source_id": cited_segment.get("source_id", ""),
                    "timestamp_label": cited_segment["timestamp_label"],
                    "line_start": cited_segment.get("line_start"),
                })
        row["quality_warnings"] = reasons
        reviewed.append(row)
        seen.add(segment["segment_id"])
    counts = {key: sum(segment.get("time_parse_status", "no_time") == key for segment in segments)
              for key in ("timed", "no_time", "failed")}
    counts["timed"] = sum(bool(segment.get("start_seconds")) for segment in segments)
    result = {
        "transcript": {key: transcript.get(key) for key in ("transcript_id", "project_id", "filename", "imported_at", "format", "timestamp_status", "timestamp_detail")},
        "diagnostics": {"source_audio_count": len({s.get("source_audio") for s in segments if s.get("source_audio")}),
                        "segment_count": len(segments), "time": counts,
                        "candidate_count": len(drafts),
                        "candidate_by_type": {kind: sum(d["kind"] == kind for d in drafts) for kind in {d["kind"] for d in drafts}},
                        "pending_review": sum(d["review_status"] == "草稿" for d in drafts),
                        "posted": sum(d["review_status"] == "已確認" for d in drafts),
                        "coverage": "規則候選，未完成語意整理；段落均可人工檢視",
                        "parser": "txt-range-v2" if transcript["format"] == "txt" else "transcript-json",
                        "extractor": EXTRACTION_METHOD, "run_mode": "rule-candidates-only"},
        "no_confirmed_decision": not any(d["kind"] == "決議" for d in drafts),
        "unclassified_segment_count": max(0, len(segments) - len(seen)),
        "items": reviewed, "warnings": warnings,
    }
    try:
        result["transcript"]["metadata"] = json.loads(transcript.get("metadata") or "{}")
    except (TypeError, json.JSONDecodeError):
        result["transcript"]["metadata"] = {}
    notice = result["transcript"]["metadata"].get("注意", "")
    result["diagnostics"]["transcript_notice"] = notice
    result["diagnostics"]["machine_transcript_unreviewed"] = any(
        marker in notice for marker in ("機器逐字稿", "尚未人工確認", "未人工確認"))
    return result


def meeting_results_markdown(result):
    transcript = result["transcript"]
    diagnostics = result["diagnostics"]
    lines = [f"# 會議成果預覽：{transcript['filename']}", "",
             "> 規則候選預覽；未完成語意會議整理。候選均需人工核對後再入帳。", "",
             f"- 會議日期：{transcript.get('metadata', {}).get('會議日期', '未提供')}",
             f"- 會議名稱：{transcript.get('metadata', {}).get('會議名稱', '未提供')}",
             f"- 匯入時間：{transcript['imported_at']}",
             f"- 來源音檔數：{diagnostics['source_audio_count']}",
             f"- 段落：{diagnostics['segment_count']}；時間成功 {diagnostics['time']['timed']}、無時間 {diagnostics['time']['no_time']}、解析失敗 {diagnostics['time']['failed']}",
             f"- 候選：{diagnostics['candidate_count']}；待審核 {diagnostics['pending_review']}；已入帳 {diagnostics['posted']}", ""]
    if diagnostics.get("transcript_notice"):
        lines[2:2] = [f"> 逐字稿注意：{diagnostics['transcript_notice']}", ""]
    decisions = [item for item in result["items"] if item["kind"] == "決議"]
    if not decisions:
        lines.extend(["## 明確決議", "未辨識到可確認的決議。", ""])
    for item in result["items"]:
        evidence = item["evidence"]
        outcome = item.get("outcome") or {}
        lines.extend([f"## {item['kind']}｜{item['summary']}",
                      f"- 審核狀態：{item['review_status']}；負責人：{item['owner'] or '未指定'}；期限：{item['due_date'] or '未指定'}",
                      f"- 來源：{evidence['source_audio'] or '音檔未標示'}｜{evidence['source_id'] or '來源 ID 未標示'}｜{evidence['timestamp_label']}｜第 {evidence['line_start'] or '?'} 行",
                      f"- 原文：{evidence['raw_text']}"])
        if outcome.get("action"):
            lines.append(f"- 待辦動作：{outcome['action']}")
        if outcome.get("deliverable"):
            lines.append(f"- 交付成果：{outcome['deliverable']}")
        if evidence["context"]:
            lines.append("- 上下文：")
            lines.extend(f"  - {context['source_audio']}｜{context['timestamp_label']}｜{context['text']}" for context in evidence["context"])
        for extra in item.get("additional_evidence", []):
            lines.append(f"- 其他依據：{extra['source_audio']}｜{extra['source_id']}｜{extra['timestamp_label']}｜第 {extra['line_start'] or '?'} 行｜{extra['quote']}")
        if item["quality_warnings"]:
            lines.extend(["- 待核對：" + "；".join(item["quality_warnings"])])
        lines.append("")
    return "\n".join(lines)


def quality_repair_preview(conn, transcript_id):
    """Suggests review targets for legacy drafts and ledger entries without mutating them."""
    bundle = meeting_results(conn, transcript_id)
    transcript = get_transcript(conn, transcript_id)
    issues = _linked_issues(conn, transcript_id)
    rows = []
    for issue in issues:
        reasons = []
        if issue.get("extraction_method") == "rule:v1":
            reasons.append("舊規則 rule:v1，需重新核對語意")
        if not issue.get("source_location") or "無時間資訊" in issue.get("source_location", ""):
            reasons.append("來源定位缺少時間資訊")
        if any(term in (issue.get("source_excerpt", "") + issue.get("description", "")) for term in _DECISION_CONTEXT_BLOCKS):
            reasons.append("原文或說明含可能改變決議語意的上下文詞")
        if reasons:
            rows.append({"issue_id": issue["issue_id"], "title": issue["title"], "reasons": reasons,
                         "current_source_status": issue.get("source_status", ""),
                         "source_checked": issue.get("source_checked", "未核對"),
                         "audio_checked": issue.get("audio_checked", "未核對")})
    try:
        reparsed = parse_upload(transcript["filename"], transcript["raw_text"])
        reparse = {"available": True, "parser": "txt-range-v2" if reparsed["format"] == "txt" else "transcript-json",
                   "old_segment_count": transcript["segment_count"], "new_segment_count": len(reparsed["segments"]),
                   "new_candidates": len(extract_candidates(reparsed["segments"])),
                   "time_diagnostics": reparsed.get("time_diagnostics", {
                       "timed": sum(bool(seg.get("start_seconds")) for seg in reparsed["segments"]),
                       "no_time": sum(not seg.get("start_seconds") and seg.get("time_parse_status", "no_time") == "no_time" for seg in reparsed["segments"]),
                       "failed": sum(seg.get("time_parse_status") == "failed" for seg in reparsed["segments"])}),
                   "warnings": reparsed.get("warnings", [])}
    except db.ValidationError as error:
        reparse = {"available": False, "errors": error.errors}
    return {"read_only": True, "ledger_modified": False, "reparse_preview": reparse,
            "suspected_issues": rows,
            "legacy_drafts": [dict(item, quality_warnings=item["quality_warnings"] or ["舊草稿待人工複核"])
                              for item in bundle["items"]],
            "message": "這是唯讀檢查預覽；尚未重跑或修改草稿及正式台帳。"}


def analyze_local_ollama(conn, transcript_id, base_url, model, *, timeout=120, chunk_size=32):
    transcript = get_transcript(conn, transcript_id)
    segments = list_segments(conn, transcript_id)
    if not segments:
        raise db.ValidationError(["逐字稿沒有可分析的段落"])
    parsed_url = urllib.parse.urlsplit((base_url or "").rstrip("/"))
    host = (parsed_url.hostname or "").lower()
    try:
        is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    try:
        port = parsed_url.port
    except ValueError:
        port = None
    if (parsed_url.scheme not in ("http", "https") or not is_loopback or not port
            or parsed_url.username or parsed_url.password or parsed_url.path not in ("", "/")):
        raise db.ValidationError(["本機模型網址只允許 localhost 或 loopback IP，並需包含連接埠"])
    if not model or len(model) > 120:
        raise db.ValidationError(["請在本機設定中指定已安裝的 Ollama 模型"])
    chunks = [segments[i:i + chunk_size] for i in range(0, len(segments), chunk_size)]
    all_items, covered, failures = [], [], []
    for chunk_index, chunk in enumerate(chunks):
        allowed = {segment["segment_id"]: segment for segment in chunk}
        chunk_items = []
        prompt = {
            "instruction": "整理逐字稿為議題、討論摘要、結論類型、待辦動作、交付成果。只根據提供段落；不確定歸待確認。負責人與期限只有原文明確寫出才填。不要合併互相矛盾或來源不同的說法。每個項目引用一段或多段 evidence，segment_id 必須來自輸入，quote 必須是該段 text 或 raw_text 中逐字連續的非空片段。輸出 JSON object，含 items 陣列。每個 item 必須有 topic、summary、kind、action、deliverable、owner、due_date、evidence 欄位；evidence 是含 segment_id 與 quote 的陣列。若無成果就輸出空陣列。格式範例：{\"items\":[{\"topic\":\"議題名稱\",\"summary\":\"摘要\",\"kind\":\"待確認\",\"action\":\"\",\"deliverable\":\"\",\"owner\":\"\",\"due_date\":\"\",\"evidence\":[{\"segment_id\":\"輸入中的實際 ID\",\"quote\":\"完全相同的原文片段\"}]}]}。kind 僅可為 明確決議、待辦、現況說明、提議、爭議、待確認。",
            "transcript": transcript["filename"],
            "segments": [{key: segment.get(key) for key in
                          ("segment_id", "source_id", "source_audio", "timestamp_label", "line_start", "text", "raw_text")}
                         for segment in chunk],
        }
        request_body = json.dumps({
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            "options": {"temperature": 0},
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(f"{base_url.rstrip('/')}/api/chat", data=request_body,
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                envelope = json.loads(response.read().decode("utf-8"))
            content = envelope.get("message", {}).get("content", "")
            result = json.loads(content)
            items = result.get("items") if isinstance(result, dict) else None
            if not isinstance(items, list):
                raise ValueError("模型回應缺少 items 陣列")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("成果項目不是 JSON object")
                for key in ("topic", "summary", "kind", "action", "deliverable", "owner", "due_date", "evidence"):
                    if key not in item:
                        raise ValueError(f"成果項目缺少 {key}")
                if item["kind"] not in ("明確決議", "待辦", "現況說明", "提議", "爭議", "待確認"):
                    raise ValueError("成果分類不在允許清單")
                if not isinstance(item["evidence"], list) or not item["evidence"]:
                    raise ValueError("每個成果都必須附逐字段落依據")
                for cite in item["evidence"]:
                    segment = allowed.get(cite.get("segment_id")) if isinstance(cite, dict) else None
                    if not segment:
                        raise ValueError("模型引用不存在或不屬於本分段的 segment_id")
                    quote = cite.get("quote")
                    if (not isinstance(quote, str) or not quote.strip()
                            or (quote not in segment["text"] and quote not in segment["raw_text"])):
                        raise ValueError("模型引用文字與實際原文不一致")
                item["review_status"] = "待人工核對"
                item["chunk_index"] = chunk_index
                chunk_items.append(item)
            all_items.extend(chunk_items)
            covered.extend(allowed)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
            failures.append({"chunk_index": chunk_index, "segment_ids": list(allowed), "error": str(error)})
    return {"transcript_id": transcript_id, "model": model, "method": "ollama-local",
            "complete": len(covered) == len(segments) and not failures,
            "coverage": {"covered_segments": len(set(covered)), "total_segments": len(segments),
                         "failed_chunks": failures},
            "items": all_items,
            "message": "語意整理結果仍須人工核對；此 API 不會自動寫入台帳。"}


def save_local_outcome(conn, transcript_id, item, *, now=None):
    """Persist one explicitly selected, citation-validated local model result as an editable draft."""
    transcript = get_transcript(conn, transcript_id)
    if not isinstance(item, dict):
        raise db.ValidationError(["成果格式錯誤"])
    required = ("topic", "summary", "kind", "action", "deliverable", "owner", "due_date", "evidence")
    if any(not isinstance(item.get(key), (str, list)) for key in required):
        raise db.ValidationError(["成果欄位型別錯誤"])
    if item["kind"] not in ("明確決議", "待辦", "現況說明", "提議", "爭議", "待確認"):
        raise db.ValidationError(["成果分類不在允許清單"])
    if not item["summary"].strip() or not item["topic"].strip() or not item["evidence"]:
        raise db.ValidationError(["成果需有議題、摘要與原文依據"])
    evidence, segments = [], []
    for cite in item["evidence"]:
        if not isinstance(cite, dict) or not isinstance(cite.get("segment_id"), str) or not isinstance(cite.get("quote"), str):
            raise db.ValidationError(["引用欄位格式錯誤"])
        segment = conn.execute(
            "SELECT * FROM transcript_segments WHERE transcript_id=? AND segment_id=?",
            (transcript_id, cite["segment_id"])).fetchone()
        quote = cite["quote"]
        if (not segment or not quote.strip()
                or (quote not in segment["text"] and quote not in segment["raw_text"])):
            raise db.ValidationError(["引用段落不存在或引用文字與原文不一致"])
        if cite["segment_id"] in {entry["segment_id"] for entry in evidence}:
            continue
        segments.append(dict(segment))
        evidence.append({"segment_id": cite["segment_id"], "quote": cite["quote"],
                         "source_audio": segment["source_audio"], "source_id": segment["source_id"],
                         "timestamp_label": segment["timestamp_label"], "raw_timecode": segment["raw_timecode"],
                         "line_start": segment["line_start"], "line_end": segment["line_end"],
                         "location_label": f"{segment['source_audio'] or transcript['filename']}｜{segment['source_id']}｜{segment['timestamp_label']}｜第 {segment['line_start'] or '?'} 行"})
    if not evidence:
        raise db.ValidationError(["成果沒有有效引用"])
    owner = item["owner"].strip()
    due = item["due_date"].strip()
    cited_text = " ".join(entry["quote"] for entry in evidence)
    if owner and owner not in cited_text:
        owner = ""
    if due and (due not in cited_text):
        due = ""
    if due:
        try:
            due = db.parse_date(due)
        except ValueError:
            due = ""
    method = f"ollama:{item.get('model', 'local')}"
    primary = segments[0]
    summary = f"{item['topic'].strip()}｜{item['summary'].strip()}"
    existing = conn.execute(
        "SELECT draft_id FROM drafts WHERE transcript_id=? AND segment_id=? AND extraction_method=? AND summary=?",
        (transcript_id, primary["segment_id"], method, summary)).fetchone()
    if existing:
        return {"created": False, "draft": get_draft(conn, existing["draft_id"])}
    when = now or db.now_str()
    draft_id = db.next_public_id(conn, "drafts", "draft_id", "DR-")
    labels = "\n".join(entry["location_label"] for entry in evidence)
    outcome = {key: item.get(key, "") for key in ("topic", "summary", "kind", "action", "deliverable")}
    conn.execute(
        """INSERT INTO drafts(draft_id, project_id, transcript_id, segment_id, kind, summary,
           source_quote, location_label, timestamp_label, extraction_method, limitation, owner,
           owner_origin, due_date, due_origin, review_status, issue_id, created_at, updated_at,
           evidence_json, outcome_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (draft_id, transcript["project_id"], transcript_id, primary["segment_id"], item["kind"], summary,
         evidence[0]["quote"], labels, "；".join(entry["timestamp_label"] for entry in evidence),
         method, "本機 Ollama 語意候選；來源引用已由後端逐段驗證，仍需人工核對。", owner,
         "原文" if owner else "未指定", due, "原文" if due else "未指定", "草稿", "", when, when,
         json.dumps(evidence, ensure_ascii=False), json.dumps(outcome, ensure_ascii=False)))
    return {"created": True, "draft": get_draft(conn, draft_id)}


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
        raise db.ValidationError(["只有待入帳草稿可以修改"])
    summary = db._clean_text(changes.get("summary", draft["summary"]))
    owner = db._clean_text(changes.get("owner", draft["owner"]))
    if not summary:
        raise db.ValidationError(["摘要不可留空"])
    try:
        due = db.parse_date(changes.get("due_date", draft["due_date"]))
    except ValueError:
        raise db.ValidationError(["期限格式錯誤，請使用 YYYY-MM-DD"])
    kind = db._clean_text(changes.get("kind", draft["kind"]))
    if kind not in ("決議", "明確決議", "待辦", "現況說明", "提議", "爭議", "待確認"):
        raise db.ValidationError(["成果分類不在允許清單"])
    outcome = dict(draft.get("outcome") or {})
    for key in ("action", "deliverable"):
        if key in changes:
            outcome[key] = db._clean_text(changes.get(key))
    owner_origin = draft["owner_origin"] if owner == draft["owner"] else ("人工指定" if owner else "未指定")
    due_origin = draft["due_origin"] if due == draft["due_date"] else ("人工指定" if due else "未指定")
    if (summary, owner, due, kind, outcome, owner_origin, due_origin) == (
            draft["summary"], draft["owner"], draft["due_date"], draft["kind"],
            draft.get("outcome") or {}, draft["owner_origin"], draft["due_origin"]):
        return draft
    when = now or db.now_str()
    conn.execute(
        """UPDATE drafts SET summary=?, kind=?, outcome_json=?, owner=?, owner_origin=?, due_date=?, due_origin=?, updated_at=?
           WHERE draft_id=?""",
        (summary, kind, json.dumps(outcome, ensure_ascii=False), owner, owner_origin, due, due_origin, when, draft_id))
    updated = get_draft(conn, draft_id)
    if updated["source_quote"] != draft["source_quote"]:
        raise db.ValidationError(["原文引用不可修改，請以原文為依據"])
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
    evidence_segment = get_segment(conn, draft["transcript_id"], draft["segment_id"])
    kind = draft["kind"]
    issue_type = {"決議": "會議決議", "明確決議": "會議決議", "待辦": "待辦",
                  "現況說明": "其他", "提議": "其他", "爭議": "爭議", "待確認": "其他"}.get(kind)
    if not issue_type:
        raise db.ValidationError([f"未知的草稿類型 {kind}"])
    description = (
        f"【{kind}】由逐字稿草稿確認入帳。抽取方式：{draft['extraction_method']}。\n"
        "規則抽取，不是模型。提議、疑問、否定與未定案不會直接入帳；"
        "負責人與期限留空表示原文沒有寫明。\n\n"
        f"{draft['summary']}"
    )
    evidence = json.loads(draft.get("evidence_json") or "[]")
    if not evidence:
        evidence = [{"segment_id": draft["segment_id"], "quote": draft["source_quote"],
                     "location_label": draft["location_label"], "source_audio": evidence_segment.get("source_audio", ""),
                     "source_id": evidence_segment.get("source_id", ""), "timestamp_label": evidence_segment["timestamp_label"],
                     "raw_timecode": evidence_segment.get("raw_timecode", ""),
                     "line_start": evidence_segment.get("line_start"), "line_end": evidence_segment.get("line_end")}]
    source_excerpt = "\n".join(entry["quote"] for entry in evidence)
    source_location = "\n".join(entry.get("location_label", draft["location_label"]) for entry in evidence)
    outcome = json.loads(draft.get("outcome_json") or "{}")
    if outcome.get("action"):
        description += f"\n\n待辦動作：{outcome['action']}"
    if outcome.get("deliverable"):
        description += f"\n交付成果：{outcome['deliverable']}"
    data = {
        "project_id": project["project_id"],
        "project": project["name"],
        "issue_type": issue_type,
        "title": _issue_title(draft["summary"]),
        "description": description,
        "source_file": transcript["filename"],
        "source_location": source_location,
        "source_excerpt": source_excerpt,
        "source_audio_id": evidence_segment.get("source_id", ""),
        "source_audio": evidence_segment.get("source_audio") or transcript.get("source_filename", ""),
        "source_time": "\n".join(entry.get("raw_timecode") or entry.get("timestamp_label", "") for entry in evidence),
        "source_evidence_json": json.dumps(evidence, ensure_ascii=False),
        "source_status": "無法驗證",
        "source_checked": "未核對",
        "audio_checked": "未核對",
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
         source_excerpt, issue["issue_id"]))
    for field, value in (
        ("transcript_id", draft["transcript_id"]),
        ("segment_ref", draft["segment_id"]),
        ("draft_id", draft["draft_id"]),
        ("extraction_method", draft["extraction_method"]),
        ("source_quote", source_excerpt),
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
