Generate or update the declarative solver evaluator for a solver contract. Solver key (if any): $ARGUMENTS

声明式评估器是 `ontology/scripts/` 下的一个纯字面量 Python 文件：只声明「从哪些本体对象、哪些属性取数」和评估参数，
不含任何计算代码。取数和路线指标全部由 Consumer 完成，所以本命令**只填值、不写逻辑**。

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `solver-evaluator`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock solver-evaluator`
2. `python3 .claude/scripts/state_helper.py check-prereq solver-evaluator`
   - `missing:对象已提取` → **跳步守卫**："继续会导致没有对象和属性可供映射"。选项 **继续** / **先做 对象提取**（/ontology-extract-object）。
3. `python3 .claude/scripts/state_helper.py acquire solver-evaluator start`
4. 加载已沉淀业务知识。SCOPE_HINTS = `global` + Step 1 确定的 operation + Step 2 选用的对象（确定后再执行）：
   ```bash
   SCOPE_ARGS=(--scope global --scope "operation:$OPERATION")
   for obj in $REFERENCED_OBJECTS; do
       SCOPE_ARGS+=(--scope "object:$obj")
   done
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串。

命令结束前（含中途停止）执行 `python3 .claude/scripts/state_helper.py release solver-evaluator`。

中途每次停下来问用户（选契约、选评估器处理方式、补参数取值、裁决冲突、确认提交）前，先 `python3 .claude/scripts/state_helper.py acquire solver-evaluator confirm`；用户回复后 `acquire solver-evaluator start`。

