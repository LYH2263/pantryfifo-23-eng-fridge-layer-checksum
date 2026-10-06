"""三视口对账：全层(/api/fridge) · 层页(/api/fridge?layer=) · 顶条(/api/alerts)。

三个视口都即席查 lots，本身同源；分叉的独立参照系是 consumptions 流水：
每批应有余量 = qty_in - 流水里所有成功 FEFO 扣减之和。把这个"重投影"
与三个视口的实际展示逐一核对，而不是只核一张表。

默认 mode="report"：只报告，绝不写 lots。
显式 mode="reproject"：才按流水精准回写有分叉的批，每批一条带 WHERE 谓词
的 UPDATE，不删行、不清零无关批、不动 items/settings。

整个对账在单个 BEGIN IMMEDIATE 事务的快照里完成：并发的 /consume 要么在
对账前已整笔提交（lots 与 consumptions 同时可见，投影必然自洽），要么被
写锁挡在事务之后，绝不会看到"扣了一半"的进行中一笔。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date

from app.db import connect

EPS = 1e-9

# 视口名
V_ALL = "all_layers"        # 全层页 /api/fridge
V_LAYER = "layer_page"      # 层页   /api/fridge?layer=L
V_TOP = "topbar"            # 顶条   /api/alerts


def _alert_level(expiry: str | None, today_iso: str, warn_days: int) -> str | None:
    if not expiry:
        return None
    if expiry <= today_iso:
        return "expired"
    delta = (date.fromisoformat(expiry) - date.fromisoformat(today_iso)).days
    return "soon" if delta <= warn_days else None


def run_audit(*, reproject: bool = False, conn: sqlite3.Connection | None = None) -> dict:
    """跑一次对账。reproject=False 只读；True 才回写修复。

    两次 report 连跑结果一致且零写入；reproject 幂等，修复后再跑 open_count=0。
    """
    own = conn is None
    c = conn or connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        report = _audit_in_tx(c, reproject=reproject)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()
    return report


def _audit_in_tx(c: sqlite3.Connection, *, reproject: bool) -> dict:
    today_iso = date.today().isoformat()
    warn_days = 3
    row = c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()
    if row is not None:
        try:
            warn_days = int(row["value"])
        except (TypeError, ValueError):
            pass

    lots = [dict(r) for r in c.execute(
        "SELECT id,item_id,qty_in,qty_remain,expiry,status FROM lots ORDER BY id")]
    items = {r["id"]: dict(r) for r in c.execute("SELECT id,name,layer FROM items")}
    ledger = [dict(r) for r in c.execute(
        "SELECT id,result_json,created_at FROM consumptions ORDER BY id")]

    # ---- 1. 流水重投影：每批应有余量 ----
    projected: dict[int, float] = {l["id"]: float(l["qty_in"] or 0.0) for l in lots}
    inflight_ignored: list[dict] = []
    for e in ledger:
        try:
            result = json.loads(e["result_json"] or "")
            deductions = result.get("deductions") or []
            ok = bool(result.get("ok"))
        except (ValueError, TypeError, AttributeError):
            # 解析不了的流水（含理论上可能夹在事务里的半成品记录）不参与投影，
            # 也绝不能据此判罚业务批。
            inflight_ignored.append({"consumption_id": e["id"], "reason": "unparseable"})
            continue
        if not ok or not isinstance(deductions, list):
            # 非成功扣减（short/非正量在业务路由里整笔回滚）不产生余量变化
            inflight_ignored.append({"consumption_id": e["id"], "reason": "not_applied"})
            continue
        for d in deductions:
            lid = d.get("lot_id")
            take = float(d.get("take") or 0.0)
            if lid in projected:
                projected[lid] -= take
            else:
                inflight_ignored.append(
                    {"consumption_id": e["id"], "lot_id": lid, "reason": "lot_missing"})

    # ---- 2. 按三个视口的真实查询口径取快照行 ----
    shelf_rows = [dict(r) for r in c.execute(
        """SELECT lots.id, lots.item_id, lots.qty_remain, lots.status, lots.expiry,
                  items.layer AS layer
           FROM lots LEFT JOIN items ON items.id=lots.item_id
           WHERE lots.status='on_shelf'""")]
    shelf_by_id = {r["id"]: r for r in shelf_rows}

    layers = sorted({(items.get(l["item_id"]) or {}).get("layer")
                     for l in lots if (items.get(l["item_id"]) or {}).get("layer")})

    def shelf_sum(layer: str | None) -> float:
        if layer is None:
            rows = shelf_rows
        else:
            rows = [r for r in shelf_rows if r["layer"] == layer]
        return round(sum(float(r["qty_remain"] or 0.0) for r in rows), 6)

    def expected_sum(layer: str | None) -> float:
        total = 0.0
        for l in lots:
            it = items.get(l["item_id"])
            if (it or {}).get("layer") != layer:
                continue
            eff = projected.get(l["id"], 0.0)
            # 期望在架：投影有余量，且未被过期下架（expired 是合法离架状态）
            if eff > EPS and l["status"] != "expired":
                total += eff
        return round(total, 6)

    actual_all = {L: shelf_sum(L) for L in [None] + layers}
    actual_layer_pages = {L: shelf_sum(L) for L in layers}
    expected_totals = {L: expected_sum(L) for L in layers}

    # 顶条实际会报的批（on_shelf + 实际余量>0 + 到期/临期）
    topbar_ids: set[int] = set()
    for r in shelf_rows:
        if float(r["qty_remain"] or 0.0) > EPS and \
                _alert_level(r["expiry"], today_iso, warn_days):
            topbar_ids.add(r["id"])

    # ---- 3. 逐批核对，产出分叉 ----
    findings: list[dict] = []
    repair_plan: list[dict] = []

    for l in lots:
        lid = l["id"]
        it = items.get(l["item_id"]) or {}
        layer = it.get("layer")
        eff = projected.get(lid, 0.0)
        actual = float(l["qty_remain"] if l["qty_remain"] is not None else 0.0)
        status = l["status"]
        on_shelf = status == "on_shelf"
        shown = lid in shelf_by_id  # 全层/层页是否展示
        shown_top = lid in topbar_ids

        f: dict | None = None

        if on_shelf and eff <= EPS:
            # 流水说该批已无余量，批却仍在架（三视图会虚增/顶条误报）。
            if float(l["qty_in"] or 0.0) <= EPS:
                # 入库量本身非正（脏数据），投影钳到 0
                f = {"kind": "invalid_nonpositive_inbound", "actual": actual, "expected": 0.0}
            else:
                f = {"kind": "resurrected_consumed", "actual": actual, "expected": 0.0}
            repair_plan.append({"lot_id": lid, "kind": "consume", "layer": layer})
        elif status == "consumed" and eff > EPS:
            # 反向分叉：流水说仍有余量，批却被标 consumed（三视图都漏报）
            f = {"kind": "missing_active_lot", "actual": 0.0, "expected": round(eff, 6)}
            repair_plan.append({"lot_id": lid, "kind": "revive", "qty": round(eff, 6),
                                "layer": layer})
        elif on_shelf and eff > EPS and abs(actual - eff) > EPS:
            # 人为改余量：全层/层页合计与层页过滤之和对不上，顶条口径也错
            f = {"kind": "qty_mismatch", "actual": actual, "expected": round(eff, 6)}
            repair_plan.append({"lot_id": lid, "kind": "set_qty", "qty": round(eff, 6),
                                "layer": layer})
        # status='exp' 过期下架是 sweep 的合法转移，即使投影有余量也不算分叉

        if f:
            views = []
            if shown:
                views.append(V_ALL)
                if layer:
                    views.append(f"{V_LAYER}:{layer}")
            if shown_top:
                views.append(V_TOP)
            findings.append({
                "layer": layer,
                "lot_id": lid,
                "kind": f["kind"],
                "actual": f["actual"],
                "expected": f["expected"],
                "views": views,
            })

    # ---- 4. 聚合对账：全层合计 == 层页合计，且两者 == 流水期望合计 ----
    aggregate = {
        "all_layers_total": actual_all.get(None, 0.0),
        "per_layer": {L: {
            "all_layers": actual_all.get(L, 0.0),
            "layer_page": actual_layer_pages.get(L, 0.0),
            "expected": expected_totals.get(L, 0.0),
            "balanced": abs(actual_all.get(L, 0.0) - actual_layer_pages.get(L, 0.0)) <= EPS
                        and abs(actual_layer_pages.get(L, 0.0) - expected_totals.get(L, 0.0)) <= EPS,
        } for L in layers},
        "topbar_lot_ids": sorted(topbar_ids),
    }

    repaired: list[dict] = []
    if reproject:
        repaired = _apply_repairs(c, repair_plan)

    return {
        "status": "divergent" if findings else "ok",
        "mode": "reproject" if reproject else "report",
        "open_count": len(findings),
        "ledger_consumptions": len(ledger),
        "inflight_ignored": inflight_ignored,
        "aggregate": aggregate,
        "findings": findings,
        "repaired": repaired,
    }


def _apply_repairs(c: sqlite3.Connection, plan: list[dict]) -> list[dict]:
    """按流水重投影精准回写。每条语句都带 lot 主键与状态谓词，
    作用域仅限计划内分叉批：不 DELETE、不碰其它 lot、不动业务表。"""
    done: list[dict] = []
    for p in plan:
        lid = p["lot_id"]
        if p["kind"] == "consume":
            # 复活批（余量被改回正值）或非正入库脏批：归零并置 consumed
            cur = c.execute(
                "UPDATE lots SET status='consumed', qty_remain=0 "
                "WHERE id=? AND status='on_shelf'", (lid,))
        elif p["kind"] == "revive":
            cur = c.execute(
                "UPDATE lots SET status='on_shelf', qty_remain=? "
                "WHERE id=? AND status='consumed'", (p["qty"], lid))
        else:  # set_qty
            cur = c.execute(
                "UPDATE lots SET qty_remain=? WHERE id=? AND status='on_shelf'",
                (p["qty"], lid))
        if cur.rowcount:
            done.append({"lot_id": lid, "layer": p.get("layer"), "action": p["kind"]})
    return done


def _main() -> int:
    ap = argparse.ArgumentParser(description="pantryfifo 三视图对账")
    ap.add_argument("--reproject", action="store_true",
                    help="显式开关：按流水重投影并回写分叉批（默认只报告）")
    args = ap.parse_args()
    report = run_audit(reproject=args.reproject)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["open_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(_main())
