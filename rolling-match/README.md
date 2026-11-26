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

在此基础上还实现了**联合报销**：同一期活动的多个项目用各自已经兑现的配捐，
共同承担一家供应商的一张发票，财务一次性付清整张发票；付款经过一个独立、
可本地运行、可重放的模拟银行通道，不接真实银行。详见第 8 节。

在此基础上还实现了**分期结账**：财务为活动发起结账、在固定截点取得对账草稿，
核对后签认或放弃另取截点；签认账单是下一期的期初依据，且永不改写。详见第 9 节。

在此基础上还实现了**项目交接（并入）**：停止运营的项目把剩余资金和全部未结清
义务一次性、不可撤销地交给同一活动内另一个仍在运营的项目；在途付款不改指令、
由接手方承担占用，旧单据仍可按原项目追溯。详见第 10 节。

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

联合报销的示例另行保存在第 8.7 节。

## 3. 测试

```bash
pytest
```

测试覆盖分配顺序、捐赠人额度、部分/多次/超额退款、逐单冲正、项目单独兑现、
幂等重试/改参拒绝，以及 16–20 线程下的同号兑现、并发兑现、并发退款和捐赠人
额度竞争。联合报销另覆盖份额登记与改参冲突、分别同意/撤回、排队跳过、在途占用、
缺口、失败重排队、响应丢失/重启恢复、重复与矛盾回执，以及并发送付/退款/确认结果
不会只扣一部分项目。分期结账另覆盖首期/续期口径、截点隔离、银行优先、跨期指令
只计一次、草稿/签认/放弃生命周期、不可签认原因定位、并发起草与并发签认、重启后
不重复成期，以及签认账单在后续业务变化中保持原样。项目并入另覆盖余额（含负值与
银行已确认未同步支出）转移、份额与同意失效/接手方重新确认、同发票多份额合并
判断、在途付款跨并入不丢占用且不改指令、失败后重新确认再送付、旧捐款冲正跟随
接手方、并入链、并发并入/送付/退款的清晰先后，以及旧数据库的原地迁移。

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
| `POST` | `/campaigns/{cid}/invoices` | **登记联合报销发票**（收款方、发票号、各项目份额） |
| `GET` | `/campaigns/{cid}/invoices` | 发票列表及当前可付款/排队状态、项目额度 |
| `GET` | `/campaigns/{cid}/invoices/{iid}` | 单张发票详情 |
| `POST` | `/campaigns/{cid}/invoices/{iid}/consents` | 某项目同意/撤回承担份额 |
| `POST` | `/campaigns/{cid}/invoices/{iid}/payments` | 财务送付整张发票（仅可付款时） |
| `GET` | `/campaigns/{cid}/invoices/{iid}/payments` | 该发票的付款指令及确认结果 |
| `GET` | `/campaigns/{cid}/payments` | 全活动付款指令 |
| `GET` | `/campaigns/{cid}/payments/{num}` | 单笔付款指令及银行状态 |
| `POST` | `/campaigns/{cid}/payments/{num}/results` | 送达银行终态结果（成功/失败） |
| `POST` | `/campaigns/{cid}/payments/{num}/sync` | 重启后按指令号查回/补交并同步结果 |
| `GET` | `/campaigns/{cid}/reimbursement-funds` | **各项目已付、在途、暂占、可用与缺口** |
| `POST` | `/campaigns/{cid}/closings` | **发起结账草稿**（固定截点、期号、对账结果） |
| `GET` | `/campaigns/{cid}/closings` | 全部结账（草稿/已签认/已放弃）及最近签认 |
| `GET` | `/campaigns/{cid}/closings/{clid}` | 单份结账账单（含分项依据） |
| `POST` | `/campaigns/{cid}/closings/{clid}/sign` | **签认该份确切草稿**（内容不重算） |
| `POST` | `/campaigns/{cid}/closings/{clid}/discard` | 放弃草稿，可另取截点 |
| `POST` | `/campaigns/{cid}/mergers` | **项目并入**：并出方把账面余额与未结清义务交给接手方 |
| `GET` | `/campaigns/{cid}/mergers` | 全部并入记录 |
| `GET` | `/campaigns/{cid}/mergers/{mid}` | 单条并入记录（含转账分录与转移的发票份额） |
| `GET` | `/campaigns/{cid}/project-responsibility` | 每个项目当前由哪个项目负责及并入链 |
| `POST` | `/simulated-bank/payments` | 模拟通道受理（独立库，按指令号去重） |
| `GET` | `/simulated-bank/payments/{num}` | 查询模拟通道受理/结果 |
| `POST` | `/simulated-bank/payments/{num}/results` | 模拟通道确认成功/失败 |
| `POST` | `/simulated-bank/lose-next-response` | 模拟"受理成功但响应丢失" |

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
app/db.py              SQLite schema、连接、BEGIN IMMEDIATE 写事务与旧库迁移
app/service.py         暂配算法、退款冲正、兑现、幂等和不变量
app/reimbursement.py   联合报销：份额、同意、排队、送付、银行结果、项目额度/缺口
app/bank.py            独立持久化的模拟付款通道（按指令号去重、终态不可覆盖）
app/closing.py         分期结账：固定截点对账草稿、签认/放弃、银行优先与跨期归期
app/merger.py          项目并入：责任链、账面余额内部转账、份额/同意转移与责任时点归属
app/models.py          请求模型与金额校验
app/main.py            FastAPI 路由和错误响应
scripts/demo_ledger.py         原有配捐小账
scripts/demo_reimbursement.py  联合报销可复跑示例
scripts/demo_closing.py        分期结账可复跑示例
scripts/demo_merger.py         项目并入可复跑示例
tests/                 领域、HTTP、并发、模拟通道与迁移测试
```

## 8. 联合报销

### 8.1 资金口径

每个项目的可报销额度只来自**已兑现的实际配捐**：

```text
realized_net_grant = 该项目所有 grant_items − 该项目所有 reversals
available          = realized_net_grant − paid − in_flight − reserved
gap                = max(0, paid + in_flight − realized_net_grant)
```

- 暂配（provisional）不参与报销，任何项目都不能借别的项目的钱；
- 已付（paid）与在途（in_flight）先扣；
- `reserved` 是当前排队中被整张发票暂占的份额；
- 若已付+在途超过净拨款，就显示 `gap_cents`，后续拨款先补缺口，剩余才成为可用额度。

### 8.2 登记发票

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/campaigns/oss-2026/invoices \
  -H 'Content-Type: application/json' \
  -d '{
    "source_event_id": "invoice-0001",
    "payee": "Print Shop Ltd",
    "vendor_invoice_number": "INV-1001",
    "shares": [
      {"project_id": "docs",        "share_cents": 300},
      {"project_id": "kernel",      "share_cents": 200}
    ]
  }'
```

