# 助手草稿交接

## 制作时的独立验收设计

先从业务规则和输入资料设计预期，再实现 Parser 与 Handler，不从生成代码反推一组恰好通过的示例。除正常句、边界和缺参外，选择至少一组语义等价变体：在明确业务参数前后加入文件标题中的年份、编号或另一类对象的规格数字，预期业务参数和结果不变；这些上下文必须确实与该业务参数无关。数字应结合字段、单位和语义位置识别，不能把全句所有数字都当作同一字段，也不能先删除所有数字再猜。保留负数、小数、错误单位、多个矛盾业务值的拒绝测试，避免为兼容标题而放过非法参数。

在16项平台向量限额内优先覆盖不同风险，额外表达变体可做补充测试；不得删去失败原句、修改独立预期或把本地测试声称为真实沙箱结果。复核时比较原句及变体的完整参数、issues 和输出，而不只判断 matched。已确认参数的覆盖行为与普通自然语言解析分别验证。

使用 Provider 时先建立对象显示名、英文名、数据映射资源及实际查询类别/属性的对应关系，遵循 deterministic-provider-sdk.md。合成夹具必须依据独立确认的数据绑定构造，不能照抄 Handler 的错误类别让二者一起通过。没有真实连接时，将真实绑定标为待核验；即使沙箱全通过，也不能宣称已完成真实数据接入。

普通用户只选择业务操作、确认业务输入输出，不应编写 Parser、Schema 或测试向量。用户选择候选后，由助手完成下面的草稿。受管目录 `ext/deterministic-execution/contracts/` 仍禁止直接写入。

将候选草稿写到 `ext/deterministic-execution/drafts/<handler>.json`，其中 handler 是已存在的唯一绑定脚本名（不带 .py）。该文件不会自动启用或保存为正式契约；用户在确定性执行面板重新扫描并点击“制作草稿”时读取它。已有草稿或正式契约时，优先提示用户点击“读取助手新版草稿”，核对后确认保存，再运行沙箱验证；不要要求删除正式契约或放弃重建。不要让用户复制代码或编辑 JSON。

JSON 顶层字段：

- `schema_version`: 固定 `deterministic-draft.v1`。
- `sources`: 文件相对路径到 SHA256 十六进制摘要的对象。包含当前 Operation 文档（`ontology/operation/<名称>.md`，旧结构则为 `ontology/logic/<名称>.md`）、Handler 本身及其全部本地 Python 依赖，以及 Parser 引用的本地 Python 依赖。对最终文件原始字节计算摘要。业务文档或代码修改后必须重新生成摘要，平台拒绝过期草稿。
- `draft`: 以下完整契约草稿。

`draft` 字段：

- `key`: 小写字母开头，只含小写字母、数字、下划线，最多64字符，与已有契约不冲突。
- `operation`: 用户选择的业务操作名称。
- `handler`: 唯一绑定脚本名，不带 .py。
- `data_access`: `none` 或 `workspace_provider`，必须与脚本实际能力一致。
- `enabled`: 布尔值。
- `aliases`: 业务别名字符串数组。不要重复 operation 名、key 或其他操作别名；大小写、空白及全半角规范化后也不得重复。没有额外别名可留空数组。
- `routing`: `{ "all_terms": [], "any_terms": ["业务触发词"] }`。覆盖用户真实提问表达，避免过宽关键词误路由规则解释。
- `parser_source`: 完整 Python 源码字符串；接收 `--request-json`，输出单个 `{matched, arguments, issues}` JSON。`matched` 是布尔值，`arguments` 是对象，`issues` 是对象数组，每项至少有非空字符串 `field`，并提供业务可理解的 `message`，例如 `[{"field":"amount","message":"请提供金额"}]`；不能输出字符串数组。`expected_issues` 使用同样结构，且逐项与 Parser 输出一致。已确认的 `known_arguments` 优先；缺失、非法、歧义输入返回明确 issues，不猜测、不用默认金额、不吞掉非法值。业务自然语言解析由助手实现，不能要求用户用英文字段名替代正常表达。
- `input_schema`、`output_schema`: 自包含 JSON Schema 对象或其 JSON 字符串；输入必填规则、数值范围与业务一致。缺失参数应在 Parser 通过 issues 追问，不把缺失输入送到要求完整成功输出的 Handler。
- `test_vectors`: 1到16项业务测试，每项包含 `name`、`text`、`known_arguments`、`expected_matched`、`expected_arguments`、`expected_issues`、`expected_output`、`provider_fixtures`。用用户业务规则独立计算预期，不能用占位值，不能把实际错误输出直接抄成预期。至少覆盖自然语言正常输入、缺失、非法输入及边界。issues 非空或 matched 为 false 时无需运行 Handler，expected_output 可为 `{}`。

