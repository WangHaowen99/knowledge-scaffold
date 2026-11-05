---
decision_point: object_extraction
title: 示例订单系统的表字段字典和表到对象映射
---

## 表到对象映射

| 表名 | 对象 | 说明 |
|---|---|---|
| demo_order_head | 销售订单 | 一单一行 |
| demo_order_line | 订单明细 | 一单多行，通过 order_no 关联抬头 |
| demo_customer | 客户档案 | 一个客户一行 |

## 表字段字典

### demo_order_head

| 字段 | 中文名称 | 类型 | 主键 | 说明 |
|---|---|---|---|---|
| order_no | 订单号 | 字符串 | 是 | |
| customer_code | 客户编码 | 字符串 | 否 | 关联 demo_customer.customer_code |
| order_status | 订单状态 | 枚举 | 否 | 取值见状态码 |
| order_amount | 订单金额 | 小数 | 否 | 单位：元 |
| created_at | 创建时间 | 日期 | 否 | |

### demo_order_line

| 字段 | 中文名称 | 类型 | 主键 | 说明 |
|---|---|---|---|---|
| line_id | 明细编号 | 字符串 | 是 | |
| order_no | 订单号 | 字符串 | 否 | 关联 demo_order_head.order_no |
| item_code | 商品编码 | 字符串 | 否 | |
| quantity | 数量 | 小数 | 否 | |

### demo_customer

| 字段 | 中文名称 | 类型 | 主键 | 说明 |
|---|---|---|---|---|
| customer_code | 客户编码 | 字符串 | 是 | |
| customer_name | 客户名称 | 字符串 | 否 | |

对象属性的英文名称直接用字段名。表名要在项目 schema 里核对后再写进对象的“数据来源”。
