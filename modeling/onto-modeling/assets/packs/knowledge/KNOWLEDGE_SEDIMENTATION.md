<!-- KNOWLEDGE_SEDIMENTATION.md — 知识沉淀包的守则片段（详设 5.3：沉淀这一段从 core 拆出）。
     由 .claude/BUSINESS_KNOWLEDGE_SNIPPET.md 引用；本包停用后这个文件不在装配目录里，
     助手只应用已沉淀的知识，不再新增条目。不是子代理，不能派出。 -->

## 业务知识识别守则

在与用户对话过程中，**主动识别**以下两类专家信号并按既定流程沉淀：

**1. 纠错信号**：用户表示我之前写错或推断错时，例如使用「不对 / 你弄错了 / 应该是 / 其实 / 实际上」。
**2. 业务常识/规范信号**：用户主动给出领域规则时，例如「我们公司 / 我们这边 / 规则是 / 通常 / 必须 / 行业里 / 标准做法」。

### 执行前检查清单（每次 add 都必读）

调用 `knowledge_helper.py add` 之前在心里勾一遍：

- [ ] **冲突检查跑过了吗？** 已 dispatch `knowledge-conflict-agent` 并拿到 JSON 数组结论？
- [ ] **命令一字不差吗？** 下文「标准 add 调用」是唯一权威样板 —— 不要凭直觉重写参数名（不是 `--title/--content`，是 `--name/--body`；不是 `--confidence 高` 而是 `--confidence high`，但脚本也接受中文别名）。
- [ ] **错误流没被吞？** 命令末尾用 `2>&1`，**永远不要写 `2>/dev/null`** —— 一旦出错你需要看到提示。

任何一格没勾上就停下来补齐，不要先发命令再 `--help` 自纠正。

### 触发后的标准动作

任何 add 触发都走以下结构（两条入口、共用前置、统一落盘）：

1. **确认通路** — 命中信号词时，用 `.claude/INTERACTION_CONVENTIONS.md` 中「业务知识确认类」模板询问用户。
2. **快捷通路** — 用户主动指令「记一下：…」「记下这条规则」时跳过 step 1 的确认对话。
3. **两条通路都汇入 ↓** — 执行下文「Add 前置：冲突检查」。检查无阻断后再调用「标准 add 调用」。

### 标准 add 调用（直接复制并替换值，禁止改名或删字段）

⚠ 下面这段是脚本接受的**唯一权威调用形式**。**不要**改字段名（agent 直觉里的 `--title/--content` 是兼容别名，但请用规范名 `--name/--body` 避免漂移）。所有字段都必填，包括 `--after-conflict-check`。

```bash
python3 .claude/scripts/knowledge_helper.py add \
  --type domain-rule \
  --slug order-amount-manual-review \
  --name "大额订单强制人工审核" \
  --scope global \
  --scope operation:订单审核与分仓路由 \
  --confidence high \
  --source "ontology-extract-operation@2026-05-27T21:34" \
  --body "$(cat <<'BODY'
订单金额 >= 5000 元时，必须转人工审核，不可由系统自动通过下单校验。该规则优先于所有其他自动审核逻辑。

**Why:** 业务方对大额订单有合规风控要求（用户口述）。
**How to apply:** /ontology-extract-operation、/ontology-validate 在审核相关 operation 中将此作为硬约束遵循。
BODY
)" \
  --after-conflict-check none-found \
  2>&1
```

字段速查：

| 字段 | 值域 / 别名 |
|---|---|
| `--type` | `correction` / `convention` / `terminology` / `domain-rule` |
| `--slug` | kebab-case 或 pinyin，唯一标识 |
| `--name`（或 `--title`） | 一句话标题 |
| `--scope` | 可多次，`global` / `object:<name>` / `operation:<name>` 等 |
| `--confidence` | `high` / `medium` / `low`（也接受 `高` / `中` / `低` 中文别名） |
| `--source` | `{当前命令名}@{ISO 时间}` |
| `--body`（或 `--content`） | 正文（建议加 **Why:** 与 **How to apply:** 两行） |
| `--after-conflict-check` | **必填**：`none-found` / `duplicate-suggested` / `conflict-suggested` / `relations-only` / `skipped-with-reason`（最后一项还需 `--skip-reason "..."`） |

### Add 前置：冲突检查（所有 add 路径必经）

调用 `knowledge_helper.py add` 之前，**永远**先 dispatch `knowledge-conflict-agent`，无论是确认通路、「记一下」快捷通路、还是命令内部的自动沉淀路径。

**Dispatch 步骤：**

1. 构造 CANDIDATE（YAML 块），字段对齐 `.claude/agents/knowledge-conflict-agent.md`「输入」段：`name` / `type` / `slug` / `scopes` / `confidence` / `body` / `related`。
2. Read `.claude/agents/knowledge-conflict-agent.md`，dispatch as subagent，CANDIDATE 作为输入变量传入。
3. 等待返回的 JSON 数组。

**结果处理：**

- **空数组 `[]`** → 无重复 / 无冲突 / 无相关，直接走 add 调用，命令行带 `--after-conflict-check none-found`。
- **非空** → 按 severity 排序后向用户呈现：

   ```
   发现 {N} 条相关业务知识：

   1. ❌ [知识冲突] {existing_name}（slug: {existing_slug}）
      依据：{evidence}
      建议：{suggestion}
   2. ⚠️ [知识重复] ...
   3. 💡 [知识相关] ...

   请选择：
   - **覆盖** → 仅 relation=duplicate 且 suggestion=overwrite_or_revise 时可用，改走 `knowledge_helper.py revise` 替换既有条目
   - **合并** → main agent 撰写合并后的 body，仍走 `revise` 落盘到既有 slug
   - **仍新增** → 新增本条；若 agent 报告 `related`，把对方 slug 写进 candidate.related 做双向链接；add 时按主关系传 `--after-conflict-check duplicate-suggested` / `conflict-suggested` / `relations-only`
   - **取消** → 放弃本次 add，不落盘
   ```

- 用户选定后再执行对应命令（`add` / `revise` / 取消）。

> ⚠ 任何绕过此前置检查的 add 调用都视为流程缺陷。`knowledge-conflict-agent` 报告为空时也要执行 — 这是 add 路径的常规步骤,不是异常分支。
>
> 极端情况确实无法跑 conflict-agent（如脚本测试），才允许 `--after-conflict-check skipped-with-reason --skip-reason "..."`，落盘后会带 ⚠ 标记。

### 用户面前的措辞

仅用：**业务知识 / 专家经验 / 沉淀**。
禁止：memory / knowledge base / frontmatter / 数据库 / 索引文件 等技术术语。
