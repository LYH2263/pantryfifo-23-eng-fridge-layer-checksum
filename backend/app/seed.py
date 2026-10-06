from app.db import connect
from app.engines.reconcile import PROJECTION_DDL, ensure_schema, rebuild_projection

def init_db():
    c = connect()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT, layer TEXT, unit TEXT);
    CREATE TABLE IF NOT EXISTS lots(
      id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INT, qty_in REAL, qty_remain REAL,
      expiry TEXT, status TEXT, data_quality TEXT
    );
    CREATE TABLE IF NOT EXISTS consumptions(id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT, result_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """ + PROJECTION_DDL)
    if c.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0:
        c.executemany("INSERT INTO items(name,layer,unit) VALUES (?,?,?)", [
            ("牛奶", "upper", "盒"), ("鸡蛋", "mid", "个"), ("冻饺", "lower", "袋"),
        ])
        c.executemany(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
            [
                (1, 2, 2, "2026-10-01", "on_shelf", "clean"),
                (1, 1, 1, "2026-09-28", "on_shelf", "clean"),
                (2, 12, 12, "2026-11-01", "on_shelf", "clean"),
                (3, 1, 1, "2025-01-01", "on_shelf", "dirty"),
                (2, -3, -3, "2026-12-01", "on_shelf", "dirty"),
            ],
        )
        c.execute("INSERT INTO settings(key,value) VALUES ('warn_days','3')")
        # 与业务种子同事务建立初始投影
        ensure_schema(c)
        rebuild_projection(c)
        c.commit()
    else:
        # 既有库升级：若投影缺失/为空则补建一次
        ensure_schema(c)
        if c.execute("SELECT COUNT(*) c FROM shelf_projection").fetchone()["c"] == 0:
            rebuild_projection(c)
            c.commit()
    c.close()
