根据数据评测结果优化知识包：读取评测报告的失分用例，归因到知识包缺陷，经专家确认后实施修订并留演进记录。目标评测轮（可选，报告文件名或留空取最新）：$ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `evolution`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock evolution`
2. `python3 .claude/scripts/state_helper.py check-prereq evolution`
   - `missing:...` → 跳步守卫："还没有可优化的本体产物"。选项 **先做对象提取**（/ontology-extract-object）/ **取消**。
3. `python3 .claude/scripts/state_helper.py acquire evolution start`
4. 业务知识按 Step 2 归因出的涉事对象增量加载（`knowledge_helper.py load --scope object:{stem}`），不在此处全量加载。

命令结束前 `release evolution` 并 `update-progress evolution_last_run {timestamp}`。

## Step 1: 定位评测轮与失分用例

1. 确定评测报告：
   - `$ARGUMENTS` 非空 → 使用 `reports/{$ARGUMENTS}`（容忍省略 `reports/` 前缀）；
   - 为空 → 取 `reports/eval-*.md` 中时间戳最新的一份。
   - 一份都没有 → 终止并提示："尚无评测报告——请先在左栏「数据评测」阶段运行一轮评测。" 释放锁后结束。
2. 读取 `.ontology-state/eval.json`（若存在）获取总分与轮次号，读取报告的「逐用例明细」，收集所有
   `⚠️ partial` / `❌ fail` / `🛑 error` 用例：问题、标准答案、智能体回答、判分理由。
3. 若全部用例均为 ✅ pass：告知"本轮评测无失分项，无需演进"，可选提议（不强制）："要不要我基于报告
   做一次预防性审视（别名覆盖、描述清晰度）？" 用户拒绝则释放锁结束。

## Step 2: 失分归因（诊断矩阵）

对每个失分用例，结合知识包现状（相关对象/操作/规则文档、`objects` 属性表、`ontology/testcase`）判断归因，
类别与判定要点：

| 类别 | 含义 | 典型信号 |
|---|---|---|
| A 知识缺失 | 问题涉及的对象/属性/操作/规则在知识包中不存在或不完整 | 回答"本体中没有定义…"、判分理由指出关键概念缺失 |
| B 语义模糊 | 概念存在但描述/别名不足，检索或理解失败 | 回答答非所问、用错对象；问题用词与文档措辞/别名对不上 |
| C 数据映射 | 数据问答类失分：来源Sheet、英文列名与评测库表结构不一致 | 回答"查询失败/无此表/无此列"；`check_eval_data.py` 有差异项 |
| D 用例问题 | 标准答案本身错误、过严或表述歧义 | 智能体回答实质正确但被判 fail；标准答案与资料矛盾 |
| E 运行异常 | 🛑 error：环境/凭据/服务问题，与知识包无关 | 判分理由为"运行失败/连接失败/无可用模型配置" |

输出**编号归因表**（遵循 INTERACTION_CONVENTIONS 编号列表话术）：

```
| # | 用例（截断） | 判分 | 归因 | 拟修订 |
|---|---|---|---|---|
| 1 | 配送单 D002 是否已送达？ | fail | C 数据映射 | 配送单.md 来源Sheet 改为实际表名 |
```

然后用 AskUserQuestion 卡请专家确认处理范围。**卡片被拒绝或超时未应答时：不做任何修订，
按 Step 6 释放锁后正常结束（严禁留下 lock-evolution）**。选项：
- **全部修复**：按归因表逐条实施（E 类只给指引不改文件）；
- **自选编号**：专家回复编号列表（如 `1 3`），只处理选中项；
- **只出诊断报告**：跳过 Step 3–4，直接生成演进报告（归因结论 + 建议），不改任何文件。

## Step 3: 修订实施

**总则**：最小修订、单点归因单点改；`resource/` 只读；不碰他人锁定的文件；临时脚本一律写 `tmp/`
（workspace-rules 8.05）；始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。

按类别的修订手法：

- **A 知识缺失**：
  - 缺属性 → 在对象七列属性表中补行（中文名/英文名/描述/主键/类型/别名/规则齐全，无别名写 `无`）；
  - 缺对象/操作/规则 → 提示这是结构性缺口，建议走对应提取命令（/ontology-extract-object 等）补齐后再评测；
    专家坚持时可就地新建文档（沿用 `.claude/templates/` 模板），并同步维护 index 与交叉引用。
- **B 语义模糊**：
  - 在属性别名/对象别名中补充问题里出现的业务叫法（保持别名占位规范：无别名写 `无`，多个用 `, ` 分隔）；
  - 重写含混的描述——描述要能独立回答"这个字段/对象是什么、什么取值代表什么"。
- **C 数据映射**：
  - 先跑 `python3 .claude/scripts/check_eval_data.py` 拿三方对账差异（数据列 ↔ schema ↔ 属性英文名）；
  - 按差异修正对象文档的英文名称列或来源 Sheet 对应关系；改完复跑对账确认清零。
- **D 用例问题**：改 `ontology/testcase/` 中对应场景的预期行（不改知识包），并在演进报告中注明
  "标准答案修订"；下轮评测用例复核弹窗中会体现新预期。
- **E 运行异常**：不改文件。在演进报告中给出指引（如"评测库未就绪，需管理员启用 eval profile"），
  并建议修复环境后直接重跑评测。

每改完一个文件，向专家展示一句话摘要（文件 + 修订点），不粘贴全文。

## Step 4: 一致性复检

1. `python3 .claude/scripts/ontology_checker.py`——硬错误必须清零；本次修订引入的 warning 要么修复要么向专家说明。
2. 若实施过 C 类修订：复跑 `check_eval_data.py` 确认对账差异清零。
3. 若修订面较大（≥3 个对象文档），建议专家追加 `/ontology-validate {涉事对象}` 增量校验；不强制执行。

## Step 5: 演进报告

写入 `reports/evolution-{YYYYMMDD}-{HHMMSS}.md`（Asia/Shanghai），结构：

```markdown
# 知识包演进报告 — {日期时间}

> 依据评测轮：{报告文件名}（总分 {score}，{pass}✓/{fail}✗/{error}⊘）

## 失分归因
{Step 2 的归因表（含未处理项及原因）}

## 本次修订
| 文件 | 修订点 | 归因 |

## 预期与验证
- 预期改善：{逐条对应失分用例，说明修订如何回应判分理由}
- 验证方式：在「数据评测」阶段重跑同一批用例，对比分数（修订不保证提分，以实测为准）
```

## Step 6: 收尾

1. `git add -A && git commit`（Conventional Commits，如 `fix(ontology): 评测演进——补配送单别名与来源Sheet 映射`）。
2. `python3 .claude/scripts/state_helper.py update-progress evolution_last_run {timestamp}`
3. `python3 .claude/scripts/state_helper.py release evolution`
4. 结束语（用户词表话术）："演进完成：修订 {N} 处，报告见 reports/evolution-*.md。
   请在左栏「数据评测」阶段重跑评测验证效果——分数以实测为准。"

## 边界与守则

- 本命令**只优化知识包与用例**，不改评测编排、不代跑评测（评测由平台「数据评测」阶段触发）；
- 一轮演进只处理一份评测报告；专家想连续多轮时，每轮之间必须重跑评测拿新报告；
- 与 /ontology-update 的分工：update 响应"资料变更"，evolution 响应"评测失分"——两者都改知识包，
  但归因输入不同，不要混用；
- 全程遵循 `.claude/INTERACTION_CONVENTIONS.md`（错误恢复、跳步守卫、编号列表）与
  `.claude/VALIDATION_BOUNDARIES.md`（校验归口）。
