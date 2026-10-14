"""備份與還原。還原前一律先備份目前資料；備份檔本身以唯讀方式開啟，不會被修改。"""
import datetime as dt
import pathlib
import sqlite3


def backup(db_path, backup_dir, label="backup"):
    backup_dir = pathlib.Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = backup_dir / f"sdt-{label}-{stamp}.db"
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(target)
    except sqlite3.OperationalError:
        raise ValueError("無法建立備份檔：資料夾路徑可能超過 260 字元或沒有寫入權限，"
                         "請把程式放在較短的路徑（例如 C:\\tools\\scada-decision-trace）")
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def validate_backup(path):
    path = pathlib.Path(path)
    if not path.is_file():
        raise ValueError("找不到備份檔")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        raise ValueError("檔案不是有效的 SQLite 資料庫")
    if not {"issues", "issue_history"} <= tables:
        raise ValueError("檔案不是本工具的備份（缺少 issues／issue_history 資料表）")


def restore(db_path, source, backup_dir):
    """回傳還原前自動建立的備份路徑。"""
    validate_backup(source)
    pre = backup(db_path, backup_dir, label="pre-restore") if pathlib.Path(db_path).exists() else None
    src = sqlite3.connect(f"{pathlib.Path(source).resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(db_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return pre
