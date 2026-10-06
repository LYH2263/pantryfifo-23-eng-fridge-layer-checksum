import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.engines.reconcile import ensure_schema, rebuild_projection, reconcile

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    """全层页 / 单层页：读 shelf_projection。

    投影与 lots 在业务写事务内同步提交，全层总量与任意单层过滤之和天然对账一致。
    """
    c = connect()
    q = ("SELECT lot_id AS id, lot_id, item_id, layer, name, unit, "
         "qty_remain, expiry, in_bar, updated_at FROM shelf_projection")
    args = []
    if layer:
        q += " WHERE layer=?"; args.append(layer)
    q += " ORDER BY lot_id"
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    """顶条：只取投影中 in_bar=1 的批次（在架、正余量、到期 <= today+warn_days）。"""
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today()
    rows = [dict(r) for r in c.execute(
        "SELECT lot_id AS id, lot_id, item_id, layer, name, unit, "
        "qty_remain, expiry, in_bar, updated_at "
        "FROM shelf_projection WHERE in_bar=1 ORDER BY expiry, lot_id")]
    c.close()
    out = []
    for r in rows:
        # 投影已按 today+warn 过滤；这里保留前端需要的 level/days_left 分级
        if r["expiry"] <= today.isoformat():
            r["level"] = "expired"
            out.append(r)
        else:
            delta = (date.fromisoformat(r["expiry"]) - today).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    # 业务写入与投影重建在同一事务内提交，对账器不可能观察到中间态。
    try:
        cur = c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
            (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
        ensure_schema(c)
        rebuild_projection(c)
        c.commit()
    except Exception:
        c.rollback(); raise
    lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    try:
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0",
            (body.item_id,))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"] and result["reason"] == "qty_non_positive":
            raise HTTPException(400, result["reason"])
        if not result["ok"]:
            raise HTTPException(409, result)
        for d in result["deductions"]:
            c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?",
                      (d["take"], d["lot_id"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?",
                            (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?",
                          (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        ensure_schema(c)
        rebuild_projection(c)
        c.commit()
    except HTTPException:
        c.rollback(); raise
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    try:
        lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
        ids = expire_lots(lots, date.today().isoformat())
        for i in ids:
            c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
        ensure_schema(c)
        rebuild_projection(c)
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return {"expired_ids": ids}

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

class ReconcileIn(BaseModel):
    reproject: bool = False

@app.post("/api/reconcile")
def reconcile_run(body: ReconcileIn = ReconcileIn()):
    """全层 / 层页 / 顶条三方对账。

    默认（reproject=false）只报告，不写任何业务数据，连跑结果稳定；
    显式 reproject=true 才在单事务内以 lots 为事实源重建只读投影，
    修复路径永不修改、清零业务 lots。
    """
    return reconcile(reproject=body.reproject)
