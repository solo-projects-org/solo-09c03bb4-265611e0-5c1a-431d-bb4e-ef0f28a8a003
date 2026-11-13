"""摘要重建 API：待修登记、校验失败保留待修、重建成功恢复查询。

覆盖需求要点：
- 重建前把待修状态落库，待修期间该试验峰谷查询提示待修（409），其他试验照常；
- 校验（严格递增 / 点数 / 首尾时刻）失败 -> 422，待修保留，可重试；
- 重建成功 -> 新摘要、最高层号、解除待修一起生效，原编号可查峰谷；
- 待修状态已提交，换新连接（模拟服务重启）仍在；
- 重建不改采样与试验编号。
"""

from __future__ import annotations

import os
import tempfile

# 与 test_api.py 共用同一套环境；独立运行时也能自洽
_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("PUMP_DB_PATH", os.path.join(_tmp.name, "test.db"))
os.environ.setdefault("PUMP_BLOCK_SIZE", "4")
os.environ.setdefault("PUMP_MAX_BUCKETS", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)

BASE_TS = 1_731_800_000_000  # 固定基准时刻，避免与 test_api 的数据重叠


def _rows(n, base=BASE_TS, interval=10):
    return [(base + i * interval, 100.0 + i) for i in range(n)]


def _csv_bytes(rows):
    buf = "timestamp_ms,pressure_kpa\n" + "".join(f"{int(ts)},{p}\n" for ts, p in rows)
    return buf.encode()


