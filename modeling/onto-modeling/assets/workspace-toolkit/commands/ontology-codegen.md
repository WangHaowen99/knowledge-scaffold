Generate Python scripts from verified operation documents. Scope (if any): $ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `codegen`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock codegen`
2. `python3 .claude/scripts/state_helper.py check-prereq codegen`
   - `missing:逻辑已提取` → **跳步守卫**："继续会导致没有逻辑可生成代码"。选项 **继续** / **先做 逻辑提取**（/ontology-extract-operation）。

   > codegen 只要求 `逻辑已提取`。若逻辑描述中包含外部副作用，只在生成代码中保留注释说明，不再生成单独函数桩。
3. `python3 .claude/scripts/state_helper.py acquire codegen start`

4. 加载已沉淀业务知识。SCOPE_HINTS = `global` + codegen 范围内的每个 operation + 这些 operation 引用的对象（从各 operation 文档的 `# 与object的关联` section 解析）：
   ```bash
   SCOPE_ARGS=(--scope global)
   for operation in $TARGET_OPERATIONS; do
       SCOPE_ARGS+=(--scope "operation:$operation")
   done
   for obj in $REFERENCED_OBJECTS; do
       SCOPE_ARGS+=(--scope "object:$obj")
   done
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

   > 注：
   > - TARGET_OPERATIONS 与 REFERENCED_OBJECTS 在确定 codegen scope 后才能填入；本 load 实际在 **Step 2**（解析 scope）之后执行。
   > - `TARGET_OPERATIONS` 是 Step 2 解析出的生成目标，加上每个目标脚本覆盖的子操作（见 Step 2「按流程生成」），按 operation 名去重。
   > - `REFERENCED_OBJECTS` 是 TARGET_OPERATIONS 内所有 operation 文档的 `# 与object的关联` section 中 `[[...]]` 引用的对象并集，同样去重。

额外推荐（非强制）：检查 `reports/` 最近是否有未处理的 `[ ]`，若有，提示"建议先跑 /ontology-validate 处理完问题后再生成代码"。

命令结束前 `release codegen` 并 `update-progress`。

## 业务知识识别 & 应用（贯穿整个命令）

整个 codegen 命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。

本命令以 main agent 直接执行为主（无 sub-agent dispatch）。Step 3-4 生成 Python 代码时，**必须**先扫描 Step 0 加载的 `KNOWLEDGE_BLOCK`：
- 置信度 `高` → **硬约束**：SQL/字段处理/枚举/分隔符/单位等实现细节必须符合（如 `';'` 边界匹配、双轨 plain+JSON 过滤等）；
- 置信度 `中` / `低` → 在脚本注释中显式标注以便后续核对；
- 若 operation 文档与高置信知识条目冲突，停下来汇报冲突给用户裁决，不要静默照搬 operation 文档。

## Step 1: Check prerequisites

List files in `ontology/operation/`. If empty: "未找到逻辑文档，请先完成提取和校验流程。" Stop.

## Step 2: Determine scope

**强制规则**：codegen 必须显式指定目标 operation，**不接受"对全部 operation 一键生成"**。理由：盲跑会把不适合脚本化的 operation（人工决策、政策陈述等）也生成空壳脚本，污染 `ontology/scripts/`。要批量处理请先走 `/ontology-codegen-scan` 由人工评审候选。

### 按流程生成：一个完整流程一个脚本

consumer 只按 operation 调用脚本：选中哪个 operation，就执行绑定在它上面的那一个脚本，并把结果当作这个 operation 的完整结论。所以脚本必须按**完整业务流程**生成，而不是给流程里的每一步各生成一个：

- 主/子关系只看 operation 文档「# 与operation的关联」：`被operation调用 > [[父]]` 有值的是子操作，`调用operation > [[子]]` 有值的是主操作；`可单独调用 > 是 / 否` 只对子操作有意义，缺省为否。
- **主操作能脚本化**：只给主操作生成一个脚本，覆盖它调用的全部子操作；「可单独调用」为否的子操作写成主脚本里的函数，**不单独生成脚本**。consumer 选中这类子操作时会借用主操作的脚本跑完整流程。
- **子操作「可单独调用 > 是」**：它另有自己的脚本，单独调用时结论也要对（见 Step 4「可单独调用的子操作」）；主脚本 import 它的函数，同一段逻辑不写两份。父流程在调用它之前有输入检查、前置判断等停止条件时（规则检查会警告），它单独执行就会跳过这些检查：要么改为「可单独调用 > 否」并入主脚本（推荐），要么让它的脚本先执行同样的检查。
- **主操作不能脚本化**（含人工判断等）：子操作各自生成脚本；consumer 会按主操作文档逐步执行，走到这一步时调用子操作脚本。
- **一个 operation 只对应一个脚本**，没有片段脚本：只有部分步骤能脚本化时，把这几步拆成子操作（`/ontology-update`），再按上面的规则生成。

`python3 .claude/scripts/ontology_checker.py ontology --json --include-scripts` 会按同一规则检查，违反时报 `脚本契约` 错误并给出改法，知识包导出同样拦截。

**If $ARGUMENTS provided:**
按空格切分，每段是一个 operation 名；逐项 resolve 到 `ontology/operation/{operation}.md`，缺失文件给警告并跳过。旧的 `operation名:步骤区间` 片段写法不再支持：提示"片段不再单独生成脚本，请把这几步拆成子操作后再生成"，并跳过该项。

