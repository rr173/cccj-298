# 开源基金会滚动配捐后端

用 Python + FastAPI + SQLite 实现的一期活动滚动配捐账本。所有金额均为
**整数分**。系统保证：

- 按捐款受理自增顺序计算当前暂配；
- 先扣除仍然有效的实际拨款（毛拨款 − 实际冲正），再安排暂配；
- 一分剩余实捐最多配一分；
- 总池额度、单捐赠人全期额度均不可透支；
- 财务可只兑现一个已准备好的项目，其他项目继续等待或参加配捐；
- 实际拨款只追加、不改写；退款冲正也是独立追加记录；
- 冲正释放的额度只进入后续“暂配”，不会自动变成对其他项目的实际拨款；
- 捐款、退款、兑现均按 `source_event_id` 做同参幂等重试、改参冲突拒绝；
- SQLite `BEGIN IMMEDIATE` 写事务保证并发下不会出现半笔拨款或额度透支。

## 1. 运行

```bash
cd rolling-match
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 默认使用当前目录 rolling_match.db；可用 ROLLING_MATCH_DB 覆盖
uvicorn app.main:app --reload
```

打开交互式 API 文档：<http://127.0.0.1:8000/docs>。

## 2. 可复跑小账

```bash
python scripts/demo_ledger.py
```

脚本每次都会重建 `demo_ledger.db`，直接调用与 FastAPI 路由相同的
`LedgerService` 事务层，因此只用系统 Python + SQLite 即可复跑；HTTP 层由
`pytest` 中的 TestClient 测试覆盖。完整稳定输出保存在
[`demo_output.txt`](./demo_output.txt)。小账中可以看到关键一幕：

1. 总池 `1000¢`，`don-1 → docs 600¢` 先受理，`don-2 → kernel 600¢`
   后受理；因此暂配是 `docs=600¢`、`kernel=400¢`，kernel 候补 `200¢`。
2. 财务只兑现 kernel，得到第一笔不可变实际拨款 `400¢`。
3. `don-1` 退款 `100¢`。因为该单没有实际拨款，所以**不产生冲正**；
   但它在受理顺序中靠前，暂配从 `600¢` 降到 `500¢`，使 kernel 的新暂配
   从 `0¢` 增加到 `100¢`。
4. 再退 `500¢` 后，kernel 暂配增加到 `200¢`，财务产生第二笔实际拨款
   `200¢`；第一笔 `400¢` 的拨款明细仍原样保留。
5. 新增 `don-3 → translation 500¢`，暂配 `400¢`。
6. `don-2` 退款 `250¢`，剩余实捐 `350¢` 不足以支撑已有净拨款 `600¢`，
   因此对 `don-2` 记实际冲正 `250¢`。translation 的暂配随后从 `400¢`
   增加到 `500¢`，但系统不会自动兑现 translation。
7. 同参重试返回原结果并带 `Idempotency-Replayed: true`；同号改参返回
   `409 idempotency_parameter_mismatch`。

## 3. 测试

```bash
pytest
```

测试覆盖分配顺序、捐赠人额度、部分/多次/超额退款、逐单冲正、项目单独兑现、
幂等重试/改参拒绝，以及 16–20 线程下的同号兑现、并发兑现、并发退款和捐赠人
额度竞争。

## 4. 主要接口

