# 订阅用量计费（subscription-usage-billing）

一套“用户订阅计划 → 周期性上报用量 → 按周期归集 → 出账 → 支付/作废/退款”
的计费服务。重点保证**重放安全、并发一致、周期归属确定、状态流转合法**，
任何参数缺失/非法、越权或非法流转都会以**可区分的错误码**拒绝，不算错账。

- 技术栈：Python 3.14 + FastAPI（内存仓储 + 锁，零外部依赖即可运行/测试）
- 金额：全程 `Decimal`，落账按 `0.01`、`ROUND_HALF_UP` 取整
- 时间：统一 UTC、时区感知；朴素时间按 UTC 处理

## 1. 计费模型

### 1.1 计划（Plan）

| 字段 | 含义 |
| --- | --- |
| `base_fee` | 每个计费周期固定收取的基础费 |
| `pricing_mode` | `overage`（超额）或 `tiered`（阶梯） |
| `included_units` | overage 模式下包含的用量，超出才计费 |
| `overage_price` | overage 模式下超出部分的单价 |
| `tiers` | tiered 模式的阶梯 `[{up_to, unit_price}]`，末档 `up_to=null` |

计价公式（用量部分，基础费另计）：

- **超额 overage**：`max(0, quantity - included_units) * overage_price`
- **阶梯 tiered**：累计用量在各阶梯切片，每片乘该档单价后求和。
  阶梯上界 `up_to` 必须严格递增，且只有最后一档允许为 `null`（无上限）。

账单：`total_amount = quantize(base_fee) + quantize(usage_amount)`，
出账时统一按两位小数四舍五入。

### 1.2 订阅与计费周期

- 订阅有 `anchor_day`（锚定日，仅允许 1..28，避免短月漂移）。
- 计费周期为**自然月周期**，区间 **左闭右开 `[start, end)`**。
- **周期键**就是周期起始日 `YYYY-MM-DD`（URL 安全、无歧义）。
  例如锚定日 15：`2026-09-15` 表示 `[2026-09-15 00:00, 2026-10-15 00:00)`。
- **归属规则（确定且稳定）**：用量只按 `occurred_at` 落入的周期归属，
  与上报时间、是否延迟上报无关。
  - 周期起点当天 `00:00:00` 起归入新周期（含起点）；
  - 周期终点当天 `00:00:00` 起归入下一周期（不含终点）；
  - 带时区的时间先换算成 UTC 再归属（如北京时间 10-01 07:00 == UTC 09-30 23:00，归 9 月）。
- 用量必须在订阅有效期内：早于 `started_at` 或晚于 `canceled_at` 一律拒绝。

### 1.3 周期锁定

- 只有**已结束**的周期才允许出账；周期未结束 → `PERIOD_NOT_ENDED`。
- 出账成功后该周期的汇总被**锁定（closed）**，此后补报用量一律
  `PERIOD_CLOSED`（409），防止账单金额被事后篡改。
- 账单作废（void）后允许对该周期**重新出账**（新版本账单，金额按事件重算）；
  已退款（refunded）为终态，不允许重新出账。

## 2. 幂等规则

两层幂等，保证“同一事件 / 同一幂等键重复提交绝不重复计费”：

1. **业务事件 ID 去重**（`POST /usage` 的 `event_id`，全局唯一）
   - 相同 `event_id` + 相同内容：返回首次结果，响应 `replayed=true`，
     不再次累加用量；
   - 相同 `event_id` 但订阅/数量/时间不同：`DUPLICATE_EVENT`（409）拒绝；
   - 去重与归集在**同一把锁的临界区**内完成，并发下不丢计、不重复计。
2. **HTTP 幂等键**（请求头 `Idempotency-Key`，按用户隔离）
   - 同键 + 同请求指纹（方法+路径+载荷）：直接返回首次响应，不重复执行业务；
   - 同键但请求内容不同：`IDEMPOTENCY_CONFLICT`（409），防止“同键不同单”；
   - 出账、支付、作废、退款均支持幂等键；
   - 出账本身也按 `(subscription_id, period_key)` 幂等：重复出账返回同一张
     账单（同 ID、同金额），并发出账也只会生成一张。