再把每个目标换算成实际要写的脚本：

1. 目标是子操作、「可单独调用」为否，且它的某个上级已有脚本或也在本次目标里 → 改为生成（或更新）那个上级的脚本，告诉用户"「{子操作}」并入「{上级}」的脚本"。
2. 目标是子操作、「可单独调用」为否，上级都没有脚本也不在目标里 → 先问用户：
   - **生成上级流程「{上级}」的脚本**（上级能脚本化时推荐）；
   - **只为「{子操作}」生成脚本**（上级含人工判断等、不能脚本化时）。
3. 目标是子操作、「可单独调用」为是，而父流程在调用它之前有停止条件（`ontology_checker.py` 对这类子操作给出「流程关联不完整」警告）→ 先问用户：
   - **改为「可单独调用 > 否」，并入「{上级}」的脚本**（推荐：用户只问这一步时由主脚本的 `steps` 作答）；
   - **保留单独脚本**：把父流程在它之前的检查写进库模块，主脚本和它的脚本都先执行这些检查（见 Step 4「可单独调用的子操作」）。
4. 目标是主操作 → 生成主脚本；它下面「可单独调用」为否的子操作已有的脚本要删除并把逻辑并入主脚本（告诉用户将删除哪些文件）；「可单独调用」为是的子操作还没有脚本的一并生成（主脚本要 import 它；父流程在调用它之前有停止条件的，按第 3 条先问用户）。

例：`/ontology-codegen 订单审核 交易对账` → 生成 `订单审核`（覆盖其子操作 库存校验、折扣计算）与 `交易对账` 两个脚本。

> 若 `$ARGUMENTS` 仅包含 `全部` / `all` / `*` 等通配词，**拒绝并提示**："代码生成不接受全量目标，请改用 `/ontology-codegen 逻辑1 逻辑2` 形式，或先跑 `/ontology-codegen-scan` 评估候选。"

**If $ARGUMENTS empty:**
List all operation files and ask:
"发现 {N} 个逻辑文档。请指定要生成代码的范围（不支持一键全部）：

- 回复 **扫描**（/ontology-codegen-scan）→ 先扫一遍候选清单挑（推荐）
- 回复 **选择 逻辑1 逻辑2** → 只生成指定逻辑的代码（子操作按上面的规则并入主操作脚本）
"

**Handler 路径：**

- **扫描** → `python3 .claude/scripts/state_helper.py release codegen`，然后 read `.claude/commands/ontology-codegen-scan.md` and follow its instructions（scan 走完会自动回到 codegen 并带回 `$ARGUMENTS`）。
- **选择 …** → 把 `选择` 后的内容当作 `$ARGUMENTS`，回到 Step 2 顶部的 "$ARGUMENTS provided" 分支重新解析。
- **取消** → `python3 .claude/scripts/state_helper.py release codegen`，Stop。
- **全部** / **all** / ***** → 复述强制规则并重新提示上述三选项，不进入生成流程。

> 若不确定哪些 operation 适合脚本化，`/ontology-codegen-scan` 会按流程输出带编号的候选报告（含每个流程覆盖的子操作、输入输出概要、判定理由）。

## Step 3: Read dependencies

For each operation doc in scope（主操作还要读它调用的每个子操作文档，子操作的逻辑要写进主脚本）:
1. Read the operation document from `ontology/operation/{name}.md`
2. Extract referenced objects from `与object的关联` section
3. Read each referenced object from `ontology/object/`
4. If `ontology/data-mock/` has mock data, read it for test examples

## Step 4: Generate Python code

对 Step 2 换算出的每个目标 operation 生成一个脚本：`ontology/scripts/{逻辑英文名}.py`（文件名只用英文、数字和下划线：知识包里对外脚本按 ASCII 文件名发布，被 import 的模块按原名发布）。

> 文件名就是知识包里的脚本 key：不超过 80 字符，不含 `/ \ # ? & |` 与 `..`。

**头部注释**：严格按 `.claude/templates/script-template.py` 模板填写，须包含：
- `关联operation`：本脚本对应的 operation 名称（一个 operation 只对应一个脚本，不写 ` / 步骤X` 后缀）。**要被评测或 consumer 执行的脚本必须绑定一个已存在的 operation**（`ontology/operation/` 下的文件名，不带 `.md`）：写了不存在的名字或填"无"，导出时都不会成为独立脚本，只留源码文件。"无"只用于被其他脚本 import 复用的库模块
- `功能描述`：一句话说明脚本做什么，主脚本写明覆盖了哪些子操作。它会成为描述符的标题，consumer 选脚本时看得到
- `详细逻辑`：对应 operation 的逻辑展开描述；主脚本按子操作逐段写
- `用法`：CLI（`python3 ... --参数 值`）+ import 两种调用范例
- `输入参数`：只列 `_cli()` 中 argparse 声明的 CLI 参数，每个一行"名称    类型    说明（必填 / 默认值；对象、数组参数写清字段并给一个示例）"；仅供 import 调用的函数形参不写在这里，放到「用法」的 import 示例中说明。纯库脚本（无 argparse）本段填"无"
- `输出`：stdout 那一个 JSON 对象的字段（即业务函数的 return 值），每个一行"字段    类型    说明"；主脚本必须有 `steps    object    各子操作的结论，键为子操作名`。脚本不写表、不生成文件
- `依赖`：所需 pip 包，无则删除本行

