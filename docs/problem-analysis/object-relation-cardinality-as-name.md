# 对象关系名称被生成为关系基数代码问题分析结论

> 代码仓：当前仓库
> 分析基线：`599140572ba2d04fae4d370bf0ea91af1147b757`（分支 `feat/object-relation-type-issue-develop`，基于 `origin/develop`）
> 分析日期：`2026-09-01`
> 问题现象：对象详情 Markdown 的“关系”列预期填写具体关系名称，实际可能出现 `1:1`、`1:N` 等关系基数。
> 分析范围：`modeling` 的对象生成、结构化规格校验、对象领域工具、Markdown 渲染和 Neo4j 图导入；未读取真实知识包、线上日志或模型私有推理过程。

## 1. 问题是否存在

**结论：** 确认存在。

**置信度：** 高。

**适用条件与影响范围：** 在当前基线中，只要对象生成模型或 `upsert_object` 调用把 `SpecRelation.relation` 提交为 `1:1`、`1:N` 等基数文本，系统就会把该文本作为关系名称写入 `objects/<object-key>.md`。所有使用对象关系做语义理解的知识包都可能受到影响；使用 Neo4j 图导入时还会在后续阶段失败。实际发生频率未知，因为本次遵循数据安全要求，没有读取真实知识包或生产记录。

**关键证据：**

| 证据 | 位置或命令 | 观察结果 | 证明了什么 |
|---|---|---|---|
| 类型契约 | `modeling/src/types.ts:350`，`SpecRelation` | `relation` 的注释是“关系名”，示例为 `belongs_to`、`references` | 预期语义是具体关系名称，不是关系基数 |
| 生成工具契约 | `modeling/src/workflow/tools.ts:251`，`specObjectSchema` | `relation` 只是带说明的任意 `Type.String`，没有确定性约束 | 模型即使返回 `1:N`，工具 schema 也会接受 |
| 最小复现 | Node `v22.22.1`；用合成的 `ObjectA`、`ObjectB` 调用 `renderPackage`、`validateSpecIntegrity`、`runFormatChecks` 和 `validateKnowledgeMarkdownContract` | 产物包含 `| 1:N | ObjectB | 合成关系 |  |`；规格校验通过，格式 finding 数为 `0`，Markdown 契约问题为空 | 用户描述的错误产物可稳定生成，并能通过现有生成门禁 |
| 原样传播 | `modeling/src/workflow/package-render.ts:125`，`objectMd` | 第 136～146 行直接把 `r.relation` 写入“关系”列 | 错误值不是前端显示转换造成的，而是规格值被原样持久化 |
| 下游影响复现 | 用带合成表映射的同一规格调用 `extractGraphImportSchema` | 报错 `对象 ObjectA 的关系类型格式非法：1:N` | 该值不仅语义不清，还会使 Neo4j 图导入在更晚阶段失败 |

**反证与未确认点：** `specObjectSchema` 已用文字说明“关系名”并给出正确示例，因此不是完全没有提示；但该提示不是确定性约束，不能推翻复现结果。Neo4j 图导入会拒绝含冒号的名称，但拒绝发生在知识包已经生成之后，也不能防止错误包产生。没有真实模型 Trace，无法确认某次输出 `1:N` 是由哪段材料或模型推理触发；这不影响“当前代码允许并持久化该值”的结论。

## 2. 根因

**根因结论：** `SpecRelation.relation` 的代码契约只声明了“这是字符串”，没有在对象生成入口和最终规格门禁中强制它必须是具体关系谓词，因此关系基数会被误当作关系名称合法传递。

**因果链：**

1. 材料、候选对象或模型上下文中出现对象关联及其基数，模型把 `1:1` 或 `1:N` 填入 `relations[].relation`。
2. `modeling/src/workflow/tools.ts:251` 的工具 schema 和 `modeling/modeling-agent/modeling_agent/tools.py:554` 的 `upsert_object` schema 都只要求该字段是字符串。
3. 批量生成的 `validateObjectBatch`（`modeling/src/workflow/spec-validation.ts:93`）只检查候选认领和对象别名；最终 `validateSpecIntegrity` 在第 331～334 行只检查关系目标是否存在。`upsertObject`（`modeling/src/agent/core-service.ts:1990`）同样只做非空文本和目标引用检查。
4. `renderPackage` 只校验资源 key，并由 `objectMd` 原样渲染 `relation`；格式和 Markdown 契约只检查表格结构，不检查第一列是否为具体关系名。
5. 知识包于是把基数展示为关系类型；若进入图导入，`modeling/src/graph-import/package-schema.ts:171` 才因图关系标识非法而报错。

