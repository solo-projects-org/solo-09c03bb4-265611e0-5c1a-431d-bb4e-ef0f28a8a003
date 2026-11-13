"""故障盘救回试验的摘要重建。

背景：试验库从故障盘救回后，部分试验的原始采样（samples）还在，
峰谷摘要（blocks）却残缺或整层丢失，直接查询会把"索引缺失"误看成
记录仪断采。档案员调用重建接口修复，流程分两段事务：

1. **待修登记**（独立事务，先落库）：向 pending_repairs 写入/更新
   待修记录并立即提交。此后对该试验的峰谷查询一律提示"待修"，
   其他试验不受影响。服务中途重启、后续校验或写入失败，待修状态
   都保留，档案员可重试。
2. **校验 + 重建 + 解除待修**（单事务，全成或全不成）：
   - 按入库行号（samples.rowid）核对采样时刻严格递增；
   - 核对采样点数、首尾时刻与试验登记（tests 行）一致；
   - 校验通过才删除旧摘要、逐级重建全部层级、更新最高层号
     （tests.max_level）、删除待修登记——四者在同一事务提交；
   - 任何一步失败整体回滚，旧摘要原样保留，待修状态不丢。

全程不改 samples 与 tests 的试验编号（id），只动 blocks、
tests.max_level 与 pending_repairs。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

from .summaries import build_summaries


class RebuildValidationError(ValueError):
    """重建前校验失败：采样与试验登记对不上，需人工核对，待修保留。"""


@dataclass
class ValidationReport:
    """校验阶段对采样与登记信息的一致性核对结果。"""

    registered_sample_count: int
    registered_start_ts: int
    registered_end_ts: int
    actual_sample_count: int
    actual_start_ts: int
    actual_end_ts: int
    strictly_increasing: bool

    def as_dict(self) -> dict:
        return asdict(self)


def ensure_pending(
    conn: sqlite3.Connection,
    test_id: int,
    reason: str = "",
    requested_by: str = "",
) -> None:
    """登记待修（幂等）：已待修时保留首次登记信息，仅累加尝试次数。

    调用方负责提交事务；本函数只写 pending_repairs，不动其他表。
    """
    conn.execute(
        """INSERT INTO pending_repairs (test_id, reason, requested_by)
           VALUES (?, ?, ?)
           ON CONFLICT(test_id) DO UPDATE SET
               attempts  = attempts + 1,
               updated_at = datetime('now')""",
        (test_id, reason, requested_by),
    )


def get_pending(conn: sqlite3.Connection, test_id: int) -> dict | None:
    """读取某试验的待修记录；未待修返回 None。"""
    row = conn.execute(
        "SELECT * FROM pending_repairs WHERE test_id = ?", (test_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def record_attempt_error(
    conn: sqlite3.Connection, test_id: int, error: str
) -> None:
    """把最近一次重建失败原因记到待修行上（独立事务提交，便于排查）。"""
    conn.execute(
        """UPDATE pending_repairs
           SET last_error = ?, updated_at = datetime('now')
           WHERE test_id = ?""",
        (error, test_id),
    )


def clear_pending(conn: sqlite3.Connection, test_id: int) -> None:
    """解除待修。只在重建事务末尾调用，与新摘要、最高层号一起提交。"""
    conn.execute("DELETE FROM pending_repairs WHERE test_id = ?", (test_id,))


def validate_test_samples(
    conn: sqlite3.Connection, test_id: int
) -> ValidationReport:
    """重建前校验：采样与试验登记必须一致，否则拒绝重建。

    核对项（只读，不改任何数据）：
    1. 按入库行号（samples.rowid）采样时刻严格递增；
    2. 实际采样点数与登记点数一致；
    3. 首、尾采样时刻与登记的起止时刻一致。
    """
    test = conn.execute(
        "SELECT sample_count, start_ts, end_ts FROM tests WHERE id = ?",
        (test_id,),
    ).fetchone()
    if test is None:
        raise KeyError(f"试验 {test_id} 不存在")

    # 1) 严格递增：按入库行号顺序比较相邻两行的时间戳
    disorder = conn.execute(
        """SELECT rid, ts, prev_ts FROM (
               SELECT rowid AS rid, ts,
                      LAG(ts) OVER (ORDER BY rowid) AS prev_ts
               FROM samples WHERE test_id = ?
           ) WHERE prev_ts IS NOT NULL AND ts <= prev_ts
           ORDER BY rid LIMIT 1""",
        (test_id,),
    ).fetchone()
    if disorder is not None:
        raise RebuildValidationError(
            f"入库行号 {disorder['rid']} 的采样时刻 {disorder['ts']} 未严格递增"
            f"（前一行为 {disorder['prev_ts']}）：救回数据疑似错乱，"
            "需人工核对记录仪原始文件后重新导入采样"
        )

    # 2) 点数核对
    actual_count = conn.execute(
        "SELECT COUNT(*) FROM samples WHERE test_id = ?", (test_id,)
    ).fetchone()[0]
    if actual_count == 0:
        raise RebuildValidationError(
            "该试验没有任何采样行，无法重建摘要，"
            "请先从记录仪备份补齐采样"
        )
    if actual_count != test["sample_count"]:
        raise RebuildValidationError(
            f"采样点数不符：库内实际 {actual_count} 点，"
            f"试验登记 {test['sample_count']} 点，"
            "疑似救回时丢行，需人工核对后补齐采样或更正登记"
        )

    # 3) 首尾时刻核对
    actual_start = conn.execute(
        "SELECT ts FROM samples WHERE test_id = ? ORDER BY rowid LIMIT 1",
        (test_id,),
    ).fetchone()[0]
    actual_end = conn.execute(
        "SELECT ts FROM samples WHERE test_id = ? ORDER BY rowid DESC LIMIT 1",
        (test_id,),
    ).fetchone()[0]
    if actual_start != test["start_ts"]:
        raise RebuildValidationError(
            f"首个采样时刻不符：实际 {actual_start}，"
            f"试验登记 {test['start_ts']}，需人工核对登记信息"
        )
    if actual_end != test["end_ts"]:
        raise RebuildValidationError(
            f"末个采样时刻不符：实际 {actual_end}，"
            f"试验登记 {test['end_ts']}，需人工核对登记信息"
        )

    return ValidationReport(
        registered_sample_count=test["sample_count"],
        registered_start_ts=test["start_ts"],
        registered_end_ts=test["end_ts"],
        actual_sample_count=actual_count,
        actual_start_ts=actual_start,
        actual_end_ts=actual_end,
        strictly_increasing=True,
    )


def rebuild_test_summaries(
    conn: sqlite3.Connection, test_id: int, block_size: int
) -> dict:
    """校验并重建某试验的全部摘要层级。

    必须在事务内调用（调用方负责提交/回滚）：校验通过后，
    删除旧摘要、逐级重建、更新最高层号、解除待修，随事务一起生效。
    校验失败抛 RebuildValidationError，事务回滚后旧摘要原样保留。
    """
    report = validate_test_samples(conn, test_id)

    conn.execute("DELETE FROM blocks WHERE test_id = ?", (test_id,))
    max_level = build_summaries(conn, test_id, block_size)
    conn.execute(
        "UPDATE tests SET max_level = ? WHERE id = ?", (max_level, test_id)
    )
    clear_pending(conn, test_id)

    block_counts = dict(
        conn.execute(
            "SELECT level, COUNT(*) FROM blocks WHERE test_id = ? GROUP BY level",
            (test_id,),
        ).fetchall()
    )
    return {
        "test_id": test_id,
        "max_level": max_level,
        "block_counts": {str(k): v for k, v in sorted(block_counts.items())},
        "validation": report.as_dict(),
    }