所有业务接口在 `/api/v1` 下。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/campaigns` | 创建一期活动：总池额度、每位捐赠人上限 |
| `POST` | `/campaigns/{cid}/projects` | 录入项目 |
| `PATCH` | `/campaigns/{cid}/projects/{pid}/ready` | 标记项目是否已准备好兑现 |
| `POST` | `/campaigns/{cid}/donations` | 录入模拟捐款事件（整数分、来源编号） |
| `POST` | `/campaigns/{cid}/refunds` | 对原捐款单发起一次可部分、可多次的退款 |
| `GET` | `/campaigns/{cid}/provisional` | **当前暂配**、候补金额、池/捐赠人剩余额度 |
| `POST` | `/campaigns/{cid}/projects/{pid}/settlements` | 按当时暂配兑现该项目 |
| `GET` | `/campaigns/{cid}/projects/{pid}/grants` | **该项目每次实际拨款及原单明细** |
| `GET` | `/campaigns/{cid}/donations/{don_source}/reversals` | **某张原单的实际冲正记录** |
| `GET` | `/campaigns/{cid}/reversals` | 全活动所有原单冲正 |

### 捐款

```bash
curl -sS POST http://127.0.0.1:8000/api/v1/campaigns/oss-2026/donations \
  -H 'Content-Type: application/json' \
  -d '{
    "source_event_id": "don-0001",
    "donor_id": "alice",
    "project_id": "kernel",
    "amount_cents": 600
  }'
```

### 退款

`donation_source_event_id` 指向捐款原单。累计退款不能超过原捐款金额。

```bash
curl -sS POST http://127.0.0.1:8000/api/v1/campaigns/oss-2026/refunds \
  -H 'Content-Type: application/json' \
  -d '{
    "source_event_id": "ref-0001",
    "donation_source_event_id": "don-0001",
    "amount_cents": 100,
    "reason": "partial refund"
  }'
```

响应中的 `actual_reversal` 为 `null` 表示本次退款没有造成实际冲正；否则给出
追加的冲正记录。

### 兑现一个项目

```bash
curl -sS POST \
  http://127.0.0.1:8000/api/v1/campaigns/oss-2026/projects/kernel/settlements \
  -H 'Content-Type: application/json' \
  -d '{"source_event_id":"set-kernel-0001"}'
```

兑现事务内重新计算暂配，只选取该项目下 `provisional_match_cents > 0` 的原单，
写入一个 `settlement` 和每条原单对应的 `grant_items`。其他项目不需要一起兑现。

## 5. 计算规则

对每张原捐款单 `d`：

```text
remaining_donation = original_amount - successful_refunds
gross_granted      = Σ grant_items.amount
reversed           = Σ reversals.amount
net_granted        = gross_granted - reversed
ungranted_balance  = max(0, remaining_donation - net_granted)
```

当前暂配每次实时重算：

1. 总池已用额度 = 所有毛拨款 − 所有冲正；
2. 各捐赠人已用额度 = 该捐赠人所有毛拨款 − 所有冲正；
3. 按捐款单自增受理顺序逐单分配：

```text
provisional(d) = min(
    ungranted_balance(d),
    total_pool_limit - effective_pool_granted - earlier_provisional,
    donor_cap - effective_donor_granted - earlier_donor_provisional
)
```

退款后若：

```text
net_granted_before > remaining_donation_after_refund
```

则只对这张原单追加金额为差额的实际冲正：

```text
reversal_amount = net_granted_before - remaining_donation_after_refund
```

旧的 `grant_items` 行保持不变；财务审计时同时查看毛拨款批次和独立冲正记录。

## 6. 并发与幂等

- 每个写请求使用一个 SQLite 连接并执行 `BEGIN IMMEDIATE`，因此“幂等检查 →
  额度计算 → 插入退款/拨款/冲正 → 约束自检 → 提交”是串行化的原子事务。
- `idempotency_records` 以 `(campaign_id, operation, source_event_id)`
  为主键，并保存所有业务参数的 SHA-256 哈希和首次响应 JSON。
- 同参重试直接返回首次响应体，响应头为
  `Idempotency-Replayed: true`。
- 同号但金额、项目、原单等参数不同，返回
  `409 idempotency_parameter_mismatch`。
- 事务提交前还会执行不变量检查：累计退款不超捐款、净拨款不超剩余实捐、
  总池和捐赠人净拨款不透支。

## 7. 代码地图

```text
app/db.py          SQLite schema、连接、BEGIN IMMEDIATE 写事务
app/service.py     暂配算法、退款冲正、兑现、幂等和不变量
app/models.py      请求模型与金额校验
app/main.py        FastAPI 路由和错误响应
scripts/demo_ledger.py
tests/             领域、HTTP、并发测试
```
