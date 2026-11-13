"""FastAPI 入口：试验导入、列表、分桶查询、摘要重建。

启动后访问 /docs 即可在 Swagger UI 上传样例 CSV 并调整桶宽查询。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlite3 import Connection

from . import config
from .db import init_db, session
from .ingest import CsvValidationError, import_test
from .rebuild import (
    RebuildValidationError,
    ensure_pending,
    get_pending,
    record_attempt_error,
    rebuild_test_summaries,
)
from .schemas import (
    ImportResponse,
    QueryResponse,
    RebuildErrorOut,
    RebuildRequest,
    RebuildResponse,
    RepairStatusOut,
    TestOut,
)
from .service import RepairPendingError, bucket_query

# 列表 / 详情共用的查询列：附带待修状态
_TEST_COLUMNS = """
    t.id, t.name, t.source_filename, t.sample_count, t.start_ts, t.end_ts,
    t.max_level, t.imported_at,
    (r.test_id IS NOT NULL) AS repair_pending
"""
_TEST_FROM = "FROM tests t LEFT JOIN pending_repairs r ON r.test_id = t.id"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="泵站启泵试验水锤压力分析",
    description=(
        "导入记录仪单次试验 CSV（毫秒时间戳、kPa 压力），"
        "在 SQLite 中保存原始采样与多级摘要；按起止时刻与桶宽分桶，"
        "返回每桶首/尾/最高/最低点，空桶标记为缺测。"
        "故障盘救回的试验库可按编号重建摘要：重建前登记待修并暂停查询，"
        "校验通过、各级摘要重建完成后才解除待修。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_conn() -> Connection:
    with session() as conn:
        yield conn


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok"}


@app.post(
    "/tests/import",
    response_model=ImportResponse,
    status_code=201,
    tags=["试验导入"],
    summary="上传单次试验 CSV",
)
def upload_test(
    file: UploadFile = File(..., description="记录仪导出的 CSV，首行表头"),
    name: str = Form(..., description="试验名称，库内唯一"),
    conn: Connection = Depends(get_conn),
):
    content = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {config.MAX_UPLOAD_BYTES} 字节上限",
        )
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        result = import_test(conn, name=name, filename=file.filename or "", content=content)
    except CsvValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return result


@app.get("/tests", response_model=list[TestOut], tags=["试验查询"], summary="列出全部试验")
def list_tests(conn: Connection = Depends(get_conn)):
    rows = conn.execute(
        f"SELECT {_TEST_COLUMNS} {_TEST_FROM} ORDER BY t.id"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get(
    "/tests/{test_id}",
    response_model=TestOut,
    tags=["试验查询"],
    summary="查看单个试验元信息",
)
def get_test(test_id: int, conn: Connection = Depends(get_conn)):
    row = conn.execute(
        f"SELECT {_TEST_COLUMNS} {_TEST_FROM} WHERE t.id = ?",
        (test_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"试验 {test_id} 不存在")
    return dict(row)


@app.get(
    "/tests/{test_id}/query",
    response_model=QueryResponse,
    tags=["试验查询"],
    summary="按起止时刻与桶宽分桶，定位每桶水锤峰谷",
)
def query_buckets(
    test_id: int,
    start_ts: int = Query(..., description="查询起点毫秒时间戳（含），桶从该时刻起划分"),
    end_ts: int = Query(..., description="查询终点毫秒时间戳（不含），末桶截到该时刻"),
    bucket_width_ms: int = Query(..., gt=0, description="桶宽，毫秒，必须为正整数"),
    conn: Connection = Depends(get_conn),
):
    if end_ts <= start_ts:
        raise HTTPException(
            status_code=400, detail="end_ts 必须大于 start_ts（半开区间 [start, end)）"
        )
    try:
        return bucket_query(conn, test_id, start_ts, end_ts, bucket_width_ms)
    except RepairPendingError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "test_id": test_id,
                "repair_pending": True,
            },
        ) from None
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.post(
    "/tests/{test_id}/rebuild-summaries",
    response_model=RebuildResponse,
    responses={422: {"model": RebuildErrorOut, "description": "校验失败，待修保留"}},
    tags=["摘要重建"],
    summary="按试验编号重建峰谷摘要（故障盘救回后修复索引）",
)
def rebuild_summaries_api(
    test_id: int,
    payload: RebuildRequest | None = Body(
        None, description="可选的待修登记信息（原因、发起人）"
    ),
):
    reason = payload.reason if payload else ""
    requested_by = payload.requested_by if payload else ""

    # 阶段 1：待修登记先落库（独立事务）。此后该试验的峰谷查询提示待修；
    # 即使服务中途重启，待修状态也保留，可重试。
    with session() as conn:
        if conn.execute(
            "SELECT 1 FROM tests WHERE id = ?", (test_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail=f"试验 {test_id} 不存在")
        ensure_pending(conn, test_id, reason=reason, requested_by=requested_by)

    # 阶段 2：校验 + 重建 + 解除待修在同一事务生效；任何失败整体回滚，
    # 旧摘要原样保留，待修状态不丢。
    try:
        with session() as conn:
            result = rebuild_test_summaries(conn, test_id, config.BLOCK_SIZE)
    except RebuildValidationError as exc:
        with session() as conn:
            record_attempt_error(conn, test_id, str(exc))
        return JSONResponse(
            status_code=422,
            content=RebuildErrorOut(
                test_id=test_id,
                status="pending_repair",
                repair_pending=True,
                error=str(exc),
                message="校验失败：待修状态保留，该试验峰谷查询继续暂停；"
                "人工核对并修复采样后可重新发起重建",
            ).model_dump(),
        )
    except Exception as exc:
        # 写入失败等意外：待修同样保留（阶段 2 已回滚），记录原因后上抛
        try:
            with session() as conn:
                record_attempt_error(
                    conn, test_id, f"重建中断：{type(exc).__name__}: {exc}"
                )
        except Exception:
            pass  # 记录失败不影响原始异常上抛
        raise

    return RebuildResponse(
        test_id=test_id,
        status="rebuilt",
        repair_pending=False,
        max_level=result["max_level"],
        block_counts=result["block_counts"],
        validation=result["validation"],
        message="摘要重建完成，待修已解除，峰谷查询恢复",
    )


@app.get(
    "/tests/{test_id}/repair",
    response_model=RepairStatusOut,
    tags=["摘要重建"],
    summary="查看试验的待修登记状态",
)
def get_repair_status(test_id: int, conn: Connection = Depends(get_conn)):
    row = conn.execute(
        "SELECT id, name FROM tests WHERE id = ?", (test_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"试验 {test_id} 不存在")
    pending = get_pending(conn, test_id)
    if pending is None:
        return RepairStatusOut(
            test_id=test_id, test_name=row["name"], repair_pending=False
        )
    return RepairStatusOut(
        test_id=test_id,
        test_name=row["name"],
        repair_pending=True,
        reason=pending["reason"],
        requested_by=pending["requested_by"],
        attempts=pending["attempts"],
        last_error=pending["last_error"],
        marked_at=pending["marked_at"],
        updated_at=pending["updated_at"],
    )


@app.get(
    "/repairs",
    response_model=list[RepairStatusOut],
    tags=["摘要重建"],
    summary="列出全部待修试验",
)
def list_pending_repairs(conn: Connection = Depends(get_conn)):
    rows = conn.execute(
        """SELECT p.test_id, t.name AS test_name, p.reason, p.requested_by,
                  p.attempts, p.last_error, p.marked_at, p.updated_at
           FROM pending_repairs p JOIN tests t ON t.id = p.test_id
           ORDER BY p.marked_at"""
    ).fetchall()
    return [
        RepairStatusOut(
            test_id=r["test_id"],
            test_name=r["test_name"],
            repair_pending=True,
            reason=r["reason"],
            requested_by=r["requested_by"],
            attempts=r["attempts"],
            last_error=r["last_error"],
            marked_at=r["marked_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