**CLI 契约**：源码中 `argparse.add_argument()` 的实际声明是唯一契约来源。头部参数表只能描述这些声明，不能根据参数 key 自动推导 flag；长选项统一使用源码中声明的 `--kebab-case`（如 `--order-id`）。CLI 必须把业务函数返回值序列化为单个 JSON 写到 stdout，诊断信息写 stderr。

- 头部「输入参数」与 argparse 声明**一一对应**：每个 `add_argument` 在头部有一行，头部也不能多出 argparse 没有的参数。函数形参多于 CLI 参数（如只在 import 时传入的记录列表）是允许的，但多出的形参不进头部参数表。
- 头部「类型」写 **consumer 调用时的类型**，只用六种，必须与 argparse 对得上：

  | argparse 写法 | 头部类型 | consumer 实际传入 |
  |---|---|---|
  | 不写 `type=` | `string` | 原样字符串 |
  | `type=int` | `integer` | `5`（写成 number 时 consumer 会传 `5.0`，`type=int` 解析失败） |
  | `type=float` | `number` | `5.0` |
  | `action="store_true"` | `boolean` | 为 true 时才带上开关 |
  | `type=json.loads`，取值是 JSON 对象 | `object` | 紧凑 JSON 字符串 |
  | `type=json.loads`，取值是 JSON 数组 | `array` | 紧凑 JSON 字符串 |
  | `action="append"` | 元素的类型 | 重复传 `--x a --x b` |

- consumer 传不进去的写法会被契约检查直接拒绝：位置参数、`nargs="+"` / `"*"`（consumer 重复 flag 传多值，argparse 只留最后一组；改用 `action="append"` 或一个 JSON 数组参数）、`type=bool` / `list` / `dict`、`count` 等 action、必填的布尔开关。
- 布尔开关统一用 `parser.add_argument("--is-urgent", action="store_true")`，头部写 `boolean`、「默认 false」。不要写 `default="false"` 再在函数里解析字符串——那样 argparse 按 `string` 接收。
- 默认值写字面量（数字、字符串、`True/False`、列表）：描述符按 JSON 写出，智能体不传时 consumer 原样补上；引用常量的默认值不会写进描述符，由脚本自己生效。
- 必填：`required=True` 的参数头部说明含「必填」；其余写「默认 X」或不标必填。
- **结构化参数的说明要写清来源对象、字段和示例**：consumer 的智能体只看得到「参数名=说明 必填/可选 类型」，看不到源码，例如 `orders    array    [[订单]] 查询结果，必填；每项原样拷贝一条，例：[{"ORDER_ID":"A-001","QTY":2}]`。说明里不要写「不传时按缺少记录 / 资料不足处理」这类缺省行为：智能体在假设场景里会据此整条不传。描述符会自动在记录参数说明的最前面写明「记录存在就照传，没取值传 `{}`」，规则检查对这类写法给出警告。

