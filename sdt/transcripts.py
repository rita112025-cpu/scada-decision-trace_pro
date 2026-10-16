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

RULE_V2 = "rule:v2"
RULE_V3 = "rule:v3"
RULE_V4 = "rule:v4"
RULE_V5 = "rule:v5"
EXTRACTION_METHOD = RULE_V5
LIMITATION = (
    "能力限制：此環境沒有可用的既有抽取器，也未呼叫外部 API 或下載模型。"
    "草稿只是可人工審核的候選，不是對會議的自動理解。"
    "目前抽取版本是 rule:v5。明文標記（決議：、定案、決定採用、待辦、請＋姓名＋動作）仍沿用 rule:v2，信心標為高。"
    "口語定案與交辦信心標為中；本機說法清單對上的標為低。"
    "「麻煩」要後面接著對象和動作（例如確認、提供、回覆、更新、補）才列為待辦。"
    "談預算、報價、費用，以及「但是麻煩」這種有條件的請求，不列為待辦。"
    "「你們先」「你先」「你們這邊先」後面接常見動作才列為待辦；句子長或口語本身不構成排除。"
    "問句、假設、建議、否定、取消、報價議價，以及「核准過」「之前已經」「你們先前」這類過去的事，不列為候選。"
    "「那就這樣」若在談價錢或帶著「但是」，不當成定案。"
    "「然後就是核准過的版本」這種歷史敘述不是這次的決議。很短的應聲（對、嗯、好、OK）也不列。"
    "負責人與期限只有這一句明文寫出才填，不依講者或前後文猜測。"
    "同一句若同時像決議又像交辦，只產生一筆決議草稿。"
    "相鄰段落只作核對用，不會把多人的話拼成一句原話。"
    "草稿必須經人工確認後才會寫進正式台帳。沒有列進規則的講法仍可能漏掉，列進去的仍可能誤收。"
)

_JSON_KEYS = (
    "model", "language", "source_filename", "mode", "text", "raw_text",
    "segments", "duration_seconds", "status", "export_time",
)
_SEG_KEYS = ("id", "start", "end", "speaker", "text", "raw_text", "edited", "timestamp_source")
_MODES = {"cloud", "local"}

_STAMP = re.compile(
    rf"^\[(?P<sh>\d{{2,}}):(?P<sm>\d{{2}}):(?P<ss>\d{{2}})(?:\.(?P<sf>\d{{1,3}}))?"
    rf"(?:\s*[–—-]\s*(?P<eh>\d{{2,}}):(?P<em>\d{{2}}):(?P<es>\d{{2}})(?:\.(?P<ef>\d{{1,3}}))?)?\]"
    rf"\s*(?P<rest>.*)$"
)
_BRACKET = re.compile(r"^\[(?P<inside>[^\]]*)\]\s*(?P<rest>.*)$")
_DATE = re.compile(
    r"(?P<y>\d{4})\s*(?:[-/.]|年)\s*(?P<m>\d{1,2})\s*(?:[-/.]|月)\s*(?P<d>\d{1,2})\s*日?"
)
_DECISION_BLOCK = re.compile(
    r"尚未|還未|還沒|未決|未定|沒有決議|未決議|不決議|不是決議|並非決議|不算決議|不構成決議|"
    r"未核准|沒有核准|沒有定案|未定案|暫不|暫緩|先不|不予|不同意|否決|"
    r"不採用|不通過|不確定|無法確認|沒有結論|未有結論|再議|待議|待確認|待決定|"
    r"再討論|提議|提案|建議|擬議|考慮|或許|也許|好像|似乎|"
    r"是否|能不能|能否|要不要|是不是|有沒有|嗎|[？?]"
)
_HISTORICAL = re.compile(
    r"核准過|已核准過|已經核准|先前核准|之前核准|上次核准|當時核准|原本核准|早先核准|曾經核准|"
    r"上次決議|先前決議|之前的決議|當時的決議|以前的決議|"
    r"已經決定過|決定過了|先前決定|之前決定|已經通過"
)
_DECISION_FRAME = re.compile(r"(?:^|\s)決議[：:]|本次決議|這次決議|今天決議|本日決議")
_ANAPHORA = re.compile(r"這個|那樣|這樣|照辦|就照|如前|如上")
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
            "line_start": None,
            "line_end": None,
            "edited": seg["edited"],
            "location_label": f"段落 {sid}　{label}",
            "time_kind": "complete",
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
        "time_counts": _time_counts(parsed),
        "notes": ["JSON 欄位依 Transcript_translation 的匯出格式讀取；檔案裡多出來的欄位會略過，不會寫進台帳。"],
    }


def _frac_ms(frac):
    if not frac:
        return 0
    return int(frac) * (10 ** (3 - len(frac)))


def _clock_ms(hours, minutes, seconds, frac):
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + _frac_ms(frac)


def _fmt_ms(ms):
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, milli = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milli:03d}"


def _ms_to_seconds(ms):
    return f"{ms / 1000:.3f}"


def _parse_speaker(rest):
    """只在冒號左側不像決議或交辦用語時，把左側當成講者。避免把「決議：」吃掉。"""
    matched = re.match(r"^([^:：]{1,40})[:：](.*)$", rest)
    if not matched:
        return "", rest.strip()
    left, right = matched.group(1).strip(), matched.group(2).strip()
    if not right:
        return "", rest.strip()
    if any(key in left for key in ("決議", "待辦", "請", "定案", "核准", "確認")):
        return "", rest.strip()
    return left, right


def _time_error(line_no, message, text=""):
    return {
        "time_kind": "time_error",
        "speaker": "",
        "text": text,
        "start_seconds": "",
        "end_seconds": "",
        "timestamp_label": "時間錯誤",
        "timestamp_source": f"第 {line_no} 行：{message}",
    }


