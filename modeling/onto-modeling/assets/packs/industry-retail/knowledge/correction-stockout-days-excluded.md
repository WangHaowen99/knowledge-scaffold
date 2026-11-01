---
name: 日均销量的分母只计有效销售日，缺货日要剔除
type: correction
slug: stockout-days-excluded
scopes:
  - "concept:销量"
  - "concept:安全库存"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

曾按近 28 天总销量除以 28 计算日均销量。缺货期间销量为 0，会把日均销量拉低，形成越缺货越少补的循环。

## 规则

日均销量 = 统计窗口内销量 ÷ 有效销售日数。有效销售日指门店当天营业且日初可用库存大于 0 的日子。有效销售日少于 7 天时视为样本不足，不出自动补货建议，转人工确认。

**Why:** 缺货日的 0 销量不是需求为 0，而是没有货可卖。

**How to apply:** 资料里如果只有日销量、没有日初库存，就无法判定缺货日，要在研究报告里列为待补充数据，不要退回按自然日计算。
