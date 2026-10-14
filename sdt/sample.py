"""虛構範例資料。專案、圖號、檔名、人名皆為虛構，不對應任何真實專案。"""
import datetime as dt

from . import db


def _d(today, days):
    return (today + dt.timedelta(days=days)).isoformat()


def sample_rows(today=None):
    t = dt.date.fromisoformat(today) if today else dt.date.today()
    base = {"project": "示範專案甲", "system": "SCADA"}
    return [
        dict(base, issue_type="會議決議", title="RTU 盤面增設通訊模組備品位置",
             description="協調會決議：RTU 盤預留一組通訊模組安裝空間，並更新盤面配置圖。",
             source_file="DEMO_協調會紀錄_第03次.pdf", source_location="第 2 頁 第 4 點",
             source_excerpt="RTU 盤內預留一組通訊模組空間，由電控組更新盤面圖。",
             source_status="已確認", owner="王小明", due_date=_d(t, -5), priority="高", status="處理中",
             drawing_no="DEMO-E-101", drawing_rev="B", acceptance_criteria="盤面配置圖 C 版顯示預留空間"),
        dict(base, issue_type="規範要求", title="光纖路由需與動力電纜分槽",
             description="依規範章節要求，通訊光纖不得與動力電纜同槽。",
             source_file="DEMO_技術規範.pdf", source_location="第 5.3.2 節",
             source_excerpt="Communication fiber shall be routed in a separate tray.",
             source_status="已確認", owner="陳大華", due_date=_d(t, 10), priority="中", status="待驗收",
             model_file="DEMO_MEP_Model.rvt", model_element_id="TRAY-0042",
             acceptance_criteria="模型中光纖槽與動力槽分離，碰撞檢查無交疊",
             acceptance_evidence="evidence/DEMO_clash_report.html"),
        dict(base, issue_type="圖面問題", title="I/O 清單點位數與盤面端子數不一致",
             description="I/O 清單共 48 點，端子圖僅標示 44 點，待設計方說明。",
             source_file="DEMO_IO_List.xlsx", source_location="工作表 AI, 第 12-60 列",
             source_excerpt="", source_status="缺乏證據", owner="林小美", due_date=_d(t, -1),
             priority="高", status="等待他人回覆", drawing_no="DEMO-E-205", drawing_rev="A"),
        dict(base, issue_type="模型問題", title="控制室機櫃與風管碰撞",
             description="模型檢查發現機櫃頂部與風管高程重疊。",
             source_file="DEMO_碰撞報告.html", source_location="第 7 筆",
             source_excerpt="Clash: Cabinet-CR-03 vs Duct-AH-12", source_status="已確認",
             owner="陳大華", due_date=_d(t, 3), priority="中", status="處理中",
             model_file="DEMO_Arch_Model.rvt", model_element_id="CAB-CR-03",
             acceptance_criteria="重新執行碰撞檢查，本項不再出現"),
        dict(base, issue_type="待確認", title="UPS 供電時間要求是 30 或 60 分鐘",
             description="兩份文件數值不同，待業主確認。",
             source_file="DEMO_需求說明.docx", source_location="第 3 段",
             source_excerpt="UPS 備援時間 30 分鐘", source_status="有爭議",
             owner="王小明", due_date=_d(t, 7), priority="高", status="等待他人回覆"),
        dict(base, issue_type="會議決議", title="SCADA 畫面命名規則採用新版",
             description="會議口頭提及，紀錄未載明頁碼，待補證據。",
             source_file="", source_location="", source_excerpt="",
             source_status="缺乏證據", owner="林小美", due_date="", priority="低", status="待處理"),
        dict(base, issue_type="圖面問題", title="單線圖斷路器編號更正",
             description="斷路器編號 CB-07 更正為 CB-17。",
             source_file="DEMO_審查意見表.xlsx", source_location="第 9 項",
             source_excerpt="CB-07 應為 CB-17", source_status="已確認", owner="王小明",
             due_date=_d(t, -20), priority="中", status="已驗收",
             drawing_no="DEMO-E-001", drawing_rev="C",
             acceptance_criteria="單線圖 C 版編號為 CB-17", acceptance_result="通過",
             acceptance_evidence="evidence/DEMO-E-001_C_markup.pdf"),
    ]


def load_sample(conn, operator, today=None):
    if conn.execute("SELECT 1 FROM issues WHERE is_sample=1").fetchone():
        return 0
    for row in sample_rows(today):
        db.create_issue(conn, row, operator, action="sample", is_sample=True)
    return len(sample_rows(today))