**入参按来源对象分组（对齐 consumer 参数证据校验，必须遵守）**：consumer 在启动脚本前核对参数——参数里每个 JSON 对象（顶层参数整体也算一个）是一条记录，记录里能在本轮查询结果中按**同名字段**找到的字段，只要涉及标识字段（`*_id`、`*_no`、`*_code`、`*_name`、编号、名称等），就必须出自同一条查询结果；标识字段的取值也必须出现在用户请求或查询结果里。对不上时判 `evidence_record_mismatch` / `ungrounded_identity`，脚本不运行，同样的错误再出现一次整轮对话就会终止。所以：
- **一个记录参数只装一个对象**：每项就是该对象的一条查询结果，字段名逐字使用对象「对象属性清单」的英文名称（大小写一致，即查询结果里的字段名），取值原样拷贝，不改格式、不换算、不补值。说明里写明来源对象 `[[对象名]]`，示例字段取自它的属性清单。
- **不同对象分开传，在脚本里关联**：需要事件及其设备名称时，设 `events`（[[事件]]）与 `devices`（[[设备]]）两个参数，在脚本里按 `DEVICE_ID` 关联；不要让智能体把设备名称拼进事件记录。
- **派生值在脚本里算**：合计、换算、比较结果等不作为记录字段传入。
- **顶层标量参数只放用户给的条件**（时间范围、阈值、模式等），名字不要与对象属性同名（如 `query_month`、`min_amount`）；对象的字段放进该对象的记录参数。与对象属性同名的顶层参数最多一个。
- **记录里的标识字段和外键在脚本里按可选处理**：用户没给、查询结果里也没有时智能体会省略（智能体查询时只取它认为需要的字段，用作过滤条件的外键常常不在返回结果里，consumer 又不允许从别的记录补）。关联与一致性核对只在两边都有值时做，缺失时按调用范围关联（同一次调用传入的记录属于同一查询范围），必要时按规则输出资料不足，不要因缺这类字段报错退出。
- **记录是否提供只看参数传没传**：传了就算提供，包括 `{}` 和字段为空的记录，缺哪个字段按该字段的规则处理（如在用到它的那一步输出资料不足）；只有参数没传才算记录缺失。不要用编号、也不要用「有没有字段取值」判断记录在不在——用户只说「某记录已提供」却没给取值时，智能体会传 `{}`（描述符自动注明这条约定）。只有一边有编号时不判不一致。
- **没有编号时怎么关联**：每个参数只有一条记录时直接按调用范围关联。多条记录需要一一对应时（如每条需求对应一条事件），在参数说明里写明顺序约定（如「needs 第 i 条对应 events 第 i 条」），脚本按这个顺序关联；没有约定、数量对不上时输出资料不足，并说明缺哪条关联。顺序约定在所有用到关联的地方都要用上，包括汇总、集合判断（如「某类需求关联的全部事件」）这类跨记录的计算。
- **没有编号时不拿编号当键**：记录之间的匹配、去重、分组，以及 `steps` 的键，都用模板的 `_record_key`——有编号用编号，没有编号用记录在数组里的位置（如 `events[1]`）。两个空编号不算同一条：拿 `str(e.get("event_id") or "") == src` 这种写法匹配，没有编号时每条需求都会匹配到第一条事件，按空编号去重又会把不同记录并成一条，结论会悄悄变错。
- **编号类参数不设必填**：顶层的编号参数（如 `--order-id`）和只含编号字段的记录参数都设为可选。用户只描述情况、没给编号时（如「假设某个订单……会怎样」），查询结果里也没有，智能体只能编造编号，consumer 判 `ungrounded_identity`、脚本不运行。缺编号时按上一条关联，资料不全就输出资料不足（退出码 0），不要报错退出——consumer 遇到非 0 退出会直接结束本轮对话。参数示例里的编号只是格式示例，描述符会自动注明「没有就省略、不要照抄」。
- **「是/否」字段先归一再判断**：取值原样拷贝，脚本就要认数据源的表示——图数据源把「是/否」列存成布尔值，查询结果里是 `true` / `false`。判断前先用模板里的 `_yes_no` 归一（布尔值与 是/否/true/false/yes/no/y/n 都认，认不出返回 None 按资料不足处理），不要直接写 `value == "是"` 或 `value in ("是", "否")`，否则布尔值会被当成缺失。
- **枚举字段先校验取值**：对象属性清单里类型为「枚举」、写了「取值：a / b」的字段，描述符会在记录参数说明里自动列出允许的取值；脚本用模板的 `_check_choice` 校验，不在范围内时按参数错误返回（`ok: false`，写明字段位置和允许取值），智能体改正后重试。不要把不认识的取值当成「条件不满足」往下算——智能体在假设场景里常不读对象文档，按字面意思自造取值（如把产品名填成需求类型），结论就会悄悄变错。operation 文档对非法值另有规定（如按资料不足处理）时，按文档写。

契约检查（Step 4.4）按同一口径拦下「一条记录混入不同对象字段」「顶层多个参数与对象属性同名」「编号参数或只含编号的记录参数设为必填」，知识包导出同样拒绝；按「是/否」字符串判断却没有归一、参数错误以非 0 退出（`_public_error("INVALID_INPUT", ...)`）、记录参数说明写了不传时的处理时给出警告。描述符会在记录参数的说明后自动补上原样拷贝的要求和枚举字段的允许取值，并对编号字段注明没有就省略。

**输出与报错（consumer 判定成败的约定）**：consumer 遇到非 0 退出会直接结束本轮对话（不可重试），所以非 0 退出只留给脚本自身的异常。
- 成功：退出码 0，stdout 只输出一个 JSON 对象，字段与头部「输出」一一对应，结论字段放在最前面（运行验证只看 stdout 前 1500 字）。「不通过」「资料不足」「资料冲突」等业务结论也算成功，写在业务字段里（如 `conclusion`）；**不要**用 `ok: false`、`error`、`status: "failed"` 表达业务结论，consumer 会把它当成调用失败。
- 资料问题不是错误：记录、字段、编号或外键缺失，记录之间无法关联，取值互相矛盾，都按业务结论输出（写明缺什么、哪两个值冲突），退出码 0。记录参数没传造成的缺失用模板的 `_missing_record(参数名)` 说明：写明参数未传，并提示「题面说明已提供、只是没给取值时传 `{}` 重新调用」，智能体第一次漏传时才会改正。
- 主脚本每条结论（逐记录输出时每一行）最前面放「最终结论」字段，一句话直接回答用户的问题：结论是什么（如通过或不通过）、资料不足或资料冲突。流程在某一步停止时，按 operation 文档的规则写出停止对应的结论（如停止审批就是「不通过」），并写明停在哪一步、为什么停、不修改任何输入继续判断；不要只留 `null` 或「未执行」，否则智能体会回答「无法判断」，或者顺着用户的问法给出与停止相反的结论。头部「输出」也要列出这个字段。
- 参数错误（类型、格式、取值范围不对，智能体改参数就能修正）：入口先校验参数再计算，校验失败抛 `ValueError(原因)`，由 `_cli` 交给 `_invalid_input`：退出码 0，stdout 输出 `{"ok": false, "error": {"code": "INVALID_INPUT", "message": "原因"}}`。consumer 把它当作这次调用失败、原因交给智能体，智能体改了参数可以重试（参数不变原样重试会被判为停滞）。
- 原因写到字段级、给出改法，不回显取值：如 `events[1].EVENT_DATE 不是 YYYY-MM-DD 格式`、`needs 不是数组：需要 [[需求]] 查询结果数组`。
- 脚本自身异常：`_cli` 捕获其余异常，在 stderr 输出 `ONTO_PUBLIC_ERROR`（code `SCRIPT_FAILED`，message 写出错的步骤与异常类型）后非 0 退出，不要让未处理的异常直接退出。
- 退出方式：`_invalid_input`、`_public_error` 只输出并返回退出码，由 `_cli` 返回，只在 `if __name__ == "__main__": sys.exit(_cli())` 入口退出进程；函数里不要调用 `sys.exit` 或 `raise SystemExit`，stdout / stderr 用模板的 `_emit` 写（CodeCheck G.ERR.11、G.LOG.02）。

