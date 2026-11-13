"""HTTP 请求 / 响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PointOut(BaseModel):
    ts: int = Field(..., description="毫秒时间戳")
    pressure: float = Field(..., description="压力 kPa")
    roles: list[str] = Field(
        ..., description="该点在桶内承担的角色：first/last/max/min"
    )


class BucketOut(BaseModel):
    bucket_index: int = Field(..., description="从 0 开始的桶序号")
    start_ts: int = Field(..., description="桶左边界（含），毫秒")
    end_ts: int = Field(..., description="桶右边界（不含），毫秒；末桶截到查询终点")
    sample_count: int = Field(..., description="桶内采样点数")
    missing: bool = Field(..., description="空桶（缺测）时为 true")
    points: list[PointOut] = Field(
        ..., description="按时序排列、去重后的首/尾/最高/最低点"
    )


class QueryMetrics(BaseModel):
    blocks_used: int = Field(..., description="直接采用摘要块的次数")
    blocks_by_level: dict[str, int] = Field(
        ..., description="各层级被直接采用的块数，层级越高粒度越粗"
    )
    raw_rows_read: int = Field(
        ..., description="回退读取原始采样的行数；边界不切块时为 0"
    )


class QueryResponse(BaseModel):
    test_id: int
    test_name: str
    start_ts: int
    end_ts: int
    bucket_width_ms: int
    bucket_count: int
    missing_bucket_count: int
    metrics: QueryMetrics
    buckets: list[BucketOut]


class TestOut(BaseModel):
    id: int
    name: str
    source_filename: str
    sample_count: int
    start_ts: int
    end_ts: int
    max_level: int
    imported_at: str
    repair_pending: bool = Field(
        False, description="是否处于摘要待修状态（待修期间峰谷查询暂停）"
    )


class ImportResponse(TestOut):
    block_counts: dict[str, int] = Field(
        ..., description="每个摘要层级生成的块数"
    )


class RebuildRequest(BaseModel):
    reason: str = Field("", description="待修原因备注，随待修登记保存")
    requested_by: str = Field("", description="发起人（档案员）标识")


class RebuildValidationOut(BaseModel):
    """重建前对采样与登记信息的核对结果。"""

    registered_sample_count: int
    registered_start_ts: int
    registered_end_ts: int
    actual_sample_count: int
    actual_start_ts: int
    actual_end_ts: int
    strictly_increasing: bool


class RebuildResponse(BaseModel):
    test_id: int
    status: str = Field(..., description="rebuilt 表示重建完成")
    repair_pending: bool = Field(..., description="重建成功后为 false（待修已解除）")
    max_level: int = Field(..., description="重建后的最高摘要层级号")
    block_counts: dict[str, int] = Field(..., description="每个摘要层级重建出的块数")
    validation: RebuildValidationOut
    message: str


class RebuildErrorOut(BaseModel):
    """校验失败响应：待修状态保留，修复数据后可重试。"""

    test_id: int
    status: str = Field(..., description="pending_repair 表示仍处待修")
    repair_pending: bool = Field(..., description="校验失败时恒为 true")
    error: str = Field(..., description="校验失败原因")
    message: str


class RepairStatusOut(BaseModel):
    test_id: int
    test_name: str
    repair_pending: bool
    reason: str = ""
    requested_by: str = ""
    attempts: int = 0
    last_error: str | None = None
    marked_at: str | None = None
    updated_at: str | None = None
