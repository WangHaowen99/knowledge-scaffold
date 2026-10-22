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
   > - `TARGET_OPERATIONS` 是 Step 2 解析出的 `(operation, fragment_scope)` 元组列表**按 operation 名去重**后的集合——多个片段共享一条 operation 时只算一次 `--scope operation:活动归因`。
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

**强制规则**：codegen 必须显式指定目标 operation 或片段，**不接受"对全部 operation 一键生成"**。理由：盲跑会把不适合脚本化的 operation（人工决策、政策陈述等）也生成空壳脚本，污染 `ontology/scripts/`。要批量处理请先走 `/ontology-codegen-scan` 由人工评审候选。

每个 scope 项是一个 `(operation, fragment_scope)` 元组：

- 纯 operation 名 `客群聚类` → fragment_scope = `整段`
- `operation名:步骤区间` → fragment_scope 取冒号后内容；合法形式：`步骤N` / `步骤N-M` / `步骤N,M`（步骤号必须能在 operation 文档「# 逻辑描述」中找到）

**If $ARGUMENTS provided:**
按空格切分；每段按上述规则解析。逐项 resolve 到 `ontology/operation/{operation}.md`，缺失文件或非法 fragment_scope 给警告并跳过。

例：`/ontology-codegen 客群聚类 活动归因:步骤1-3 交易对账` →
- (客群聚类, 整段)
- (活动归因, 步骤1-3)
- (交易对账, 整段)

> 若 `$ARGUMENTS` 仅包含 `全部` / `all` / `*` 等通配词，**拒绝并提示**："代码生成不接受全量目标，请改用 `/ontology-codegen 逻辑1 逻辑2` 形式，或先跑 `/ontology-codegen-scan` 评估候选。"

**If $ARGUMENTS empty:**
List all operation files and ask:
"发现 {N} 个逻辑文档。请指定要生成代码的范围（不支持一键全部）：

- 回复 **扫描**（/ontology-codegen-scan）→ 先扫一遍候选清单挑（推荐）
- 回复 **选择 逻辑1 逻辑2:步骤1-3** → 只生成指定逻辑/片段的代码（片段语法见上）
"

**Handler 路径：**

- **扫描** → `python3 .claude/scripts/state_helper.py release codegen`，然后 read `.claude/commands/ontology-codegen-scan.md` and follow its instructions（scan 走完会自动回到 codegen 并带回 `$ARGUMENTS`）。
- **选择 …** → 把 `选择` 后的内容当作 `$ARGUMENTS`，回到 Step 2 顶部的 "$ARGUMENTS provided" 分支重新解析。
- **取消** → `python3 .claude/scripts/state_helper.py release codegen`，Stop。
- **全部** / **all** / ***** → 复述强制规则并重新提示上述三选项，不进入生成流程。

> 若不确定哪些 operation 适合脚本化，`/ontology-codegen-scan` 会输出带编号的候选报告（含每个 operation 的片段范围、输入输出概要、判定理由）。

## Step 3: Read dependencies

For each operation doc in scope:
1. Read the operation document from `ontology/operation/{name}.md`
2. Extract referenced objects from `与object的关联` section
3. Read each referenced object from `ontology/object/`
4. If `ontology/data-mock/` has mock data, read it for test examples

## Step 4: Generate Python code

对 Step 2 解析出的每个 `(operation, fragment_scope)` 元组生成一个脚本：

**文件名规则**：

| fragment_scope | 文件名 |
|---|---|
| `整段` | `ontology/scripts/{逻辑英文名}.py` |
| `步骤N` | `ontology/scripts/{逻辑英文名}_step{N}.py` |
| `步骤N-M` | `ontology/scripts/{逻辑英文名}_steps{N}-{M}.py`（区间保留 `-`）|
| `步骤N1,N2,...` | `ontology/scripts/{逻辑英文名}_steps{N1}_{N2}_...py`（按用户输入顺序，每个数字用 `_` 拼接，不要排序）|

> 文件名就是知识包里的脚本 key：不超过 80 字符，不含 `/ \ # ? & |` 与 `..`。
>
> 区间与列表用不同分隔符以避免冲突（`步骤1-3` → `_steps1-3.py`；`步骤1,3` → `_steps1_3.py`，不会撞）。同一条 operation 的多个片段脚本可以共存；与原"一 operation 一脚本"约定向后兼容（整段命名不变）。

