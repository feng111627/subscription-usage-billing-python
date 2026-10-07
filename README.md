# Subscription Usage Billing

订阅用量计费服务：用户订阅计划后周期性上报用量，系统按周期归集、出账，并支持账单的支付、作废与退款。基于 FastAPI，内存存储，全程保证幂等与并发一致。

## 计费模型

### 计划（Plan）与计费周期

- 每个计划定义 `period_days`（周期长度）与计价方式。
- 订阅时以 `anchor`（订阅起始时刻）为锚点，周期边界由锚点推导：`[anchor + k·period, anchor + (k+1)·period)`。
- 周期为**左闭右开**：恰好落在边界上的用量事件归属于以该边界为起点的新周期，归属规则确定且稳定。
- 早于锚点的用量被拒绝（`USAGE_OUT_OF_PERIOD`）；已出账关闭的周期不再接受新用量（`USAGE_OUT_OF_PERIOD`），重放该周期内已存在的事件则返回原记录。

### 计价方式

| 模式 | 规则 |
| --- | --- |
| `TIERED` | 阶梯计价：每单位按其所在档位单价计费，档位严格递增，末档必须无界（`up_to=null`） |
| `OVERAGE` | 超额计价：`included_units` 以内免费，超出部分按 `overage_unit_price_cents` 统一单价 |

非法参数（周期非正、阶梯缺失/非递增/末档有界、负单价等）在建计划时以 `VALIDATION_FAILED` 拒绝，不会进入计费。

## 幂等规则

- **用量上报**：以 `event_id` 去重。相同 `event_id` + 相同载荷的重放返回原记录、不重复计费；相同 `event_id` + 不同载荷返回 `IDEMPOTENCY_CONFLICT`（409）。
- **出账**：`close_period` 携带 `idempotency_key`。相同键重放返回同一张账单；相同键配不同请求返回 `IDEMPOTENCY_CONFLICT`。此外按 `(订阅, 周期)` 唯一约束保证**每个周期至多一张账单**，与使用哪个键无关。
- **并发**：所有变更在单把可重入锁内完成。并发上报不丢不重；并发出账后每个周期恰好一张账单，金额与归集用量一致。

## 账单状态机

```
ISSUED ──pay──────▶ PAID ──refund──▶ REFUNDED
   │                                   (终态)
   └──void──────▶ VOID (终态)
```

- 出账即 `ISSUED`；只允许上表中的转移，其余一律 `ILLEGAL_STATE_TRANSITION`（409）。
- `void` / `refund` 需要 `actor="admin"`，否则 `FORBIDDEN`（403）；越权检查优先于状态检查。
- 已支付账单不能作废（须走退款）；作废/退款不改变账单金额，出账、作废、退款前后账目一致。
- 每次转移递增 `version`。

## 错误原因（可区分）

| reason | HTTP | 含义 |
| --- | --- | --- |
| `PLAN_NOT_FOUND` / `SUBSCRIPTION_NOT_FOUND` / `INVOICE_NOT_FOUND` | 404 | 资源不存在 |
| `VALIDATION_FAILED` | 422 | 参数缺失或非法 |
| `USAGE_OUT_OF_PERIOD` | 422 | 用量落在锚点之前或已关闭周期 |
| `IDEMPOTENCY_CONFLICT` | 409 | 同一幂等键/事件 ID 携带不同载荷 |
| `ILLEGAL_STATE_TRANSITION` | 409 | 非法账单状态转移 |
| `FORBIDDEN` | 403 | 越权（非 admin 作废/退款） |

## 本地验证

```bash
# 安装依赖并运行全部测试（幂等 / 并发 / 周期边界 / 状态流转 / API）
uv run pytest -v

# 测试日志会标注每条用例覆盖的业务域与结果，例如：
#   [COVERAGE] 幂等与重放 :: tests/test_idempotency.py::...::test_replay_same_event_does_not_double_count
#   [RESULT]   PASSED 幂等与重放 :: ...

# 启动服务
uv run python main.py   # 监听 127.0.0.1，随机端口
```

### API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plans` | 创建计划（校验计价参数） |
| POST | `/subscriptions` | 订阅计划，锚定计费周期 |
| POST | `/usage` | 上报用量（`event_id` 幂等） |
| POST | `/periods/close` | 关闭当前周期并出账（`idempotency_key` 幂等） |
| POST | `/invoices/{id}/pay` | 支付 |
| POST | `/invoices/{id}/void` | 作废（admin） |
| POST | `/invoices/{id}/refund` | 退款（admin） |
| GET | `/invoices/{id}` | 查询账单 |

## 代码结构

```
billing/
  errors.py    # 错误原因枚举与 BillingError
  models.py    # 计划/订阅/用量/账单模型与状态枚举
  pricing.py   # 周期归属与阶梯/超额计价
  store.py     # 线程安全内存存储与幂等记录
  service.py   # 业务逻辑：上报、出账、状态机
main.py        # FastAPI 路由与错误映射
tests/         # 按业务域划分的测试（见 BUSINESS_AREAS）
```
