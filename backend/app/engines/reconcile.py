"""三视角库存对账：全层(fridge) / 层页(layer page) / 顶条(alert bar)。

事实表是业务写事务维护的 ``lots``；``shelf_projection`` 是它的只读投影，
全层、层页、顶条三个页面口径都从这一张投影导出。

约定（详见 README「对账」一节）：

* 默认 ``reproject=False`` —— **只报告，不改任何数据**，连跑任意次结果稳定。
* 显式 ``reproject=True`` —— 在同一写事务内以 ``lots`` 为准重建投影，
  重建后于同一快照立即复核；该路径**永不写 lots**，也不会把业务 lots 清零。
* 所有读取都在单一事务快照内完成，进行中（未提交）的消费对快照不可见，
  因而不会被误报为分叉。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from app.db import db_path

EPS = 1e-9
KNOWN_LAYERS = ("upper", "mid", "lower")

# ---- schema ---------------------------------------------------------------

PROJECTION_DDL = """
CREATE TABLE IF NOT EXISTS shelf_projection(
  lot_id     INTEGER PRIMARY KEY,
  item_id    INTEGER,
  layer      TEXT,
  name       TEXT,
  unit       TEXT,
  qty_remain REAL,
  expiry     TEXT,
  in_bar     INTEGER DEFAULT 0,
  updated_at TEXT
);
"""


def ensure_schema(c: sqlite3.Connection) -> None:
    c.execute(PROJECTION_DDL)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(shelf_projection)")}
    if "unit" not in cols:  # 旧库迁移
        c.execute("ALTER TABLE shelf_projection ADD COLUMN unit TEXT")


# ---- pure helpers ---------------------------------------------------------

def bar_horizon(today: date, warn_days: int) -> date:
    """顶条收录上界：到期日 <= today+warn_days（含已过期与临期）。"""
    return today + timedelta(days=int(warn_days))


def _warn_days(c: sqlite3.Connection) -> int:
    row = c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()
    try:
        return int(row["value"]) if row else 3
    except (TypeError, ValueError):
        return 3


def _lots_checksum(c: sqlite3.Connection) -> str:
    """整表 lots 的稳定指纹，用于在重投影前后自证业务表未被触碰。"""
    h = hashlib.sha256()
    for r in c.execute(
        "SELECT id,item_id,qty_in,qty_remain,expiry,status,data_quality "
        "FROM lots ORDER BY id"
    ):
        h.update(repr(tuple(r)).encode())
    return h.hexdigest()


# ---- projection maintenance ----------------------------------------------

def rebuild_projection(c: sqlite3.Connection, today: date | None = None,
                       _now: str | None = None) -> int:
    """在调用方给定的事务内，以 lots 为唯一事实源重建投影。返回投影行数。

    被业务写路由（inbound / consume / expire-sweep）在**同一事务**内调用，
    与业务改动一起提交，因此任一已提交状态下投影与 lots 必然一致。
    """
    today = today or date.today()
    warn = _warn_days(c)
    horizon = bar_horizon(today, warn).isoformat()
    today_iso = today.isoformat()
    c.execute("DELETE FROM shelf_projection")
    cur = c.execute(
        """
        INSERT INTO shelf_projection(lot_id,item_id,layer,name,unit,qty_remain,expiry,in_bar,updated_at)
        SELECT l.id, l.item_id, i.layer, i.name, i.unit, l.qty_remain, l.expiry,
               CASE WHEN l.expiry IS NOT NULL AND l.expiry <= ?
                          AND l.qty_remain > 0 THEN 1 ELSE 0 END,
               ?
          FROM lots l JOIN items i ON i.id = l.item_id
         WHERE l.status='on_shelf'
        """,
        (horizon, _now or today_iso),
    )
    return cur.rowcount


# ---- snapshot reads -------------------------------------------------------

def _read_actual(c: sqlite3.Connection, horizon_iso: str) -> dict[int, dict]:
    """事实口径：直接从 lots 现算全层/层页/顶条。"""
    rows = c.execute(
        """
        SELECT l.id AS lot_id, l.item_id, i.layer AS layer, i.name AS name,
               l.qty_remain AS qty, l.expiry AS expiry
          FROM lots l JOIN items i ON i.id = l.item_id
         WHERE l.status='on_shelf'
        """
    ).fetchall()
    return {
        r["lot_id"]: {
            "item_id": r["item_id"],
            "layer": r["layer"],
            "name": r["name"],
            "qty": float(r["qty"]),
            "expiry": r["expiry"],
            # 顶条口径：到期 <= 上界且仍有正余量（与 /alerts 完全一致）
            "in_bar": bool(r["expiry"] is not None and r["expiry"] <= horizon_iso
                           and float(r["qty"]) > 0),
        }
        for r in rows
    }


def _read_projection(c: sqlite3.Connection) -> dict[int, dict]:
    return {
        r["lot_id"]: {
            "item_id": r["item_id"],
            "layer": r["layer"],
            "name": r["name"],
            "qty": float(r["qty_remain"]),
            "expiry": r["expiry"],
            "in_bar": bool(r["in_bar"]),
        }
        for r in c.execute(
            "SELECT lot_id,item_id,layer,name,qty_remain,expiry,in_bar FROM shelf_projection"
        )
    }


def _lot_status(c: sqlite3.Connection, lot_id: int) -> str | None:
    row = c.execute("SELECT status FROM lots WHERE id=?", (lot_id,)).fetchone()
    return row["status"] if row else None


def _diff(actual: dict, projected: dict, status_of) -> list[dict]:
    """逐批比对，并产出全层 / 各层页 / 顶条三方覆盖汇总。"""
    divergences: list[dict] = []

    for lot_id in sorted(set(actual) | set(projected)):
        a, p = actual.get(lot_id), projected.get(lot_id)
        if a is None:  # 投影在架、事实已不在架
            status = status_of(lot_id)
            kind = "phantom_consumed" if status == "consumed" else "phantom_off_shelf"
            divergences.append({
                "kind": kind,
                "layer": p["layer"],
                "lot_id": lot_id,
                "name": p["name"],
                "detail": (f"投影仍把 {status or 'missing'} 批次 {lot_id} 计为在架，"
                           "其已离开货架，层页/全层/顶条出现幽灵批"),
                "actual": None,
                "projected": {"qty": p["qty"], "in_bar": p["in_bar"]},
            })
            continue
        if p is None:  # 事实在架、投影漏报
            divergences.append({
                "kind": "missing_in_projection",
                "layer": a["layer"],
                "lot_id": lot_id,
                "name": a["name"],
                "detail": f"在架批次 {lot_id} 未进入投影，全层/层页/顶条漏算",
                "actual": {"qty": a["qty"], "in_bar": a["in_bar"]},
                "projected": None,
            })
            continue
        if a["layer"] != p["layer"]:
            divergences.append({
                "kind": "layer_mismatch",
                "layer": a["layer"],
                "lot_id": lot_id,
                "name": a["name"],
                "detail": f"批次 {lot_id} 层归属不一致：事实={a['layer']} 投影={p['layer']}",
                "actual": {"layer": a["layer"]},
                "projected": {"layer": p["layer"]},
            })
        if abs(a["qty"] - p["qty"]) > EPS:
            divergences.append({
                "kind": "qty_mismatch",
                "layer": a["layer"],
                "lot_id": lot_id,
                "name": a["name"],
                "detail": f"批次 {lot_id} 余量分叉：事实={a['qty']} 投影={p['qty']}，"
                          f"该层({a['layer']})全层总量与层页过滤之和因此不等",
                "actual": {"qty": a["qty"]},
                "projected": {"qty": p["qty"]},
            })
        if a["in_bar"] != p["in_bar"]:
            divergences.append({
                "kind": "bar_mismatch",
                "layer": a["layer"],
                "lot_id": lot_id,
                "name": a["name"],
                "detail": f"批次 {lot_id} 顶条标记分叉：事实在顶条={a['in_bar']} "
                          f"投影在顶条={p['in_bar']}",
                "actual": {"in_bar": a["in_bar"]},
                "projected": {"in_bar": p["in_bar"]},
            })

    return divergences


def _coverage(actual: dict, projected: dict) -> dict:
    """三方覆盖：全层总量、每层层页过滤之和、顶条 lot_id 集合。"""
    layers = sorted({d["layer"] for d in list(actual.values()) + list(projected.values())
                     if d["layer"]} | set(KNOWN_LAYERS))

    def sums(view: dict) -> dict:
        per_layer = {L: 0.0 for L in layers}
        for d in view.values():
            if d["layer"] in per_layer:
                per_layer[d["layer"]] += d["qty"]
        return per_layer

    a_sum, p_sum = sums(actual), sums(projected)
    return {
        "full": {
            "actual_qty": round(sum(d["qty"] for d in actual.values()), 6),
            "projected_qty": round(sum(d["qty"] for d in projected.values()), 6),
            "actual_lots": len(actual),
            "projected_lots": len(projected),
        },
        "layers": {
            L: {"actual_qty": round(a_sum[L], 6), "projected_qty": round(p_sum[L], 6)}
            for L in layers
        },
        "bar": {
            "actual_lot_ids": sorted(k for k, d in actual.items() if d["in_bar"]),
            "projected_lot_ids": sorted(k for k, d in projected.items() if d["in_bar"]),
        },
    }


# ---- entry point ----------------------------------------------------------

def reconcile(path: str | Path | None = None, *, today: date | None = None,
              reproject: bool = False) -> dict:
    """对账。返回结构化报告。

    report 模式在单一 DEFERRED 快照内完成（只读，绝不写库）；
    reproject 模式在 BEGIN IMMEDIATE 写事务内做：快照核对 → 以 lots 重建投影
    → 同一事务内复核 → 提交。业务表 lots 在任何分支下都不会被写。
    """
    today = today or date.today()
    path = Path(path) if path else db_path()
    c = sqlite3.connect(path, isolation_level=None)  # autocommit：显式管事务
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    try:
        ensure_schema(c)
        warn = _warn_days(c)
        horizon_iso = bar_horizon(today, warn).isoformat()

        c.execute("BEGIN IMMEDIATE" if reproject else "BEGIN")
        try:
            checksum_before = _lots_checksum(c)
            actual = _read_actual(c, horizon_iso)
            projected = _read_projection(c)
            before = _diff(actual, projected, lambda lid: _lot_status(c, lid))

            rebuilt = False
            if reproject and before:
                rebuild_projection(c, today=today)
                rebuilt = True
                projected = _read_projection(c)
                actual = _read_actual(c, horizon_iso)  # 同一快照，事实不变
            elif reproject and not before:
                # 已一致即 no-op：连跑第二次零写入、结果稳定
                pass

            after = _diff(actual, projected, lambda lid: _lot_status(c, lid))
            checksum_after = _lots_checksum(c)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

        return {
            "mode": "reprojected" if reproject else "report",
            "rebuilt": rebuilt,
            "today": today.isoformat(),
            "warn_days": warn,
            "divergence_count": len(after),
            "divergences": after,
            "divergences_before_reproject": before if reproject else [],
            "coverage": _coverage(actual, projected),
            # 审计自证：重投影前后业务 lots 指纹必须一致
            "business_lots_modified": checksum_before != checksum_after,
            "lots_checksum_before": checksum_before,
            "lots_checksum_after": checksum_after,
        }
    finally:
        c.close()


# ---- CLI ------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全层/层页/顶条三方库存对账")
    ap.add_argument("--reproject", action="store_true",
                    help="发现分叉时在事务内以 lots 重建投影（默认只报告）")
    ap.add_argument("--db", default=None, help="SQLite 路径（默认 DATA_DIR）")
    args = ap.parse_args(argv)

    rep = reconcile(args.db, reproject=args.reproject)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if rep["business_lots_modified"]:
        return 3  # 绝不允许：修复路径触碰了业务 lots
    if rep["divergence_count"] != 0:
        return 2 if args.reproject else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