def _import(rows, name):
    resp = client.post(
        "/tests/import",
        data={"name": name},
        files={"file": ("r.csv", _csv_bytes(rows), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _query(test_id, start, end, width):
    return client.get(
        f"/tests/{test_id}/query",
        params={"start_ts": start, "end_ts": end, "bucket_width_ms": width},
    )


def _db():
    return connect()


# ---------------------------------------------------------------- 成功重建

def test_rebuild_success_restores_query():
    """救回试验：采样在、摘要残缺 -> 查询出现缺测假象 -> 重建后恢复。"""
    rows = _rows(12)
    imported = _import(rows, "rebuild-ok")
    tid = imported["id"]
    first_ts, last_ts = rows[0][0], rows[-1][0]

    # 模拟故障盘救回：level>=2 整层丢失，level 1 尾部块也丢了
    conn = _db()
    conn.execute("DELETE FROM blocks WHERE test_id = ? AND level >= 2", (tid,))
    conn.execute(
        "DELETE FROM blocks WHERE test_id = ? AND level = 1 AND block_index >= 2",
        (tid,),
    )
    conn.commit()

    # 重建前：查询返回 200，但尾部区间因索引缺失而显示缺测（假象）
    span_query = _query(tid, first_ts, last_ts + 1, config.BLOCK_SIZE * 10)
    assert span_query.status_code == 200
    missing_before = [b for b in span_query.json()["buckets"] if b["missing"]]
    assert missing_before, "摘要残缺时应出现缺测假象"

    # 发起重建 -> 200：新摘要、最高层号、解除待修一起生效
    resp = client.post(f"/tests/{tid}/rebuild-summaries")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rebuilt"
    assert body["repair_pending"] is False
    assert body["max_level"] == imported["max_level"]
    assert body["block_counts"] == imported["block_counts"]

    # 重建后：原编号查询恢复，残缺区间的桶不再缺测
    after = _query(tid, first_ts, last_ts + 1, config.BLOCK_SIZE * 10)
    assert after.status_code == 200
    assert all(not b["missing"] for b in after.json()["buckets"])
    assert after.json()["metrics"]["raw_rows_read"] == 0  # 全程走新摘要

    # 待修已解除
    assert client.get(f"/tests/{tid}/repair").json()["repair_pending"] is False
    conn.close()


def test_rebuild_does_not_touch_samples_or_test_row():
    """重建只换摘要与最高层号：采样行、试验编号、登记信息原样保留。"""
    rows = _rows(12, base=1_731_900_000_000)
    imported = _import(rows, "rebuild-immutable")
    tid = imported["id"]

    conn = _db()
    before_samples = conn.execute(
        "SELECT rowid, ts, pressure FROM samples WHERE test_id = ? ORDER BY rowid",
        (tid,),
    ).fetchall()
    before_test = conn.execute(
        "SELECT * FROM tests WHERE id = ?", (tid,)
    ).fetchone()
    conn.close()

    resp = client.post(f"/tests/{tid}/rebuild-summaries")
    assert resp.status_code == 200, resp.text

    conn = _db()
    after_samples = conn.execute(
        "SELECT rowid, ts, pressure FROM samples WHERE test_id = ? ORDER BY rowid",
        (tid,),
    ).fetchall()
    after_test = conn.execute(
        "SELECT * FROM tests WHERE id = ?", (tid,)
    ).fetchone()
    conn.close()

    assert [tuple(r) for r in after_samples] == [tuple(r) for r in before_samples]
    # 除 max_level 外登记列不变（采样本身未动，max_level 也与原登记一致）
    assert dict(after_test) == dict(before_test)


# ---------------------------------------------------------------- 校验失败

def _insert_disorder_test(name):
    """直接写库造一场'救回'试验：采样时刻未严格递增（绕过导入校验）。"""
    rows = _rows(12, base=1_732_000_000_000)
    rows[5], rows[6] = rows[6], rows[5]  # 交换两行，制造乱序
    conn = _db()
    cur = conn.execute(
        "INSERT INTO tests (name, source_filename, sample_count, start_ts, end_ts, max_level)"
        " VALUES (?, 'recorder_dump.csv', ?, ?, ?, 1)",
        (name, len(rows), rows[0][0], rows[-1][0]),
    )
    tid = cur.lastrowid
    conn.executemany(
        "INSERT INTO samples (test_id, ts, pressure) VALUES (?, ?, ?)",
        [(tid, ts, p) for ts, p in rows],
    )
    conn.commit()
    conn.close()
    return tid, rows


def test_disorder_rebuild_rejected_and_stays_pending():
    """采样乱序 -> 422，待修保留；该试验查询转 409，其他试验不受影响。"""
    tid, rows = _insert_disorder_test("rebuild-disorder")

    resp = client.post(f"/tests/{tid}/rebuild-summaries")
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["repair_pending"] is True
    assert "严格递增" in body["error"] or "未严格递增" in body["error"]

    # 待修期间：该试验峰谷查询一律 409
    q = _query(tid, rows[0][0], rows[-1][0] + 1, 40)
    assert q.status_code == 409
    assert q.json()["detail"]["repair_pending"] is True

    # 换一个健康试验：照常可查（互不影响）
    ok = _import(_rows(8, base=1_732_100_000_000), "rebuild-healthy")
    assert _query(ok["id"], 1_732_100_000_000, 1_732_100_000_000 + 80, 40).status_code == 200

    # 待修状态已落库：换新连接（模拟服务重启后）仍在
    conn = _db()
    pending = conn.execute(
        "SELECT * FROM pending_repairs WHERE test_id = ?", (tid,)
    ).fetchone()
    assert pending is not None and pending["attempts"] == 1
    conn.close()

    # 重试仍是 422，尝试次数累加，待修不丢
    resp2 = client.post(f"/tests/{tid}/rebuild-summaries")
    assert resp2.status_code == 422
    conn = _db()
    pending = conn.execute(
        "SELECT * FROM pending_repairs WHERE test_id = ?", (tid,)
    ).fetchone()
    assert pending is not None and pending["attempts"] == 2
    conn.close()


def test_count_mismatch_rejected_and_stays_pending():
    """登记点数与实际采样不符 -> 422，待修保留。"""
    rows = _rows(10, base=1_732_200_000_000)
    imported = _import(rows, "rebuild-count-mismatch")
    tid = imported["id"]

    # 模拟救回丢行：删掉末尾 2 行采样，登记信息不动
    conn = _db()
    conn.execute(
        "DELETE FROM samples WHERE test_id = ? AND rowid IN "
        "(SELECT rowid FROM samples WHERE test_id = ? ORDER BY rowid DESC LIMIT 2)",
        (tid, tid),
    )
    conn.commit()
    conn.close()

    resp = client.post(f"/tests/{tid}/rebuild-summaries")
    assert resp.status_code == 422, resp.text
    assert "点数不符" in resp.json()["error"]

    # 待修保留，last_error 可见
    status = client.get(f"/tests/{tid}/repair").json()
    assert status["repair_pending"] is True
    assert "点数不符" in status["last_error"]

    # 待修列表里能看到这场试验
    listed = client.get("/repairs").json()
    assert any(r["test_id"] == tid for r in listed)


def test_first_last_ts_mismatch_rejected():
    """首/末采样时刻与登记不符 -> 422。"""
    rows = _rows(8, base=1_732_300_000_000)
    imported = _import(rows, "rebuild-ts-mismatch")
    tid = imported["id"]

    # 模拟登记信息被改坏：末时刻与采样对不上
    conn = _db()
    conn.execute("UPDATE tests SET end_ts = end_ts + 1 WHERE id = ?", (tid,))
    conn.commit()
    conn.close()

    resp = client.post(f"/tests/{tid}/rebuild-summaries")
    assert resp.status_code == 422
    assert "末个采样时刻不符" in resp.json()["error"]
    assert client.get(f"/tests/{tid}/repair").json()["repair_pending"] is True


# ---------------------------------------------------------------- 重试成功

def test_manual_fix_then_rebuild_succeeds():
    """人工修复采样后重试：重建成功，待修解除，原编号可查峰谷。"""
    tid, rows = _insert_disorder_test("rebuild-manual-fix")

    assert client.post(f"/tests/{tid}/rebuild-summaries").status_code == 422
    assert _query(tid, 1_732_000_000_000, 1_732_000_000_000 + 120, 40).status_code == 409

    # 档案员人工修复：把交换的两行时刻换回来（只动这两行的 ts）
    conn = _db()
    r5 = conn.execute(
        "SELECT rowid, ts FROM samples WHERE test_id = ? ORDER BY rowid LIMIT 1 OFFSET 5",
        (tid,),
    ).fetchone()
    r6 = conn.execute(
        "SELECT rowid, ts FROM samples WHERE test_id = ? ORDER BY rowid LIMIT 1 OFFSET 6",
        (tid,),
    ).fetchone()
    conn.execute("UPDATE samples SET ts = ? WHERE rowid = ?", (r6["ts"], r5["rowid"]))
    conn.execute("UPDATE samples SET ts = ? WHERE rowid = ?", (r5["ts"], r6["rowid"]))
    conn.commit()
    conn.close()

    # 重试成功：待修解除，原编号查询恢复
    resp = client.post(f"/tests/{tid}/rebuild-summaries")
    assert resp.status_code == 200, resp.text
    assert resp.json()["repair_pending"] is False
    q = _query(tid, 1_732_000_000_000, 1_732_000_000_000 + 120, 40)
    assert q.status_code == 200
    assert all(not b["missing"] for b in q.json()["buckets"])
    assert client.get(f"/tests/{tid}/repair").json()["repair_pending"] is False