**脚本安全**：评测打包前会逐字扫描整份源码（**含注释与字符串**），命中下列写法整份脚本被拒，所以注释里也不要出现这些模块名或调用：

- 起子进程或执行系统命令：`subprocess`、`os.system` / `os.popen` / `os.spawn*`
- 网络访问：`requests`、`httpx`、`aiohttp`、`socket`、`urllib.request` / `urlopen`
- 高风险模块：`ctypes`、`multiprocessing`、`pickle`、`marshal`
- 写文件：写模式的 `open(..., "w" / "a" / "x" / "+")`；递归删除 `shutil.rmtree`
- 动态执行：`eval`、`exec`、`compile`、`__import__`（`re.compile` 这类模块方法不算）
- 源码超过 100000 字符

结果一律作为 JSON 写到 stdout，交给调用方落库或展示。另外，评测侧判写文件的正则会把 `open(` 同一行里以 w / a / x / + 开头的任何字符串（如 `encoding="ascii"`）当成写模式，`open` 调用里只用 `encoding="utf-8"` 这类写法。

**主体结构**：
```python
"""<按模板填写的头部注释>"""
from __future__ import annotations
import argparse
import json
import sys
# 其他 import

# --- 数据结构（来自引用的 object，可选）---
@dataclass
class {ObjectEnglishName}:
    """对象描述"""
    {attribute_english}: {python_type}  # {中文名称}: {描述}

# --- 子操作：主脚本里每个「可单独调用」为否的子操作一个函数，函数注释写明对应的子操作 ---
def {sub_operation_english_name}(...) -> dict:
    """子操作：{子操作名}"""
    ...

# --- 主函数：使用普通形参，便于被其他脚本 import 调用 ---
def {operation_english_name}(arg1: T, arg2: T | None = None) -> dict:
    steps = {}
    steps["{子操作名}"] = {sub_operation_english_name}(...)  # 按逻辑描述的顺序与停止条件调用
    ...
    return {"conclusion": ..., "steps": steps}

# --- CLI 入口：纯库脚本可删除 _cli 与下方 __main__ 块；只在 __main__ 入口退出进程 ---
def _cli() -> int:
    parser = argparse.ArgumentParser(description="{一句话业务描述}")
    parser.add_argument("--arg1", required=True, help="...")
    parser.add_argument("--arg2", default=None, help="...")
    try:
        result = {operation_english_name}(**vars(parser.parse_args()))
    except ValueError as exc:  # 入口校验抛出的参数形态错误：退出码 0、ok=false，智能体改参数后可重试
        return _invalid_input(str(exc))
    except Exception as exc:  # 脚本自身异常：非 0 退出，consumer 会结束本轮对话
        return _public_error("SCRIPT_FAILED", f"{{当前步骤}}出错（{type(exc).__name__}）")
    _emit(json.dumps(result, ensure_ascii=False))
    return 0

def _invalid_input(message: str) -> int:
    _emit(json.dumps({"ok": False, "error": {"code": "INVALID_INPUT", "message": message[:240]}}, ensure_ascii=False))
    return 0

def _public_error(code: str, message: str) -> int:
    _emit("ONTO_PUBLIC_ERROR:" + json.dumps({"code": code, "message": message[:240]}, ensure_ascii=False), sys.stderr)
    return 2

def _emit(text: str, stream=None) -> None:  # consumer 读取的命令产物，直接写流、不走 logging
    target = sys.stdout if stream is None else stream
    target.write(f"{text}\n")

if __name__ == "__main__":
    sys.exit(_cli())
```

**主脚本的 `steps`**：按子操作名给出每一步的结论（被停止条件跳过的步骤写明未执行及原因）。consumer 选中「可单独调用」为否的子操作时会执行主脚本，再从 `steps["<子操作名>"]` 取这一步的结论；用户只问某一步时也靠它作答。每一步除了结论，还要给出这一步算出的中间结果：评分类给每个分项的取值与得分、总分和阈值，判断类给命中与未命中的条件及比较用的取值。operation 文档「输出」只写了最终结论时也要给——用户常追问分项和原因，智能体只能转述脚本的输出。

**可单独调用的子操作**：它自己的脚本单独被调用时结论也必须对——把它依赖的前置检查（主流程里排在它前面、它的结论依赖的输入检查与筛选，如资料缺失、资料冲突的判断）一并执行。这些检查写进「关联operation：无」的库模块，主脚本与子操作脚本都 import 同一份，不各写一遍；子操作脚本检查不通过时按主流程同样的结论输出（资料不足、资料冲突等）。主脚本 `from {子操作脚本} import {子操作函数}` 复用核心计算，不重写同一段逻辑。

