---
name: 已发货的销售订单不能直接关闭，要先走退货
type: domain-rule
slug: shipped-order-close
scopes:
  - "concept:销售订单"
  - "concept:订单明细"
source: sample-system
confidence: medium
revisions: []
related: []
---
## 背景

示例订单系统只允许 10、20 状态的订单直接转到 90。

## 规则

状态为 30 的销售订单要先完成退货，才能转到 90。

**Why:** 发货后库存已经扣减，直接关闭会让库存和订单对不上（合成示例）。

**How to apply:** 抽取“关闭订单”操作时把这个前置条件写进触发条件；项目做过二次开发时以项目资料为准。