- 发票总额由各项目正整数分份额相加得到，不单独传；
- 同一活动 + 收款方 + 供应商发票号只能有一张：同参重试取回原单
  （`Idempotency-Replayed: true`）；改份额重试返回
  `409 invoice_registration_conflict`，不能借重试改份额；
- 每个项目在发票中最多出现一次。

### 8.3 分别同意 / 撤回

```bash
curl -sS -X POST \
  http://127.0.0.1:8000/api/v1/campaigns/oss-2026/invoices/1/consents \
  -H 'Content-Type: application/json' \
  -d '{"source_event_id":"consent-docs-1","project_id":"docs","consented":true}'
```

- 各分摊项目分别同意；只有**全部同意**的发票才参与排队；
- 发票没有在途（pending）或已成功（succeeded）的付款时可以撤回同意；
  有在途/已成功付款时返回 `409 invoice_consent_locked`。

### 8.4 排队与可付款状态

- 按发票登记顺序处理已全部同意的未付发票；
- 一张发票只有**每个项目都能各自承担自己的份额**时才整张暂占；放不下就整张跳过，
  不影响后面的发票；
- 无法付款的发票在 `not_payable_reason` 中说明原因：
  `consent_pending` / `insufficient_project_funds` / `payment_in_flight` /
  `invoice_already_paid`；资金不足时 `shortfall_cents_by_project` 给出每个项目的缺口；
- 项目新增拨款、收到冲正、同意或撤回后，未送付发票的可付款状态按当前资金与同意情况实时重算。

### 8.5 送付、在途与结果

```bash
# 财务只能送付当时可付款的整张发票
curl -sS -X POST \
  http://127.0.0.1:8000/api/v1/campaigns/oss-2026/invoices/1/payments \
  -H 'Content-Type: application/json' \
  -d '{"source_event_id":"pay-invoice-1"}'
# -> {"instruction_number":"PAY-1-1", ...,"status":"pending"}
```

- 受理送付即从各项目份额中保留该笔付款，直到银行给出明确结果；
- 期间即使发生冲正，也不挪用这笔占用、不改动付款指令；只是项目的
  `gap_cents` 会显示缺口，后续拨款先补缺口；