整个命令期间遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`：置信度 `高` 的知识条目是硬约束；与本体文档冲突时停下来请用户裁决。

## Step 1: 确定契约与目标文件（只读）

`ontology/solver/` 是平台受管目录（规则 8.06），本命令**只读不写**；需要改契约时提示用户到「求解模型」面板修改。

1. **确定 solver_key**：
   - `$ARGUMENTS` 非空 → 取第一个词作为 solver_key，页面发来的 `solver_key=<键>` 同样取等号后的值；带 `mode=template` 时按模板重新生成评估器（覆盖前先向用户确认），读 `ontology/solver/{solver_key}/binding.yaml`；不存在则提示"未找到求解契约 {solver_key}，请先在「求解模型」面板保存契约。"并停止。
   - `$ARGUMENTS` 为空 → 列出 `ontology/solver/*/binding.yaml`：没有则提示"还没有求解契约，请先在「求解模型」面板保存契约后再运行。"并停止；只有一个直接使用；多个时用编号列表请用户选择。
2. 从 `binding.yaml` 读出 `operation`（记为 OPERATION），从同目录 `model.yaml` 读出 `fact_source.evaluator_path`（可能没有）。
3. **确定目标文件 TARGET**：
   - 声明了 `evaluator_path`（形如 `scripts/<文件>.py`）→ TARGET = `ontology/<evaluator_path>`。
   - 未声明 → 对 `ontology/scripts/*.py` 逐个运行
     `python3 .claude/scripts/solver_evaluator_contract.py <文件> --json`，按输出的 `kind` 找出已有评估器
     （`legacy` 旧式 / `declarative` 声明式，`null` 不是评估器）：
     - 一个都没有 → TARGET = `ontology/scripts/{solver_key}_evaluator.py`（新建）。
     - 恰好一个 → TARGET = 该文件。
     - 多个 → 停止并说明："发现多个评估器（{列表}），Consumer 无法自动选择。请在「求解模型」面板为契约指定评估器路径，或删除多余的评估器后重新运行。"
4. **TARGET 是旧式评估器**（`kind` 为 `legacy`，包内自带计算公式）时，本命令不改写它。先 `acquire solver-evaluator confirm`，停下来询问：

   ```
   当前契约使用旧式评估器 {TARGET}（包内自带计算代码）。本命令不会改写旧式评估器。

   - 回复 **新建替换** → 新建声明式评估器 {solver_key}_evaluator.py，提交时删除旧式评估器
   - 回复 **新建保留** → 只新建声明式评估器；两者并存时需你在「求解模型」面板把评估器路径改为新文件
   - 回复 **取消** → 不做修改
   ```

   只有用户明确回复 **新建替换** 才能删除旧文件；**新建保留** 时在 Step 5 提醒用户修改评估器路径。

## Step 2: 收集依据（只读）

1. 读 OPERATION 文档（`ontology/operation/{OPERATION}.md`，不存在时找 `ontology/logic/`）。
2. 读 `ontology/object/` 下的对象文档。TABLE_SOURCES 中：
   - `object` 填对象文档的**文件名**（不含 `.md`，即图谱 Label）；
   - `fields` 的值填对象字段表里的**英文名称**，不是中文名称；
   - 某个逻辑列在本对象的字段表里没有、但能经另一个对象查到（例如本对象只记录了另一个对象的 ID 或名称，所需的列在那个对象上）→ 写成一跳关联 `{"lookup": 关联对象, "match": {本对象属性: 关联对象属性}, "field": 关联对象属性}`，见模板注释。
   - **不要**为凑齐逻辑列修改对象文档或补造属性：图谱数据里没有这个属性，Consumer 取到的只会是空值。经一跳关联也查不到的列，停下来问用户数据从哪里来。
3. 读 `ontology/rule/`、`ontology/metric/` 中与能力、修正系数、储罐库存、生产工况相关的文档。
4. 选定要用的对象（含 lookup 关联对象）后，执行 Step 0 第 4 步加载业务知识。

参数来源只有三种：本体文档、业务知识、用户回答。**不能编造**，也不能把 `ontology/data-mock/` 的模拟数据当作真实参数。

## Step 3: 填写声明

1. 新建时把 `.claude/templates/solver-evaluator-template.py` 复制为 TARGET；已是声明式时在原文件上修改。
2. 只替换 `<待填写：…>` 占位符和需要修正的值，模板注释说明了每个名字的含义。必须遵守：
   - 只能有 docstring、`from __future__ import annotations` 和模板中那些名字的字面量赋值；不能写函数、类、其它 import、变量引用、运算或类型标注。
   - 不增删模板里的名字；`YIELD_*` 保持默认值。
   - 标「可选」的逻辑列在本体中没有对应属性时删掉该行（Consumer 按 0 计）；其它逻辑列不能删。映射了的列在用到的行上取到空值时评估直接失败，不会按 0 计。
   - ID 列（`OilYardID`、`RefineryUnitID`、`VGOUnitID`、`LogicTankID`）映射的属性必须是字符串类型。对象文档里写成数值类型时停下来告诉用户，由用户修正对象文档与导入数据，不要自行改文档。
   - lookup 只支持一跳：`match` 两边是同一含义、同一类型的属性（Consumer 按原值等值匹配，不做类型转换）。本表中在关联对象里匹配不到的行不参与评估（例如与本次求解无关、未登记的装置产品），匹配到多条记录时评估直接失败。不确定关联对象上 `match` 属性的组合是否唯一时，问用户。
   - 各单元编号必须在 `DIESEL_UNIT_IDS`、`SOLVER_CONTEXT.unit_capacity_keys.diesel_by_unit`、`SOLVER_CONTEXT.correction_factor_prefix_by_unit` 三处一一对应。
   - 单元编号（上面三处的键）写用户提问时对该单元的称呼，即业务文档里的单元名称，不超过 32 个字符；`DIESEL_UNIT_IDS` 的值才是 RefineryUnitID。Consumer 的 Agent 按这些键匹配用户说法并原样写进求解结果，键写成 ID 时用户按名称提问会被就近换成某个 ID。文档里找不到单元名称时，列入第 3 步的问题问用户，不要拿 ID 充当名称。
   - `SOLVER_CONTEXT.production_condition` 固定为 `NO_JET`（不生产航煤），这是 Consumer 求解引擎唯一支持的工况，不要换成文档或业务知识里的工况名称。文档表明本场景生产航煤时，停下来告诉用户当前求解引擎不支持该工况。
   - `SOLVER_CONTEXT.yield_unit` 填 `percent`（收率属性是百分数，如 30 表示 30%）或 `fraction`（小数，如 0.3 表示 30%）。按对象文档的字段说明判断；文档没有写明时问用户，不要按 mock 数据或常识猜。
   - `ATMOS_DIESEL_FEED`、`ATMOS_HC_FEED`、`SOLVER_CONTEXT.route_stream_products` 三组产品名互不重叠，且每个名字在收率表里都要有对应行。
   - `CORRECTION_FACTORS` 每个来源的系数名必须与修正前缀对应，见模板注释。
   - docstring 的「关联operation」「输入参数」保持「无」：评估器由求解契约引用，不是 operation 调用的脚本。可以在「功能描述」中注明对应的 solver_key。
3. 文档和业务知识里都找不到的值，先 `acquire solver-evaluator confirm`，汇总成**一个编号列表一次性问用户**，注明每项的用途和取值形式（如"二次加工单元在 vgo_ht_unit 表中的 VGOUnitID 取值"）。用户回答前，对应位置保留占位符。

## Step 4: 自检（确定性，必做）

1. 运行契约自检，直到没有错误：
   ```bash
   python3 .claude/scripts/solver_evaluator_contract.py {TARGET} --ontology-dir ontology
   ```
   - 退出码 0：没有错误；1：按每条 `[错误]` 修正后重跑；2：读取失败或 TARGET 是旧式评估器，停止并汇报。
   - 「仍有未填写的模板占位符」：回到 Step 3 第 3 步问用户，不要自己编值。
   - ID 属性是数值类型、lookup 的 `match` 两边类型不同，都是 `[错误]`：问题在对象文档或导入数据，不在评估器。停下来把这几条交给用户，不要为消除错误改用别的属性。
   - `[警告]`（如未找到对象目录、未核对引用）不阻断，但要在 Step 5 原样告诉用户。
   - 同一错误修正两轮仍未消除时停下来，把错误清单交给用户裁决。
2. 运行脚本契约检查，确认 TARGET 没有 `脚本契约` 类错误：
   ```bash
   python3 .claude/scripts/ontology_checker.py ontology/ --json --include-scripts
   ```

本命令的自检与「求解模型」面板的静态检查是同一份实现：这里通过，面板上就不会再报评估器结构问题。

## Step 5: 确认与提交

`python3 .claude/scripts/state_helper.py acquire solver-evaluator confirm`，then 展示（用户确认后切回 `start` 再提交）：

```
求解评估器已生成：{TARGET}（契约 {solver_key}）

取数映射：
- {逻辑表} ← {对象}（{已映射的逻辑列数} 列{；其中 {逻辑列} 经 {关联对象} 按 {match} 关联取得}）
...

关键参数：加工单元 ID、可选单元编号、能力项、修正系数来源数、生产工况、收率单位
自检警告：{无 / 逐条列出}

确认无误请回复 **确认**；需要修改请直接说明。
```

用户 **确认** 后提交（**新建替换** 时同时删除旧式评估器）：

```bash
git add {TARGET}
git rm {旧式评估器}   # 仅 新建替换
git commit -m "solver-evaluator: {solver_key}"
```

提交后执行 `release solver-evaluator`，并提示（不列下一步菜单）：

"评估器已保存。下一步请到「求解模型」面板对契约 {solver_key} 重新发起**验证**：评估器或它引用的对象文档（含 lookup 关联对象）改动后，已有的验证结果会过期，发布前需要重新验证。{若为 新建保留：请同时在面板中把评估器路径改为 {TARGET}。}"