**库模块**：多个脚本共用、不对应任何 operation 的代码可放进「关联operation：无」的库模块（如 `shared_rules.py`），由对外脚本 import；库模块不对外调用、没有描述符。

**列表/对象类入参**（如 `list[dict]`、SKU 明细等）：使用一个 JSON CLI value（`type=json.loads`，头部类型写 `array` / `object`），与 Consumer 的结构化参数契约一致；不要把同一个参数拆成重复 flag。按上方「入参按来源对象分组」，每个这类参数只装一个对象的查询结果。大批量数据仍可使用 `--input-file path.json`，但必须在头部和 operation 文档中声明。脚本优先接收调用方按查询边界筛选后的记录，只做纯计算；不得在脚本中通过表名、整表或无条件查询自行取数。

```python
# events：[[事件]] 查询结果；devices：[[设备]] 查询结果；在脚本里按 DEVICE_ID 关联
parser.add_argument("--events", required=True, type=json.loads, help="[[事件]] 查询结果，每项原样拷贝一条，例如 [{\"EVENT_ID\":\"E-1\",\"DEVICE_ID\":\"D-1\",\"EVENT_DATE\":\"2026-01-01\"}]")
parser.add_argument("--devices", default=None, type=json.loads, help="[[设备]] 查询结果，每项原样拷贝一条，例如 [{\"DEVICE_ID\":\"D-1\",\"DEVICE_NAME\":\"设备A\"}]")
args = parser.parse_args()
{operation_english_name}(events=args.events, devices=args.devices or [])
```

> 真有大批量结构化入参（如 100+ 条），用 `--input-file path.json` 由脚本读文件，而不是把 JSON 塞进 CLI value。

**Type mapping:**
- 字符串 → str
- 数字 → int or float
- 布尔 → bool（函数形参；CLI 侧写法见上方「CLI 契约」的布尔开关约定）
- 枚举 → Enum class
- 日期 → str (ISO format)
- 对象引用 → the corresponding dataclass

**Operation translation:**
- 判断 steps → if/elif/else
- 调用子流程 → 主脚本内的子操作函数（可单独调用的子操作 → import 它脚本里的函数），结论记入 `steps`
- 循环 → for/while loops
- 异常处理 → try/except

### Step 4.2: 改写对应 operation 文档（每个脚本必做，不可跳过）

> **每写完一个 `.py` 必须紧接着改对应 `ontology/operation/{name}.md` 的 `# 逻辑描述` 段**——
> 跳过此步直接进入 Step 4.5 = 流程错误。本步与生成脚本同属 Step 4 的必做项，
> 不要因 Step 4.5 有 review-agent 兜底就省略。

原 `# 逻辑描述` 中**不再重复脚本内部的详细实现**——含字段级赋值（`[[对象]].字段 = 值` / `+= 值`）、
内部循环步骤、SQL / Redis / 分布式锁等技术细节——只保留：

1. **简要描述**：一句话说明这段逻辑做什么；
2. **调用示例**：argparse 形式的 CLI 调用，使用典型业务参数值。

格式：

```markdown
1. {步骤1（高层业务语义）}
2. {步骤2}
...
N. 调用脚本 `ontology/scripts/{script}.py` 执行 {核心操作}，返回 {结果}。

**调用示例：**

\`\`\`bash
python3 ontology/scripts/{script}.py --参数1 值1 --参数2 值2
\`\`\`
```

规则：
- 业务步骤保留高层语义，**不重复**脚本内部实现细节。哪怕用了业务词汇（"库存"/"SKU"），只要出现字段级算式或内部循环就算未简化。
- 主脚本：主操作的「# 逻辑描述」保留 `调用[[子操作]]逻辑文档` 这些步骤与停止条件（只删实现细节），说明由脚本统一执行；`# 与operation的关联` 不动——导出时它就是 consumer 识别主/子操作的依据。
- 并入主脚本、没有自己脚本的子操作，「# 逻辑描述」不改写成「调用脚本」：它的步骤是主脚本里对应函数的依据。
- 调用示例使用实际业务场景中的典型参数值；脚本头部注释里的「输入参数」段是参数定义的单一来源，operation 文档不再重复字段表。

### Step 4.3: 默认值与双语支持（按需）

- 若参数有合理默认值，在脚本中定义为模块级常量（如 `DEFAULT_CLUSTERING_FACTORS`），并在 argparse 的 `default=` 中引用。描述符只写字面量默认值：引用常量时 consumer 看不到默认值、由脚本自己生效；希望智能体看到默认值的参数直接写字面量。
- 若参数支持中文名，提供 `{FIELD}_NAME_MAP` 映射字典，并在入口函数中统一归一化为内部英文字段名。

### Step 4.4: 脚本描述符与契约规则检查（确定性，必做）

写完所有脚本、进入复审前，先生成脚本描述符，再跑与知识包导出同一实现的契约检查：

```bash
python3 .claude/scripts/script_descriptor.py render ontology
python3 .claude/scripts/ontology_checker.py ontology/ --json --include-scripts
```

