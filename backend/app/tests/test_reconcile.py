"""工程化对账验收：全层 / 层页 / 顶条三方对账。

覆盖需求：
* 基线 0 分叉；
* 人为改库制造「某层余量和 ≠ 层页过滤之和」与「顶条仍报 consumed 幽灵批」，
  报告必须含 layer 与 lot_id，入口（未修复）非 0；
* 默认只报告、连跑两次稳定在同一选择（计数不变、数据不变）；
* 打开 reproject 才重投影；修复后连跑两次都为 0，第二次仍非 0 则整场失败；
* 重投影绝不修改/清零业务 lots（指纹自证）；
* 对账进行中另有消费写入时，进行中那笔不被当分叉；
* 对账范围覆盖全层、层页、顶条三方，不只核一张表。
"""

import os
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import seed
from app.db import db_path
from app.engines.reconcile import reconcile, _main as reconcile_cli


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    seed.init_db()
    return str(db_path())


def _report(path):
    return reconcile(path, reproject=False)


# ---------- 基线 -----------------------------------------------------------

def test_baseline_zero(db_env):
    r = _report(db_env)
    assert r["divergence_count"] == 0
    cov = r["coverage"]
    # 三方都要覆盖：全层、每个层页、顶条
    assert cov["full"]["actual_lots"] == cov["full"]["projected_lots"] == 5
    assert set(cov["layers"]) == {"upper", "mid", "lower"}
    assert cov["bar"]["actual_lot_ids"] == cov["bar"]["projected_lot_ids"] == [1, 2, 4]
    # 全层总量 == Σ 各层页
    assert cov["full"]["actual_qty"] == pytest.approx(
        sum(v["actual_qty"] for v in cov["layers"].values()))
    assert r["business_lots_modified"] is False


# ---------- 人为改库：层页求和分叉 -----------------------------------------

def test_layer_sum_tamper_reports_layer_and_lot(db_env):
    c = sqlite3.connect(db_env)
    c.execute("UPDATE shelf_projection SET qty_remain=99 WHERE lot_id=1")  # upper
    c.commit(); c.close()

    r = _report(db_env)
    assert r["divergence_count"] >= 1
    d = next(x for x in r["divergences"] if x["lot_id"] == 1)
    assert d["kind"] == "qty_mismatch"
    # 报告必须含 layer 与 lot_id
    assert d["layer"] == "upper" and d["lot_id"] == 1
    # 全层与该层页过滤之和因此不等
    cov = r["coverage"]
    assert cov["full"]["projected_qty"] != pytest.approx(cov["full"]["actual_qty"])
    assert cov["layers"]["upper"]["projected_qty"] != \
        pytest.approx(cov["layers"]["upper"]["actual_qty"])


# ---------- 人为改库：顶条仍报 consumed 批 ----------------------------------

def test_bar_phantom_consumed_batch(db_env):
    c = sqlite3.connect(db_env)
    c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=2")
    c.execute("UPDATE shelf_projection SET in_bar=1 WHERE lot_id=2")
    c.commit(); c.close()

    r = _report(db_env)
    d = next(x for x in r["divergences"] if x["lot_id"] == 2)
    assert d["kind"] == "phantom_consumed"
    assert d["layer"] == "upper" and d["lot_id"] == 2
    assert 2 in r["coverage"]["bar"]["projected_lot_ids"]
    assert 2 not in r["coverage"]["bar"]["actual_lot_ids"]


def test_report_entrypoint_nonzero(db_env):
    c = sqlite3.connect(db_env)
    c.execute("UPDATE shelf_projection SET qty_remain=99 WHERE lot_id=1")
    c.commit(); c.close()
    # CLI 报告模式入口退出码必须非 0
    rc = reconcile_cli(["--db", db_env])
    assert rc == 1


# ---------- 默认只报告：连跑两次稳定在同一选择 ------------------------------

def test_report_only_is_stable_across_runs(db_env):
    c = sqlite3.connect(db_env)
    c.execute("UPDATE shelf_projection SET qty_remain=99 WHERE lot_id=1")
    c.commit(); c.close()

    r1 = _report(db_env)
    snap1 = sqlite3.connect(db_env).execute(
        "SELECT lot_id,qty_remain,in_bar FROM shelf_projection ORDER BY lot_id").fetchall()

    r2 = _report(db_env)  # 第二次连跑
    snap2 = sqlite3.connect(db_env).execute(
        "SELECT lot_id,qty_remain,in_bar FROM shelf_projection ORDER BY lot_id").fetchall()

    assert r1["divergence_count"] == r2["divergence_count"] == 1
    assert snap1 == snap2                      # 只报告，数据未被改写
    assert r1["mode"] == r2["mode"] == "report"


# ---------- 打开开关才重投影；两次为 0；绝不清零 lots ------------------------

def test_reproject_then_two_clean_runs(db_env):
    c = sqlite3.connect(db_env)
    c.execute("UPDATE shelf_projection SET qty_remain=99 WHERE lot_id=1")
    c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=2")
    c.execute("UPDATE shelf_projection SET in_bar=1 WHERE lot_id=2")
    c.commit(); c.close()

    lots_before = sqlite3.connect(db_env).execute(
        "SELECT id,qty_remain,status FROM lots ORDER BY id").fetchall()

    f1 = reconcile(db_env, reproject=True)
    assert f1["rebuilt"] is True
    f2 = reconcile(db_env, reproject=True)   # 第二次连跑
    assert f2["rebuilt"] is False            # 已一致 => 幂等 no-op
    # 硬性要求：修复后连跑两次都为 0，第二次仍非 0 整场失败
    assert f1["divergence_count"] == 0, f1
    assert f2["divergence_count"] == 0, "第二次仍非 0，整场失败"

    # 业务 lots 未被对账清零/改写
    lots_after = sqlite3.connect(db_env).execute(
        "SELECT id,qty_remain,status FROM lots ORDER BY id").fetchall()
    assert lots_before == lots_after
    assert f1["business_lots_modified"] is False
    assert f1["lots_checksum_before"] == f1["lots_checksum_after"]
    # consumed 批确实已离开投影与顶条
    assert f2["coverage"]["bar"]["projected_lot_ids"] == \
        f2["coverage"]["bar"]["actual_lot_ids"]


