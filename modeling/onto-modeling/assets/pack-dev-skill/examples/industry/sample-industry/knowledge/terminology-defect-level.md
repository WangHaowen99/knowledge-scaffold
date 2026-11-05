---
name: 缺陷等级分 A、B、C 三级，A 级最严重
type: terminology
slug: defect-level
scopes:
  - "concept:缺陷"
source: sample-industry
confidence: medium
revisions: []
related: []
---
## 背景

资料里的“一级隐患”“严重缺陷”通常指同一类问题，建模时需要统一取值。

## 规则

缺陷等级取值为 A、B、C：A 级影响安全或导致停机，B 级影响产能，C 级只需记录并择机处理。

**Why:** 处理时限按等级计算，取值不统一时无法比较（合成示例）。

**How to apply:** 缺陷对象的等级属性写 `取值：A / B / C`，资料里的其他叫法放进属性别名。
