"""協調會議議程草稿。只整理現有資料，不新增事項、不推定任何決議。"""
from . import db


def _line(issue, today):
    parts = [f"**{issue['issue_id']}** {issue['title']}"]
    meta = [f"負責人：{issue['owner'] or '（未指定）'}",
            f"期限：{issue['due_date'] or '（未定）'}",
            f"狀態：{issue['status']}",
            f"優先級：{issue['priority']}",
            f"來源狀態：{issue['source_status']}"]
    if db.is_overdue(issue, today):
        meta.append("⚠ 逾期")
    target = " / ".join(x for x in (issue["drawing_no"] and f"圖號 {issue['drawing_no']}"
                                    + (f" 版次 {issue['drawing_rev']}" if issue["drawing_rev"] else ""),
                                    issue["model_file"] and f"模型 {issue['model_file']}"
                                    + (f" #{issue['model_element_id']}" if issue["model_element_id"] else ""))
                     if x)
    if target:
        meta.append(f"對象：{target}")
    return f"- {parts[0]}\n  - " + "｜".join(meta)


def build_sections(conn, today=None, project_id=""):
    today = today or db.today_str()
    filters = {"project_id": project_id} if project_id else None
    open_items = db.list_issues(conn, view="open", sort="due_date", today=today, filters=filters)
    return today, [
        ("一、逾期事項", [i for i in open_items if db.is_overdue(i, today)]),
        ("二、高優先事項", [i for i in open_items if i["priority"] == "高"]),
        ("三、等待他人回覆", [i for i in open_items if i["status"] == "等待他人回覆"]),
        ("四、尚未驗收（待驗收）", [i for i in open_items if i["status"] == "待驗收"]),
        ("五、來源待確認／有爭議", db.list_issues(conn, view="disputed", sort="due_date", today=today, filters=filters)),
    ]


def scope_label(conn, project_id=""):
    if not project_id:
        return "全部專案"
    project = db.get_project(conn, project_id)
    return f"{project['name'] or '（未命名）'}（{project['project_id']}）"


def build_agenda(conn, today=None, title="協調會議議程（草稿）", project_id=""):
    today, sections = build_sections(conn, today, project_id)
    out = [f"# {title}", "",
           f"- 產生日期：{today}",
           f"- 範圍：{scope_label(conn, project_id)}",
           "- 說明：本草稿僅依台帳現有資料整理，未新增事項、未推定任何決議；"
           "會議結論請於會後由與會者確認後再回填台帳。", ""]
    for heading, items in sections:
        out.append(f"## {heading}（{len(items)} 項）")
        out.append("")
        if items:
            out.extend(_line(i, today) for i in items)
        else:
            out.append("- （無）")
        out.append("")
    out += ["## 會議結論（會後回填，勿預填）", "", "- ", ""]
    return "\n".join(out)