对状态操作的重复提交：对**同一目标状态**的重复动作视为重放（幂等成功、
不推进版本、不重复扣款/退款）；对**跨状态**的非法动作则拒绝
（见下节状态机）。

## 3. 账单状态流转

```
                 pay                 refund
   ISSUED ───────────────► PAID ───────────────► REFUNDED（终态）
     │
     │ void
     ▼
   VOID（终态；该周期可重新出账，生成新版本账单）
```

| 当前状态 | pay | void | refund |
| --- | --- | --- | --- |
| ISSUED | ✅ → PAID | ✅ → VOID | ❌ INVALID_BILL_TRANSITION |
| PAID | 重放返回（幂等） | ❌ | ✅ → REFUNDED |
| VOID | ❌ | 重放返回（幂等） | ❌ |
| REFUNDED | ❌ | ❌ | 重放返回（幂等） |

- 非法流转统一返回 `INVALID_BILL_TRANSITION`（409），错误信息会说明
  当前状态与允许的来源状态。
- 每次成功流转 `version += 1` 并追加 `history`（动作、UTC 时间、操作人），
  重放不推进版本；可据此审计。
- **账目核对**：账单的 `quantity/base_fee/usage_amount/total_amount` 来自
  周期汇总与计划，作废→重出金额一致；出账后周期锁定，前后对得上。

### 权限模型（请求头）

| 头 | 说明 |
| --- | --- |
| `X-Actor-Id` | 必填，缺失 → `UNAUTHENTICATED`（401） |
| `X-Actor-Role` | `admin`（默认 `customer`） |

- 建计划、出账、支付、作废、退款：仅 `admin`，否则 `FORBIDDEN`（403）。
- 客户只能为自己创建订阅、上报自己的用量、查看自己的汇总/账单；
  访问他人资源 → `FORBIDDEN`。

### 统一错误结构与错误码

所有业务/校验错误均返回：

```json
{ "error": { "code": "INVALID_PARAMETER", "message": "...", "details": [] } }
```

主要错误码（原因可区分）：

| code | HTTP | 场景 |
| --- | --- | --- |
| UNAUTHENTICATED | 401 | 缺少 X-Actor-Id |
| FORBIDDEN | 403 | 越权操作 |
| MISSING_PARAMETER | 400 | 必填参数缺失/为空 |
| VALIDATION_ERROR | 422 | 请求体结构校验失败（Pydantic） |
| INVALID_PARAMETER | 422 | 数量≤0/非数值、时间非法、锚定日非法、计划配置非法等 |
| PLAN_NOT_FOUND / PLAN_ALREADY_EXISTS | 404 / 409 | 计划不存在 / 已存在 |
| SUBSCRIPTION_NOT_FOUND / _ALREADY_EXISTS | 404 / 409 | 订阅不存在 / 已存在 |
| DUPLICATE_EVENT | 409 | 同 event_id 但内容不同 |
| IDEMPOTENCY_CONFLICT | 409 | 同幂等键不同请求 |
| PERIOD_NOT_ENDED | 422 | 周期未结束就出账 |
| PERIOD_CLOSED | 409 | 已出账锁定后补报用量 |
| BILL_NOT_FOUND | 404 | 账单不存在 |
| INVALID_BILL_TRANSITION | 409 | 非法状态转移 |

