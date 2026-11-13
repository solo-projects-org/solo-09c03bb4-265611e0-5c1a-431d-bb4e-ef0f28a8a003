# 泵站启泵试验 · 水锤压力峰谷分析服务

面向泵站调试员的启泵试验复盘工具：把记录仪导出的**单次试验 CSV** 导入
FastAPI 服务，采样与多级摘要落盘到 **SQLite**；复盘时给定起止时刻和桶宽，
按桶返回首尾点与压力最高 / 最低点，快速定位水锤造成的峰谷，空桶自动标记缺测。

## 功能与约定

- **CSV 导入**（`POST /tests/import`，Swagger 可直接上传）
  - 首行表头，自动识别时间戳列与压力列，支持中英文表头
    （`timestamp_ms / ts / 时间戳 …`、`pressure_kpa / pressure / 压力(kPa) …`），
    UTF-8 与 GBK 编码均可
  - 毫秒时间戳必须为整数且**严格递增**，重复或倒序整次拒绝（HTTP 400）
  - 压力为有限数值，单位 kPa；单事务写入，失败不留半成品
- **分桶查询**（`GET /tests/{id}/query`）
  - 从 `start_ts` 起按 `bucket_width_ms` 分桶，**含左端、不含右端**，
    末桶截到 `end_ts`
  - 每桶按时序返回首点、尾点、压力最高点、最低点；**同一点只列一次**，
    角色合并到 `roles`；极值并列时取时间戳最早的点
  - 桶内无采样时 `missing=true`、`points=[]`
- **多级摘要加速**（核心）
  - level 1 为最细索引块（默认每 128 个采样一块），其上逐级合并，
    每块保存首尾点、最高 / 最低点（含时间戳）、采样数与原始 rowid 区间
  - 查询时从最粗层级开始：**完整落在桶内的块直接采用摘要**；
    只有桶边界切开 level 1 最细块时，才按 rowid 区间回读原始采样
  - 响应中的 `metrics` 如实回显：`blocks_by_level`（各级摘要命中数）
    与 `raw_rows_read`（回读原始行数，边界不切块时为 0）
- **故障库恢复：按编号重建摘要**
  - `POST /tests/{id}/rebuild-summaries`：先把待修状态落库（独立事务），
    再校验并重建；待修期间该试验峰谷查询返回 **409**，其他试验照常可查
  - 校验：按入库行号（rowid）采样时刻严格递增、点数与首尾时刻同登记一致；
    失败返回 **422**，待修保留可重试，全程不改采样与试验编号
  - 重建成功：新摘要、最高层号、解除待修**同一事务生效**；
    校验 / 写入失败或服务中途重启，待修状态都保留
  - 单次查询在**同一事务快照**内完成，不会混读重建前后的摘要

## 目录结构

```
app/
  config.py      环境变量配置（库路径、块大小、上传上限、桶数上限）
  db.py          SQLite 连接、建表（tests / samples / blocks / pending_repairs）
  ingest.py      CSV 解析、严格校验、导入
  summaries.py   多级摘要构建与区间递归查询
  rebuild.py     故障库摘要重建：待修登记、一致性校验、原子重建
  service.py     分桶编排、点去重与角色合并、待修拦截
  schemas.py     Pydantic 请求 / 响应模型
  main.py        FastAPI 路由
sample_data/     样例 CSV（由 scripts/generate_sample.py 生成）
scripts/         样例生成与恢复库播种脚本
tests/           pytest 测试（25 项）
Dockerfile, docker-compose.yml, docker-compose.recovery.yml
```

## Docker Compose 启动

```bash
docker compose up --build
```

- 服务：<http://localhost:8000>
- Swagger UI：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- SQLite 数据保存在命名卷 `pump-data`（容器内 `/data/pump_tests.db`）

## 复现样例试验

样例 `sample_data/pump_start_demo.csv` 为 2 万点、10 ms 间隔的合成启泵曲线
（静压 → 启泵爬升 → 衰减振荡，含一次低压深谷），可重新生成：

```bash
python3 scripts/generate_sample.py
```

在 Swagger（`/docs`）中的操作步骤：

1. `POST /tests/import`：`name` 填 `demo-001`，`file` 选择
   `sample_data/pump_start_demo.csv`，执行。响应返回试验 id、时间范围与各级块数。
2. `GET /tests`：确认试验已入库。
3. `GET /tests/{id}/query`：
   - `start_ts=1731300000000`、`end_ts=1731300019999`、`bucket_width_ms=1000`
     → 200 个 1 秒桶，逐桶查看峰谷；
   - 改成 `bucket_width_ms=1280`（与块边界对齐）再试，
     可见 `metrics.raw_rows_read = 0`，全程只走摘要；
   - 换 `start_ts=1731299990000&end_ts=1731299995000` 可看到全部桶 `missing=true`。

小样本 `sample_data/small_demo.csv`（60 点）适合在 Swagger 里肉眼逐桶核对。

### curl 示例