**头部注释**：严格按 `.claude/templates/script-template.py` 模板填写，须包含：
- `关联operation`：本脚本对应的 operation 名称。整段填 operation 名（如 `客群聚类`）；片段填 `客群聚类 / 步骤2-4`（用 ` / 步骤X` 后缀标注覆盖范围）。**要被评测或 consumer 执行的脚本必须绑定一个已存在的 operation**（`ontology/operation/` 下的文件名，不带 `.md`）：写了不存在的名字或填"无"，导出时都不会成为独立脚本，只留源码文件。"无"只用于被其他脚本 import 复用的辅助模块
- `功能描述`：一句话说明脚本做什么
- `详细逻辑`：对应原 operation 中被脚本实现的那段逻辑的展开描述（片段脚本只展开 fragment_scope 内的步骤）
- `用法`：CLI（`python3 ... --参数 值`）+ import 两种调用范例
- `输入参数`：只列 `_cli()` 中 argparse 声明的 CLI 参数，每个一行"名称 / 类型 / 必填或默认值"；仅供 import 调用的函数形参不写在这里，放到「用法」的 import 示例中说明。纯库脚本（无 argparse）本段填"无"
- `输出`：stdout 输出的 JSON 结构（即业务函数的 return 值），含字段说明；脚本不写表、不生成文件
- `依赖`：所需 pip 包，无则删除本行

**CLI 契约**：源码中 `argparse.add_argument()` 的实际声明是唯一契约来源。头部参数表只能描述这些声明，不能根据参数 key 自动推导 flag；长选项统一使用源码中声明的 `--kebab-case`（如 `--crude-name`），导出器会校验 flag、类型、`required`、默认值、`action` 和多值语义是否一致。CLI 必须把业务函数返回值序列化为单个 JSON 写到 stdout，诊断信息写 stderr。

- 头部「输入参数」与 argparse 声明**一一对应**：每个 `add_argument` 在头部有一行，头部也不能多出 argparse 没有的参数。函数形参多于 CLI 参数（如只在 import 时传入的记录列表）是允许的，但多出的形参不进头部参数表。
- 头部「类型」写 argparse 实际接收的类型，而不是函数形参类型：未写 `type=` 的写 `str`；`type=int` / `type=float` 写 `int` / `float`；`action="store_true"` 写 `bool`；`type=json.loads` 写 `json`。
- 布尔开关统一用 `parser.add_argument("--has-jet-fuel", action="store_true")`，头部写 `bool`、「默认 False」。不要写 `default="false"` 再在函数里解析字符串——那样 argparse 按 `str` 接收，头部就必须写 `str`。
- 必填：`required=True` 的参数头部说明含「必填」；其余写「默认 X」或不标必填。

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
# 其他 import

# --- 数据结构（来自引用的 object，可选）---
@dataclass
class {ObjectEnglishName}:
    """对象描述"""
    {attribute_english}: {python_type}  # {中文名称}: {描述}

# --- 主函数：使用普通形参，便于被其他脚本 import 调用 ---
def {operation_english_name}(arg1: T, arg2: T | None = None) -> {ReturnType}:
    # {步骤 1 from 逻辑描述}
    ...
    return result

# --- CLI 入口：纯库脚本可删除 _cli 与下方 __main__ 块 ---
def _cli() -> None:
    parser = argparse.ArgumentParser(description="{一句话业务描述}")
    parser.add_argument("--arg1", required=True, help="...")
    parser.add_argument("--arg2", default=None, help="...")
    {operation_english_name}(**vars(parser.parse_args()))

if __name__ == "__main__":
    _cli()
```

**列表/对象类入参**（如 `list[dict]`、SKU 明细等）：使用一个 JSON CLI value，与 Consumer 的结构化参数契约一致；不要把同一个参数拆成重复 flag。大批量数据仍可使用 `--input-file path.json`，但必须在头部和 operation 文档中声明。脚本优先接收调用方按查询边界筛选后的记录，只做纯计算；不得在脚本中通过表名、整表或无条件查询自行取数。

```python
parser.add_argument("--sku", required=True, type=str, help="JSON 数组，例如 [{\"sku_id\":\"sku-a\",\"quantity\":2}]")
args = parser.parse_args()
sku_details = json.loads(args.sku)
{operation_english_name}(order_id=args.order_id, sku_details=sku_details)
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
- 调用子流程 → function calls
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
- 片段脚本（仅覆盖 operation 的部分步骤）：只把被覆盖的那几条改为「调用脚本 ...」，其余步骤保持原文。
- 调用示例使用实际业务场景中的典型参数值；脚本头部注释里的「输入参数」段是参数定义的单一来源，operation 文档不再重复字段表。

