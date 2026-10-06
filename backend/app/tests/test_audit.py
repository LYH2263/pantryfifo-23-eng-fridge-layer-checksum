"""三视图对账验收：全层 / 层页 / 顶条。

覆盖：
- 干净库 0 分叉，report 连跑两次完全一致且零写入；
- 直接改 SQLite 注入分叉（改余量 / 复活 consumed 批），报告含 layer、lot_id，
  且覆盖三个视图而非一张表；
- 默认只报告；显式 reproject 才回写；两种模式各自连跑稳定在同一选择；
- 修复后连跑两次都为 0（第二次仍非 0 即整场失败）；
- 并发消费穿插时，进行中一笔不被当分叉，业务 lots 不被清零；
- 不允许 DELETE / 改 SQLite 文件式修复：修复前后行数不变、无关批不动。
"""
import json
import os
import sqlite3
import threading
import time

import pytest

from app import seed
from app.db import connect, db_path
from app.audit import run_audit


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    c = connect()
    c.executescript(
        "DELETE FROM lots; DELETE FROM items; DELETE FROM consumptions; DELETE FROM settings;")
    c.execute("INSERT INTO settings(key,value) VALUES ('warn_days','3')")
    c.executemany("INSERT INTO items(id,name,layer,unit) VALUES (?,?,?,?)", [
        (1, "牛奶", "upper", "盒"),
        (2, "鸡蛋", "mid", "个"),
    ])
    c.executemany(
        "INSERT INTO lots(id,item_id,qty_in,qty_remain,expiry,status,data_quality) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            # 今天 2026-10-06：10-08 临期（warn=3）会进顶条
            (101, 1, 2, 2, "2026-10-08", "on_shelf", "clean"),
            (102, 1, 1, 1, "2026-12-01", "on_shelf", "clean"),
            (103, 2, 12, 12, "2026-11-01", "on_shelf", "clean"),
        ],
    )
    c.commit(); c.close()
    return tmp_path


def fingerprint():
    """lots 全表指纹 + 行数，用于证明 report 零写入 / reproject 不误伤。"""
    c = connect()
    rows = [tuple(r) for r in c.execute(
        "SELECT id,item_id,qty_in,qty_remain,expiry,status FROM lots ORDER BY id")]
    c.close()
    return rows


def consume_via_api(client, item_id, qty):
    r = client.post("/api/consume", json={"item_id": item_id, "qty": qty})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 干净库 ----------

def test_clean_db_zero_and_report_is_read_only(db):
    r1 = run_audit(reproject=False)
    assert r1["status"] == "ok" and r1["open_count"] == 0
    fp_before = fingerprint()
    r2 = run_audit(reproject=False)
    assert r1 == r2                      # 连跑两次稳定在同一选择
    assert fingerprint() == fp_before   # report 绝不写 lots
    agg = r1["aggregate"]
    for L, a in agg["per_layer"].items():
        assert a["balanced"]
        assert a["all_layers"] == a["layer_page"] == a["expected"]
    # 顶条：101 临期在列
    assert agg["topbar_lot_ids"] == [101]


def test_scope_covers_all_three_views_not_one_table(db):
    c = connect()
    # 人为直接改库：把临期批 101 的余量 2 改成 5
    c.execute("UPDATE lots SET qty_remain=5 WHERE id=101")
    c.commit(); c.close()

    r = run_audit(reproject=False)
    assert r["open_count"] == 1
    f = r["findings"][0]
    assert f["lot_id"] == 101 and f["layer"] == "upper"
    assert f["kind"] == "qty_mismatch" and f["actual"] == 5 and f["expected"] == 2
    # 同一份报告同时点名三个视图
    assert "all_layers" in f["views"]
    assert "layer_page:upper" in f["views"]
    assert "topbar" in f["views"]
    # 聚合对账也破了：全层/层页虚增，期望值对不上
    a = r["aggregate"]["per_layer"]["upper"]
    assert a["all_layers"] == 6 and a["layer_page"] == 6 and a["expected"] == 3
    assert a["balanced"] is False


def test_resurrected_consumed_lot_flagged(db):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    consume_via_api(client, 1, 3)   # 101(10-08)x2 + 102(12-01)x1 全扣完 -> consumed

    c = connect()
    # 人为直接改库：把已 consumed 的 102 复活
    c.execute("UPDATE lots SET status='on_shelf', qty_remain=1 WHERE id=102")
    c.commit(); c.close()

    fp = fingerprint()
    r1 = run_audit(reproject=False)
    r2 = run_audit(reproject=False)
    assert r1["findings"] == r2["findings"]          # 两次连跑同一选择
    assert fingerprint() == fp                       # 默认不改

    kinds = {f["lot_id"]: f for f in r1["findings"]}
    assert 102 in kinds and kinds[102]["kind"] == "resurrected_consumed"
    assert kinds[102]["layer"] == "upper"
    # 101 已 consumed 且流水确实扣完，不得误报
    assert 101 not in kinds


# ---------- 模式稳定：report 不写，reproject 才写 ----------