def _classify_time(line, line_no):
    """辨識一行開頭的時間標記。失敗時指出行號，不補造時間。"""
    stripped = line.strip()
    matched = _STAMP.match(stripped)
    if matched:
        rest = matched.group("rest").strip()
        try:
            start_m = int(matched.group("sm"))
            start_s = int(matched.group("ss"))
            start_ms = _clock_ms(int(matched.group("sh")), start_m, start_s, matched.group("sf"))
        except ValueError:
            return _time_error(line_no, "時間不是有效數字，沒有補造時間。", rest)
        if start_m > 59 or start_s > 59:
            return _time_error(line_no, "分或秒超出 0–59，沒有補造時間。", rest)
        end_ms = None
        if matched.group("eh") is not None:
            end_m = int(matched.group("em"))
            end_s = int(matched.group("es"))
            if end_m > 59 or end_s > 59:
                return _time_error(line_no, "結束時間的分或秒超出 0–59，沒有補造時間。", rest)
            end_ms = _clock_ms(int(matched.group("eh")), end_m, end_s, matched.group("ef"))
            if end_ms <= start_ms:
                return _time_error(line_no, "結束時間早於或等於開始時間，沒有補造時間。", rest)
        speaker = ""
        spoken = rest
        if end_ms is None:
            speaker, spoken = _parse_speaker(rest)
        if end_ms is None:
            label = f"{_fmt_ms(start_ms)}（無結束時間）"
            kind = "start_only"
            end_seconds = ""
        else:
            label = f"{_fmt_ms(start_ms)}–{_fmt_ms(end_ms)}"
            kind = "complete"
            end_seconds = _ms_to_seconds(end_ms)
        return {
            "time_kind": kind,
            "speaker": speaker,
            "text": spoken,
            "start_seconds": _ms_to_seconds(start_ms),
            "end_seconds": end_seconds,
            "timestamp_label": label,
            "timestamp_source": "",
        }
    bracket = _BRACKET.match(stripped)
    if bracket and (":" in bracket.group("inside")) and any(ch.isdigit() for ch in bracket.group("inside")):
        inside = bracket.group("inside")
        if inside.startswith("-") or ":-" in inside:
            message = "時間不可為負，沒有補造時間。"
        elif re.search(r"[–—-]\s*$", inside) or re.search(r"[–—-]\s*[–—-]", inside):
            message = "有區間符號但沒有完整的結束時間，沒有補造時間。"
        elif re.search(r"\.\d{4,}", inside):
            message = "小數秒只接受 1 到 3 位，沒有補造時間。"
        else:
            message = "時間格式無法辨識，沒有補造時間。"
        err = _time_error(line_no, message)
        err["text"] = bracket.group("rest").strip()
        return err
    return {
        "time_kind": "no_time",
        "speaker": "",
        "text": stripped,
        "start_seconds": "",
        "end_seconds": "",
        "timestamp_label": "無時間資訊",
        "timestamp_source": "",
    }


def _time_counts(segments):
    counts = {"complete": 0, "start_only": 0, "no_time": 0, "time_error": 0}
    for seg in segments:
        kind = seg.get("time_kind")
        if kind not in counts:
            if seg.get("timestamp_label") == "時間錯誤":
                kind = "time_error"
            elif seg.get("timestamp_label") == "無時間資訊" or not seg.get("start_seconds"):
                kind = "no_time"
            elif not seg.get("end_seconds"):
                kind = "start_only"
            else:
                kind = "complete"
        counts[kind] += 1
    return counts


def _summarize_txt_times(segments):
    counts = _time_counts(segments)
    errors = [s["timestamp_source"] for s in segments if s.get("time_kind") == "time_error" and s.get("timestamp_source")]
    if counts["time_error"]:
        status = "有時間錯誤"
        detail = "部分行的時間無法採用，已在該行標成時間錯誤，沒有補造時間。" + "".join(errors)
    elif segments and counts["complete"] == len(segments):
        status = "齊全"
        detail = "每一段都有檔案提供的開始與結束時間，沒有另外估算。"
    elif segments and counts["no_time"] == len(segments):
        status = "無時間資訊"
        detail = "此 TXT 沒有可辨識的時間標記，已依行號定位，時間標為無時間資訊，沒有補造時間。"
    else:
        status = "不齊全"
        detail = (
            f"完整時間 {counts['complete']} 段、只有開始時間 {counts['start_only']} 段、"
            f"沒有時間 {counts['no_time']} 段。沒有結束時間的段落留空，不推估。"
        )
    notes = [
        f"完整時間 {counts['complete']} 段、只有開始時間 {counts['start_only']} 段、"
        f"沒有時間 {counts['no_time']} 段、時間錯誤 {counts['time_error']} 段。",
        detail,
    ]
    return status, detail, counts, notes


