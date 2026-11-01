---
name: 超出区域配额的补货要经审批才能下发
type: domain-rule
slug: regional-quota-approval
scopes:
  - "concept:区域配额"
  - "concept:审批"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

配送中心按区域和品类分配周度配额。单家门店超量补货会挤占同区域其他门店的份额。

## 规则

按区域 × 品类 × 自然周累计已确认的补货量。本单加入后超过配额的，补货单状态置为「待审批」，不自动下发。超出部分不超过配额的 20% 由区域负责人审批，超过 20% 升级到运营负责人。审批通过后再计入已用配额。

**Why:** 配额是配送中心的发货能力约束，绕过审批直接下发会造成其他门店缺货。

**How to apply:** 超量判断在补货单编制完成后、下发前执行。只把超量的品类行置为待审批，同单其他品类正常下发。
