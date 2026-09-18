Generate Python scripts from verified logic documents. Scope (if any): $ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `codegen`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock codegen`
2. `python3 .claude/scripts/state_helper.py check-prereq codegen`
   - `missing:逻辑已提取` → **跳步守卫**："继续会导致没有逻辑可生成代码"。选项 **继续** / **先做 逻辑提取**（/ontology-extract-logic）。

   > codegen 只要求 `逻辑已提取`。若逻辑描述中包含外部副作用，只在生成代码中保留注释说明，不再生成单独函数桩。
3. `python3 .claude/scripts/state_helper.py acquire codegen start`

4. 加载已沉淀业务知识。SCOPE_HINTS = `global` + codegen 范围内的每个 logic + 这些 logic 引用的对象（从各 logic 文档的 `# 与object的关联` section 解析）：
   ```bash
   SCOPE_ARGS=(--scope global)
   for logic in $TARGET_LOGICS; do
       SCOPE_ARGS+=(--scope "logic:$logic")
   done
   for obj in $REFERENCED_OBJECTS; do
       SCOPE_ARGS+=(--scope "object:$obj")
   done
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

   > 注：
   > - TARGET_LOGICS 与 REFERENCED_OBJECTS 在确定 codegen scope 后才能填入；本 load 实际在 **Step 2**（解析 scope）之后执行。
   > - `TARGET_LOGICS` 是 Step 2 解析出的 `(logic, fragment_scope)` 元组列表**按 logic 名去重**后的集合——多个片段共享一条 logic 时只算一次 `--scope logic:活动归因`。
   > - `REFERENCED_OBJECTS` 是 TARGET_LOGICS 内所有 logic 文档的 `# 与object的关联` section 中 `[[...]]` 引用的对象并集，同样去重。

额外推荐（非强制）：检查 `reports/` 最近是否有未处理的 `[ ]`，若有，提示"建议先跑 /ontology-validate 处理完问题后再生成代码"。

命令结束前 `release codegen` 并 `update-progress`。

## 业务知识识别 & 应用（贯穿整个命令）

整个 codegen 命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。

本命令以 main agent 直接执行为主（无 sub-agent dispatch）。Step 3-4 生成 Python 代码时，**必须**先扫描 Step 0 加载的 `KNOWLEDGE_BLOCK`：
- 置信度 `高` → **硬约束**：SQL/字段处理/枚举/分隔符/单位等实现细节必须符合（如 `';'` 边界匹配、双轨 plain+JSON 过滤等）；
- 置信度 `中` / `低` → 在脚本注释中显式标注以便后续核对；
- 若 logic 文档与高置信知识条目冲突，停下来汇报冲突给用户裁决，不要静默照搬 logic 文档。

## Step 1: Check prerequisites

List files in `ontology/logic/`. If empty: "未找到逻辑文档，请先完成提取和校验流程。" Stop.

## Step 2: Determine scope

**强制规则**：codegen 必须显式指定目标 logic 或片段，**不接受"对全部 logic 一键生成"**。理由：盲跑会把不适合脚本化的 logic（人工决策、政策陈述等）也生成空壳脚本，污染 `ontology/scripts/`。要批量处理请先走 `/ontology-codegen-scan` 由人工评审候选。

每个 scope 项是一个 `(logic, fragment_scope)` 元组：

- 纯 logic 名 `客群聚类` → fragment_scope = `整段`
- `logic名:步骤区间` → fragment_scope 取冒号后内容；合法形式：`步骤N` / `步骤N-M` / `步骤N,M`（步骤号必须能在 logic 文档「# 逻辑描述」中找到）

**If $ARGUMENTS provided:**
按空格切分；每段按上述规则解析。逐项 resolve 到 `ontology/logic/{logic}.md`，缺失文件或非法 fragment_scope 给警告并跳过。

例：`/ontology-codegen 客群聚类 活动归因:步骤1-3 交易对账` →
- (客群聚类, 整段)
- (活动归因, 步骤1-3)
- (交易对账, 整段)

> 若 `$ARGUMENTS` 仅包含 `全部` / `all` / `*` 等通配词，**拒绝并提示**："代码生成不接受全量目标，请改用 `/ontology-codegen 逻辑1 逻辑2` 形式，或先跑 `/ontology-codegen-scan` 评估候选。"

**If $ARGUMENTS empty:**
List all logic files and ask:
"发现 {N} 个逻辑文档。请指定要生成代码的范围（不支持一键全部）：

- 回复 **扫描**（/ontology-codegen-scan）→ 先扫一遍候选清单挑（推荐）
- 回复 **选择 逻辑1 逻辑2:步骤1-3** → 只生成指定逻辑/片段的代码（片段语法见上）
"

**Handler 路径：**

