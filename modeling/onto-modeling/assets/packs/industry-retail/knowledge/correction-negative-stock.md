---
name: 负库存按 0 参与测算，并列入待盘点清单
type: correction
slug: negative-stock
scopes:
  - "concept:库存"
  - "concept:盘点"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

先销售后入库、扫码串号等原因会让系统库存出现负数。常见的错误做法是让负数直接参与计算。

## 规则

测算时现有库存小于 0 的一律按 0 计，不能用负数抵减在途数量或放大补货量；同时把该门店商品列入待盘点清单。

**Why:** 负数直接参与计算会让建议补货量偏大，实物到货后超过陈列上限，盘点差异也被掩盖。

**How to apply:** 连续 3 天为负的门店商品列为重点盘点对象。盘点完成前，负库存修正只影响测算，不回写系统库存。
