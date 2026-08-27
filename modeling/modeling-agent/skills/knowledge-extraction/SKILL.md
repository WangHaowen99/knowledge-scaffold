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
- `manifest.yaml` 必须以当前正式文件为基线修改，禁止从空白重新编造。始终保留核心注入契约中的全部 `requiredFields`；`kind`、`schema_version`、`id`、`version`、`status` 等 `platformManagedFields` 原样保留，只更新本次业务变化确实涉及的描述或资源声明。版本和状态由平台在接受候选时统一更新。
- 候选预检失败时完整处理返回的每一项 `findings`。按 `ruleId` 和 `path` 定位、按 `message` 与 `suggestion` 修复，并重新发起预检；不得只修第一项、删除无关内容，或要求专家处理可自动修复的格式问题。
- 本轮系统上下文中的 `packageContract.markdown` 由实际 consumer 校验器直接生成，是 frontmatter、固定章节、表头、嵌套引用和索引规则的唯一事实源。写每类详情或索引前读取对应条目，禁止在 Skill 中另存或凭记忆采用一份规则副本。
- 需要稳定数值计算才提出脚本候选。先用问题卡确认输入、输出、边界和测试用例，再生成脚本；调用 `run_script_test` 在 JiuwenBox 逐例执行，并把每例的输入、预期与实际结果展示给专家。没有全部通过时不得请求脚本确认或绑定 operation。
- 每批最多 5 个变更组。operation/object/metric 结构、rule 业务口径、索引分别成组；口径不得与结构或索引混入同一组。
- 写完主动 focus 到最值得专家判断的流程节点或 diff。
