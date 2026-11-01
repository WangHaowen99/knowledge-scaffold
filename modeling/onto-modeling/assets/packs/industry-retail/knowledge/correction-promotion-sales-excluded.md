---
name: 日常安全库存的销量窗口要剔除促销日
type: correction
slug: promotion-sales-excluded
scopes:
  - "concept:销量"
  - "concept:促销"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

曾把促销期的高销量计入日常日均销量。促销结束后安全库存虚高，门店出现积压。

## 规则

计算日常日均销量时，统计窗口剔除促销日，销量和销售天数都要剔除。促销期备货单独按促销补货系数测算。

**Why:** 促销销量由价格和活动拉动，不代表日常需求水平。

**How to apply:** 促销日的判定以促销档期为准，档期覆盖的日子都算促销日，不论当天该商品是否参加活动；单品级促销明细齐全时可以只剔除参加活动的商品。
