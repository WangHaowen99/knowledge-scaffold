---
name: 门店特殊需求与通用标准冲突时取严
type: domain-rule
slug: special-demand-stricter
scopes:
  - "concept:门店特殊需求"
  - "concept:陈列量标准"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

门店会因场地、客群等原因提出与通用标准不同的要求，例如货架更短、某些商品不再陈列。

## 规则

门店特殊需求和通用标准约束同一个量时取更严的一侧：上限取两者中较小的值，下限取两者中较大的值；「不陈列」类要求直接生效，该商品不出补货建议。

**Why:** 取严不会让门店收到放不下或不该卖的货，出错的代价最小。

**How to apply:** 需要放宽标准的特殊需求（如团购临时加量）不走取严，单独生成补货类型为「特殊需求」的补货行，并进入审批。