- 明确成功：写入 `succeeded`，保留每个项目实际承担的份额明细（`allocations`）；
- 明确失败：释放占用，发票凭仍然有效的同意重新排队，由财务决定是否再发一笔；
- **没有明确结果不算失败**，指令保持 `pending`；
- 同一发票同时最多一笔在途、最多一笔成功（数据库部分唯一索引兜底）。

送达银行结果（也可由模拟通道驱动）：

```bash
curl -sS -X POST \
  http://127.0.0.1:8000/api/v1/campaigns/oss-2026/payments/PAY-1-1/results \
  -H 'Content-Type: application/json' \
  -d '{"outcome":"succeeded","result_event_id":"bank-evt-1"}'
```

重复送达同一结果无副作用并带 `Idempotency-Replayed: true`；互相矛盾的终态
返回 `409 payment_result_conflict`，已确认结果不被覆盖；旧指令的回执只作用于旧指令，
不会影响新指令。

### 8.6 模拟付款通道与重启恢复

- 通道是独立 SQLite 库（默认 `<ledger>.bank.db`），不接真实银行；
- 按**付款指令号**去重：同一指令号重复提交只对应一笔付款；改支付要素复用同一号码会被拒绝；
- 支持"受理成功但响应丢失"（`POST /api/v1/simulated-bank/lose-next-response`）；
- 应用重启或送付重试时，`GET /payments/{num}` 能查到同一笔待确认指令，
  `POST /payments/{num}/sync` 会把银行的明确结果同步回账本；若进程恰好在提交银行前崩溃，
  sync 会以**同一个指令号**补交，绝不生成第二笔付款；
- 重试旧的送付请求（同 `source_event_id`）返回原指令，不会变成一次新付款。

### 8.7 可复跑示例

```bash
python scripts/demo_reimbursement.py
```

每次重建 `demo_reimbursement.db` 与独立的 `demo_reimbursement.db.bank.db`，
只用系统 Python + SQLite。完整稳定输出保存在
[`demo_reimbursement_output.txt`](./demo_reimbursement_output.txt)，依次演示：
实际配捐额度、发票登记与幂等/改参冲突、分别同意、按登记顺序整张跳过、
送付在途占用、在途冲正产生缺口、响应丢失与重启恢复、失败释放重排队、二次成功保留分摊明细。

### 8.8 并发保证

所有报销写操作（登记、同意、送付、确认结果）都在记账库的
`BEGIN IMMEDIATE` 事务里串行化；付款指令的状态变更一次作用于整张发票的所有份额，
因此并发退款、同意变更、送付或确认结果时，不会留下"只扣一部分项目"的账。
模拟通道另有独立事务和自己的约束，保证结果重复送达、旧回执、矛盾终态都不会破坏已确认结果。

## 9. 分期结账

财务为活动发起结账，系统在**固定截点**给出对账草稿；核对后可签认这份草稿，
或放弃它另取截点。签认账单可供今后查阅，也是下一期的期初依据。草稿等候签认期间，
活动照常运行（捐款、退款、兑现、送付、银行回执都不受影响）。

### 9.1 账单口径

结账逐项目列出：

```text
opening_book_balance   上一份已签认账单的期末账面余额（首期为 0）
+ period_grants        上次签认截点之后到本次截点的新增实际拨款
- period_reversals     同期的新增实际冲正
- bank_confirmed_reimbursement
                       本期内由银行确认成功、属于该项目份额的报销支出
= closing_book_balance 期末账面余额
```

首期期初为零；此后**只计上次签认截点之后到本次截点的新发生额**，绝不把历史累计额
再算一遍。另列截点尚未明确成功或失败的送付占用，并区分：

- `bank_not_accepted`：业务账已送付，但银行还没有受理记录（正常在途，不算失败）；
- `bank_accepted_pending`：银行已受理，但截点前没有明确成功/失败；

并据此给出 `available_after_in_flight_cents`（扣占用后可用）或
`gap_after_in_flight_cents`（扣占用后的缺口）。**暂配（provisional）和未送付发票的
暂占（reserved）都不进结账单。**

每个金额都附可查的分项依据：`basis.grants` / `basis.reversals` /
`basis.bank_confirmed_reimbursements` / `basis.outstanding_in_flight`。跨期在途指令在
下期计入支出时会带 `prior_in_flight`，指回它在上期哪份已签账单被记为占用；
每个报销分项同时给出原指令号、发票号、收款方与项目份额，以及对应的原捐款单 /
退款单 / 兑现事件编号。