- `render` 按每个 `.py` 的头部注释与 argparse 生成同名 `ontology/scripts/<脚本>.md`（consumer 调用这个脚本的契约，知识包里原样发布），删掉没有对外脚本的描述符。描述符**不要手改**：要改参数、类型、说明或输出，改 `.py` 后重新 render。
- 过滤 `file` 在 `scripts/` 下、`category` 为 `脚本契约` / `脚本安全` / `脚本绑定` / `命名约束` 的 issue，`错误` 必须在这里清零：头部与 argparse 不一致、consumer 传不进去的参数写法、会被 consumer 参数证据校验拒绝的入参形态（一条记录混入不同对象的字段、顶层多个参数与对象属性同名）、一个 operation 多个脚本、子操作与主操作脚本重复覆盖、描述符缺失或过期，在导出/评测打包时都会被同一实现拦截；安全写法与脚本绑定的口径见上方「脚本安全」与头部「关联operation」。按上方「CLI 契约」「脚本安全」与 Step 2「按流程生成」修正头部、argparse 声明、源码写法或文件名后，重跑这两条命令直到这些错误清零。`脚本绑定` 的「建议」（关联operation 为「无」）只需确认该脚本确实是库模块。`警告`（`json` 类型没写明 object/array、结构化参数缺示例、「输出」没逐行列字段等）也应在这里改掉——它们决定 consumer 的智能体能不能传对参数、读懂结果。其他类别的 issue 不在本步处理。

---

## Step 4.5: 代码复审

Read `.claude/agents/codegen-review-agent.md`. Dispatch it as a subagent.