**责任代码：** 首要责任在 `modeling/src/workflow/spec-validation.ts` 的对象关系语义校验边界；这里同时服务知识生成最终门禁和多个规格修改流程。`modeling/src/agent/core-service.ts` 的 `upsert_object` 是另一条直接结构化写入路径，需要复用同一规则，不能只依赖生成 Prompt。

**促成因素：** `modeling/src/workflow/m2.ts:503` 的对象生成 Prompt 只要求“补充有材料依据的对象关系”，没有明确区分“关系名称”和“关系基数”；Python Agent 的系统提示同样强调对象来源和映射，没有给关系字段负例。`SpecRelation` 也没有独立的 `cardinality` 字段，模型更容易把材料里的基数塞进唯一可用的 `relation` 字段。这些因素提高了发生概率，但不是错误值能够入包的根因。

**已排除原因：** 已排除 Markdown 渲染器主动把具体关系名转换成 `1:N`，因为它只原样读取 `r.relation`；已排除前端展示层改写，因为最小复现直接检查后端 `renderPackage` 产物；已排除目标对象不存在，因为合成目标 `ObjectB` 存在且规格完整性校验通过。

## 3. 最推荐的修复或优化建议

**推荐建议：** 增加一个共享的关系名称校验器，在对象批次生成、最终规格校验和 `upsert_object` 结构化写入边界统一拒绝“只有基数、没有业务谓词”以及“谓词中重复源/目标对象名称”的关系名；同时收紧两套生成工具说明和对象生成 Prompt，让模型在现有自动补交机制中自行更正，并默认生成简洁、具方向性的中文关系谓词。

**为什么能解决：** 该改动在因果链第 3 步阻断错误值。批量生成路径会把问题加入 `validateObjectBatch` 的 `lastIssues`，复用当前最多 3 次的自动补交流程；最终规格校验提供第二道防线；`upsert_object` 直接写入路径在候选落盘前返回可行动错误。Prompt 只降低首轮出错概率，确定性校验保证错误值不能靠模型偶然服从规则才被拦住。

**具体改动：**

| 文件或模块 | 关键符号或接口 | 改动内容 | 行为变化 |
|---|---|---|---|
| `modeling/src/workflow/relation-name.ts`（建议新增） | `relationNameProblem` | 统一做 NFKC、关系基数识别，并用源/目标对象的 key、标题、别名检查关系名是否重复端点名称 | `1:N`、`属于申请`、`对应申请`、`申请项属于` 等均不能入包；不对存量纯谓词英文关系做破坏性拒绝 |
| `modeling/src/workflow/spec-validation.ts` | `validateObjectBatch`、`validateSpecIntegrity` | 对每条 `object.relations` 携带源/目标上下文调用共享校验器，错误信息带对象 key 和字段位置 | 新生成批次会自动补交；对象编辑和最终保存前仍有兜底门禁 |
| `modeling/src/agent/core-service.ts` | `upsertObject` | 先解析源/目标对象身份，再校验 `relations[].relation`，并沿用 `INVALID_DOMAIN_PAYLOAD` 返回 | Agent 直写路径不能绕过连读校验，拒绝后不写候选 |
| `modeling/src/workflow/tools.ts`、`modeling/src/workflow/m2.ts` | `specObjectSchema`、对象生成系统提示 | 明确 `relation` 只填写 `属于`、`包含`、`生成`、`引用`、`依赖`、`适用于` 这类谓词，不包含源/目标名称；提交前默读“源对象 -> 关系名称 -> 目标对象” | 新生成关系默认使用中文纯谓词，降低首轮模型误填概率，自动补交时也有明确修正方向 |
| `modeling/modeling-agent/modeling_agent/tools.py`、`modeling/modeling-agent/modeling_agent/agent.py` | `upsert_object` schema、Leader 提示 | 与 TypeScript 生成路径使用同一字段语义和中文优先规则；仅在材料明确采用英文术语、外部契约要求或保留存量关系时使用英文 | 两套当前可达的建模入口行为一致，同时保持存量兼容 |
| 相关测试 | `tests/knowledge-artifacts.test.ts`、`tests/agent-core-routes.test.ts`，必要时补工具契约测试 | 增加基数负例、具体关系正例、生成补交错误和直写拒绝用例 | 形成“旧实现失败、修复后通过”的回归证据 |