def parse_txt(text):
    """逐行解析。空白行略過，不要求段與段之間空行。一行格式不符不會把其他行改成無時間。

    開頭的中文標題列保留為沒有時間的段落，不會讓後面的時間列整份失效。
    換行除了 LF、CRLF，也接受只含 CR，以及 Unicode 行分隔，避免整份被併成一段。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    segments = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        classified = _classify_time(line, line_no)
        label = classified["timestamp_label"]
        segments.append({
            "source_segment_id": str(line_no),
            "seq": len(segments) + 1,
            "text": classified["text"],
            "raw_text": line,
            "speaker": classified["speaker"],
            "start_seconds": classified["start_seconds"],
            "end_seconds": classified["end_seconds"],
            "timestamp_label": label,
            "timestamp_source": classified["timestamp_source"],
            "line_start": line_no,
            "line_end": line_no,
            "edited": False,
            "location_label": f"第 {line_no} 行　{label}",
            "time_kind": classified["time_kind"],
        })
    if not segments:
        raise db.ValidationError(["檔案是空的，沒有可匯入的文字"])
    status, detail, counts, notes = _summarize_txt_times(segments)
    return {
        "format": "txt",
        "segments": segments,
        "timestamp_status": status,
        "timestamp_detail": detail,
        "time_counts": counts,
        "model": "",
        "language": "",
        "source_filename": "",
        "mode": "",
        "duration_seconds": "",
        "export_status": "",
        "export_time": "",
        "notes": notes,
    }


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
        "time_counts": parsed.get("time_counts") or _time_counts(parsed["segments"]),
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


def _decision_rule(sentence):
    """回傳 (規則編號, 理由)。單獨的核准、決定、確認，以及歷史敘述，不構成這次的決議。"""
    if _DECISION_BLOCK.search(sentence):
        return None
    if _DECISION_FRAME.search(sentence) or sentence.startswith("決議"):
        return "D1", "這一句有明確的「決議」標記，先列為本次定案候選。單獨出現的核准或確認不會走這條規則。"
    if _HISTORICAL.search(sentence):
        return None
    if "定案" in sentence:
        return "D2", "這一句寫了「定案」，先列為本次定案候選。不是只因為出現「決定」。"
    if re.search(r"(?:決定|確定|同意)(?:採用|使用)", sentence):
        return "D3", "這一句寫明決定採用、確定採用、同意採用，或對應的「使用」。單獨的「決定」不算。"
    if re.search(r"(?:會議|審查)通過|(?:本次|這次|今天|本日)通過", sentence):
        return "D4", "這一句寫明會議通過、審查通過，或本次／今天通過。單獨的「已通過」不算。"
    if "予以確認" in sentence:
        return "D5", "這一句寫明「予以確認」。單獨的「確認」不算。"
    if re.search(r"(?:本次|這次|今天|本日)核准", sentence):
        return "D6", "這一句寫明本次或今天核准。單獨的「核准」或「核准過」不算。"
    return None


def _todo_rule(sentence):
    if _TODO_BLOCK.search(sentence):
        return None
    if "待辦" in sentence:
        return "T1", "這一句含「待辦」。否定與取消若已認得會先排除；沒列到的講法仍要人工看。"
    if extract_owner(sentence):
        return "T2", "這一句是「請＋姓名＋動作」。姓名只取自這一句，不依講者或前後文補。"
    if re.search(r"請於|應於", sentence) and extract_due(sentence):
        return "T3", "這一句寫明請於或應於某個日期之前。日期只取自這一句。"
    return None


class CueError(Exception):
    """本機說法清單格式不符。訊息不含清單內容。"""


_V2_CUE = {
    "D1": "決議：",
    "D2": "定案",
    "D3": "決定採用",
    "D4": "會議通過",
    "D5": "予以確認",
    "D6": "本次核准",
    "T1": "待辦",
    "T2": "請＋姓名＋動作",
    "T3": "請於／應於",
}
_DECISION_CUES = (
    "那就這樣",
    "就這樣定",
    "就照這個做",
    "我們就採用",
    "確定用",
    "不用改了",
    "先維持原案",
    "OK那就",
)
_TODO_CUES = (
    "回去確認",
    "要再提供",
    "下次會議前",
    "我們這邊會",
    "你們先",
    "再回覆",
    "補給我們",
    "麻煩",
)
_LEAD = re.compile(
    r"^(?:(?:對+|嗯+|喔+|哦+|啊+|恩+|好+|OK|ok)[，,\s]+|(?:就是|然後)[，,\s]+)"
)
_V3_GUARD = re.compile(
    r"嗎|呢|[？?]|要不要|是不是|會不會|有沒有|能不能|能否|是否|對不對|好不好|"
    r"如果|若是|假如|要是|可能|應該|建議|要不然|不然就|不然的話|考慮|或許|也許|好像|似乎|"
    r"不是|不要|先別|別再|沒有要|不用再|不要再|不再|先不要|先不|並沒有|沒要|"
    r"未核准|沒有核准|不採用|不通過|不同意|否決|不確定|"
    r"待辦已取消|已取消|沒有待辦|不是待辦|不麻煩|不必|無需|請勿|暫不|尚未|還沒|"
    r"核准過|已核准|已經核准|先前核准|之前核准|上次|之前已經|以前已經|先前已經|"
    r"之前就|以前就|先前就|早就|已經決定過|決定過了|曾經核准|過了"
)
_COLLOQUIAL_OWNER = (
    re.compile(
        rf"麻煩(?P<name>{_NAME})(?=回去|再|先|幫|確認|提供|回覆|更新|補|看|處理|整理|追蹤|列出|於)"
    ),
    re.compile(rf"請(?P<name>{_NAME})(?=回去|再|先|幫|回覆|更新)"),
)
_GROUP_ADDRESS = {"你", "你們", "各位", "貴司", "廠商", "我們", "大家", "他們"}
_MONEY = re.compile(r"預算|報價|價格|價錢|費用|價金|金額|付款|請款|議價|折扣|成本")
_HEDGE = re.compile(r"但是|不過|可是")
_CONDITIONAL_ASK = re.compile(r"(?:但是|不過|可是|但)(?:你們先|麻煩)")
_ACTION = (
    "回去確認|列出來|想|規劃|規畫|評估|研擬|擬|盤點|清點|檢查|看看|草擬|安排|溝通|聯繫|連絡|"
    "確認|提供|回覆|更新|補上|補充|補給|整理|處理|追蹤|修改|修正|繪製|完成|列出|核對|補"
)
_TROUBLE_V4 = re.compile(
    rf"麻煩(?:你們|各位|貴司|廠商|你|[\u4e00-\u9fff]{{2,4}})[^。！？!?]{{0,40}}?(?:{_ACTION})"
)
_YOU_FIRST_V4 = re.compile(
    rf"你們先(?:把|再|去|幫)?[^。！？!?]{{0,24}}?(?:{_ACTION})"
)
# v5：你們先／你先／你們這邊先，可帶「那／就」與短填充，再接到常見動作。
# 「你們先前」是過去式，不當成「你們先」。句子其餘部分多長都不在這裡擋。
_YOU_FIRST_V5_VERBS = (
    "看一下|對一下|看看|出圖|確認|檢查|整理|提供|修改|更新|回覆|詢問|評估|測試|準備|安排|"
    "跟[^。！？!?]{0,8}?對|"
    "補|列|寄|傳|給|改|問|做|弄|拉|畫(?!面)|寫"
)
_YOU_FIRST_V5 = re.compile(
    r"(?:那|就)?(?:你們這邊先|你們先|你先)(?!前)"
    r"(?:(?:就|那|嗯|把|再|去|幫我們|幫|請)[，,\s]*){0,4}"
    rf"[^。！？!?]{{0,8}}?(?:{_YOU_FIRST_V5_VERBS})"
)
_QUOTE = re.compile(r"報個價|報.{0,2}價")


def _core_len(sentence):
    text = (sentence or "").strip()
    previous = None
    while text and text != previous:
        previous = text
        text = _LEAD.sub("", text).strip()
    core = re.sub(r"[\s，,。！？!?、…\.：:]+", "", text)
    return len(core)


def _colloquial_blocked(sentence):
    if _core_len(sentence) < 4:
        return True
    return _V3_GUARD.search(sentence) is not None


def _matches_decision_cue(sentence, cue):
    if cue == "OK那就":
        return re.search(r"(?i)ok那就", sentence) is not None
    if cue == "確定用" and "確定用戶" in sentence:
        return False
    return cue in sentence


def _decision_cue(sentence, *, tightened=False):
    for cue in _DECISION_CUES:
        if tightened and cue == "那就這樣" and (_MONEY.search(sentence) or _HEDGE.search(sentence)):
            continue
        if _matches_decision_cue(sentence, cue):
            return cue
    return ""


def _price_context(sentence):
    return _MONEY.search(sentence) is not None or _QUOTE.search(sentence) is not None


def _todo_cue(sentence, *, method):
    """v3 的「麻煩」「你們先」較寬。v4 起「麻煩」要有對象與動作。

    v4 的「你們先」只接較短的動作清單。v5 放寬成常見工程交辦動詞，
    仍排除問句以外、由呼叫端先擋下的假設／否定／過去，以及報價議價。
    """
    tight_trouble = method in {RULE_V4, RULE_V5}
    for cue in _TODO_CUES:
        if cue == "麻煩":
            if re.search(r"(?:比較|很|太|有點|真|稍|不)麻煩", sentence):
                continue
            if tight_trouble:
                if _CONDITIONAL_ASK.search(sentence) or _MONEY.search(sentence):
                    continue
                if _TROUBLE_V4.search(sentence):
                    return cue
                continue
            if re.search(r"麻煩.{2,}", sentence):
                return cue
            continue
        if cue == "你們先":
            if method == RULE_V4:
                if _CONDITIONAL_ASK.search(sentence) or _MONEY.search(sentence):
                    continue
                if _YOU_FIRST_V4.search(sentence):
                    return cue
                continue
            if method == RULE_V5:
                if _price_context(sentence):
                    continue
                if _YOU_FIRST_V5.search(sentence):
                    return cue
                continue
            if cue in sentence:
                return cue
            continue
        if cue == "我們這邊會" and "會不會" in sentence:
            continue
        if cue in sentence:
            return cue
    return ""


def _local_cue(sentence, phrases):
    for phrase in phrases or []:
        if phrase and phrase in sentence:
            return phrase
    return ""


def normalize_cues(data):
    """整理本機說法清單。只收字面說法，不執行正規表示式。格式不符時不带回清單內容。"""
    if not isinstance(data, dict):
        raise CueError()
    out = {"decisions": [], "todos": []}
    for key in ("decisions", "todos"):
        rows = data.get(key, [])
        if rows is None:
            rows = []
        if not isinstance(rows, list) or len(rows) > 40:
            raise CueError()
        seen = set()
        for row in rows:
            if not isinstance(row, str):
                raise CueError()
            phrase = row.strip()
            if len(phrase) < 4 or len(phrase) > 40 or any(ord(ch) < 32 for ch in phrase):
                raise CueError()
            if phrase in seen:
                continue
            seen.add(phrase)
            out[key].append(phrase)
    return out


def extract_owner_explicit(sentence):
    """姓名只取自這一句的明文。不看講者。"""
    name = extract_owner(sentence)
    if name:
        return name
    for cre in _COLLOQUIAL_OWNER:
        matched = cre.search(sentence or "")
        if not matched:
            continue
        found = matched.group("name")
        if found in _GROUP_ADDRESS or any(part in found for part in _NAME_BAD_PARTS):
            continue
        if found not in sentence:
            continue
        return found
    return ""


def _explicit_hit(kind, rule_id, reason, also_todo):
    return {
        "kind": kind,
        "rule_id": rule_id,
        "reason": reason,
        "cue": _V2_CUE[rule_id],
        "confidence": "高",
        "also_todo": also_todo,
    }


def _spoken_hit(kind, rule_id, cue, confidence, source, also_todo):
    where = "內建口語" if source == "builtin" else "本機說法清單"
    if kind == "決議":
        reason = f"{where}對上「{cue}」。信心：{confidence}。這不是明文的決議標記，請人工確認是不是這次定案。"
    else:
        reason = (
            f"{where}對上「{cue}」。信心：{confidence}。"
            "請人工確認是不是明確交辦。這一句沒寫的負責人與期限留空。"
        )
    return {
        "kind": kind,
        "rule_id": rule_id,
        "reason": reason,
        "cue": cue,
        "confidence": confidence,
        "also_todo": also_todo,
    }


def _classify_candidate(sentence, method, extra_cues):
    """rule:v2 只看明文標記。v3 起才收口語。v4 收緊寬說法，v5 放寬「你們先」。"""
    decision = _decision_rule(sentence)
    todo = _todo_rule(sentence)
    if decision:
        rule_id, reason = decision
        return _explicit_hit("決議", rule_id, reason, bool(todo))
    if method == RULE_V2:
        if not todo:
            return None
        rule_id, reason = todo
        return _explicit_hit("待辦", rule_id, reason, False)
    if todo:
        rule_id, reason = todo
        return _explicit_hit("待辦", rule_id, reason, False)
    if _colloquial_blocked(sentence):
        return None
    tightened = method in {RULE_V4, RULE_V5}
    cue = _decision_cue(sentence, tightened=tightened)
    todo_cue = _todo_cue(sentence, method=method)
    local_decision = _local_cue(sentence, (extra_cues or {}).get("decisions"))
    local_todo = _local_cue(sentence, (extra_cues or {}).get("todos"))
    if cue:
        return _spoken_hit("決議", "D7", cue, "中", "builtin", bool(todo_cue or local_todo))
    if local_decision:
        return _spoken_hit("決議", "D8", local_decision, "低", "local", bool(todo_cue or local_todo))
    if todo_cue:
        return _spoken_hit("待辦", "T4", todo_cue, "中", "builtin", False)
    if local_todo:
        return _spoken_hit("待辦", "T5", local_todo, "低", "local", False)
    return None


def _review_gaps(kind, owner, due, context_used, also_todo):
    gaps = []
    if not owner:
        gaps.append("缺負責人：這一句沒有明文姓名，欄位留空，不依講者或前後文猜測。")
    if not due:
        gaps.append("缺期限：這一句沒有明文日期，欄位留空，不依前後文猜測。")
    if kind == "決議":
        gaps.append("請人工核對這是本次定案，而不是提議、疑問、否定、取消、歷史狀態或一般描述。")
        if also_todo:
            gaps.append("這一句也像交辦，但同一句只留決議候選，交辦內容請人工另判。")
    else:
        gaps.append("請人工核對這是明確交辦，而不是順口提到的動作或已取消的事項。")
    if context_used:
        gaps.append("有附上相鄰段落的段落 ID、時間與原文供核對指代；那些文字沒有併進這筆原始引用。")
    else:
        gaps.append("這筆只引用本句，沒有把其他段落拼成一句原話。")
    return "".join(gaps)


def _context_ref(seg):
    return {
        "segment_id": seg.get("segment_id") or "",
        "source_segment_id": seg.get("source_segment_id") or "",
        "timestamp_label": seg.get("timestamp_label") or "",
        "text": seg.get("raw_text") or seg.get("text") or "",
    }


def extract_candidates(segments, method=None, extra_cues=None):
    """由段落產生候選草稿。同一句最多一筆；不會把草稿寫進正式台帳。

    前後段只在本句已有候選、且出現指代時附上，用來給人核對。不用前後段多收草稿，
    也不把多段原文接成一句。method 可指定 rule:v2、rule:v3 或 rule:v4，用來重現舊版；省略時用 rule:v5。
    extra_cues 只在 v3 以後生效，問句與歷史敘述仍然先被擋下。
    """
    method = method or EXTRACTION_METHOD
    if method not in {RULE_V2, RULE_V3, RULE_V4, RULE_V5}:
        raise db.ValidationError([f"未知的抽取版本 {method}"])
    cues = None if method == RULE_V2 else extra_cues
    ordered = list(segments)
    found = []
    seen = set()
    for index, seg in enumerate(ordered):
        source_id = str(seg.get("source_segment_id") or f"#{index}")
        for sentence_index, sentence in enumerate(_sentences(seg.get("text") or ""), start=1):
            hit = _classify_candidate(sentence, method, cues)
            if not hit:
                continue
            # 同一來源位置只留一筆（決議優先）。相同句子出現在別的段落要各自保留。
            key = (source_id, sentence_index)
            if key in seen:
                continue
            seen.add(key)
            if method == RULE_V2:
                owner = extract_owner(sentence)
            else:
                owner = extract_owner_explicit(sentence)
            due = extract_due(sentence)
            context = []
            if _ANAPHORA.search(sentence) and index > 0:
                context.append(_context_ref(ordered[index - 1]))
            found.append({
                "segment_key": source_id,
                "segment_id": seg.get("segment_id") or "",
                "sentence_index": sentence_index,
                "kind": hit["kind"],
                "summary": sentence,
                "source_quote": sentence,
                "location_label": seg.get("location_label") or "",
                "timestamp_label": seg.get("timestamp_label") or "",
                "owner": owner,
                "owner_origin": "原文" if owner else "未指定",
                "due_date": due,
                "due_origin": "原文" if due else "未指定",
                "extraction_method": method,
                "limitation": LIMITATION,
                "rule_id": hit["rule_id"],
                "reason": hit["reason"],
                "cue": hit["cue"],
                "confidence": hit["confidence"],
                "review_gaps": _review_gaps(
                    hit["kind"], owner, due, bool(context), hit["also_todo"]),
                "context_refs": context,
            })
    return found


def _public_segment(row):
    item = dict(row)
    item["edited"] = bool(item.get("edited"))
    return item


def _public_draft(row):
    item = dict(row)
    raw = item.get("context_refs") or ""
    if isinstance(raw, list):
        item["context_refs"] = raw
        return item
    try:
        refs = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        refs = []
    item["context_refs"] = refs if isinstance(refs, list) else []
    return item


def _insert_draft(conn, *, project_id, transcript_id, segment_id, cand, when):
    """新增一筆草稿。不改既有草稿，也不確認入帳。"""
    did = db.next_public_id(conn, "drafts", "draft_id", "DR-")
    conn.execute(
        """INSERT INTO drafts(
            draft_id, project_id, transcript_id, segment_id, kind, summary, source_quote,
            location_label, timestamp_label, extraction_method, limitation, owner, owner_origin,
            due_date, due_origin, review_status, issue_id, rule_id, reason, review_gaps,
            context_refs, sentence_index, cue, confidence, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (did, project_id, transcript_id, segment_id, cand["kind"], cand["summary"],
         cand["source_quote"], cand["location_label"], cand["timestamp_label"],
         cand["extraction_method"], cand["limitation"], cand["owner"], cand["owner_origin"],
         cand["due_date"], cand["due_origin"], "草稿", "", cand.get("rule_id") or "",
         cand.get("reason") or "", cand.get("review_gaps") or "",
         json.dumps(cand.get("context_refs") or [], ensure_ascii=False),
         str(cand.get("sentence_index") or ""), cand.get("cue") or "", cand.get("confidence") or "",
         when, when))
    return get_draft(conn, did)


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
        drafts.append(_insert_draft(
            conn, project_id=project["project_id"], transcript_id=tid,
            segment_id=seg["segment_id"], cand=cand, when=when))
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
        f"對上的說法：{draft.get('cue') or '（無）'}。信心：{draft.get('confidence') or '（無）'}。\n"
        "規則抽取，不是模型。沒有被收成草稿的句子不代表已經判斷正確；"
        "負責人與期限留空表示這一句沒有明文。\n\n"
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


