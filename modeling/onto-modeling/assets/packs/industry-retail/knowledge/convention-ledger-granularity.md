---
name: 补货台账按门店、商品、补货日期和补货类型记一行
type: convention
slug: ledger-granularity
scopes:
  - "concept:补货台账"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

台账粒度不清时，「某门店某商品上周补了多少」这类问题容易算重或漏算。

## 规则

台账一行对应一个门店、一个商品、一个补货日期、一种补货类型（常规、促销、特殊需求）。同一天的常规补货和特殊需求补货分两行记录。

**Why:** 补货类型分开后，复盘时能区分日常缺口和临时加量，促销效果评估也不会混入常规补货。

**How to apply:** 台账主键取门店编码、商品编码、补货日期、补货类型四列组合。按日汇总时再对前三列聚合。