**为什么首选：** 方案保持现有 `SpecRelation` 和知识包 Markdown 格式不变，不需要数据库迁移，也不会把某个基数武断映射成“包含”“属于”等无法从基数推导的业务语义。中文优先和连读自检放在生成契约中，确定性校验只阻断基数值及可客观识别的端点名称重复，因此既能改善新生成内容的可读性，也不会把关系名限制为固定数组或让存量纯谓词英文关系名突然失效。校验放在领域边界并由所有生成入口复用，符合仓库已有“Prompt 引导 + 确定性校验 + 自动补交”的模式，改动可局部回滚。

**未选建议：** 不建议只改 Prompt，因为模型输出不具确定性，当前 schema 和后端仍会接受坏值；不建议在渲染器中把 `1:N` 自动替换成通用“关联”，因为这会隐藏缺失的业务语义；不建议本次新增 `cardinality` 字段，因为用户问题只要求恢复具体关系名，新增字段会扩大知识包合同、导入导出和消费端兼容范围。若后续确实要结构化保留基数，应单独设计该能力。

**验证门槛：**

| 验证 | 场景或输入 | 旧实现预期 | 修复后预期 |
|---|---|---|---|
| 回归测试 | 合成对象关系分别提交 `1:1`、`1:N`、`N:1`、`N:N` | `validateObjectBatch` 和 `validateSpecIntegrity` 通过 | 返回明确问题，生成批次进入自动补交且不能保存错误包 |
| 连读回归 | “申请项 -> 属于申请 -> 申请”“申请项 -> 对应申请 -> 申请” | 关系名作为普通字符串通过 | 拒绝重复目标名称，要求改为“申请项 -> 属于/对应 -> 申请” |
| 结构化写入 | `upsert_object` 提交 `relation: "1:N"`，目标对象存在 | 返回成功并生成对象 Markdown | 返回 `400 INVALID_DOMAIN_PAYLOAD`，候选文件和内部规格均不写入 |
| 中文正常路径 | 提交 `属于`、`包含`、`生成`、`引用`、`依赖`、`适用于` | 通过 | 继续通过且新生成 Markdown 使用原中文关系名 |
| 英文兼容路径 | 打开或更新含 `belongs_to`、`contains` 的存量对象，且没有要求改名 | 通过 | 继续通过，不因中文优先规则破坏存量关系 |
| 兼容验证 | 打开含历史基数关系的存量包，不修改该关系 | 当前可读取 | 仍可读取；新增或修改关系、重新发布时给出可定位修复项，不自动改写历史语义 |
| 图导入 | 修正后的 Neo4j 包执行 schema 提取 | `1:N` 在导入阶段失败 | 具体且合法的关系名通过 schema 提取 |

**实施就绪状态：** 可实施，且已按本建议完成实现。聚焦规格、工具 schema、核心路由和中文图关系测试均已通过；本次按现有合同不新增结构化基数字段，若后续需要让消费端使用基数，应另行设计该能力。

## 4. 可能产生的新影响

