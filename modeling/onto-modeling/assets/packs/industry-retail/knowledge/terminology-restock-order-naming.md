---
name: 补货单是标准称谓，要货单等叫法按别名处理
type: terminology
slug: restock-order-naming
scopes:
  - "concept:补货单"
source: industry-retail
confidence: high
revisions: []
related: []
---
## 背景

门店、配送中心和采购部门的资料对同一张单据有补货单、要货单、订货单等不同叫法，建模时混用会拆出重复对象。

## 规则

对象和操作统一用「补货单」命名，要货单、订货单、补货申请单写进对象别名。

**Why:** 同一张单据有多个名字时，问答会把同一份数据当成两类对象，统计口径也会对不上。

**How to apply:** 资料里出现这些叫法时，先核对字段和流转环节是否一致，一致就并入补货单。企业向供应商下的采购单不属于补货单，要单独建对象。
