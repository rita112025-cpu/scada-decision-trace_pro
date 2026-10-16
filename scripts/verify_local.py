"""本機驗收：只讀使用者指定目錄裡的 TXT，在記憶體裡產生候選，報告寫回該目錄。

不連外網、不呼叫模型、不確認入帳、不改寫原始檔。
預設目錄是 C:\\Users\\user\\sdt-verify。雲端代理不應拿這個腳本去讀真實會議。
"""
import argparse
import csv
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdt import transcripts  # noqa: E402
from sdt.db import ValidationError  # noqa: E402

DEFAULT_DIR = pathlib.Path(r"C:\Users\user\sdt-verify")
DOCX_NAME = "SCADA會議紀錄.docx"


def read_only_text(path):
    before = path.stat()
    fd = os.open(path, os.O_RDONLY)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            text = handle.read()
    except UnicodeDecodeError as exc:
        raise ValidationError([f"{path.name} 不是 UTF-8"]) from exc
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValidationError([f"讀取期間 {path.name} 的大小或時間變了，已停止"])
    return text


def _load_json(path):
    return json.loads(read_only_text(path))


def _as_items(data):
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        raise ValidationError(["標註檔的 items 必須是陣列"])
    items = []
    for row in data:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "").strip()
        if kind not in {"決議", "待辦"}:
            continue
        line = row.get("line", "")
        segment_id = str(row.get("segment_id") or "").strip()
        note = str(row.get("note") or "")
        line_no = ""
        if line not in ("", None):
            line_no = str(int(line))
        items.append({"kind": kind, "line": line_no, "segment_id": segment_id, "note": note})
    return items


def load_table(path):
    if path is None or not path.is_file():
        return []
    if path.suffix.lower() == ".json":
        return _as_items(_load_json(path))
    fd = os.open(path, os.O_RDONLY)
    with os.fdopen(fd, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return _as_items(rows)


def candidate_rows(parsed):
    rows = []
    for cand in transcripts.extract_candidates(parsed["segments"]):
        seg = next(s for s in parsed["segments"] if s["source_segment_id"] == cand["segment_key"])
        rows.append({
            "kind": cand["kind"],
            "line": str(seg["line_start"] or ""),
            "segment_id": seg["source_segment_id"],
            "rule_id": cand["rule_id"],
            "quote": cand["source_quote"],
            "owner": cand["owner"],
            "due_date": cand["due_date"],
        })
    return rows


def _key(item):
    if item.get("line"):
        return ("line", str(item["line"]))
    if item.get("segment_id"):
        return ("segment", str(item["segment_id"]))
    return None


def score_kind(kind, gold, predicted):
    gold_k = [g for g in gold if g["kind"] == kind and _key(g)]
    pred_k = [p for p in predicted if p["kind"] == kind]
    gold_keys = {_key(g) for g in gold_k}
    pred_keys = [_key(p) for p in pred_k]
    matched_pred = sum(1 for key in pred_keys if key in gold_keys)
    matched_gold = len(gold_keys & set(pred_keys))
    return {
        "kind": kind,
        "matched_predictions": matched_pred,
        "predictions": len(pred_k),
        "matched_gold": matched_gold,
        "gold": len(gold_k),
        "precision": f"{matched_pred}/{len(pred_k)}" if pred_k else "無候選（分母 0）",
        "recall": f"{matched_gold}/{len(gold_k)}" if gold_k else "無標註（分母 0）",
    }


def analyze_file(path):
    text = read_only_text(path).replace("\r\n", "\n").replace("\r", "\n")
    parsed = transcripts.parse_upload(path.name, text)
    return {
        "filename": path.name,
        "time_counts": parsed.get("time_counts") or {},
        "timestamp_status": parsed.get("timestamp_status", ""),
        "candidates": candidate_rows(parsed),
    }


def build_report(directory, analyses, gold, pending, false_hits, docx_present):
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
    lines.append("評估樣本範圍：目錄第一層的 UTF-8 .txt。子目錄與其他副檔名不納入。")
    if not analyses:
        lines.append("沒有找到 TXT。")
    total = {"決議": 0, "待辦": 0}
    predicted = []
    for item in analyses:
        counts = item["time_counts"]
        lines.append(
            f"檔案 {item['filename']}：時間狀態 {item['timestamp_status']}；"
            f"完整時間 {counts.get('complete', 0)}、只有開始時間 {counts.get('start_only', 0)}、"
            f"沒有時間 {counts.get('no_time', 0)}、時間錯誤 {counts.get('time_error', 0)}；"
            f"候選 {len(item['candidates'])} 筆。"
        )
        for cand in item["candidates"]:
            total[cand["kind"]] = total.get(cand["kind"], 0) + 1
            predicted.append(dict(cand, filename=item["filename"]))
            lines.append(
                f"  候選 {cand['kind']} 規則 {cand['rule_id']} 第 {cand['line']} 行："
                f"{cand['quote']}"
            )
    lines.append("")
    lines.append(f"候選數：決議 {total.get('決議', 0)}、待辦 {total.get('待辦', 0)}。")
    if pending:
        lines.append("待確認（尚未對到逐字稿行號或段落，不進入召回率分母）：")
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
    anchored = [g for g in gold if _key(g)]
    if not anchored:
        lines.append("")
        lines.append("未建立可對到行號或段落 ID 的標註答案。此處不計算 precision 或 recall，也不聲稱召回率。")
    else:
        lines.append("")
        lines.append(f"標註答案 {len(anchored)} 筆（只含已填行號或段落 ID 的項目）。樣本是上面列出的 TXT。")
        for kind in ("決議", "待辦"):
            scored = score_kind(kind, anchored, predicted)
            lines.append(
                f"{kind} precision {scored['precision']}（分子是預測裡對到標註的筆數，分母是該類候選數）；"
                f" recall {scored['recall']}（分子是標註裡至少被一筆候選對到的筆數，分母是該類已對到行號的標註數）。"
            )
        lines.append("對不到逐字稿的參考項目若放在 pending.json，沒有算進分母。")
    lines.append("")
    lines.append("未驗證：本腳本沒有打開瀏覽器、沒有讀取參考 docx 內文、沒有把候選寫進事項台帳。")
    return "\n".join(lines) + "\n"


def verify(directory):
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        raise ValidationError([f"找不到驗收目錄：{directory}"])
    analyses = []
    for path in sorted(directory.glob("*.txt")):
        if not path.is_file():
            continue
        analyses.append(analyze_file(path))
    gold = load_table(directory / "annotation.json")
    if not gold and (directory / "annotation.csv").is_file():
        gold = load_table(directory / "annotation.csv")
    pending = load_table(directory / "pending.json")
    false_hits = load_table(directory / "false-positives.json")
    docx_present = (directory / DOCX_NAME).is_file()
    report = build_report(directory, analyses, gold, pending, false_hits, docx_present)
    out_dir = directory / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "verify-report.txt"
    out.write_text(report, encoding="utf-8")
    return {"report_path": str(out), "text": report, "files": len(analyses)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="在本機目錄驗收虛構或使用者自己的 TXT，不連外網。")
    parser.add_argument("--dir", default=os.environ.get("SDT_VERIFY_DIR", str(DEFAULT_DIR)))
    args = parser.parse_args(argv)
    try:
        result = verify(args.dir)
    except ValidationError as exc:
        print("\n".join(exc.errors), file=sys.stderr)
        return 2
    print(result["text"])
    print(f"報告已寫入 {result['report_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
