---
description: 复核对象命名，写复核报告
argument-hint: [对象名，可省略]
---

# 复核对象命名

范围：$ARGUMENTS（为空时复核 `ontology/object/` 下的全部对象）。

1. 逐个读对象文件，检查名称是否为业务名词，同一概念有没有两种叫法。
2. 把结果写进 `reports/sample-review-check.md`：每条写对象名、问题、建议名称，没有问题时写「未发现问题」。
3. 告诉用户发现了几条问题。不要改对象文件。