在 dispatch prompt 顶部（在任何输入变量之前）插入：

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}
```

变量：
- `TARGET_SCRIPTS`: Step 4 中生成或修改的 `ontology/scripts/*.py` 文件相对路径（空格分隔）
- `RELATED_OPERATIONS`: 对应 `ontology/operation/*.md` 文件相对路径（空格分隔）

在 dispatch prompt 底部（在已有的收尾指令之后）追加：

```

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for it to return a JSON array.

### 4.5.1: 处理错误级 issue（自动修复 + 单轮回归）

按 `severity=错误` 过滤 issue。由 main agent 直接修复：

- `头部缺失` / `头部字段错误` → 按 `.claude/templates/script-template.py` 补齐 7 字段
- `CLI不规范` → 替换为 argparse + 源码实际声明的 `--kebab-case` 形式；结构化参数使用一个 JSON value；入参形态问题按 Step 4「入参按来源对象分组」拆成按对象的记录参数、改顶层参数名，并同步改脚本里的关联逻辑与验证用例
- `骨架不规范` → 按模板补 `from __future__ import annotations` / `_cli()` 等
- `脚本安全` → 按 Step 4「脚本安全」去掉禁用写法：外部数据改由 CLI 参数传入，结果改为 stdout JSON
- `operation未简化` → 改写对应 `ontology/operation/{name}.md` 的「逻辑描述」段（按 Step 4.2 执行）。本应在 Step 4.2 生成时已完成；如果跑到这里说明 Step 4.2 漏做了，现在补
- `流程覆盖` → 按 Step 4「主体结构」把「可单独调用」为否的子操作写成主脚本里的函数、按顺序与停止条件调用并补齐 `steps`（含每一步的中间结果，以及停止时的最终结论）；「可单独调用」为是的子操作改为 import 它脚本里的函数
- `业务知识冲突` → 按 KNOWLEDGE_BLOCK 中对应高置信条目调整脚本（如 `';'` 边界匹配 SQL）

修复后先重跑 Step 4.4 的契约检查，再**重新 dispatch codegen-review-agent**（同 TARGET_SCRIPTS / RELATED_OPERATIONS）做一次回归。若回归后仍有 `severity=错误` 且与上一轮相同（按 `file + message` 精确配对），停下来汇报：

```
自动修复未能消除以下错误，需人工介入：

1. ❌ [{category}] {file} → {message}
2. ...

请逐条说明如何修复，或回复 **回滚** 撤销本次生成。
```

回归仅做一次，避免死循环。

### 4.5.2: 处理警告/建议级 issue

若 4.5.1 后仅剩 `警告` / `建议`，照搬 `.claude/commands/ontology-validate.md` A5 的交互：

```
代码复审还有 {M} 个建议/警告：

1. ⚠️ [{category}] {file} → {message}
2. 💡 [{category}] {file} → {message}

要处理哪些？[全部 / 输入编号如 1,3 / 跳过]
```

- **全部** / **编号**：逐条按 `[修改 {N}/{TOTAL}]` 模板提出修复方案，用户选 `[确认修改]` / `[我来提供方案]` / `[跳过]`。
- **跳过**：保留现状，继续 Step 4.6。

### 4.5.3: 无问题 / 处理完毕

首轮 agent 返回 `[]`，或 4.5.1 + 4.5.2 处理完毕后无遗留 `错误` → 进入 Step 4.6。

---

## Step 4.6: 脚本运行验证（必做，不可跳过）

每个新建或修改过的脚本都必须真正跑通：pre-commit 会拦下没有当前版本通过记录的脚本。

1. 为每个脚本写用例文件 `tmp/verify/<脚本名>.cases.json`（`tmp/` 不进 git）：

   ```json
   [
     {"name": "正常", "argv": ["--orders", "[{\"sku_id\":\"sku-a\",\"quantity\":2}]", "--quantity", "3"], "expect": "success", "stdout_contains": ["通过"]},
     {"name": "缺少必填参数", "argv": ["--quantity", "3"], "expect": "error"}
   ]
   ```

   - argv 按脚本头部「输入参数」里的 CLI 参数写，每一项都是字符串（数字和 JSON 也写成字符串）；
   - 记录参数按 consumer 实际传参的样子写：每项原样拷贝对应对象的一条 mock 记录（`ontology/data-mock/`），字段名与对象属性清单的英文名称一致，不拼别的对象的字段；再加一个**不带任何编号的成功用例**，按用户只描述情况、没给编号的样子写：顶层编号参数不传，记录里的编号、外键字段全部删掉；题面没给取值的记录传 `{}`（数组每条传一个 `{}`），不要编题面不会给的字段；用 `stdout_contains` 写明预期结论，应与资料完整、带编号的同一场景一致，不能变成资料不足。参数里有编号的对外脚本缺这个用例、或它没写 `stdout_contains` 时，`script_verify` 直接拒绝用例文件。有多条记录需要一一对应时，再加一个多条记录都不带编号的用例：让各条记录的结论不一样（如一条已终结、一条进行中），用 `stdout_contains` 断言每条各自的结论，并确认 `steps` 里每条记录各有一项、没有互相覆盖，涉及跨记录集合判断的流程要覆盖到这一步；「是/否」字段至少有一个用例按图数据源的样子传布尔值 `true` / `false`，确认结论与传「是/否」时一致；有枚举字段的脚本加一个传不在清单里的取值的用例（`expect: error`），确认输出 `ok: false` 并列出允许的取值；可单独调用的子操作脚本还要有资料缺失、资料冲突的用例，确认它和主流程给出同样的结论；
   - 覆盖正常、边界、缺少必填参数、未知参数、空结果、资料不足或资料冲突、参数形态错误（没有对应场景的可以省略），至少一个 `expect: success`，最多 12 个；参数形态错误的用例要确认 stdout 是 `ok: false` 且原因写到字段级；
   - 主脚本的成功用例要覆盖完整流程与停止条件：输出里有 `steps`，每个子操作的结论（或未执行的原因）和中间结果都在；停止条件的用例用 `stdout_contains` 写明停止对应的最终结论；
   - `success`：退出码 0、stdout 是单个 JSON，且不是 `ok: false` 这类失败结果；`error`：非 0 退出（如 argparse 报错），或退出码 0 但输出 `ok: false` 的参数错误。资料不足、资料冲突是业务结论，用例写 `success`；
   - 成功用例尽量用 `stdout_contains` 写明预期结论（如 `["通过", "资料冲突"]`），写结论取值本身，不写带引号和冒号的 JSON 片段；只比对 stdout 前 1500 字，所以结论字段要放在输出最前面；
   - 输入用调用方会传进来的合成数据，不要依赖数据库或网络：沙箱内禁止出网。

2. 运行：

   ```bash
   python3 .claude/scripts/script_verify.py run ontology/scripts/<脚本名>.py --cases tmp/verify/<脚本名>.cases.json
   ```

   - 经脚本验证网关（`ONTO_WEB_SANDBOX_GATEWAY_URL`）在 jiuwenbox 沙箱中执行；网关或 jiuwenbox 不可用时自动改在 onto-modeling 容器内执行，此时结果里 `runtime` 为 `local`，`fallback_reason` 写明原因；
   - 退出码 0 = 全部通过且已记录；1 = 有用例失败（看 `cases[].reason` 和 `stderr`）；2 = 用例文件或参数写错；3 = 执行器本身失败。

3. 有失败就修脚本（或修正写错的用例），重跑 Step 4.4 的契约检查和本步，直到全部通过。同一用例连续两轮因相同原因失败，停下来把失败用例和 stderr 汇报给用户。

> 改了脚本或它 import 的同目录模块，之前的验证记录自动失效，需要重跑本步。

---

## Step 5: Present results

Show list of generated files（每个脚本旁有同名描述符）：
"代码生成完成：
- ontology/scripts/order_review.py + order_review.md        ← 订单审核（覆盖子操作：库存校验）
- ontology/scripts/discount_calc.py + discount_calc.md      ← 折扣计算（可单独调用，订单审核 import 它）
...

运行验证：{N} 个脚本全部通过（在 jiuwenbox 沙箱中 / jiuwenbox 不可用，在 onto-modeling 容器内）。

共 {N} 个脚本。如需修改请说明；确认无误请回复 **确认**。"

## Step 6: Commit

```bash
python3 .claude/scripts/script_descriptor.py render ontology
git add ontology/scripts/ ontology/operation/
git commit -m "codegen: {TOPIC}"
```

> pre-commit 会拦下没有当前版本运行验证记录的脚本，以及缺失或过期的描述符（提交前先 render）。`ontology/scripts/` 同时纳入 `.py` 与同名 `.md` 描述符；Step 4.5.1 可能改写 `ontology/operation/*.md` 的「逻辑描述」段（简化为业务步骤 + 调用示例），故一并纳入 `git add`。

## Step 7: Suggest next step

"代码生成完成，已保存至 `ontology/scripts/`。

**下一步建议：**
- 回复 **追踪**（/ontology-trace）→ 用场景进一步验证逻辑
- 回复 **整体视图**（/ontology-overview）→ 查看全局关系
- 回复 **扫描候选**（/ontology-codegen-scan）→ 看还有哪些逻辑没生成
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。"
