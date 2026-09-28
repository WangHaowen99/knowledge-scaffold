# Mermaid 流程图文字引号约束

## Context

工作台图形视图直接把 `diagrams/*.mmd` 交给 Mermaid 渲染。Agent 生成的节点文字里出现半角 `(` `)` 时，Mermaid 词法器会把它当成形状开头而不是普通文字，整张图在图形视图报 Parse error，只能看源码视图。实测 mermaid 11：`≥`、`？`、`<br/>`、全角括号和中文标点都能正常渲染，真正的触发条件只有“未加引号的文字里带结构字符”。

原有 `validateMermaidSource` 只检查 Markdown 代码围栏和首行声明，不看正文语法，所以这种源码能通过写入闸门和知识包预检，一路写进候选并展示给专家。

## Decision

- 在 `mermaid-source-contract.ts` 增加 `mermaid.label-quoting` 规则：扫描每行的节点形状文字和 `|…|` 连线文字，未加双引号且含 `(` `)` `[` `]` `{` `}` `|` `"` 时判为违规；已加引号但内部还有未转义双引号也判为违规。校验版本升到 `mermaid-source-v2`。
- 规则只做行级扫描，不做完整语法解析：跳过 `%%` 注释行、跳过引号内区间，`>…]` 形状要求前一个字符是 `[A-Za-z0-9_]`（排除连字符），避免把 `-->` 的 `>` 当成形状开头。
- 单文件最多列 10 条 finding，超出部分合并成一条汇总，避免 findings 回灌 Agent 上下文时膨胀。
- 修复建议明确要求“补引号保留业务原文”，禁止把 `≥` 改写成 `>=` 或删改括号内的口径。
- 同一套规则在 `rails.py` 的 `_mermaid_source_findings` 内同步实现，消息文本逐字节一致，保证两侧 findings 指纹相同、自动修复轮次能正确去重。
- 约束同时写进 `package-contract.ts` 的 `packageFormat.diagrams.labelQuoting` 和 `process-modeling` 技能，让生成端先写对，而不是只靠校验兜底。

## Alternatives considered

- 按 issue 建议把 `≥` 替换为 `>=`：实测 `≥` 本身合法，替换会改写业务口径却修不好真正的解析失败。
- 要求所有节点文字一律加引号：`A[开始] --> B[结束]` 这类能正常渲染的写法会被判违规，现有 fixture 和大量存量流程图会集体报错，把一个渲染缺陷变成大面积校验失败。
- 直接复用 `mermaid-import.ts` 的 `importMermaidFlowchart` 做写入前解析：它的 `MERMAID_HTML_FORBIDDEN` 会拒绝 `<br/>`，而 Mermaid 实际支持、存量流程图也在用，接进写入路径会误杀。
- 引入真实 Mermaid 运行时做服务端渲染校验：为一条词法约束引入浏览器依赖，包体和运行成本都不成比例。

## Consequences

- 写入闸门（`MERMAID_CONTENT_INVALID`，可重试）和知识包预检共用这条规则，Agent 在生成时就会被挡回并按建议补引号。
- 存量已损坏的 `.mmd` 重新跑知识包预检会命中该 finding，由既有 `maxAutomaticRepairRounds: 3` 自动修复轮次驱动改写，不需要人工改源码。
- 规则在 TypeScript 和 Python 两侧重复实现，后续修改必须同步改两处并保持消息逐字节一致，否则指纹漂移会让重复 finding 判定失效。
- 行级扫描不是完整语法解析，只覆盖这一类可稳定判定的违规；新增 Mermaid 形状语法时需要同步扩展 `LABEL_SHAPES` 并补回归用例。