### Step 4.3: 默认值与双语支持（按需）

- 若参数有合理默认值，在脚本中定义为模块级常量（如 `DEFAULT_CLUSTERING_FACTORS`），并在 argparse 的 `default=` 中引用。
- 若参数支持中文名，提供 `{FIELD}_NAME_MAP` 映射字典，并在入口函数中统一归一化为内部英文字段名。

### Step 4.4: 脚本契约规则检查（确定性，必做）

写完所有脚本、进入复审前，先跑与知识包导出同一实现的契约检查：

```bash
python3 .claude/scripts/ontology_checker.py ontology/ --json --include-scripts
```

过滤 `file` 在 `scripts/` 下、`category` 为 `脚本契约` / `脚本安全` / `脚本绑定` / `命名约束` 的 `错误` 级 issue，必须在这里清零：其中头部与 argparse 不一致的问题在导出/评测打包时会被同一实现拦截，安全写法与脚本绑定的口径见上方「脚本安全」与头部「关联operation」，其余（长选项、stdout 单个 JSON 等）是脚本被调用执行时依赖的约定。按上方规则修正头部、argparse 声明、源码写法或文件名，然后重跑本命令直到这些错误清零。`脚本绑定` 的「建议」（关联operation 为「无」）只需确认该脚本确实是辅助模块。其他类别的 issue 不在本步处理。

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
- `CLI不规范` → 替换为 argparse + 源码实际声明的 `--kebab-case` 形式；结构化参数使用一个 JSON value
- `骨架不规范` → 按模板补 `from __future__ import annotations` / `_cli()` 等
- `脚本安全` → 按 Step 4「脚本安全」去掉禁用写法：外部数据改由 CLI 参数传入，结果改为 stdout JSON
- `operation未简化` → 改写对应 `ontology/operation/{name}.md` 的「逻辑描述」段（按 Step 4.2 执行）。本应在 Step 4.2 生成时已完成；如果跑到这里说明 Step 4.2 漏做了，现在补
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
     {"name": "正常", "argv": ["--orders", "[{\"sku_id\":\"sku-a\",\"quantity\":2}]", "--quantity", "3"], "expect": "success"},
     {"name": "缺少必填参数", "argv": ["--quantity", "3"], "expect": "error"}
   ]
   ```

   - argv 按脚本头部「输入参数」里的 CLI 参数写，每一项都是字符串（数字和 JSON 也写成字符串）；
   - 覆盖正常、边界、缺少必填参数、未知参数、空结果、预期业务错误（没有对应场景的可以省略），至少一个 `expect: success`，最多 12 个；
   - `success`：退出码 0 且 stdout 是单个 JSON；`error`：非 0 退出（argparse 报错或业务错误）；
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

Show list of generated files（示例同时含整段与片段两种命名）：
"代码生成完成：
- ontology/scripts/客群聚类.py                  ← 整段
- ontology/scripts/活动归因_steps1-3.py          ← 片段（步骤1-3）
- ontology/scripts/风控审批_steps2_4.py          ← 片段（步骤2,4）
...

运行验证：{N} 个脚本全部通过（在 jiuwenbox 沙箱中 / jiuwenbox 不可用，在 onto-modeling 容器内）。

共 {N} 个文件。如需修改请说明；确认无误请回复 **确认**。"

## Step 6: Commit

```bash
git add ontology/scripts/ ontology/operation/
git commit -m "codegen: {TOPIC}"
```

> pre-commit 会拦下没有当前版本运行验证记录的脚本。Step 4.5.1 可能改写 `ontology/operation/*.md` 的「逻辑描述」段（简化为业务步骤 + 调用示例），故一并纳入 `git add`。

## Step 7: Suggest next step

"代码生成完成，已保存至 `ontology/scripts/`。

**下一步建议：**
- 回复 **追踪**（/ontology-trace）→ 用场景进一步验证逻辑
- 回复 **整体视图**（/ontology-overview）→ 查看全局关系
- 回复 **扫描候选**（/ontology-codegen-scan）→ 看还有哪些逻辑没生成
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。"
