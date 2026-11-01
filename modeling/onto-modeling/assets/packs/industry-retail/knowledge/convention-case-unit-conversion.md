---
name: 补货按件测算、按整箱下单
type: convention
slug: case-unit-conversion
scopes:
  - "concept:计量单位"
  - "concept:补货单"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

销量和库存按件记录，配送按箱作为最小单位。件和箱混算是补货数量出错的常见原因。

## 规则

测算环节一律换算成件。下单箱数 = 建议补货件数 ÷ 箱规，向上取整；如果向上取整后会超过陈列量上限，改为向下取整。补货单同时保存件数和箱数。

**Why:** 只存箱数时，下游要按件统计销量覆盖天数就得再查一次箱规，箱规调整后历史数据会算错。

**How to apply:** 箱规是商品属性，表示每箱件数，取正整数。缺箱规的商品不出补货建议，列入待补主数据清单。