- **扫描** → `python3 .claude/scripts/state_helper.py release codegen`，然后 read `.claude/commands/ontology-codegen-scan.md` and follow its instructions（scan 走完会自动回到 codegen 并带回 `$ARGUMENTS`）。
- **选择 …** → 把 `选择` 后的内容当作 `$ARGUMENTS`，回到 Step 2 顶部的 "$ARGUMENTS provided" 分支重新解析。
- **取消** → `python3 .claude/scripts/state_helper.py release codegen`，Stop。
- **全部** / **all** / ***** → 复述强制规则并重新提示上述三选项，不进入生成流程。

> 若不确定哪些 logic 适合脚本化，`/ontology-codegen-scan` 会输出带编号的候选报告（含每个 logic 的片段范围、输入输出概要、判定理由）。

## Step 3: Read dependencies

For each logic doc in scope:
1. Read the logic document from `ontology/logic/{name}.md`
2. Extract referenced objects from `与object的关联` section
3. Read each referenced object from `ontology/object/`
4. If `ontology/data-mock/` has mock data, read it for test examples

## Step 4: Generate Python code

对 Step 2 解析出的每个 `(logic, fragment_scope)` 元组生成一个脚本：

**文件名规则**：

| fragment_scope | 文件名 |
|---|---|
| `整段` | `ontology/scripts/{逻辑英文名}.py` |
| `步骤N` | `ontology/scripts/{逻辑英文名}_step{N}.py` |
| `步骤N-M` | `ontology/scripts/{逻辑英文名}_steps{N}-{M}.py`（区间保留 `-`）|
| `步骤N1,N2,...` | `ontology/scripts/{逻辑英文名}_steps{N1}_{N2}_...py`（按用户输入顺序，每个数字用 `_` 拼接，不要排序）|

> 区间与列表用不同分隔符以避免冲突（`步骤1-3` → `_steps1-3.py`；`步骤1,3` → `_steps1_3.py`，不会撞）。同一条 logic 的多个片段脚本可以共存；与原"一 logic 一脚本"约定向后兼容（整段命名不变）。

**头部注释**：严格按 `.claude/templates/script-template.py` 模板填写，须包含：
- `关联logic`：本脚本对应的 logic 名称。整段填 logic 名（如 `客群聚类`）；片段填 `客群聚类 / 步骤2-4`（用 ` / 步骤X` 后缀标注覆盖范围）；无关联填"无"
- `功能描述`：一句话说明脚本做什么
- `详细逻辑`：对应原 logic 中被脚本实现的那段逻辑的展开描述（片段脚本只展开 fragment_scope 内的步骤）
- `用法`：CLI（`python3 ... --参数 值`）+ import 两种调用范例
- `输入参数`：每个参数的"名称 / 类型 / 必填或默认值"
- `输出`：return 结构 / 写入表 / 生成文件 等，含字段或结构说明
- `依赖`：所需 pip 包，无则删除本行

**主体结构**：
```python
"""<按模板填写的头部注释>"""
from __future__ import annotations
import argparse
# 其他 import

# --- 数据结构（来自引用的 object，可选）---
@dataclass
class {ObjectEnglishName}:
    """对象描述"""
    {attribute_english}: {python_type}  # {中文名称}: {描述}

# --- 主函数：使用普通形参，便于被其他脚本 import 调用 ---
def {logic_english_name}(arg1: T, arg2: T | None = None) -> {ReturnType}:
    # {步骤 1 from 逻辑描述}
    ...
    return result

# --- CLI 入口：纯库脚本可删除 _cli 与下方 __main__ 块 ---
def _cli() -> None:
    parser = argparse.ArgumentParser(description="{一句话业务描述}")
    parser.add_argument("--arg1", required=True, help="...")
    parser.add_argument("--arg2", default=None, help="...")
    {logic_english_name}(**vars(parser.parse_args()))

if __name__ == "__main__":
    _cli()
```

**列表/对象类入参**（如 `list[dict]`、SKU 明细等）：禁用 JSON 串作为 CLI value（shell 引号转义易错）。改用 `action="append"` + 紧凑分隔符在 `_cli()` 里组装：

```python
parser.add_argument(
    "--sku", action="append", required=True, metavar="SKU_ID:数量",
    help="可重复，例：--sku SKU-3001:2 --sku SKU-3008:1",
)
args = parser.parse_args()
sku_details = [
    {"sku_id": s.split(":")[0], "quantity": int(s.split(":")[1])}
    for s in args.sku
]
{logic_english_name}(order_id=args.order_id, sku_details=sku_details)
```

> 真有大批量结构化入参（如 100+ 条），用 `--input-file path.json` 由脚本读文件，而不是把 JSON 塞进 CLI value。

