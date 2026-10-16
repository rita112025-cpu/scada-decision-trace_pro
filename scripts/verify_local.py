"""本機驗收：只讀使用者指定目錄裡的 TXT，在記憶體裡產生候選，報告寫回該目錄。

不連外網、不呼叫模型、不確認入帳、不改寫原始檔。
終端只印狀態碼與彙總數字。完整報告、候選原文、人工備註、檔名與路徑只留在報告檔。
預設目錄是 C:\\Users\\user\\sdt-verify。雲端代理不應拿這個腳本去讀真實會議。
"""
import argparse
import csv
import io
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdt import db, transcripts  # noqa: E402

DEFAULT_DIR = pathlib.Path(r"C:\Users\user\sdt-verify")
DOCX_NAME = "SCADA會議紀錄.docx"

_PROBLEM_TEXT = {
    "missing_file": "缺少檔案識別，不能只靠行號評分",
    "missing_line": "缺少行號",
    "missing_sentence": "缺少句子位置。同一行可能有多句，不能把整行算成命中",
    "duplicate": "同一檔案、同一行、同一句、同一類型重複標註",
    "not_found": "找不到這個檔案或行，或句子位置超出該行句數",
    "ambiguous_line": "這個行號在檔案裡對到不只一個段落，無法唯一定位",
    "duplicate_prediction": "預測在同一檔案、同一行、同一句、同一類型出現超過一次，無法一對一配對",
}


