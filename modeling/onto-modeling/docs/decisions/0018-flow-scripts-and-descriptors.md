# 0018 脚本按完整流程生成，描述符随脚本落工作区并按 consumer 调用口径生成

## Context

consumer 只按 operation 调用脚本（`consumer/backend/agent/tools/execute_script.py`）：选中哪个
operation，就执行绑定在它上面的那一个脚本，并把结果当作这个 operation 的完整结论；同一 operation
绑了多个脚本时只用文件名排第一的那个。

此前 `/ontology-codegen` 按 operation 一对一生成脚本，还支持「步骤区间」片段脚本，造成三类问题：

- **主操作与子操作各有脚本**：consumer 的智能体选中子操作时只跑了半个流程，主流程的输入检查、
  前后步骤与停止条件都被跳过，结论是错的。导出的 `operations/index.md` 标题、输入、输出三列都填
  operation 名，consumer 提示词里「比对输出列、优先选主流程」的规则无从发挥，模型只能按名字挑。
- **片段脚本**：片段绑定到整条 operation，consumer 把它当成 operation 的完整实现，其余步骤不再执行。
- **描述符只在导出时临时生成**：工作区里看不到 consumer 实际拿到的调用契约；参数类型也没按 consumer
  的读法写——`type=int` 标成 `number`（consumer 传 `5.0`，argparse 解析失败，参数省略时默认值同样
  被转成 `10.0`）、`json.loads` 参数标成 `json`（consumer 当字符串）、`nargs="+"`（consumer 重复
  flag 传多值，argparse 只留最后一组）、默认值写常量名或 Python 列表（consumer 原样当值传入）；
  参数段是二级标题，modeling 工作台只认一级标题，读出来参数为空；`「关联operation：无」` 的库模块
  也生成了没有绑定的描述符，工作台评测前校验因此拒绝整包。

## Decision

1. **按完整流程生成脚本，主/子关系只看「# 与operation的关联」。** 新增一行
   `- 可单独调用 > 是 / 否`（只对子操作有意义，缺省为否）。主操作能脚本化时只生成主脚本：
   「可单独调用」为否的子操作写成主脚本里的函数，结论按子操作名放进输出的 `steps`；为是的子操作另有
   自己的脚本（单独调用时结论也要对），主脚本 import 它。主操作不能脚本化时子操作各自生成脚本。一个
   operation 只对应一个脚本，不再有片段脚本；只有部分步骤能脚本化时把这几步拆成子操作。
   `operation_relations.py` 解析调用关系，`script_descriptor.resolve_script_bindings` 按整个 scripts
   目录执行这些规则，规则检查（`--include-scripts`）与导出共用，违反时报 `脚本契约` 错误、导出拒绝
   （`script_binding_conflict`）。
2. **导出把调用关系交给 consumer。** operation frontmatter 写 `uses.operations`（主操作按顺序调用的
   子操作）；`operations/index.md` 的输入、输出取自 operation 文档的输入输出表，子操作的输出格注明
   「「主操作」流程中的一步」（表头不变，modeling 工作台按表头校验）。consumer 据此标注协调者与子步骤，
   没有自己脚本的子操作借用唯一最近的有脚本上级的脚本执行，执行结果附带 `_execution_note`
   （consumer 侧改动见 `consumer/docs/ontology-package-final-design.md`）。
3. **描述符是 `.py` 的派生物，落在工作区。** 每个对外脚本旁有同名 `<脚本>.md`，由
   `script_descriptor.render_descriptor` 按头部注释与 argparse 生成，格式对齐工作台 `scriptMd`
   （`# 脚本用途 / # 输入参数 / # 输出 / # 执行边界`）：
   - 类型只用 consumer 的 string / integer / number / boolean / object / array，`json` 作为旧写法保留并告警；
   - 默认值写 JSON 字面量，引用常量的默认值留空、由脚本自己生效；
   - `# 输出` 表来自头部「输出」逐行声明的字段；`# 执行边界` 写明 consumer 判定成败的约定
     （业务结论不用 ok=false / error / status=failed 表达；参数错误退出码 0、输出 ok=false；脚本异常才非 0 退出，
     在 stderr 输出 `ONTO_PUBLIC_ERROR`，见 0019）；
   - consumer 传不进去的 argparse 写法（位置参数、`nargs` 多值、`type=bool/list/dict`、`count` 等）在
     契约解析时直接拒绝。
   生成时机：`/ontology-codegen`、`/ontology-update` 改完脚本跑 `script_descriptor.py render`；后端
   Web 保存 `.py` 时同一提交带上描述符，刷新文件列表、导入与迁移时补齐或清理。描述符不能在 Web 端
   直接编辑；规则检查报描述符缺失、过期与多余，pre-commit 据此拦截。导出用同一渲染器，只把
   `entrypoint` 换成包内 ASCII 文件名，工作区看到的就是 consumer 拿到的。
4. **库模块不对外。** 「关联operation：无」的脚本只随包发布供 import，没有描述符，并保留原文件名
   （对外脚本按原名 import 它）。

## Alternatives considered

- **提取时不允许主/子操作，全部拍平**：子操作需要单独调用时无处落脚，共用的子流程要在每个主流程里
  各抄一份，资料里本来就有的子流程也要强行合并；问题出在「哪些能被 consumer 直接调用」这个执行层，
  不该靠改扁知识模型解决。改为在提取侧加轻约束：只在被复用、资料明确标为子流程、会被单独询问三种
  情况下拆出子操作。
- **保留子操作脚本，只在输出里加「这只是某一步」的提示**：靠模型自觉，选错时照样跑半个流程。
- **读子操作文档时改道到主操作**：会把只问口径、要解释某一步的请求也拦住；借用主脚本只作用于执行。
- **只在导出时生成描述符（原状）**：工作区不可见、不可评审，上面那些类型问题正是这样积累下来的。

## Consequences

- 工作区 `ontology/scripts/` 多出 `.md` 描述符；目录索引用描述符 frontmatter 的 `title` 作标题。
- 原先能导出、但有片段脚本、同一 operation 多个脚本、或主操作与不可单独调用的子操作各有脚本的
  工作区，现在检查报错、导出拒绝，需要按 `/ontology-codegen` Step 2 合并；这些包到 consumer 本来就会
  选错脚本或只跑半个流程。
- 头部类型写成 `int` / `str` 等旧写法照常兼容；写成 `number` 的 `type=int` 参数现在报错。
- consumer 不识别 `uses.operations` 的旧版本照常可用，只是没有子操作借用与角色标注。
