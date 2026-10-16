"""本機設定：讀取專案根目錄的 config.json（不存在時用預設值）。"""
import json
import pathlib
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent

DEFAULTS = {
    "operator": "本機使用者",
    "port": 8765,
    "data_dir": "data",
    "local_notebook_url": "",
    "local_notebook_db": "",
}


@dataclass
class Config:
    operator: str
    port: int
    data_dir: pathlib.Path
    local_notebook_url: str = ""
    local_notebook_db: str = ""

    @property
    def db_path(self):
        return self.data_dir / "tracker.db"

    @property
    def backup_dir(self):
        return self.data_dir / "backups"


def load(path=None):
    path = pathlib.Path(path) if path else ROOT / "config.json"
    values = dict(DEFAULTS)
    if path.is_file():
        values.update(json.loads(path.read_text(encoding="utf-8-sig")))
    data_dir = pathlib.Path(values["data_dir"])
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    notebook_db = str(values.get("local_notebook_db") or "").strip()
    if notebook_db:
        db_path = pathlib.Path(notebook_db)
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        notebook_db = str(db_path)
    return Config(operator=str(values["operator"]).strip() or DEFAULTS["operator"],
                  port=int(values["port"]), data_dir=data_dir,
                  local_notebook_url=str(values.get("local_notebook_url") or "").strip(),
                  local_notebook_db=notebook_db)
