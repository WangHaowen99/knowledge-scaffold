Update an already-built ontology based on a reference (free-form prompt text or a file path). Reference input: $ARGUMENTS

> 流程总览：解析参考内容 → 影响范围分析（含级联）→ 用户确认 → 执行更新 → 规则+语义校验。

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `update`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock update`
   - `locked:{ts}:{phase}` → 按 **错误恢复类** 提示；phase 可能是 `analyze` / `confirm` / `apply` / `validate`，**续执**时跳到对应阶段。
2. `python3 .claude/scripts/state_helper.py check-prereq update`
   - `missing:逻辑已提取,...` → 按 **跳步守卫** 提示："当前缺少 {阶段列表}，没有可更新的 ontology。" 选项 **先做 逻辑提取**（/ontology-extract-operation）。
3. `python3 .claude/scripts/state_helper.py acquire update analyze`
4. 加载已沉淀业务知识（全量 + 已存在对象/逻辑 scope）：

   ```bash
   SCOPE_ARGS=(--scope global)
   for f in ontology/object/*.md ontology/operation/*.md; do
       [ -f "$f" ] || continue
       stem=$(basename "$f" .md)
       case "$f" in
           */object/*) SCOPE_ARGS+=(--scope "object:$stem") ;;
           */operation/*)   SCOPE_ARGS+=(--scope "operation:$stem") ;;
       esac
   done
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

进入新阶段时同步切换锁：`acquire update {analyze|confirm|apply|validate}`。

命令结束前 `release update`。

## 业务知识识别（贯穿整个命令）

整个 update 命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。
update-impact-agent / update-apply-agent 的 prompt 都需 transclude 该文件全文。

---

## 按已确认的变更清单执行（页面发来的 `change_list_id=…`）

`$ARGUMENTS` 里带 `change_list_id=<清单>` 时，是用户在页面上逐条确认过的变更点清单（平台写在
`.onto/run/change-lists/<清单>.json`，字段：`points[]` 的 `scope`（要改的文件）、`value`（变更说明）、`condition`
（依据）、`state`（`accepted` 照原说明改，`rewritten` 按 `rewritten_text` 改）、`impact`（会连带影响的条目））：

1. 读这份清单，**跳过 Step 1～3**（影响分析与确认已经在页面上做完），把每个变更点当成 Step 4 的一个直接变更条目；
   `acquire update apply` 后进入 Step 4，`UPDATE_BASE=$(git rev-parse HEAD)`；
2. 同时带了 `fix_scenarios=[…]`（回归结果卡上「交给助手修」）：清单已经执行过，只针对这些场景的回归失败修改相关条目，
   改完对这些场景重新推演（若启用了场景走查能力），然后进入 Step 5 只校验本次改动；
3. 收尾同 Step 6。清单文件由平台管理，不要修改它。

## Step 1: 解析参考内容 $ARGUMENTS

`$ARGUMENTS` 的几种情况：

- **空** → 提示用户：
  ```
  请输入参考内容（更新依据），任选一种：
  - 直接粘贴提示词文本（如"客户对象增加手机号字段，必填"）
  - 输入文件路径（如 `notes/2026-05-20-客户字段补充.md`、`research/最新资料分析.md`）
  ```
  等待用户输入后继续。

- **非空** → 判定是路径还是内联文本：
  1. 若把 `$ARGUMENTS` 当作路径调用 `test -f`，文件存在 → `REFERENCE_KIND = file`，`REFERENCE_PATH = $ARGUMENTS`，读取整份文件作为 `REFERENCE_CONTENT`。
  2. 否则 → `REFERENCE_KIND = inline`，`REFERENCE_CONTENT = $ARGUMENTS` 原文。

如果 `REFERENCE_KIND = file` 但路径在 `ontology/`、`reports/`、`.claude/`、`.ontology-state/` 之下，弹一个警告：
```
⚠️ 参考路径位于工具目录内部 ({REFERENCE_PATH})，确认这是更新依据而不是要改的文档？
- 回复 **继续** → 继续把它当参考
```

向用户回显一行摘要：
```
更新依据：{REFERENCE_KIND}  |  长度：{N} 字符  |  {若 file，附路径}
```

---

## Step 2: 影响范围分析

`acquire update analyze`。

Read `.claude/agents/update-impact-agent.md`. Dispatch it as a subagent with：

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

REFERENCE_KIND: {file 或 inline}
REFERENCE_PATH: {若 file，写路径；否则省略此行}
REFERENCE_CONTENT: |
{把 REFERENCE_CONTENT 整段内嵌进来，前面加 2 个空格缩进}

OBJECTS_DIR: ontology/object/
OPERATION_DIR: ontology/operation/
MOCK_DIR: ontology/data-mock/
TESTCASE_DIR: ontology/testcase/
SCRIPTS_DIR: ontology/scripts/

请按照你的指令完成影响分析，返回 JSON 数组。

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for it to return a JSON array of impact entries. 每条结构：

```json
{
  "file": "ontology/object/客户.md",
  "kind": "object",
  "operation": "modify",
  "change_summary": "新增字段「手机号」并标记必填",
  "reason": "参考内容明确要求保留客户手机号用于触达",
  "cascading": [
    {"file": "ontology/data-mock/客户.md", "needs_change": true, "reason": "测试数据需补一列"},
    {"file": "ontology/operation/客群聚类.md", "needs_change": false, "reason": "引用客户对象但未涉及该字段"}
  ]
}
```

- `operation ∈ {modify, create, delete}`
- `kind ∈ {object, operation, mock, testcase, script}`
- `cascading[].needs_change` = `true` 表示该级联文件也需要改动，会出现在变更清单里；`false` 仅作为提醒。
- `kind = script` 指 `ontology/scripts/*.py`：脚本与某个 operation 通过头部 docstring 的「关联operation」字段或首行 operation 名称绑定。若该 operation 的改动会影响脚本（如输入/输出参数变化、字段重命名、逻辑步骤改写），就出现一条 `kind=script` 的 modify 条目。纯数据结构变化（如 operation 文档措辞调整）不必涉及脚本。
  - 子操作（「可单独调用」为否）没有自己的脚本，它的逻辑写在所属主操作的脚本里：改了这类子操作的逻辑、输入或输出，要修改的是主操作的脚本（按「被operation调用」找到有脚本的上级）。
  - 改了「与operation的关联」（增删子操作、改「可单独调用」）也会改变哪个脚本覆盖哪一步，按 `/ontology-codegen` Step 2「按流程生成」同步合并或拆分脚本。

把 agent 返回的原始 JSON 写入 `.ontology-state/update-plan.json`，方便续执。

### 2.1 用 impact_analysis.py 补全级联

对每个 `operation != create` 的根目标，跑一次只读分析以补全级联清单（防止 agent 漏掉）：

```bash
python3 .claude/scripts/impact_analysis.py "{ontology-relative-path}" --json
```

把脚本返回里的 `operation_refs / called_by / object_refs / mock_refs / testcase_refs` 中**尚未出现在 agent cascading 里**的条目，补成一条 `{"file": "...", "needs_change": false, "reason": "downstream reference (impact_analysis)"}`。这一步纯静态字面引用，作用是兜底，不替换 agent 的语义判断。

---

## Step 3: 展示变更清单并确认

把 plan 整理为人类可读的表格：

```
影响分析完成。共 {ROOT} 个直接变更，{CASCADE} 个级联文件。

### 直接变更
| # | 文件 | 操作 | 变更说明 |
|---|------|------|----------|
| 1 | ontology/object/客户.md | 修改 | 新增字段「手机号」（必填） |
| 2 | ontology/operation/客群聚类.md | 修改 | 输入对象关联补充「手机号」字段引用 |
| 3 | ontology/object/手机号触达记录.md | 新建 | 参考内容中提到的新对象 |
| 4 | ontology/scripts/cluster.py | 脚本 | `cluster()` 入参表同步新增 `phone` 字段说明 |

### 级联提醒（不一定要改，仅供参考）
- ontology/data-mock/客户.md：测试数据可能需要补一列 → 建议跑 /ontology-mock 重新生成
- ontology/testcase/客群聚类.md：追踪场景包含该 operation，建议跑 /ontology-trace 回归

### 风险提示
{若有删除操作：警告该文件被以下 N 个文档引用，删除前请确认}

**下一步：**
- 回复 **确认** → 按清单执行更新
- 回复 **编辑** → 让你逐条调整清单（增删条目或改变更说明）
```

Handle reply:
- **确认** → `acquire update apply`，进入 Step 4。
- **编辑** → 进入子循环：
  ```
  请按以下任一格式回复（可多条）：
  - 删除 <编号>
  - 改 <编号>: <新的变更说明>
  - 新增: <ontology 相对路径> | <modify|create|delete> | <变更说明>
  调整完后直接说明已改完即可。
  ```
  逐条 patch 内存里的 plan，最后重新打印清单再次确认。
- **取消** → `release update` 后 Stop。

---

## Step 4: 执行更新

`acquire update apply`。

先记下改动前的版本，供 Step 5 只校验本次改动：`UPDATE_BASE=$(git rev-parse HEAD)`，并写入 `.ontology-state/update-plan.json` 的 `base_commit`（续执时从这里读回）。

按 plan 中 `operation != cascading-only` 的条目（即出现在「直接变更」表格里的）逐个执行。

Read `.claude/agents/update-apply-agent.md`. Dispatch it as a subagent with：

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

REFERENCE_KIND: {file 或 inline}
REFERENCE_PATH: {若 file，写路径；否则省略此行}
REFERENCE_CONTENT: |
{把 REFERENCE_CONTENT 整段内嵌进来}

PLAN: |
{原样粘贴最终确认的 plan JSON}

TEMPLATES_DIR: .claude/templates/

请按照 PLAN 逐条修改/新建/删除文件，并返回执行结果。

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for completion. Display 执行摘要：

```
更新完成：
- 修改 {N1} 个文件：{filenames}
- 新建 {N2} 个文件：{filenames}
- 删除 {N3} 个文件：{filenames}
- 跳过 {N4} 个：{filenames}（原因：...）
```

把变更暂存（改动涉及 `ontology/scripts/*.py` 时先按 `/ontology-codegen` Step 4.6 用 `script_verify.py run` 跑通改过的脚本，再运行 `python3 .claude/scripts/script_descriptor.py render ontology` 刷新同名描述符，否则 pre-commit 会拦下）：

```bash
git add ontology/
```

（先不 commit；校验阶段如果还要再改，一起提交。）

---

## Step 5: 自动跑规则+语义校验

`acquire update validate`。

Tell user: "更新已写入，自动进入规则+语义校验……"

Read `.claude/commands/ontology-validate.md`. Follow it in **incremental mode** —— `$ARGUMENTS` 取 PLAN 中所有被实际改动的 **ontology 文档** 的 stem（即 `kind ∈ {object, operation}` 的条目；脚本/mock/testcase 不进 validate 增量范围，因为 ontology-validate 只校验 object/operation 两类文档）。stem 去掉 `ontology/{kind}/` 前缀和 `.md` 后缀，空格分隔，末尾追加 `--since {UPDATE_BASE}`，传给 ontology-validate 的 Step 0.5。这样语义检查只看本次改动及其牵连，改动前就有的问题单独列出、默认不处理。

若 PLAN 中 **没有任何** ontology 文档被改动（极端情况：只改了脚本），跳过 validate，直接进入 Step 6 提交。

> 注：ontology-validate 自身会 acquire/release 它自己的 `validate` 锁；与本命令的 `update` 锁互不冲突。

如果用户在 ontology-validate 的某一步选了 **取消**，回到本命令把当前 plan + 已执行结果保存到 `.ontology-state/update-plan.json`，然后 `release update`。

---

## Step 6: 提交与收尾

ontology-validate 内部已经在规则修复 / 语义修复后各做了 commit（`fix: address rule check issues` / `fix: address semantic check issues`），但**本次 update 的首次落盘**还没有专门的 commit。

校验完全跑完（不论是否有修复），如果 `git status` 仍显示有未提交的 ontology 改动（来自 Step 4 但还没被任何 fix commit 吃掉的部分），按以下格式提交：

```bash
git add ontology/
git commit -m "$(cat <<'EOF'
update: apply reference changes

{若 REFERENCE_KIND=file：参考来源：{REFERENCE_PATH}}
{若 inline：参考来源：内联提示词}
EOF
)"
```

如果 `git status` 已经没有 ontology 待提交项（说明全部改动已经合并进 validate 的 fix commit），跳过本次 commit。

`release update`。

最后展示：

```
已保存版本 {N}: update {简短描述}
如需回退到上一版本，回复"回退"

**下一步建议：**
- 回复 **追踪**（/ontology-trace）→ 对受影响的 operation 重新走场景
- 回复 **测试数据**（/ontology-mock）→ 若影响了对象结构，重新生成 mock
- 回复 **代码生成**（/ontology-codegen）→ 若 operation 变化较大或 update 跳过了脚本修改（change_summary 过于宽泛），重新生成对应脚本
- 回复 **整体视图**（/ontology-overview）→ 看更新后的全局
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle reply by reading the corresponding command file.

---

## Note on 续执（resume）

若 Step 0 检查到 `locked:{ts}:{phase}` 且用户选了 **续执**：
- `phase = analyze` → 重新跑 Step 2（agent 是无状态的，重做一次即可）
- `phase = confirm` → 读 `.ontology-state/update-plan.json`，重新展示清单等用户确认
- `phase = apply` → 读 plan，跳过已成功执行的条目（按 git status 推断），继续剩余条目；`UPDATE_BASE` 取 plan 的 `base_commit`，不要重新取 HEAD
- `phase = validate` → 直接读 plan 中的 changed stems 进 ontology-validate，同样带上 `--since {base_commit}`