测试向量也必须满足平台入口协议：name 唯一且1～128字符；text 为非空白的1～4000字符字符串；known_arguments 和 expected_arguments 始终为对象，缺参用 `{}`，非法业务参数作为对象的字段值测试。空请求文本、数组/null作为整个参数对象属于接口层非法请求，不能混入业务沙箱向量；可另做本地协议防御测试。expected_matched 为布尔值，expected_issues 为上述对象数组。不要使用错误类型的协议输入来代替非法业务值测试。

自然语言验收须保留用户提供的完整原句，并覆盖礼貌前缀、对象名称插入、单位和标点变体、缺参后补齐、冲突数值与规则咨询；不能只测“字段名为值”，不能把失败原句改写为容易通过的句式。无法可靠识别时明确追问，不从负数、小数、单位不同的金额中截取合法数字。每个正向示例还须检查整个知识包的触发条件是否唯一命中目标操作；避免用共享的单字或行业通用词独自触发某项操作。规则解释应走普通解释；同一个用户问题不能同时命中计算和查询。平台预检查会检查已有正式契约与当前草稿在业务示例上的触发冲突，其他尚未保存的候选仍须一并自查。

Parser 应区分不属于此操作的咨询与本操作的非法输入。纯规则咨询且未请求实际计算时返回 `matched=false, arguments={}, issues=[]`；需要执行但缺参、越界或有歧义时返回 `matched=true` 和明确的 `issues`。不要用 `matched=false` 掩盖非法业务值。消费侧只有隐式候选、无已确认参数且空负结果时允许普通 Agent 继续；显式执行和补参中的操作仍然拒绝不匹配请求。

显式已确认参数会覆盖 Parser 参数，且该字段的 Parser issue 被视为已补齐；平台随后继续检查合并参数的 Schema。不要将非法 known_arguments 写成“Parser issues 即视为整个用例成功”的向量。非法值追问用正常自然语言 text 测试；显式参数覆盖用合法 known_arguments 测试。非法显式参数由平台 Schema 拒绝，属于另行的协议防御验证，不得降低业务 Schema 范围以让它成功。

workspace_provider 的 `provider_fixtures` 每项格式为 `{ "operation": "count_search", "arguments": {"element_class": "SyntheticRecord", "element_type": "NODE", "filter_dict": {}}, "result": {"total": 2, "truncated": false, "warnings": []} }`（人工合成示例）。operation、arguments 必须与 Handler 实际 SDK 调用一致，result 按 `.claude/reference/deterministic-provider-sdk.md` 返回协议。覆盖成功、零结果和失败不冒充零；没有执行 Handler 的缺参用例不需夹具。夹具只用于沙箱验证，不是实时业务数据；Consumer 仍需真实工作空间连接。

已有正式契约需要修复时，先读取受管契约及验证报告作为上下文，但只写 drafts 下的新草稿，保留已有 key 和 operation。面板“读取助手新版草稿”只替换当前本地草稿，用户确认保存后才更新正式契约，旧验证不会继承。用户无需删除旧契约。为输出 Schema 的字段提供准确业务 title 或 description（含单位），并让测试名称、自然语言输入、缺参追问对普通用户可理解。

交付前在项目根目录运行 `onto-deterministic-check --operation '所选业务操作名称'`。该只读命令复用页面草稿读取检查，不执行项目代码、不保存契约、不生成验证回执。按返回的完整 findings 修复草稿后再运行，ready 为 true 才提示用户读取。命令不可用时应明确说明预检查未完成，不能以自编的本地检查替代平台预检查。多个候选分别检查。命令会提前检查预期参数与已确认参数合并后是否满足输入 Schema；不允许降低业务约束换取通过。

只写草稿与实现，不伪造 verification。平台进行来源核对、静态检查后展示草稿，用户确认保存，再点击“验证执行闭包”。失败时用户可点击“让助手完善”或“让助手修复读取问题”，失败清单随命令传回；先逐项处理清单，不先改与失败无关的业务。助手修改草稿/脚本后再走预检查、读取、保存、验证。只有真实沙箱通过才可称为确定性执行验证通过，普通本地脚本与平台预检查通过都不等同于此。
