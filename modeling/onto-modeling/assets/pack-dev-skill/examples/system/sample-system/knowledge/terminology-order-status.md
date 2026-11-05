---
name: 销售订单状态码 10 待审核、20 已审核、30 已发货、90 已关闭
type: terminology
slug: order-status
scopes:
  - "concept:销售订单"
source: sample-system
confidence: high
revisions: []
related: []
---
## 背景

示例订单系统把订单状态存为数字，存放在 demo_order_head.order_status，界面只显示中文名称。

## 规则

状态码取值：10=待审核、20=已审核、30=已发货、90=已关闭。

**Why:** 业务资料写中文名称，导出数据写数字，需要一张对照才能核对（合成示例）。

**How to apply:** 销售订单的订单状态属性写 `取值：10=待审核 / 20=已审核 / 30=已发货 / 90=已关闭`。
