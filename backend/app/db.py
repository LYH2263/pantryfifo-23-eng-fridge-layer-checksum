import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    c = sqlite3.connect(db_path(), timeout=10.0)
    c.row_factory = sqlite3.Row
    # 对账持写锁时，并发的消费写入等待后整笔提交，而不是撞到锁报错
    c.execute("PRAGMA busy_timeout=10000")
    return c