def test_reproject_cli_zero_exit(db_env):
    c = sqlite3.connect(db_env)
    c.execute("UPDATE shelf_projection SET qty_remain=50 WHERE lot_id=3")
    c.commit(); c.close()
    assert reconcile_cli(["--db", db_env, "--reproject"]) == 0
    assert reconcile_cli(["--db", db_env, "--reproject"]) == 0


def test_reproject_never_writes_lots_sql(db_env):
    src = open(os.path.join(os.path.dirname(__file__), "..", "engines", "reconcile.py")).read()
    # 修复路径里禁止出现针对业务 lots 的写/清零语句
    for forbidden in ("DELETE FROM lots", "UPDATE lots", "UPDATE  lots", "TRUNCATE"):
        assert forbidden not in src.replace("  ", " ")


# ---------- 进行中的消费不被当分叉（单连接未提交） ---------------------------

def test_inflight_uncommitted_consume_not_flagged(db_env):
    writer = sqlite3.connect(db_env, timeout=30)
    writer.execute("PRAGMA busy_timeout=30000")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE lots SET qty_remain=qty_remain-1 WHERE id=3")
    # 投影故意不动：模拟消费事务进行到一半、尚未提交/尚未维护投影
    try:
        r = _report(db_env)   # 对账必须读已提交快照，看不到这笔
        assert r["divergence_count"] == 0
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert _report(db_env)["divergence_count"] == 0


# ---------- 并发压测：边消费边对账 ------------------------------------------

def test_concurrent_consumes_and_reconciles_never_diverge(db_env, monkeypatch):
    monkeypatch.setenv("DATA_DIR", os.path.dirname(db_env))
    from app.main import app
    stop = threading.Event()
    errors = []

    def consumer():
        with TestClient(app) as client:
            for _ in range(20):
                if stop.is_set():
                    return
                resp = client.post("/api/consume", json={"item_id": 2, "qty": 1})
                if resp.status_code not in (200, 409):
                    errors.append(resp.status_code)

    threads = [threading.Thread(target=consumer) for _ in range(4)]
    reports = []
    with TestClient(app) as client:
        for t in threads:
            t.start()
        for _ in range(30):
            rep = client.post("/api/reconcile", json={"reproject": False})
            assert rep.status_code == 200
            body = rep.json()
            reports.append(body["divergence_count"])
        stop.set()
        for t in threads:
            t.join()
        final = client.post("/api/reconcile", json={"reproject": False}).json()

    assert not errors
    # 对账期间任何已提交时刻都一致：进行中那笔从未被报为分叉
    assert all(n == 0 for n in reports), reports
    assert final["divergence_count"] == 0


# ---------- API 级：业务路由后三方始终一致 ----------------------------------

def test_api_business_writes_keep_projection_consistent(db_env, monkeypatch):
    monkeypatch.setenv("DATA_DIR", os.path.dirname(db_env))
    from app.main import app
    with TestClient(app) as client:
        # FEFO 消费若干（会把 lot 1、lot 2 吃到 consumed），再入库、再扫过期
        for _ in range(3):
            client.post("/api/consume", json={"item_id": 1, "qty": 1})
        client.post("/api/lots", json={"item_id": 2, "qty": 5, "expiry": "2026-10-08"})
        client.post("/api/expire-sweep", json={})

        full = client.get("/api/fridge").json()
        per_layer = {L: client.get(f"/api/fridge?layer={L}").json()
                     for L in ("upper", "mid", "lower")}
        alerts = client.get("/api/alerts").json()
        rep = client.post("/api/reconcile", json={"reproject": False}).json()

        # 全层 == Σ 各层页（lot_id 集合与余量和）
        assert {x["id"] for x in full} == set().union(
            *[{x["id"] for x in rows} for rows in per_layer.values()])
        assert sum(x["qty_remain"] for x in full) == pytest.approx(
            sum(x["qty_remain"] for rows in per_layer.values() for x in rows))
        # 顶条只含全层在架批，且不含 consumed
        assert {x["id"] for x in alerts} <= {x["id"] for x in full}
        assert all(x["status"] != "consumed" for x in alerts if "status" in x)
        assert rep["divergence_count"] == 0
        assert rep["coverage"]["full"]["projected_lots"] == len(full)


def test_reconcile_endpoint_default_is_report_only(db_env, monkeypatch):
    monkeypatch.setenv("DATA_DIR", os.path.dirname(db_env))
    from app.main import app
    raw = sqlite3.connect(db_env)
    raw.execute("UPDATE shelf_projection SET qty_remain=99 WHERE lot_id=1")
    raw.commit(); raw.close()
    with TestClient(app) as client:
        b1 = client.post("/api/reconcile", json={}).json()      # 默认体 => 只报告
        b2 = client.post("/api/reconcile", json={"reproject": False}).json()
        assert b1["mode"] == b2["mode"] == "report"
        assert b1["divergence_count"] == b2["divergence_count"] >= 1
        fixed = client.post("/api/reconcile", json={"reproject": True}).json()
        assert fixed["divergence_count"] == 0
        again = client.post("/api/reconcile", json={"reproject": True}).json()
        assert again["divergence_count"] == 0 and again["rebuilt"] is False
