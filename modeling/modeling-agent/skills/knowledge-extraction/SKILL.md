---
name: knowledge-extraction
description: 以已确认流程为主线抽取 operation、object、rule、metric 和脚本候选
---

# 知识抽取

- 一条已确认流程是一批抽取工作的边界。先读流程源码和证据，按有独立输入、动作、输出的业务节点拆 operation，再补它依赖的 object、rule、metric；不要把整条流程重复塞进一个 operation。
- 先复用知识包已有的文件格式、frontmatter、key 和索引约定。operation 详情只能写入 `operations/<key>.md`，object 写入 `objects/<key>.md`，rule 写入 `rules/<key>.md`，metric 写入 `metrics/<key>.md`；不得创建 `knowledge/` 通用目录。详情文件保持单一职责，引用使用稳定 key；先写详情文件，全部成功后再更新各目录的 `index.md` 和根 `manifest.yaml`，避免索引指向不存在的内容。
- 每个 operation 明确输入对象、动作、输出对象、适用装置或工况，并回指对应 `diagrams/*.mmd` 节点。只有名称相同但业务动作或工况不同的节点不得强行合并。
- object 只声明多处 operation 共同使用的业务实体和资料明确支持的字段；不要把流程步骤、整张 Excel 表或数据库内部 ID 当成对象字段。
- rule 只承载会影响业务判断或计算的口径。A1、A2 等流程图里标成待确认的假设不得写成生效规则，只能作为开放问题或明确的未决说明。
- metric 的定义、单位、统计口径和适用工况必须来自同一可核对来源；百分比、温度、压力、能力和质量指标等精确值必须引用实际包含原值的表格或段落。
- 原值和限制值必须保留资料表头的单位；同一指标出现 `%`、`mg/kg` 等不同量纲时不得自行换算或并列为同一口径。设计值、操作值、正常范围和报警上限必须明确区分；出现“操作值高于正常上限”等内部矛盾时先回读原文，无法解释就提问，不写成生效知识。
- evidence 使用 `material:<document id>#<锚点>` 或已确认 `user-decision:`，document id 从 `list_documents` 原样复用；`write_file.evidence` 与正文引用一致。v1 不强制非空不等于允许编造来源，也不能用“主要产品”章节证明具体数值。
- 提交前逐个读取本批文件，核对 operation 的输入输出、引用 key、精确值证据和未决标识；只确认资料支持的内容，宁可减少条目，不以数量补齐目录。
- 需要稳定数值计算才提出脚本候选。先用问题卡确认输入、输出、边界和测试用例，再生成脚本；调用 `run_script_test` 在 JiuwenBox 逐例执行，并把每例的输入、预期与实际结果展示给专家。没有全部通过时不得请求脚本确认或绑定 operation。
- 每批最多 5 个变更组。operation/object/metric 结构、rule 业务口径、索引分别成组；口径不得与结构或索引混入同一组。
- 写完主动 focus 到最值得专家判断的流程节点或 diff。

## Markdown 消费契约

不得自创 `name`、`type` frontmatter 或“输入对象/输出对象/动作”等近义章节；consumer 只解析以下固定契约，缺字段会静默丢失知识：

- `operations/<key>.md` frontmatter 必含 `resource: operation`、`key`、`title`、`description`，引用放在嵌套对象 `uses`，证据放 `evidence`。必须写成 `uses:` 换行后再缩进写 `objects:`、`rules:`、`metrics:`；禁止写成扁平键 `uses.objects:`。正文固定为 `# 业务目标`、`# 输入`、`# 输出`、`# 流程`、`# 规则、指标和注意事项`、`# 知识证据`；输入表头严格为 `参数 key | 中文名 | 类型 | 必填 | 说明`，输出表头为 `输出 key | 中文名 | 类型 | 说明`，流程使用编号列表。正文里的 `相关规则：rule:<key>`、`相关指标：metric:<key>` 必须与 `uses.rules/metrics` 一致。
- `objects/<key>.md` frontmatter 必含 `resource: object`、`key`、`title`、`description`，可带 `aliases`、`sources`；`sources` 只接受 `文档：<文件名> / <章节>` 或 `表格：<表名>`，不接受 material token。object 不支持 `evidence` frontmatter，也没有 `# 知识证据` 章节。正文固定为 `# 业务定义`、`# 字段`、可选 `# 关系`、`# 查询边界`；字段表头严格为 `字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名`。如果写 `# 关系`，必须使用表头 `关系 | 目标对象 | 说明 | 证据`；没有结构化关系就省略整个章节。
- `rules/<key>.md` frontmatter 必含 `resource: rule`、`key`、`title`，可带 `description`、嵌套对象 `applies_to` 和 `evidence`；必须写成 `applies_to:` 换行后缩进写 `objects:`，禁止写成 `applies_to.objects:`。正文固定为 `# 规则内容`、可选 `# 判断条件`（表头 `条件 | 结论`）、可选 `# 例外`、`# 知识证据`。
- `metrics/<key>.md` frontmatter 必含 `resource: metric`、`key`、`title`，可带 `description`、`unit`、`grain`、嵌套对象 `uses` 和 `evidence`；必须写成 `uses:` 换行后缩进写 `objects:`，禁止写成 `uses.objects:`。正文固定为 `# 业务口径`、`# 计算边界`、可选 `# 空值策略`、可选 `# 维度`、`# 知识证据`；计算边界使用项目列表，空值策略表头严格为 `字段 | 处理`，维度表头严格为 `维度 key | 说明`。没有可填写表格时省略对应可选章节。
- 四类索引分别使用 `resource: operation_index/object_index/rule_index/metric_index`，`key` 分别为 `operations/objects/rules/metrics`，并保留现有索引表头。存在详情文件就不得删除对应索引。
- frontmatter 的 `key` 必须与文件名一致，详情里的引用 key 必须能在同批详情或已确认文件中找到。提交确认前用 `read_file` 逐个核对上述固定字段和章节。

嵌套 YAML 最小示例：

```yaml
uses:
  objects:
    - hydrocracking_feedstock
  rules:
    - hydrocracking_feedstock_quality
  metrics:
    - hydrocracking_yields
```