def _reparse_plan(conn, transcript_id):
    item = get_transcript(conn, transcript_id)
    if item["format"] != "txt":
        raise db.ValidationError(["只有 TXT 逐字稿可以重新解析時間。JSON 的時間來自原檔欄位，不會在這裡改寫。"])
    parsed = parse_txt(item["raw_text"])
    existing = list_segments(conn, transcript_id)
    by_line = {}
    for seg in existing:
        key = str(seg["line_start"] if seg["line_start"] else seg["source_segment_id"])
        by_line[key] = seg
    changes = []
    matched = set()
    for new in parsed["segments"]:
        key = str(new["line_start"])
        old = by_line.get(key)
        if old is None:
            continue
        matched.add(key)
        changed = any((
            (old.get("start_seconds") or "") != new["start_seconds"],
            (old.get("end_seconds") or "") != new["end_seconds"],
            (old.get("timestamp_label") or "") != new["timestamp_label"],
            (old.get("text") or "") != new["text"],
            (old.get("speaker") or "") != new["speaker"],
        ))
        changes.append({
            "segment_id": old["segment_id"],
            "line": new["line_start"],
            "changed": changed,
            "raw_text": old["raw_text"],
            "before": {
                "start_seconds": old.get("start_seconds") or "",
                "end_seconds": old.get("end_seconds") or "",
                "timestamp_label": old.get("timestamp_label") or "",
                "text": old.get("text") or "",
            },
            "after": {
                "start_seconds": new["start_seconds"],
                "end_seconds": new["end_seconds"],
                "timestamp_label": new["timestamp_label"],
                "timestamp_source": new["timestamp_source"],
                "text": new["text"],
                "speaker": new["speaker"],
                "location_label": new["location_label"],
            },
        })
    drafts = conn.execute(
        "SELECT draft_id, review_status, source_quote, segment_id FROM drafts WHERE transcript_id=?",
        (transcript_id,)).fetchall()
    issues = conn.execute(
        "SELECT issue_id, source_quote FROM issues WHERE transcript_id=?", (transcript_id,)).fetchall()
    return {
        "transcript": item,
        "parsed": parsed,
        "changes": changes,
        "unmatched_existing": [by_line[k]["segment_id"] for k in by_line if k not in matched],
        "unmatched_new_lines": [s["line_start"] for s in parsed["segments"] if str(s["line_start"]) not in by_line],
        "new_by_line": {str(s["line_start"]): s for s in parsed["segments"]},
        "drafts": [dict(r) for r in drafts],
        "issues": [dict(r) for r in issues],
    }