**Type mapping:**
- 字符串 → str
- 数字 → int or float
- 布尔 → bool
- 枚举 → Enum class
- 日期 → str (ISO format)
- 对象引用 → the corresponding dataclass

**Logic translation:**
- 判断 steps → if/elif/else
- 调用子流程 → function calls
- 循环 → for/while loops
- 异常处理 → try/except

### Step 4.2: 改写对应 logic 文档（每个脚本必做，不可跳过）

> **每写完一个 `.py` 必须紧接着改对应 `ontology/logic/{name}.md` 的 `# 逻辑描述` 段**——
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
N. 调用脚本 `ontology/scripts/{script}.py` 执行 {核心操作}，将结果 {写入 xxx / 返回 xxx}。

**调用示例：**

\`\`\`bash
python3 ontology/scripts/{script}.py --参数1 值1 --参数2 值2
\`\`\`
```

规则：
- 业务步骤保留高层语义，**不重复**脚本内部实现细节。哪怕用了业务词汇（"库存"/"SKU"），只要出现字段级算式或内部循环就算未简化。
- 片段脚本（仅覆盖 logic 的部分步骤）：只把被覆盖的那几条改为「调用脚本 ...」，其余步骤保持原文。
- 调用示例使用实际业务场景中的典型参数值；脚本头部注释里的「输入参数」段是参数定义的单一来源，logic 文档不再重复字段表。

### Step 4.3: 默认值与双语支持（按需）

- 若参数有合理默认值，在脚本中定义为模块级常量（如 `DEFAULT_CLUSTERING_FACTORS`），并在 argparse 的 `default=` 中引用。
- 若参数支持中文名，提供 `{FIELD}_NAME_MAP` 映射字典，并在入口函数中统一归一化为内部英文字段名。

---

## Step 4.5: 代码复审

Read `.claude/agents/codegen-review-agent.md`. Dispatch it as a subagent.

在 dispatch prompt 顶部（在任何输入变量之前）插入：

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}
```

变量：
- `TARGET_SCRIPTS`: Step 4 中生成或修改的 `ontology/scripts/*.py` 文件相对路径（空格分隔）
- `RELATED_LOGICS`: 对应 `ontology/logic/*.md` 文件相对路径（空格分隔）

在 dispatch prompt 底部（在已有的收尾指令之后）追加：

```

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for it to return a JSON array.

### 4.5.1: 处理错误级 issue（自动修复 + 单轮回归）

按 `severity=错误` 过滤 issue。由 main agent 直接修复：

- `头部缺失` / `头部字段错误` → 按 `.claude/templates/script-template.py` 补齐 7 字段
- `CLI不规范` → 替换为 argparse + `--参数名` 形式；移除 JSON 串参数
- `骨架不规范` → 按模板补 `from __future__ import annotations` / `_cli()` 等
- `logic未简化` → 改写对应 `ontology/logic/{name}.md` 的「逻辑描述」段（按 Step 4.2 执行）。本应在 Step 4.2 生成时已完成；如果跑到这里说明 Step 4.2 漏做了，现在补
- `业务知识冲突` → 按 KNOWLEDGE_BLOCK 中对应高置信条目调整脚本（如 `';'` 边界匹配 SQL）

修复后**重新 dispatch codegen-review-agent**（同 TARGET_SCRIPTS / RELATED_LOGICS）做一次回归。若回归后仍有 `severity=错误` 且与上一轮相同（按 `file + message` 精确配对），停下来汇报：

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
- **跳过**：保留现状，继续 Step 5。

### 4.5.3: 无问题 / 处理完毕

首轮 agent 返回 `[]`，或 4.5.1 + 4.5.2 处理完毕后无遗留 `错误` → 直接进入 Step 5。

---

## Step 5: Present results

Show list of generated files（示例同时含整段与片段两种命名）：
"代码生成完成：
- ontology/scripts/客群聚类.py                  ← 整段
- ontology/scripts/活动归因_steps1-3.py          ← 片段（步骤1-3）
- ontology/scripts/风控审批_steps2_4.py          ← 片段（步骤2,4）
...

共 {N} 个文件。如需修改请说明；确认无误请回复 **确认**。"

## Step 6: Commit

```bash
git add ontology/scripts/ ontology/logic/
git commit -m "codegen: {TOPIC}"
```

> Step 4.5.1 可能改写 `ontology/logic/*.md` 的「逻辑描述」段（简化为业务步骤 + 调用示例），故一并纳入 `git add`。

## Step 7: Suggest next step

"代码生成完成，已保存至 `ontology/scripts/`。

**下一步建议：**
- 回复 **追踪**（/ontology-trace）→ 用场景进一步验证逻辑
- 回复 **整体视图**（/ontology-overview）→ 查看全局关系
- 回复 **扫描候选**（/ontology-codegen-scan）→ 看还有哪些逻辑没生成
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。"
