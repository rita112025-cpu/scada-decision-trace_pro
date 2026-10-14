"""本機設定：讀取專案根目錄的 config.json（不存在時用預設值）。"""
import json
import pathlib
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent

DEFAULTS = {"operator": "本機使用者", "port": 8765, "data_dir": "data"}


@dataclass
class Config:
    operator: str
    port: int
    data_dir: pathlib.Path

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
    return Config(operator=str(values["operator"]).strip() or DEFAULTS["operator"],
                  port=int(values["port"]), data_dir=data_dir)
