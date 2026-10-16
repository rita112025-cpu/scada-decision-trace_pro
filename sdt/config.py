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


class ConfigError(Exception):
    """設定無法使用。訊息不含檔案路徑。"""


def _port(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError("連接埠必須是 1 到 65535 的整數")
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ConfigError("連接埠必須是 1 到 65535 的整數")
        value = int(text)
    if value < 1 or value > 65535:
        raise ConfigError("連接埠必須是 1 到 65535 的整數")
    return value


def load(path=None):
    path = pathlib.Path(path) if path else ROOT / "config.json"
    values = dict(DEFAULTS)
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            raise ConfigError("設定檔不是有效的 JSON") from None
        except OSError:
            raise ConfigError("設定檔無法讀取") from None
        if not isinstance(loaded, dict):
            raise ConfigError("設定檔不是有效的 JSON")
        values.update(loaded)
    try:
        port = _port(values.get("port"))
    except ConfigError:
        raise
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
                  port=port, data_dir=data_dir,
                  local_notebook_url=str(values.get("local_notebook_url") or "").strip(),
                  local_notebook_db=notebook_db)
