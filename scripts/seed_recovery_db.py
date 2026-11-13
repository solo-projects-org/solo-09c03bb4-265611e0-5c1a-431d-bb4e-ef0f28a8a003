#!/usr/bin/env python3
"""构造"故障盘救回"样例库，演示按编号重建摘要的完整流程。

向目标 SQLite 库写入两场"救回"试验：

- `recovered-ok`：采样完整，但摘要残缺（level>=2 整层丢失、level 1 尾部块
  缺失）。直接查询会把索引缺失看成记录仪断采；调用重建接口后恢复。
- `recovered-broken`：登记 200 点，救回后实际只剩 195 行采样（尾部丢失），
  摘要同样残缺。重建校验失败（点数不符），待修保留，该试验查询转 409，
  人工修复前重试仍失败。

幂等：试验名已存在则跳过，重复启动容器不会重复写入。

用法：
    python3 scripts/seed_recovery_db.py [db_path]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 保证能 import app 包（容器内 WORKDIR=/app，或仓库根目录直接运行均可）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, init_db  # noqa: E402
from app.ingest import import_test  # noqa: E402
from scripts.generate_sample import pressure_at  # noqa: E402

DEFAULT_DB = os.environ.get("PUMP_DB_PATH", "/data/pump_tests.db")

# 样例曲线参数：与 generate_sample 同一基准，便于和正常导入的试验对比
OK_BASE_TS = 1_731_300_000_000
OK_POINTS = 1500
BROKEN_BASE_TS = 1_731_400_000_000
BROKEN_POINTS = 200
LOST_ROWS = 5  # recovered-broken 救回时丢失的尾部采样行数


def _csv_bytes(rows: list[tuple[int, float]]) -> bytes:
    return ("timestamp_ms,pressure_kpa\n" + "".join(f"{int(ts)},{p}\n" for ts, p in rows)).encode()


def _synth_rows(n: int, base_ts: int, interval_ms: int = 10) -> list[tuple[int, float]]:
    return [(base_ts + i * interval_ms, pressure_at(i * interval_ms)) for i in range(n)]


def _exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM tests WHERE name = ?", (name,)).fetchone() is not None


def seed_recovered_ok(conn) -> None:
    """采样完整、摘要残缺的试验：重建应当成功。"""
    name = "recovered-ok"
    if _exists(conn, name):
        print(f"[skip] {name} 已存在")
        return
    rows = _synth_rows(OK_POINTS, OK_BASE_TS)
    result = import_test(
        conn, name=name, filename="recorder_dump_ok.csv",
        content=_csv_bytes(rows),
    )
    tid = result["id"]
    # 模拟故障盘救回：level>=2 摘要整层丢失，level 1 尾部块也没救回来
    conn.execute("DELETE FROM blocks WHERE test_id = ? AND level >= 2", (tid,))
    conn.execute(
        "DELETE FROM blocks WHERE test_id = ? AND level = 1 AND block_index >= 8",
        (tid,),
    )
    print(
        f"[seed] {name}: id={tid}, {result['sample_count']} 点；"
        "摘要残缺（level>=2 丢失、level 1 尾部块缺失），直接查询尾部区间会出现缺测假象"
    )


def seed_recovered_broken(conn) -> None:
    """登记与实存采样不符的试验：重建校验失败，待修保留。"""
    name = "recovered-broken"
    if _exists(conn, name):
        print(f"[skip] {name} 已存在")
        return
    rows = _synth_rows(BROKEN_POINTS, BROKEN_BASE_TS)
    result = import_test(
        conn, name=name, filename="recorder_dump_broken.csv",
        content=_csv_bytes(rows),
    )
    tid = result["id"]
    # 模拟救回丢行：末尾 LOST_ROWS 行采样没救回来，登记信息保持原样
    conn.execute(
        "DELETE FROM samples WHERE test_id = ? AND rowid IN "
        "(SELECT rowid FROM samples WHERE test_id = ? ORDER BY rowid DESC LIMIT ?)",
        (tid, tid, LOST_ROWS),
    )
    # 摘要也残缺：level>=2 丢失
    conn.execute("DELETE FROM blocks WHERE test_id = ? AND level >= 2", (tid,))
    print(
        f"[seed] {name}: id={tid}, 登记 {result['sample_count']} 点 / 实存 "
        f"{result['sample_count'] - LOST_ROWS} 行；重建校验应失败（点数不符），待修保留"
    )


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    init_db(db_path)
    conn = connect(db_path)
    try:
        seed_recovered_ok(conn)
        seed_recovered_broken(conn)
        conn.commit()
    finally:
        conn.close()
    print("[done] 恢复样例库就绪")


if __name__ == "__main__":
    main()