## 4. HTTP 接口一览

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/plans` | admin | 创建计划 |
| GET | `/plans` | 登录即可 | 计划列表 |
| POST | `/subscriptions` | 客户本人/admin | 创建订阅 |
| GET | `/subscriptions` | 登录即可 | 订阅列表（客户只看自己） |
| POST | `/subscriptions/{id}/cancel` | 本人/admin | 取消订阅（幂等） |
| POST | `/usage` | 本人/admin | 上报用量（event_id + 可选 Idempotency-Key） |
| GET | `/subscriptions/{id}/aggregates/{period_key}` | 本人/admin | 查询周期汇总 |
| POST | `/subscriptions/{id}/bills/{period_key}/issue` | admin | 周期出账（幂等） |
| POST | `/bills/{id}/pay` | admin | 确认支付 |
| POST | `/bills/{id}/void` | admin | 作废账单 |
| POST | `/bills/{id}/refund` | admin | 退款 |
| GET | `/bills/{id}` | 本人/admin | 查询账单 |

> `period_key` 为周期起始日，如 `2026-09-01`、`2026-09-15`。

## 5. 本地验证

需 Python 3.14 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 1) 安装依赖（会创建 .venv）
uv sync

# 2) 运行全部测试（78 例：周期边界 / 计价 / 幂等 / 并发 / 状态流转 / HTTP）
uv run pytest

# 仅跑重点类别
uv run pytest tests/test_concurrency.py    # 并发：不丢计、不重复、出账唯一
uv run pytest tests/test_idempotency.py   # 幂等：事件ID + 幂等键
uv run pytest tests/test_periods.py       # 周期边界与跨周期归属

# 3) 启动服务
uv run python main.py
# 或：uv run uvicorn billing.api:app --reload
```

### 业务覆盖日志

测试通过 `@pytest.mark.business("业务/子场景")` 标注所属业务。每次运行会在
终端打印并在项目根目录写入 `test-run.log`（已被 gitignore），可直观看出
**覆盖了哪些业务、各有哪些用例、成功/失败结果与耗时**，形如：

```
■ 并发安全/并发出账-只生成一张账单且金额确定（1 例）
    tests/test_concurrency.py::test_concurrent_issue_single_bill  ✓
...
合计 78 例：成功 78，失败 0，跳过 0，耗时 0.38s
```

### 手动快速验证（curl 思路）

```bash
BASE=http://127.0.0.1:8000
ADMIN='X-Actor-Id: admin-1'  ROLE='X-Actor-Role: admin'
USER='X-Actor-Id: user-1'

curl -s -XPOST $BASE/plans -H "$ADMIN" -H "$ROLE" -H 'Content-Type: application/json' \
  -d '{"id":"P1","name":"std","pricing_mode":"overage","base_fee":"10.00",
       "included_units":100,"overage_price":"0.50"}'

curl -s -XPOST $BASE/subscriptions -H "$USER" -H 'Content-Type: application/json' \
  -d '{"id":"S1","plan_id":"P1","customer_id":"user-1","anchor_day":1,
       "started_at":"2026-09-01T00:00:00Z"}'

curl -s -XPOST $BASE/usage -H "$USER" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: u1' \
  -d '{"event_id":"e1","subscription_id":"S1","quantity":"120",
       "occurred_at":"2026-09-10T00:00:00Z"}'

curl -s -XPOST $BASE/subscriptions/S1/bills/2026-09-01/issue -H "$ADMIN" -H "$ROLE"
# total_amount = 10 + (120-100)*0.5 = 20.00
```

## 6. 代码结构

```
billing/
  models.py       领域模型（计划/订阅/事件/汇总/账单、枚举）
  periods.py      计费周期、锚定日、周期键反解、用量归属（左闭右开）
  pricing.py      overage / tiered 计价
  errors.py       错误码与 BillingError
  repository.py   线程安全内存仓储（RLock；事件去重、汇总、账单索引）
  idempotency.py  HTTP 幂等键存储与请求指纹
  service.py      应用服务（校验/鉴权/原子归集/出账/状态机/幂等编排）
  api.py          FastAPI 路由、异常处理、统一错误结构
tests/            78 个用例，按业务分文件 + 业务覆盖日志（conftest.py）
main.py           ASGI 入口
```

> 说明：当前为内存实现，重启数据清空，定位为可独立运行与并发验证的参考实现；
> 仓储层接口（`billing/repository.py`）可平移到数据库，用唯一约束
> （event_id、(subscription_id, period_key)）与事务替代内存锁，语义不变。