def reparse_preview(conn, transcript_id):
    """預覽重新解析。不寫入、不刪除段落、不改草稿狀態或事項。"""
    plan = _reparse_plan(conn, transcript_id)
    parsed = plan["parsed"]
    return {
        "transcript_id": transcript_id,
        "applied": False,
        "timestamp_status": parsed["timestamp_status"],
        "timestamp_detail": parsed["timestamp_detail"],
        "time_counts": parsed["time_counts"],
        "changes": [
            {
                "segment_id": c["segment_id"],
                "line": c["line"],
                "changed": c["changed"],
                "before": c["before"],
                "after": {k: c["after"][k] for k in ("start_seconds", "end_seconds", "timestamp_label", "text")},
            }
            for c in plan["changes"]
        ],
        "unmatched_existing": plan["unmatched_existing"],
        "unmatched_new_lines": plan["unmatched_new_lines"],
        "drafts": [{"draft_id": d["draft_id"], "review_status": d["review_status"]} for d in plan["drafts"]],
        "issue_ids": [i["issue_id"] for i in plan["issues"]],
        "message": "這是預覽，尚未寫入。套用時只更新對得上的行的時間與抽取用正文，原始行、草稿審核狀態、事項與原始引用都保留。",
    }


def reparse_apply(conn, transcript_id, *, now=None):
    """套用重新解析。不刪段、不改 raw_text、不改草稿的原文與審核狀態、不改已入帳事項。"""
    plan = _reparse_plan(conn, transcript_id)
    quotes_before = {d["draft_id"]: d["source_quote"] for d in plan["drafts"]}
    status_before = {d["draft_id"]: d["review_status"] for d in plan["drafts"]}
    issue_quotes = {i["issue_id"]: i["source_quote"] for i in plan["issues"]}
    when = now or db.now_str()
    for change in plan["changes"]:
        if not change["changed"]:
            continue
        after = change["after"]
        conn.execute(
            """UPDATE transcript_segments
               SET text=?, speaker=?, start_seconds=?, end_seconds=?, timestamp_label=?,
                   timestamp_source=?
               WHERE segment_id=?""",
            (after["text"], after["speaker"], after["start_seconds"], after["end_seconds"],
             after["timestamp_label"], after["timestamp_source"], change["segment_id"]))
        conn.execute(
            """UPDATE drafts SET timestamp_label=?, location_label=?, updated_at=?
               WHERE segment_id=?""",
            (after["timestamp_label"], after["location_label"], when, change["segment_id"]))
    inserted = []
    transcript = plan["transcript"]
    for line_no in plan["unmatched_new_lines"]:
        new = plan["new_by_line"][str(line_no)]
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM transcript_segments WHERE transcript_id=?",
            (transcript_id,)).fetchone()[0] + 1
        sid = f"{transcript_id}-S{seq:04d}"
        conn.execute(
            """INSERT INTO transcript_segments(
                segment_id, transcript_id, project_id, source_segment_id, seq, text, raw_text,
                speaker, start_seconds, end_seconds, timestamp_label, timestamp_source,
                line_start, line_end, edited
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, transcript_id, transcript["project_id"], new["source_segment_id"], seq, new["text"],
             new["raw_text"], new["speaker"], new["start_seconds"], new["end_seconds"],
             new["timestamp_label"], new["timestamp_source"], new["line_start"], new["line_end"], 0))
        inserted.append(sid)
    parsed = plan["parsed"]
    total = conn.execute(
        "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id=?", (transcript_id,)).fetchone()[0]
    conn.execute(
        "UPDATE transcripts SET segment_count=?, timestamp_status=?, timestamp_detail=? WHERE transcript_id=?",
        (total, parsed["timestamp_status"], parsed["timestamp_detail"], transcript_id))
    for draft_id, quote in quotes_before.items():
        row = conn.execute(
            "SELECT source_quote, review_status FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if row["source_quote"] != quote or row["review_status"] != status_before[draft_id]:
            raise db.ValidationError(["重新解析改到了草稿原文或審核狀態，已中止"])
    for issue_id, quote in issue_quotes.items():
        row = conn.execute("SELECT source_quote FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        if row["source_quote"] != quote:
            raise db.ValidationError(["重新解析改到了事項原文，已中止"])
    stored = get_transcript(conn, transcript_id)
    if stored["raw_text"] != transcript["raw_text"]:
        raise db.ValidationError(["重新解析改到了逐字稿原文，已中止"])
    return {
        "transcript_id": transcript_id,
        "applied": True,
        "updated_segments": sum(1 for c in plan["changes"] if c["changed"]),
        "inserted_segments": inserted,
        "kept_existing_segments": plan["unmatched_existing"],
        "timestamp_status": parsed["timestamp_status"],
        "timestamp_detail": parsed["timestamp_detail"],
        "time_counts": parsed["time_counts"],
        "drafts": [{"draft_id": d["draft_id"], "review_status": status_before[d["draft_id"]]} for d in plan["drafts"]],
        "issue_ids": list(issue_quotes),
        "message": "已重新解析時間。原始行、草稿審核狀態、事項與原始引用仍在。新出現的行只補段落，不自動產生草稿，也不入帳。",
    }


def _prepare_segment(seg):
    item = dict(seg)
    if not item.get("location_label"):
        line = item.get("line_start")
        label = item.get("timestamp_label") or ""
        item["location_label"] = f"第 {line} 行　{label}" if line else (item.get("source_segment_id") or "")
    item["source_segment_id"] = str(item.get("source_segment_id") or "")
    return item


def _list_extraction_runs(conn, transcript_id):
    rows = conn.execute(
        """SELECT run_id, transcript_id, extraction_method, created_at, added_count, skipped_count
           FROM extraction_runs WHERE transcript_id=? ORDER BY run_id""",
        (transcript_id,)).fetchall()
    return [dict(r) for r in rows]


def _legacy_body(quote):
    """舊版原文可能把時間標記留在句首。認得出來就拿掉，只用於對位置，不改已存原文。"""
    text = (quote or "").strip()
    matched = _STAMP.match(text)
    if not matched:
        return text, False
    return matched.group("rest").strip(), True


def _stamp_ms_pair(matched):
    start = _clock_ms(int(matched["sh"]), int(matched["sm"]), int(matched["ss"]), matched["sf"])
    if matched["eh"] is None:
        return start, None
    end = _clock_ms(int(matched["eh"]), int(matched["em"]), int(matched["es"]), matched["ef"])
    return start, end


def _segment_matches_quote_stamp(quote, seg):
    quoted = _STAMP.match((quote or "").strip())
    if not quoted:
        return False
    want = _stamp_ms_pair(quoted)
    raw = _STAMP.match((seg.get("raw_text") or "").strip())
    if raw and _stamp_ms_pair(raw) == want:
        return True

    def as_ms(value):
        if value in ("", None):
            return None
        return int(round(float(value) * 1000))

    have = (as_ms(seg.get("start_seconds")), as_ms(seg.get("end_seconds")))
    if want[1] is None:
        return have[0] == want[0] and have[1] is None
    return have == want


def _sentence_indexes(segment, body):
    return [
        str(index)
        for index, sentence in enumerate(_sentences(segment.get("text") or ""), start=1)
        if sentence == body
    ]


def _pending_legacy(draft, segment):
    return {
        "draft_id": draft["draft_id"],
        "segment_id": draft.get("segment_id") or "",
        "line": (segment or {}).get("line_start") or "",
        "review": "待核對",
        "reason": "ambiguous",
        "source_quote": draft.get("source_quote") or "",
        "note": "這筆舊原文無法對到唯一的來源位置。原句與審核狀態保留，沒有把它當成新位置，也沒有新增草稿。",
    }


def _map_legacy_drafts(drafts, prepared, occupied=()):
    """沒有句序的舊草稿依來源位置對上。時間前綴只用來定位，不回寫原文。

    對到唯一的（檔案內段落、行、句序）就佔住該位置。對不到或對到多處則列為待核對，
    那些位置不新增草稿。
    """
    by_id = {seg["segment_id"]: seg for seg in prepared if seg.get("segment_id")}
    occupied = set(occupied)
    mapped = {}
    stripped = set()
    pending = []
    suppress = set()
    seen_pending = set()

    def flag(draft, positions):
        if draft["draft_id"] not in seen_pending:
            seen_pending.add(draft["draft_id"])
            pending.append(_pending_legacy(draft, by_id.get(draft.get("segment_id"))))
        for pos in positions:
            suppress.add(pos)

    for draft in drafts:
        if str(draft.get("sentence_index") or ""):
            continue
        body, had_stamp = _legacy_body(draft["source_quote"])
        if had_stamp:
            stripped.add(draft["draft_id"])
        own = by_id.get(draft.get("segment_id"))
        parts = _sentences(body)
        if len(parts) != 1:
            hits = []
            if own and body and body == (own.get("text") or "").strip():
                count = len(_sentences(own.get("text") or ""))
                hits = [(own["segment_id"], str(index)) for index in range(1, count + 1)]
            flag(draft, hits)
            continue
        if own:
            indexes = _sentence_indexes(own, body)
            if len(indexes) == 1:
                pos = (own["segment_id"], indexes[0])
                if pos in occupied:
                    flag(draft, [pos])
                    continue
                if pos in mapped:
                    flag(draft, [pos])
                    flag(mapped[pos], [pos])
                    mapped.pop(pos, None)
                else:
                    mapped[pos] = draft
                continue
            if len(indexes) > 1:
                flag(draft, [(own["segment_id"], index) for index in indexes])
                continue
        hits = []
        for seg in prepared:
            for index in _sentence_indexes(seg, body):
                hits.append((seg["segment_id"], index, seg))
        if had_stamp:
            stamped = [hit for hit in hits if _segment_matches_quote_stamp(draft["source_quote"], hit[2])]
            if stamped:
                hits = stamped
        if len(hits) == 1:
            pos = (hits[0][0], hits[0][1])
            if pos in occupied:
                flag(draft, [pos])
                continue
            if pos in mapped:
                flag(draft, [pos])
                flag(mapped[pos], [pos])
                mapped.pop(pos, None)
            else:
                mapped[pos] = draft
        else:
            flag(draft, [(hit[0], hit[1]) for hit in hits])
    return mapped, stripped, pending, suppress


def _reextract_plan(conn, transcript_id):
    """比對目前規則與既有草稿。不寫入。

    已有句子位置的草稿佔住 (段落, 句序)。沒有句子位置的舊草稿先辨識句首時間標記，
    再依段落與句序一對一佔位；原句不回寫。無法唯一定位的舊草稿列為待核對，不新增草稿。
    """
    item = get_transcript(conn, transcript_id)
    stored = list_segments(conn, transcript_id)
    prepared = [_prepare_segment(seg) for seg in stored]
    candidates = extract_candidates(prepared)
    drafts = [_public_draft(r) for r in conn.execute(
        "SELECT * FROM drafts WHERE transcript_id=? ORDER BY draft_id", (transcript_id,))]
    issues = [dict(r) for r in conn.execute(
        "SELECT issue_id, source_quote FROM issues WHERE transcript_id=?", (transcript_id,))]
    positioned = {}
    for draft in drafts:
        index = str(draft.get("sentence_index") or "")
        if index:
            positioned[(draft["segment_id"], index)] = draft
    by_id = {seg["segment_id"]: seg for seg in prepared if seg.get("segment_id")}
    mapped, stripped, pending, suppress = _map_legacy_drafts(drafts, prepared, positioned)
    added = []
    skipped = []
    claimed = set(positioned) | set(mapped)
    for cand in candidates:
        seg = by_id.get(cand.get("segment_id") or "")
        if seg is None:
            matches = [s for s in prepared if s.get("source_segment_id") == cand["segment_key"]]
            seg = matches[0] if len(matches) == 1 else None
        sentence_index = str(cand["sentence_index"])
        base = {
            "sentence_index": sentence_index,
            "kind": cand["kind"],
            "rule_id": cand["rule_id"],
            "cue": cand.get("cue") or "",
            "confidence": cand.get("confidence") or "",
            "line": (seg or {}).get("line_start") or "",
            "source_quote": cand["source_quote"],
        }
        if seg is None or not seg.get("segment_id"):
            skipped.append(dict(base, segment_id="", draft_id="", reason="not_found"))
            continue
        pos = (seg["segment_id"], sentence_index)
        base["segment_id"] = seg["segment_id"]
        if pos in claimed:
            holder = positioned.get(pos) or mapped.get(pos)
            if pos in positioned:
                reason = "position"
            elif holder and holder["draft_id"] in stripped:
                reason = "legacy_position"
            else:
                reason = "legacy_quote"
            skipped.append(dict(
                base, draft_id=holder["draft_id"] if holder else "", reason=reason))
            continue
        if pos in suppress:
            skipped.append(dict(base, draft_id="", reason="ambiguous"))
            continue
        added.append(dict(base, candidate=cand))
        claimed.add(pos)
    return {
        "transcript": item,
        "segments": stored,
        "drafts": drafts,
        "issues": issues,
        "added": added,
        "skipped": skipped,
        "pending_review": pending,
    }


def _public_extract_row(row):
    return {
        "segment_id": row.get("segment_id") or "",
        "sentence_index": row.get("sentence_index") or "",
        "kind": row.get("kind") or "",
        "rule_id": row.get("rule_id") or "",
        "cue": row.get("cue") or "",
        "confidence": row.get("confidence") or "",
        "line": row.get("line") or "",
        "source_quote": row.get("source_quote") or "",
        "draft_id": row.get("draft_id") or "",
        "reason": row.get("reason") or "",
    }


def reextract_preview(conn, transcript_id):
    """預覽重新抽取。不寫入、不改既有草稿、不確認入帳。"""
    plan = _reextract_plan(conn, transcript_id)
    return {
        "transcript_id": transcript_id,
        "applied": False,
        "extraction_method": EXTRACTION_METHOD,
        "added_count": len(plan["added"]),
        "skipped_count": len(plan["skipped"]),
        "kept_count": len(plan["drafts"]),
        "added": [_public_extract_row(row) for row in plan["added"]],
        "skipped": [_public_extract_row(row) for row in plan["skipped"]],
        "existing_drafts": [
            {
                "draft_id": d["draft_id"],
                "review_status": d["review_status"],
                "sentence_index": d.get("sentence_index") or "",
            }
            for d in plan["drafts"]
        ],
        "issue_ids": [i["issue_id"] for i in plan["issues"]],
        "runs": _list_extraction_runs(conn, transcript_id),
        "pending_review": plan["pending_review"],
        "pending_count": len(plan["pending_review"]),
        "message": _reextract_message(plan, applied=False),
    }


def _freeze_reextract(plan):
    return {
        "drafts": {
            d["draft_id"]: (
                d["summary"], d["source_quote"], d["review_status"], str(d.get("sentence_index") or ""),
            )
            for d in plan["drafts"]
        },
        "issues": {i["issue_id"]: i["source_quote"] for i in plan["issues"]},
        "raw_text": plan["transcript"]["raw_text"],
        "segments": [
            (s["segment_id"], s["text"], s["raw_text"], s.get("start_seconds") or "", s.get("end_seconds") or "")
            for s in plan["segments"]
        ],
    }


def _assert_reextract_unchanged(conn, transcript_id, frozen):
    for draft_id, previous in frozen["drafts"].items():
        row = conn.execute(
            "SELECT summary, source_quote, review_status, sentence_index FROM drafts WHERE draft_id=?",
            (draft_id,)).fetchone()
        current = (row["summary"], row["source_quote"], row["review_status"], row["sentence_index"] or "")
        if current != previous:
            raise db.ValidationError(["重新抽取改到了既有草稿，已中止"])
    for issue_id, quote in frozen["issues"].items():
        row = conn.execute("SELECT source_quote FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        if row["source_quote"] != quote:
            raise db.ValidationError(["重新抽取改到了已入帳事項，已中止"])
    stored = get_transcript(conn, transcript_id)
    if stored["raw_text"] != frozen["raw_text"]:
        raise db.ValidationError(["重新抽取改到了逐字稿原文，已中止"])
    segments = [
        (s["segment_id"], s["text"], s["raw_text"], s.get("start_seconds") or "", s.get("end_seconds") or "")
        for s in list_segments(conn, transcript_id)
    ]
    if segments != frozen["segments"]:
        raise db.ValidationError(["重新抽取改到了逐字稿段落，已中止"])


def reextract_apply(conn, transcript_id, *, now=None):
    """補上來源位置還沒有的候選。不覆寫既有草稿與事項，不自動確認入帳。"""
    plan = _reextract_plan(conn, transcript_id)
    frozen = _freeze_reextract(plan)
    when = now or db.now_str()
    transcript = plan["transcript"]
    new_ids = []
    for row in plan["added"]:
        draft = _insert_draft(
            conn, project_id=transcript["project_id"], transcript_id=transcript_id,
            segment_id=row["segment_id"], cand=row["candidate"], when=when)
        if draft["review_status"] != "草稿":
            raise db.ValidationError(["重新抽取不應確認入帳，已中止"])
        new_ids.append(draft["draft_id"])
    _assert_reextract_unchanged(conn, transcript_id, frozen)
    detail = {
        "added": [
            {
                "draft_id": draft_id,
                "segment_id": row["segment_id"],
                "sentence_index": row["sentence_index"],
                "kind": row["kind"],
                "rule_id": row["rule_id"],
            }
            for draft_id, row in zip(new_ids, plan["added"])
        ],
        "skipped": [
            {
                "draft_id": row.get("draft_id") or "",
                "segment_id": row.get("segment_id") or "",
                "sentence_index": row.get("sentence_index") or "",
                "kind": row.get("kind") or "",
                "rule_id": row.get("rule_id") or "",
                "reason": row.get("reason") or "",
            }
            for row in plan["skipped"]
        ],
        "pending_review": [
            {"draft_id": row["draft_id"], "reason": row["reason"]}
            for row in plan["pending_review"]
        ],
    }
    run_id = db.next_public_id(conn, "extraction_runs", "run_id", "XR-")
    conn.execute(
        """INSERT INTO extraction_runs(
            run_id, transcript_id, extraction_method, created_at, added_count, skipped_count, detail
        ) VALUES (?,?,?,?,?,?,?)""",
        (run_id, transcript_id, EXTRACTION_METHOD, when, len(new_ids), len(plan["skipped"]),
         json.dumps(detail, ensure_ascii=False)))
    return {
        "transcript_id": transcript_id,
        "applied": True,
        "extraction_method": EXTRACTION_METHOD,
        "added_count": len(new_ids),
        "skipped_count": len(plan["skipped"]),
        "kept_count": len(plan["drafts"]),
        "added_draft_ids": new_ids,
        "run_id": run_id,
        "run": _list_extraction_runs(conn, transcript_id)[-1],
        "issue_ids": [i["issue_id"] for i in plan["issues"]],
        "pending_review": plan["pending_review"],
        "pending_count": len(plan["pending_review"]),
        "message": _reextract_message(plan, applied=True),
    }


def _reextract_message(plan, *, applied):
    if applied:
        text = (
            "已依來源位置補上新候選，狀態是草稿，沒有確認入帳。"
            "既有摘要、原文、審核狀態與已入帳事項沒有覆寫。"
        )
    else:
        text = (
            "這是重新抽取的預覽，尚未寫入，也不會確認入帳。"
            "只會列出來源位置還沒有草稿的候選。"
            "既有摘要、原文、審核狀態與已入帳事項都不會被覆寫。"
            "時間重新解析是另一個動作。"
        )
    if plan["pending_review"]:
        text += "有舊草稿無法對到唯一的來源位置，已在結果標為待核對，沒有為它們新增草稿。"
    return text


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
