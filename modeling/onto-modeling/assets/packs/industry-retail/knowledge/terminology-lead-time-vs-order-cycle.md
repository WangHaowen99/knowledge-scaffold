---
name: 到货周期与订货周期是两个概念
type: terminology
slug: lead-time-vs-order-cycle
scopes:
  - "concept:到货周期"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

资料里的「周期」经常不带限定词，容易把到货周期和订货周期当成一回事。

## 规则

到货周期指补货单下达到商品到店可售的天数；订货周期指相邻两次下单的间隔天数，每天下单为 1 天，隔天下单为 2 天。

**Why:** 两者在测算里作用不同：安全库存覆盖到货周期，目标库存在安全库存之上再加一个订货周期的销量。混用会让补货量整体偏高或偏低。

**How to apply:** 字段名只写「周期」的，先问清楚含义再归入两者之一，不要按字面猜。