def test_report_never_writes_repeatedly(db):
    c = connect(); c.execute("UPDATE lots SET qty_remain=9 WHERE id=103"); c.commit(); c.close()
    fp = fingerprint()
    for _ in range(3):
        r = run_audit(reproject=False)
        assert r["mode"] == "report" and r["open_count"] == 1
        assert fingerprint() == fp


# ---------- 修复：两次连跑都必须为 0，否则整场失败 ----------

def _two_pass_gate():
    """验收闸门：修复后第 1、2 次都必须 0；第二次仍非 0 即失败。"""
    p1 = run_audit(reproject=False)
    p2 = run_audit(reproject=False)
    if p1["open_count"] != 0 or p2["open_count"] != 0:
        raise AssertionError(f"gate failed: {p1['open_count']} -> {p2['open_count']}")
    return p1, p2


def test_reproject_then_two_zero_passes(db):
    c = connect()
    c.execute("UPDATE lots SET qty_remain=5 WHERE id=101")          # 改余量
    c.commit(); c.close()
    # 103 走一笔真实消费再人为复活
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    consume_via_api(client, 2, 12)
    c = connect()
    c.execute("UPDATE lots SET status='on_shelf', qty_remain=7 WHERE id=103")
    c.commit(); c.close()

    rows_before = fingerprint()
    rep = run_audit(reproject=True)
    assert {x["lot_id"] for x in rep["repaired"]} == {101, 103}

    # 闸门：修复后连跑两次都为 0
    _two_pass_gate()

    rows_after = fingerprint()
    assert len(rows_after) == len(rows_before)            # 没删行
    before = {r[0]: r for r in rows_before}
    after = {r[0]: r for r in rows_after}
    # 无关批 102 原样不动
    assert after[102] == before[102]
    # 业务 lots 没被清零：101 回到 2，不是 0
    assert after[101][3] == 2 and after[101][5] == "on_shelf"
    # 复活批 103 按流水精准落为 consumed/0
    assert after[103][3] == 0 and after[103][5] == "consumed"
    # reproject 再跑一遍依旧 0、且无新增修复（幂等）
    again = run_audit(reproject=True)
    assert again["open_count"] == 0 and again["repaired"] == []


def test_gate_detects_unfixed_divergence(db):
    """不打开重投影开关时分叉不会自愈：闸门必须判失败。"""
    c = connect(); c.execute("UPDATE lots SET qty_remain=5 WHERE id=101"); c.commit(); c.close()
    with pytest.raises(AssertionError):
        _two_pass_gate()
    run_audit(reproject=True)
    _two_pass_gate()  # 修复后闸门通过


# ---------- 并发：进行中一笔不是分叉 ----------

def test_concurrent_consume_not_flagged(db):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    stop = threading.Event()
    errors = []

    def auditor():
        while not stop.is_set():
            try:
                r = run_audit(reproject=False)
                # 只允许零分叉：进行中的消费整笔可见或整笔不可见，绝不报分叉
                if r["open_count"] != 0:
                    errors.append(f"spurious findings: {r['findings']}")
            except Exception as e:  # 撞锁/序列化异常也算事故
                errors.append(repr(e))
            time.sleep(0.001)

    t = threading.Thread(target=auditor)
    t.start()
    try:
        # 鸡蛋 12 个，分 12 笔消费，每笔 1，与对账交错
        for _ in range(12):
            consume_via_api(client, 2, 1)
    finally:
        stop.set(); t.join()

    assert not errors, errors
    # 全部消费完后：103 consumed；对账仍 0，且没有任何在架批被错误清零
    final = run_audit(reproject=False)
    assert final["open_count"] == 0
    c = connect()
    row = c.execute("SELECT status,qty_remain FROM lots WHERE id=103").fetchone()
    c.close()
    assert tuple(row) == ("consumed", 0)
    c = connect()
    untouched = c.execute("SELECT qty_remain,status FROM lots WHERE id=101").fetchone()
    c.close()
    assert tuple(untouched) == (2, "on_shelf")   # 无关业务批没被对齐清零


def test_audit_waits_behind_held_write_lock(db):
    """对账与消费都走 BEGIN IMMEDIATE + busy_timeout：写锁被持有时等待，
    拿到锁后看到的是一致快照，而不是把半成品当分叉。"""
    holder = connect()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE lots SET qty_remain=qty_remain-1 WHERE id=101")  # 未提交

    done = {}
    def go():
        try:
            done["r"] = run_audit(reproject=False)
        except Exception as e:
            done["e"] = e

    t = threading.Thread(target=go); t.start()
    time.sleep(0.3)
    assert "r" not in done and "e" not in done     # 对账在等锁，没读到半成品
    # 补写流水并整笔提交（与 /consume 同事务顺序）
    plan = {"ok": True, "deductions": [{"lot_id": 101, "take": 1}], "short": 0.0}
    holder.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                   ("x", json.dumps(plan), "2026-10-06T00:00:00+00:00"))
    holder.commit(); holder.close()
    t.join(timeout=10)
    assert "e" not in done
    assert done["r"]["open_count"] == 0           # 整笔提交后自洽，不误报