### 9.2 银行优先与跨期归期

结账单以**同一截点的业务账与独立模拟银行记录**对账，并以银行结果为准：

- 银行在截点前已确认成功或失败，即使业务账还没同步，也按银行结果处理；
- 一笔跨期指令**只在银行首次确认成功的那一期**计支出；后来补同步不再计，已签过的
  期间也不会因此重算；
- 银行明确失败只结束占用，不产生支出；
- 同一张发票的不同付款尝试按指令号分开，各自独立归期；
- 业务账已标终态却无对应银行终态、两边终态冲突、或银行记录取不到时，草稿仍然生成，
  但 `signable=false` 并给出 `blockers`，定位到具体指令与原因；这类草稿不能签认；
- 银行可查但尚无该待定指令，视为正常在途，不当作失败；
- 结账是只读的：不补送指令、不同步回执、不改变配捐或报销状态。

### 9.3 草稿、签认与放弃

```bash
# 发起结账，取固定截点（不传 cutoff_at 则取当前时刻）
curl -sS -X POST http://127.0.0.1:8000/api/v1/campaigns/oss-2026/closings \
  -H 'Content-Type: application/json' \
  -d '{"source_event_id":"close-2026-01"}'

# 核对后签认这份确切草稿
curl -sS -X POST \
  http://127.0.0.1:8000/api/v1/campaigns/oss-2026/closings/1/sign \
  -H 'Content-Type: application/json' -d '{"source_event_id":"sign-2026-01"}'

# 或者放弃它，另取截点
curl -sS -X POST \
  http://127.0.0.1:8000/api/v1/campaigns/oss-2026/closings/1/discard \
  -H 'Content-Type: application/json' -d '{"source_event_id":"discard-2026-01"}'
```

- 同一活动只能有一份 `draft`（数据库部分唯一索引兜底），重复发起返回
  `409 closing_draft_exists` 并指回原草稿；
- 发起请求有重试编号：同号同参重试仍指向**原草稿**，不产生新截点，响应带
  `Idempotency-Replayed: true`；同号改参返回 `409 idempotency_parameter_mismatch`；
- 签认针对草稿创建时冻结的确切内容，**不重算余额**：草稿等候期间业务变化不会改变
  将签的账单；草稿内容还有 `content_hash`，签认前会校验未被篡改；
- 放弃后旧草稿不可签认；之后可另取截点（新期号仍从上一份已签账单继续）；
- 新截点必须严格晚于上一份已签账单的截点，避免两期覆盖同一期间；
- 签认重试（同号）返回原签认，不会另立一期；并发签认只会有一次成功，其余返回
  `409 closing_already_signed`；数据库唯一索引保证同一期最多一份签认账单。

### 9.4 可复跑示例

```bash
python scripts/demo_closing.py
```

每次重建 `demo_closing.db` 与独立的 `demo_closing.db.bank.db`。完整稳定输出保存在
[`demo_closing_output.txt`](./demo_closing_output.txt)，依次演示：一笔联合付款在首期
截点仍为在途占用；首期签认后业务继续运行；银行随后确认成功（此时业务账尚未同步）；
第二期把同一指令按银行首次确认计为支出、期初余额指回首份已签账单；第三期不再重复
计入；且**首期已签账单一字不变**。

### 9.5 并发保证

起草、签认、放弃都在记账库的 `BEGIN IMMEDIATE` 事务里串行化，因此账单对应一个完整
截点：与拨款、冲正、送付或银行出结果并发时，不会只带入同一笔业务的一部分。
签认前还会重新读一次独立银行库直到两次快照一致，避免银行结果恰好落在读快照的
窗口里。重启后或并发签认时，数据库唯一索引与幂等记录保证不会出现两份账单覆盖同一
期间，也不会发布半份账单。

## 10. 项目交接（并入）

开源项目停止运营时，财务把它剩余的资金和未结清款项交给同一活动内另一个项目。
一次并入指定两个**不同、且都还未被并出**的项目，立即生效、不可撤销；接手方从
生效起可以动用接管的资金，也承担相关义务。接手方以后仍可以再被并入别的项目，
由此形成一条责任链。

### 10.1 责任与账面口径

并入不改写任何历史记录，而是显式记录责任：

- 捐款、拨款、冲正、发票份额都保留**原项目 id**；拨款与冲正另记事件发生时的
  `responsible_project_id`，发票份额另记当前负责项目；
