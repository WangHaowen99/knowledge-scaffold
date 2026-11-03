Scan all operation docs to identify which business flows (an operation together with the sub-operations it calls) are suitable for translation into executable Python scripts. Outputs a markdown report for human review before running `/ontology-codegen`.

> 本命令只读 + 写报告，不修改 ontology/ 文档、不 commit。是 `/ontology-codegen` 前的可选准备步骤。

## Step 0: 前置检查

这是**只读分析命令**，不需要锁。按 `.claude/STEP0_TEMPLATE.md` 的约定只做第 2 步（阶段前置）：

```bash
python3 .claude/scripts/state_helper.py check-prereq codegen-scan
```

- 输出 `ok` → 继续。
- 输出 `missing:逻辑已提取` → **跳步守卫**："当前缺少逻辑文档，扫描结果会是空的。"选项 **继续** / **先做 逻辑提取**（/ontology-extract-operation）。

> 不调用 `check-lock` / `acquire` / `release`：本命令不写文档（仅生成只读报告到 `reports/`），不进入阶段状态，不更新 progress。

## Step 1: 路径变量

```bash
OPERATION_DIR="ontology/operation"
OBJECT_DIR="ontology/object"
SCRIPTS_DIR="ontology/scripts"
```

## Step 2: 加载业务知识（global）

```bash
KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load --scope global)
```

若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

> 本命令扫全量 operation、不预选 scope，故只注入 `global` 知识。具体 operation / object 的脚本化约束（如 SQL 形态）会在 `/ontology-codegen` 真正生成时按 scope 再次加载。

## Step 3: 派发 script-candidate-agent

Read `.claude/agents/script-candidate-agent.md`. Dispatch it as a subagent。

计算 `REPORT_PATH`：

```bash
TS=$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S)
REPORT_PATH="reports/${TS}-codegen-candidates.md"
mkdir -p reports
```

在 dispatch prompt 顶部（在任何输入变量之前）插入：

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}
```

变量（按行写入 prompt 主体）：

```
OPERATION_DIR=ontology/operation/
OBJECT_DIR=ontology/object/
SCRIPTS_DIR=ontology/scripts/
REPORT_PATH=<上面计算出的 REPORT_PATH>
```

在 dispatch prompt 底部追加：

```

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for the agent to return a JSON array AND write the markdown report to `REPORT_PATH`。

## Step 4: 呈现摘要 + 下一步

按 agent 返回的 JSON 数组统计：

```
n_total  = len(array)
n_yes    = count(verdict == "适合")
n_partial = count(verdict == "部分适合")
n_no     = count(verdict == "不适合")
n_done   = count(len(existing_scripts) > 0)
```

输出：

```
脚本化候选扫描完成，结果已记入候选报告。

共扫描 {n_total} 条逻辑：
- ✅ 适合：{n_yes}
- ⚠️ 部分适合：{n_partial}
- ❌ 不适合：{n_no}
- 📦 已生成（跳过）：{n_done}

报告按流程给出候选：每个 ✅ 候选是一个要生成脚本的逻辑，带编号、覆盖的子操作、输入输出概要与判定理由；⚠️ 部分适合给出拆分建议，要先拆成子操作再生成。

要生成哪些，直接说编号或逻辑名。
```

不列下一步菜单（界面的建议条会给出）。用户之后在对话里说「打开」「生成 全部」「生成 1,3」（报告编号，支持 `1-5`）或「生成 {逻辑名}」，按下面的分支做。

On **打开**：执行 `open {REPORT_PATH}`（macOS）。

On **生成 …**：分三步处理。

**Step A. 翻译用户输入为 operation 名列表**：

| 用户输入形态 | 翻译做法 |
|---|---|
| `全部` | 取 agent JSON 中 `verdict == 适合` 的所有条目的 `operation` |
| 编号（`1,3` / `1-5` / 混合）| 按 `id` 字段查找对应条目（编号即 agent 返回的 `id`）；非法编号回 "编号 X 不存在，已忽略"，继续处理其余 |
| operation 名 | 原样保留，跳过翻译 |
| `operation名:步骤区间` | 提示"片段不再单独生成脚本，请先把这几步拆成子操作"，跳过该项 |

每条选中条目输出它的 `operation`（`covers` / `standalone` 里的子操作由 `/ontology-codegen` 按流程一并处理，不单独列出）。把结果去重后按空格拼接得到 `ARGS`，例如 `订单审核 交易对账`。

> 编号混合 operation 名也可：`生成 1 交易对账 3` 会先翻译 `1` 和 `3`，把 `交易对账` 原样保留，结果去重。

**Step B. 路径分支：**

- **用户输入是 `全部`** → 复述展开后的清单**等二次确认**：

  ```
  即将对以下 {N} 个流程生成代码：

  - 订单审核（覆盖：库存校验；折扣计算 单独脚本）
  - 交易对账
  ...

  - 回复 **确认** → 进入 /ontology-codegen
  - 回复 **改选 1,3** → 改用编号/operation 名重新选（按 Step A 重新翻译）
  ```

  - 回 **确认** → 进入 Step C。
  - 回 **改选 ...** → 把后续参数当作新一轮"生成 ..."重新走 Step A-B。

- **用户输入是 编号 / operation 名** → 复述一行 `即将生成：{ARGS}` 作信息提示，不需要二次确认，直接进入 Step C。理由：用户已经显式挑了具体项，无需再问。

**Step C. 转交 codegen**：

Read `.claude/commands/ontology-codegen.md` and follow it with `$ARGUMENTS = ARGS`。`/ontology-codegen` 的 Step 2 会把子操作换算到它所属流程的主脚本。

## Note

本命令不修改任何文档，不做 commit。仅生成只读报告，供人工评估后决定是否进入 `/ontology-codegen`。
