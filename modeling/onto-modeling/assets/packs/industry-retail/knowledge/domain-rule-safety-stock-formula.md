---
name: 安全库存按日均销量与到货周期测算
type: domain-rule
slug: safety-stock-formula
scopes:
  - "concept:安全库存"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

安全库存要覆盖从下单到到货这段时间的销售，还要留出应对销量波动的余量。

## 规则

安全库存 = 日均销量 × (到货周期天数 + 缓冲天数)，结果向上取整到件。缓冲天数示例取 2 天，可以按品类调整。日均销量取近 28 天有效销售日的平均值。

**Why:** 只按到货周期备货，销量稍有波动就会断货；缓冲天数就是留给波动的余量。

**How to apply:** 可用库存（现有库存 + 在途数量）低于安全库存时触发补货。目标库存 = 安全库存 + 日均销量 × 订货周期天数，建议补货量 = 目标库存 − 可用库存。