class VerifyProblem(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("status=2\nerror=failed\n")
        raise SystemExit(2)


def read_only_text(path):
    try:
        before = path.stat()
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        raise VerifyProblem("failed")
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            text = handle.read()
    except UnicodeDecodeError:
        raise VerifyProblem("not_utf8")
    except VerifyProblem:
        raise
    except Exception:
        raise VerifyProblem("failed")
    try:
        after = path.stat()
    except OSError:
        raise VerifyProblem("failed")
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise VerifyProblem("source_changed")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _index(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise VerifyProblem("bad_annotation")
    if value in ("", None):
        return ""
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if not text:
        return ""
    if not text.isdigit():
        raise VerifyProblem("bad_annotation")
    return str(int(text))


def _load_json(path):
    try:
        data = json.loads(read_only_text(path))
    except VerifyProblem:
        raise
    except json.JSONDecodeError:
        raise VerifyProblem("bad_annotation")
    except Exception:
        raise VerifyProblem("bad_annotation")
    return data


def _as_items(data):
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        raise VerifyProblem("bad_annotation")
    items = []
    for row in data:
        if not isinstance(row, dict):
            raise VerifyProblem("bad_annotation")
        kind = str(row.get("kind") or "").strip()
        if kind not in {"決議", "待辦"}:
            continue
        items.append({
            "kind": kind,
            "file": str(row.get("file") or "").strip(),
            "line": _index(row.get("line", "")),
            "sentence": _index(row.get("sentence", "")),
            "segment_id": str(row.get("segment_id") or "").strip(),
            "note": str(row.get("note") or ""),
        })
    return items


def load_table(path):
    if path is None or not path.is_file():
        return []
    if path.suffix.lower() == ".json":
        return _as_items(_load_json(path))
    try:
        rows = list(csv.DictReader(io.StringIO(read_only_text(path))))
    except VerifyProblem:
        raise
    except csv.Error:
        raise VerifyProblem("bad_annotation")
    except Exception:
        raise VerifyProblem("bad_annotation")
    return _as_items(rows)


def load_cues(directory):
    """讀驗收目錄裡的 cues.json。沒有檔案就只用內建規則。內容不回傳到終端。"""
    path = pathlib.Path(directory) / "cues.json"
    if not path.is_file():
        return {"decisions": [], "todos": []}
    try:
        data = json.loads(read_only_text(path))
    except VerifyProblem:
        raise
    except Exception:
        raise VerifyProblem("bad_cues")
    try:
        return transcripts.normalize_cues(data)
    except transcripts.CueError:
        raise VerifyProblem("bad_cues")


def candidate_rows(parsed, filename, extra_cues=None):
    rows = []
    for cand in transcripts.extract_candidates(parsed["segments"], extra_cues=extra_cues):
        seg = next(s for s in parsed["segments"] if s["source_segment_id"] == cand["segment_key"])
        rows.append({
            "kind": cand["kind"],
            "file": filename,
            "line": str(seg["line_start"] or ""),
            "sentence": str(cand["sentence_index"]),
            "segment_id": seg["source_segment_id"],
            "rule_id": cand["rule_id"],
            "cue": cand.get("cue") or "",
            "confidence": cand.get("confidence") or "",
            "quote": cand["source_quote"],
            "owner": cand["owner"],
            "due_date": cand["due_date"],
        })
    return rows


def line_map(parsed):
    lines = {}
    for seg in parsed["segments"]:
        line = str(seg["line_start"] or "")
        count = len(transcripts._sentences(seg.get("text") or ""))
        if line in lines:
            lines[line] = None
        else:
            lines[line] = count
    return lines


def _location_problem(item, locations):
    if not item.get("file"):
        return "missing_file"
    if not item.get("line"):
        return "missing_line"
    if not item.get("sentence"):
        return "missing_sentence"
    found = locations.get(item["file"])
    if found is None or item["line"] not in found:
        return "not_found"
    count = found[item["line"]]
    if count is None:
        return "ambiguous_line"
    sent = int(item["sentence"])
    if sent < 1 or sent > count:
        return "not_found"
    return ""


def _pair_key(item):
    return (item["kind"], item["file"], item["line"], item["sentence"])


def evaluate_score(gold, predicted, locations):
    """預測與答案以種類、檔名、行號、句序一對一配對。

    任一答案無法唯一定位，或任一側的鍵重複，就整份拒絕評分，不把那些筆算成命中。
    """
    if not gold:
        return {
            "ok": False,
            "empty": True,
            "refused": 0,
            "problems": [],
            "matched_total": 0,
            "by_kind": None,
        }
    problems = []
    groups = {}
    for index, item in enumerate(gold, start=1):
        code = _location_problem(item, locations)
        if code:
            problems.append({"code": code, "index": index})
            continue
        groups.setdefault(_pair_key(item), []).append(index)
    for indexes in groups.values():
        if len(indexes) > 1:
            for index in indexes:
                problems.append({"code": "duplicate", "index": index})
    pred_groups = {}
    for item in predicted:
        if _location_problem(item, locations):
            problems.append({"code": "not_found", "index": 0})
            continue
        key = _pair_key(item)
        pred_groups[key] = pred_groups.get(key, 0) + 1
    for count in pred_groups.values():
        if count > 1:
            problems.append({"code": "duplicate_prediction", "index": 0, "count": count})
    if problems:
        return {
            "ok": False,
            "empty": False,
            "refused": len(problems),
            "problems": problems,
            "matched_total": 0,
            "by_kind": None,
        }
    by_kind = {}
    matched_total = 0
    for kind in ("決議", "待辦"):
        gold_keys = [key for key in groups if key[0] == kind]
        pred_keys = [key for key in pred_groups if key[0] == kind]
        matched = len(set(gold_keys) & set(pred_keys))
        matched_total += matched
        by_kind[kind] = {
            "matched": matched,
            "predictions": len(pred_keys),
            "gold": len(gold_keys),
            "precision": f"{matched}/{len(pred_keys)}" if pred_keys else "無候選（分母 0）",
            "recall": f"{matched}/{len(gold_keys)}" if gold_keys else "無標註（分母 0）",
        }
    return {
        "ok": True,
        "empty": False,
        "refused": 0,
        "problems": [],
        "matched_total": matched_total,
        "by_kind": by_kind,
    }


def analyze_file(path, extra_cues=None):
    text = read_only_text(path).replace("\r\n", "\n").replace("\r", "\n")
    try:
        parsed = transcripts.parse_upload(path.name, text)
    except db.ValidationError:
        raise VerifyProblem("failed")
    except VerifyProblem:
        raise
    except Exception:
        raise VerifyProblem("failed")
    segments = parsed.get("segments") or []
    sentences = 0
    for seg in segments:
        sentences += len(transcripts._sentences(seg.get("text") or ""))
    counts = parsed.get("time_counts") or {}
    return {
        "filename": path.name,
        "time_counts": counts,
        "timestamp_status": parsed.get("timestamp_status", ""),
        "candidates": candidate_rows(parsed, path.name, extra_cues),
        "lines": line_map(parsed),
        "segments": len(segments),
        "sentences": sentences,
        "parse_warnings": counts.get("time_error", 0),
    }


def build_report(directory, analyses, gold, pending, false_hits, docx_present, score, cue_counts=None):
    lines = [
        "本機驗收報告",
        f"目錄：{directory}",
        "原始檔只讀，沒有覆寫、搬移或刪除。沒有把內容送到外部服務，也沒有確認入帳。",
        f"抽取規則：{transcripts.EXTRACTION_METHOD}",
        "",
        transcripts.LIMITATION,
        "",
    ]
    if docx_present:
        lines.append(
            f"發現 {DOCX_NAME}。它只作待核對參考，本腳本沒有讀取內文，也沒有把它當成正確答案。"
        )
    else:
        lines.append(f"目錄裡沒有 {DOCX_NAME}。沒有參考文件可對照；這不影響逐字稿候選統計。")
    lines.append("")
    counts = cue_counts or {"decisions": 0, "todos": 0}
    cue_total = counts.get("decisions", 0) + counts.get("todos", 0)
    if cue_total:
        lines.append(
            f"已讀取本機說法清單：決議 {counts.get('decisions', 0)} 條、待辦 {counts.get('todos', 0)} 條。"
            "清單原文不轉載；只有這次對上的候選會寫出說法名稱。"
        )
    else:
        lines.append("沒有本機說法清單（cues.json）。只用內建規則。")
    lines.append("評估樣本範圍：目錄第一層的 UTF-8 .txt。子目錄與其他副檔名不納入。")
    if not analyses:
        lines.append("沒有找到 TXT。")
    total = {"決議": 0, "待辦": 0}
    predicted = []
    for item in analyses:
        counts = item["time_counts"]
        lines.append(
            f"檔案 {item['filename']}：時間狀態 {item['timestamp_status']}；"
            f"段落 {item.get('segments', 0)}、句子 {item.get('sentences', 0)}、"
            f"時間警告 {item.get('parse_warnings', 0)}；"
            f"完整時間 {counts.get('complete', 0)}、只有開始時間 {counts.get('start_only', 0)}、"
            f"沒有時間 {counts.get('no_time', 0)}、時間錯誤 {counts.get('time_error', 0)}；"
            f"候選 {len(item['candidates'])} 筆。"
        )
        for cand in item["candidates"]:
            total[cand["kind"]] = total.get(cand["kind"], 0) + 1
            predicted.append(cand)
            lines.append(
                f"  候選 {cand['kind']} 規則 {cand['rule_id']} "
                f"說法 {cand.get('cue') or '—'} 信心 {cand.get('confidence') or '—'} "
                f"第 {cand['line']} 行第 {cand['sentence']} 句：{cand['quote']}"
            )
    lines.append("")
    lines.append(f"候選數：決議 {total.get('決議', 0)}、待辦 {total.get('待辦', 0)}。")
    if pending:
        lines.append("待確認（不進入召回率分母，也不會因為缺少位置而拒絕整份評分）：")
        for row in pending:
            lines.append(f"  {row['kind']}：{row['note'] or '（沒有備註）'}")
    else:
        lines.append("沒有待確認清單（pending.json）。參考文件裡對不到原文的項目請自行寫入，不要算進召回率。")
    if false_hits:
        lines.append("人工判定的誤收（來自 false-positives.json，不是程式自己宣稱的）：")
        for row in false_hits:
            where = f"第 {row['line']} 行" if row["line"] else "未填行號"
            lines.append(f"  {row['kind']} {where}：{row['note'] or '（沒有備註）'}")
    else:
        lines.append("尚未提供 false-positives.json。誤收案例要人工記入該檔，本報告不自動判斷哪一筆是誤收。")
    lines.append("")
    if score["empty"]:
        lines.append("未建立可評分的標註答案。此處不計算 precision 或 recall，也不聲稱召回率。")
        lines.append("標註必須同時有 file、line、sentence。sentence 是該行由 1 起算的句序。只填行號不夠。")
    elif not score["ok"]:
        lines.append(
            "拒絕評分。下列標註無法唯一定位，或預測無法一對一配對。"
            "這些項目沒有算成命中，因此不計算 precision 或 recall。"
        )
        for problem in score["problems"]:
            label = _PROBLEM_TEXT.get(problem["code"], problem["code"])
            if problem.get("index"):
                item = gold[problem["index"] - 1]
                lines.append(
                    f"  標註第 {problem['index']} 筆（{item['kind']}，"
                    f"檔案 {item['file'] or '（空）'}，行 {item['line'] or '（空）'}，"
                    f"句 {item['sentence'] or '（空）'}）：{label}。"
                )
            else:
                lines.append(f"  預測：{label}。")
        lines.append("請修正標註或候選位置後再評分。在此之前，任何一筆都不會被算成命中。")
    else:
        lines.append(
            f"標註答案 {len(gold)} 筆。每一筆都有檔名、行號與句序，且沒有重複。"
            "樣本是上面列出的 TXT。配對是一對一，跨檔案不會因為行號相同而對上。"
        )
        for kind in ("決議", "待辦"):
            scored = score["by_kind"][kind]
            lines.append(
                f"{kind} precision {scored['precision']}（分子是與標註一對一對上的候選數，分母是該類候選數）；"
                f" recall {scored['recall']}（分子是與候選一對一對上的標註數，分母是該類已能唯一定位的標註數）。"
            )
        lines.append("對不到逐字稿的參考項目若放在 pending.json，沒有算進分母。")
        lines.append("這個分數只描述本次目錄裡的標註與候選。若標註是虛構句子，它不是真實會議的準確率。")
    lines.append("")
    lines.append("未驗證：本腳本沒有打開瀏覽器、沒有讀取參考 docx 內文、沒有把候選寫進事項台帳。")
    return "\n".join(lines) + "\n", predicted


def verify(directory):
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        raise VerifyProblem("missing_dir")
    cues = load_cues(directory)
    analyses = []
    try:
        names = sorted(directory.glob("*.txt"))
    except OSError:
        raise VerifyProblem("failed")
    for path in names:
        if not path.is_file():
            continue
        analyses.append(analyze_file(path, cues))
    gold = load_table(directory / "annotation.json")
    if not gold and (directory / "annotation.csv").is_file():
        gold = load_table(directory / "annotation.csv")
    pending = load_table(directory / "pending.json")
    false_hits = load_table(directory / "false-positives.json")
    docx_present = (directory / DOCX_NAME).is_file()
    locations = {item["filename"]: item["lines"] for item in analyses}
    predicted = [cand for item in analyses for cand in item["candidates"]]
    score = evaluate_score(gold, predicted, locations)
    report, _ = build_report(
        directory, analyses, gold, pending, false_hits, docx_present, score,
        {"decisions": len(cues["decisions"]), "todos": len(cues["todos"])},
    )
    try:
        out_dir = directory / "reports"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / "verify-report.txt"
        out.write_text(report, encoding="utf-8")
    except OSError:
        raise VerifyProblem("failed")
    decisions = sum(1 for cand in predicted if cand["kind"] == "決議")
    todos = sum(1 for cand in predicted if cand["kind"] == "待辦")
    return {
        "report_path": str(out),
        "text": report,
        "files": len(analyses),
        "segments": sum(item.get("segments", 0) for item in analyses),
        "sentences": sum(item.get("sentences", 0) for item in analyses),
        "parse_warnings": sum(item.get("parse_warnings", 0) for item in analyses),
        "candidates": len(predicted),
        "decisions": decisions,
        "todos": todos,
        "gold": len(gold),
        "pending": len(pending),
        "false_hits": len(false_hits),
        "scored": 1 if score["ok"] else 0,
        "refused": score["refused"],
        "score": score,
    }


def _counts(result):
    return "\n".join([
        "status=0",
        f"files={result['files']}",
        f"segments={result['segments']}",
        f"sentences={result['sentences']}",
        f"parse_warnings={result['parse_warnings']}",
        f"candidates={result['candidates']}",
        f"decisions={result['decisions']}",
        f"todos={result['todos']}",
        f"gold={result['gold']}",
        f"pending={result['pending']}",
        f"false_hits={result['false_hits']}",
        f"scored={result['scored']}",
        f"refused={result['refused']}",
    ])


def main(argv=None):
    parser = QuietParser(description="在本機目錄驗收 TXT。終端只顯示狀態與彙總數字，詳細報告寫在目錄裡。")
    parser.add_argument("--dir", default=os.environ.get("SDT_VERIFY_DIR") or str(DEFAULT_DIR), help="驗收目錄")
    args = parser.parse_args(argv)
    try:
        result = verify(args.dir)
    except VerifyProblem as exc:
        sys.stderr.write(f"status=2\nerror={exc.code}\n")
        return 2
    except Exception:
        sys.stderr.write("status=2\nerror=failed\n")
        return 2
    sys.stdout.write(_counts(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