```bash
curl -X POST http://localhost:8000/tests/import \
  -F "name=demo-001" \
  -F "file=@sample_data/pump_start_demo.csv;type=text/csv"

curl "http://localhost:8000/tests/1/query?start_ts=1731300000000&end_ts=1731300019999&bucket_width_ms=1000"
```

## 故障盘救回：按编号重建摘要

试验库从故障盘救回后，可能出现**采样还在、峰谷摘要残缺**的情况：直接查询
会把索引缺失看成记录仪断采。重建接口的流程与保证：

1. **先登记待修**（独立事务落库 `pending_repairs`）：此后该试验的峰谷查询
   一律返回 409，避免误读残缺摘要；其他试验不受影响。
2. **校验 + 重建 + 解除待修同一事务生效**：
   - 按入库行号（`samples.rowid`）核对采样时刻严格递增；
   - 核对采样点数、首尾时刻与试验登记一致；
   - 校验通过才删除旧摘要、逐级重建、更新最高层号、删除待修登记，
     四者一起提交；任何一步失败整体回滚，旧摘要原样保留。
3. **失败 / 重启都保留待修**：校验失败（422）、写入异常、服务中途重启，
   待修状态不丢，修复数据后可重试；重建全程不改采样与试验编号。
4. **不混读**：单次查询在同一事务快照内完成，不会混读重建前后的摘要。

### 一键演示（Docker Compose）

```bash
# 叠加恢复演示 compose：先播种"救回试验库"，再启动 API
docker compose -f docker-compose.yml -f docker-compose.recovery.yml up --build
```

播种容器写入两场救回试验（全新数据卷时 id 为 1、2）：

| 试验 | 状态 | 说明 |
| --- | --- | --- |
| `recovered-ok`（id 1） | 采样完整，摘要残缺 | level≥2 整层丢失、level 1 尾部块缺失 |
| `recovered-broken`（id 2） | 采样缺行 + 摘要残缺 | 登记 200 点、实存 195 行，校验必然失败 |

### 演示步骤与预期

```bash
# ① 重建前：recovered-ok 尾部区间因索引缺失显示缺测（假象）
curl "http://localhost:8000/tests/1/query?start_ts=1731300000000&end_ts=1731300015000&bucket_width_ms=1000"
# → 200，尾部桶 missing=true（索引缺失，并非记录仪断采）

# ② 对 recovered-broken 发起重建：校验失败（422），待修保留
curl -X POST http://localhost:8000/tests/2/rebuild-summaries \
  -H "Content-Type: application/json" \
  -d '{"reason":"故障盘救回后摘要缺失","requested_by":"archivist-01"}'
# → 422 {"error":"采样点数不符：库内实际 195 点，试验登记 200 点…","repair_pending":true}

# ③ 待修期间：该试验查询返回 409，其他试验不受影响
curl "http://localhost:8000/tests/2/query?start_ts=1731400000000&end_ts=1731400002000&bucket_width_ms=1000"
# → 409 {"detail":{"message":"试验 2 的峰谷摘要待修…","repair_pending":true}}
curl http://localhost:8000/tests/2/repair
# → 200 {"repair_pending":true,"attempts":1,"last_error":"采样点数不符…"}
curl "http://localhost:8000/tests/1/query?start_ts=1731300000000&end_ts=1731300005000&bucket_width_ms=1000"
# → 200（其他试验照常可查）

# ④ 重启服务：待修状态仍在（已落库），可重试
docker compose -f docker-compose.yml -f docker-compose.recovery.yml restart pump-api
curl http://localhost:8000/tests/2/repair   # → 仍 422 前的待修状态

# ⑤ 对 recovered-ok 发起重建：校验通过，摘要重建成功
curl -X POST http://localhost:8000/tests/1/rebuild-summaries
# → 200 {"max_level":2,"block_counts":{"1":12,"2":1},"repair_pending":false}

# ⑥ 重建后：原编号查询恢复，缺测假象消失；块边界对齐时全程走新摘要
curl "http://localhost:8000/tests/1/query?start_ts=1731300000000&end_ts=1731300015000&bucket_width_ms=1280"
# → 200，全部桶有数据，metrics.raw_rows_read = 0
```

待修试验列表可随时用 `GET /repairs` 查看；`GET /tests/{id}` 与 `GET /tests`
响应中的 `repair_pending` 字段标识待修状态。

## 本地开发（无 Docker）

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest tests/ -q
PUMP_DB_PATH=./data/dev.db python3 -m uvicorn app.main:app --reload --port 8000
```

## 关键环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PUMP_DB_PATH` | `/data/pump_tests.db` | SQLite 文件路径 |
| `PUMP_BLOCK_SIZE` | `128` | 每级摘要块大小（点数 / 子块数） |
| `PUMP_MAX_UPLOAD_BYTES` | `209715200` | 单次上传字节上限（200 MB） |
| `PUMP_MAX_BUCKETS` | `100000` | 单次查询桶数上限，防止桶宽过小 |