| 影响面 | 是否涉及 | 可能的新影响与触发条件 | 风险 | 控制和验证 |
|---|---|---|---|---|
| 对外行为 | 是 | 新生成关系默认展示中文；模型首轮仍提交基数时，生成会自动补交；连续失败时任务进入现有失败/专家处理流程，而不是产出错误包 | 低 | 验证中文正常路径、补交 Trace、错误摘要和最终不落盘行为 |
| 存量兼容 | 是 | 历史包可能已经包含基数关系；若全量硬阻断，可能妨碍无关编辑 | 中 | 读取保持兼容；候选修改沿用仓库的“只阻断新增问题”策略，发布前要求显式修复，不静默迁移 |
| 知识语义 | 是 | 更严格校验会暴露材料只给出基数、没有关系谓词，或模型把对象名称拼进谓词的知识缺口 | 低 | 不猜测业务关系；只拦截可客观判断的基数和端点重复，其余由模型按证据补交，证据不足时交给专家确认 |
| 数据与迁移 | 否 | 不新增字段、不改数据库 schema，也不批量重写历史包 | 无 | 检查实际 diff 中无 migration、数据库和知识包批量改写 |
| 性能与资源 | 是 | 每条关系增加一次短字符串归一化和正则判断，复杂度为 `O(关系数)` | 低 | 用现有对象批次测试确认无可测量的生成耗时回归 |
| 并发与一致性 | 否 | 校验发生在现有候选落盘和 revision 提交之前，不改变租约、事务或版本并发模型 | 无 | 现有候选原子性和 revision 测试继续通过 |
| 安全 | 否 | 不新增外部调用、权限、日志正文或敏感数据处理 | 无 | 测试只使用明确标注的合成对象和关系 |
| 部署与回滚 | 是 | 只涉及应用代码和 Prompt/工具契约；回滚后会重新允许坏值进入 | 低 | 作为同一批改动发布和回滚，避免只回滚校验或只回滚提示造成行为漂移 |

**总体判断：** 最大风险是对含历史坏值的知识包实施全量硬阻断。按“读取兼容、拒绝新增、发布前显式修复”的方式接入共享校验后，风险可接受；实施时最需要核对所有结构化对象写入入口是否都复用了同一规则。

## 5. 预计代码改动量

> 统计口径：实现已完成，以下为限定到本次 TypeScript/Python 生产代码和回归测试的实际 diff；不包含本分析文档、生成文件、锁文件、快照、依赖升级、数据库迁移和无关格式化。

| 类型 | 文件或模块 | 实际改动 | 新增 | 删除 | 核对结果 | 置信度 |
|---|---|---|---:|---:|---|---|
| 生产代码 | `modeling/src/workflow/relation-name.ts` | 新增共享关系名归一化、基数识别、端点重复检查和中文生成说明 | +76 | -0 | 新文件实际行数 | 高 |
| 生产代码 | `modeling/src/workflow/spec-validation.ts` | 接入对象批次和最终规格两道上下文门禁 | +18 | -2 | `git diff --numstat` | 高 |
| 生产代码 | `modeling/src/agent/core-service.ts` | `upsertObject` 入参校验、源/目标解析和错误映射 | +28 | -7 | `git diff --numstat` | 高 |
| 生产代码 | `modeling/src/workflow/tools.ts`、`modeling/src/workflow/m2.ts` | 收紧 schema 说明和对象生成提示 | +4 | -2 | 两文件实际 diff 合计 | 高 |
| 生产代码 | `modeling/modeling-agent/modeling_agent/tools.py`、`modeling/modeling-agent/modeling_agent/agent.py` | 对齐 Python Agent 工具说明与系统提示 | +11 | -1 | 两文件实际 diff 合计 | 高 |
| 测试 | `modeling/tests/knowledge-artifacts.test.ts` | 基数、端点重复、中英文谓词和规格门禁 | +49 | -0 | `git diff --numstat` | 高 |
| 测试 | `modeling/tests/agent-core-routes.test.ts` | 非法关系拒绝且不落候选的集成用例 | +17 | -0 | `git diff --numstat` | 高 |
| 测试 | schema、图关系、MCP 和对象编辑测试 | 两套 schema 非枚举断言、中文图关系及纯谓词夹具 | +32 | -16 | 相关测试文件实际 diff 合计 | 高 |

**原点估算：** 约 `+150/-9` 行
**实际改动：** `+235/-28` 行
**估算偏差：** 新增比原估算多 85 行、删除多 19 行；增量来自后续补充的端点名称连读校验、对象编辑/MCP 夹具和回归覆盖。
**验证结果：** TypeScript 规格与 mapping 组合测试 87 项通过，最新基线核心路由聚焦测试 1 项通过，对象编辑/MCP 测试 20 项通过；Python 工具契约 1 项通过，TypeScript 类型检查通过。
