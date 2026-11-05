---
name: 距上次巡检超过周期 1.5 倍的设备判定为逾期
type: domain-rule
slug: overdue-inspection
scopes:
  - "concept:设备"
  - "concept:巡检记录"
source: sample-industry
confidence: high
revisions: []
related: []
---
## 背景

巡检周期按设备类别设定，现场允许有限顺延。顺延比例由巡检制度统一规定。

## 规则

距上次巡检的时长超过巡检周期的 1.5 倍时判定为逾期，并列入待巡检清单。

**Why:** 各车间单独约定顺延比例时，逾期率无法横向比较（合成示例）。

**How to apply:** 抽取巡检相关的操作、规则和指标时，按这个口径判定逾期；项目资料另有规定时以项目资料为准。
