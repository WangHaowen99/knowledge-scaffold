---
decision_point: rule_metric
title: 示例订单系统的状态码和典型规则
---

## 状态码

| 字段 | 取值 | 含义 |
|---|---|---|
| demo_order_head.order_status | 10 | 待审核 |
| demo_order_head.order_status | 20 | 已审核 |
| demo_order_head.order_status | 30 | 已发货 |
| demo_order_head.order_status | 90 | 已关闭 |

## 典型规则

- 已发货的订单不能直接关闭，要先走退货。
- 订单金额等于各明细金额之和。
- 统计有效订单时排除状态 90。
