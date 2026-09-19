# 0005 资料用途标签对齐 modeling

## Context

modeling 的建模流程允许用户为每份资料声明用途（auto / goal / schema / term / evidence）
与读取模式（按需 / 全文精读），其中 `schema` 用途的表格是对象与属性的事实来源。
onto-web 的资料区此前只有目录归类，agent 只能自行猜测每份资料该怎么用。

## Decision

- 标签存储在 `resource/materials.json`（路径 → {purpose, reading_mode}），进 git、
  随导出与迁移包走；平台维护，模型只读。
- 后端：上传接收用途参数；`GET/PATCH /projects/{id}/resource-meta` 读写；删除资料
  连带清理条目；用途变更视同资料更新（触发"资料区有更新"提醒）。
- 提示词资产：工作区 CLAUDE.md 增加用途语义表（全局约束）；ontology-research 按
  goal/full 优先处理；ontology-extract-object 把 schema 表格作为属性事实来源
  （逐 Sheet、列名不虚构、类型标注推断）、term 术语表约束命名。
- 前端：资料区行内打标（用途下拉 + 全文精读勾选）；新建项目页复刻 modeling 的
  wb-material-directives（schema/term 仅表格类可选）。

## Alternatives considered

- 用途写进文件名或目录约定：不可靠、迁移易丢，放弃。
- 存 `.onto-web.json`（项目 meta）：不进导出包，agent 读不到语义，放弃。

## Consequences

- agent 对 schema/term 资料的处理有了确定性约束，对象属性英文名不再凭空生成。
- materials.json 是新的平台维护文件：资料列表隐藏它、删除接口保护它、checker 无需感知。