- `project_mergers` 记录每次并入和生效时刻，`internal_transfer_entries` 记录
  成对的内部转账分录；顺着并入链可回答“现在由谁负责”和“某一时点由谁负责”。

并出方在并入生效那期转出当时的**账面余额**，接手方以同额入账：

```text
账面余额 = 实际拨款 − 实际冲正 + 内部转入 − 内部转出 − 银行已确认成功的报销支出
```

- 已扣**实际支出**（以银行首次确认成功时点、按当时责任项目归属），**不扣在途
  占用**，允许为负；
- 银行早已成功、应用后来才同步的支出，在并入时就按银行确认时点扣除，但并入
  本身**不补送、不同步任何付款**；
- 一对转入/转出分录净额恒为零，活动总账面余额不会因并入被创造或消灭。

### 10.2 并出后不能做什么，旧业务怎么延续

- 并出方不能再接收新捐款、不能被兑现、不能承担新登记发票的份额，也不能再改
  就绪状态；
- 旧捐款仍按原单退款和兑现，配捐规则不变；从并入时起，后续拨款、冲正由当前
  接手方承接（冲正记录同时保留原捐款项目与负责项目）；
- 原捐款、拨款、冲正及发票份额始终可按原项目 id 追溯。

### 10.3 发票份额、同意与排队

- 并入把并出方仍承担的发票份额重新指向接手方，份额原 id 不变；
- 未付发票上被转移份额的**旧确认立即失效**，由接手方重新确认；一张发票的多个
  份额若现在归同一项目，按**合计金额**对照该项目的一笔可用余额判断，不能各用
  一遍；
- 接手方原有发票与接管来的发票仍按既有登记顺序和整单规则排队；
- 已成功发票的历史同意保留为最终授权记录。

### 10.4 在途付款

- 在途付款**不能因并入改指令或重发**：指令号、金额、发票与排队位置都不变，
  接手方承担其全部已接管占用；
- 银行明确成功则结清（含原并出方份额，按银行确认时点的责任项目入账），明确
  失败则释放占用；失败后若要再送付，转移过的份额须由现在的负责方重新确认；
- 这样同一笔钱不会两边同时可花，在途占用不会丢失，并入前的付款也不会被归到
  并入后。

### 10.5 分期结账中的并入

并入被当作一次内部转账：在生效那期，账单逐项目列 `period_transfer_in_cents` /
`period_transfer_out_cents`，并在 `basis.internal_transfers` 与 `mergers` 中注明
转出、转入和对应的并入记录。此后发生的拨款、冲正和实际支出归发生时的负责项目，
在途占用归截点的负责项目；银行早已成功、应用后来才同步的支出仍按银行确认时归属。
已经取好的草稿和签认账单不会因后来的并入而改写。

### 10.6 接口与幂等

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/campaigns/oss-2026/mergers \
  -H 'Content-Type: application/json' \
  -d '{
    "source_event_id": "merge-docs-into-kernel",
    "source_project_id": "docs",
    "successor_project_id": "kernel"
  }'
```

- 同编号同内容重试返回原并入记录（`Idempotency-Replayed: true`），改参返回
  `409 idempotency_parameter_mismatch`；
- 同项目并入自己返回 `422 merger_same_project`；并出方或接手方已被并出分别返回
  `409 merger_source_not_active` / `merger_successor_not_active`；
- `GET .../mergers` 可查全部并入记录，`GET .../project-responsibility` 可查每个
  项目当前由谁负责及完整并入链。

### 10.7 可复跑示例

```bash
python scripts/demo_merger.py
```

每次重建 `demo_merger.db` 与独立的 `demo_merger.db.bank.db`，稳定输出保存在
[`demo_merger_output.txt`](./demo_merger_output.txt)。示例演示：并出方带着一笔
在途付款交接（指令不变、占用由接手方承担）、接手方用合并后的余额继续支付、
被转移份额旧确认失效后由接手方重新确认、在途付款成功后如何在账上结清、旧捐款
退款的冲正如何跟随接手方，以及结账时转入/转出分录如何指回并入记录。

### 10.8 并发与不变量

并入与送付、退款、银行确认、取结账截点一样，都在记账库的 `BEGIN IMMEDIATE`
事务里串行化，因此交错时只对应一种清晰的业务先后。事务提交前的不变量检查保证：
每张发票份额都指向其当前负责项目、每对内部转账净额为零、暂占不超过可用资金。
数据库唯一索引保证一个项目最多被并出一次；幂等记录保证同号并入只生效一次。
