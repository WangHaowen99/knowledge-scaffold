根据外部反馈优化知识包：读取反馈批次里的失分用例，归因到知识包缺陷，经专家确认后实施修订并留演进记录。目标反馈批次（可选，feedback/ 下的文件名或留空取最新）：$ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `evolution`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock evolution`
2. `python3 .claude/scripts/state_helper.py check-prereq evolution`
   - `missing:...` → 跳步守卫："还没有可优化的本体产物"。选项 **先做对象提取**（/ontology-extract-object）/ **取消**。
3. `python3 .claude/scripts/state_helper.py acquire evolution start`
4. 业务知识按 Step 2 归因出的涉事对象增量加载（`knowledge_helper.py load --scope object:{stem}`），不在此处全量加载。

命令结束前 `release evolution` 并 `update-progress evolution_last_run {timestamp}`。

## Step 1: 定位反馈批次与失分用例

反馈由评测服务经反馈入口写进工作区（onto 不主动调用评测服务），每批一个文件
`feedback/{接收时间}-{feedback_id}.json`，格式 `onto-feedback/v1`：顶层 `feedback_id`、`source`（`service`、`run`）、
`release_version`（针对的发布版本，可为空）、`summary`（score、total、pass、partial、fail、error，可缺项）、`items`；
每条 item 有 `case_id`、`question`、`expected`（标准答案）、`actual`（智能体回答）、`loss_reason`（失分原因 /
判分理由）、`suspected_artifacts`（评测侧怀疑的产物）、`verdict`（fail / partial / error）、`tool_failures`（工具失败行）。

1. 确定反馈批次：
   - `$ARGUMENTS` 非空 → 使用 `feedback/{$ARGUMENTS}`（容忍省略 `feedback/` 前缀与 `.json` 后缀）；
   - 页面发来的 `feedback_id=<id>` → 使用 `feedback/*-<id>.json`；同时带 `case_ids=[…]` 时第 2 步只收集这些用例；
   - 为空 → 取 `feedback/` 下文件名最新的一份（文件名以接收时间开头）。
   - 一份都没有 → 终止并提示："还没有收到反馈——反馈由评测服务经反馈入口推送，收到后再演进。" 释放锁后结束。
2. 收集全部条目（反馈里只有失分条目）：问题、标准答案、智能体回答、失分原因、怀疑的产物，以及
   `tool_failures`——每行是 `工具名 · 错误码 · 问题：问题码 字段路径 · 退出码 · 详情`，取自评测侧的执行轨迹，
   是判断运行异常根因的一手证据。`release_version` 非空时，归因以该版本的知识包为准（`git show release/{版本}:路径`
   可对照当时的文档）。
3. 若这批反馈没有条目（只有 summary）：告知"这批反馈没有失分项，无需演进"，可选提议（不强制）："要不要我基于反馈
   做一次预防性审视（别名覆盖、描述清晰度）？" 用户拒绝则释放锁结束。

## Step 2: 失分归因（诊断矩阵）

对每个失分用例，结合知识包现状（相关对象/操作/规则文档、`objects` 属性表、`ontology/testcase`）判断归因，
类别与判定要点：

| 类别 | 含义 | 典型信号 |
|---|---|---|
| A 知识缺失 | 问题涉及的对象/属性/操作/规则在知识包中不存在或不完整 | 回答"本体中没有定义…"、判分理由指出关键概念缺失 |
| B 语义模糊 | 概念存在但描述/别名不足，检索或理解失败 | 回答答非所问、用错对象；问题用词与文档措辞/别名对不上 |
| C 数据映射 | 数据问答类失分：对象未绑定数据表，或来源表名/英文列名与下游数据库表结构不一致 | 回答"查询失败/无此表/无此列"、明明该查库却只答了概念；`table_profile.py` 对账有差异项 |
| D 用例问题 | 标准答案本身错误、过严或表述歧义 | 智能体回答实质正确但被判 fail；标准答案与资料矛盾 |
| E 运行异常 | 🛑 error：环境/凭据/服务问题，与知识包无关 | 判分理由为"运行失败/连接失败/无可用模型配置"，且「工具失败」小节（若有）指向环境 |

**🛑 error 用例先读「工具失败」再归因，不要凭判分理由猜**。判分理由里的"运行失败：Tool execution
stopped after a repeated failure."之类只说明运行被中止，真正原因在该用例的「工具失败」行：

| 「工具失败」信号 | 归因 |
|---|---|
| `execute_script` + `SCRIPT_ARGUMENT_VALIDATION_FAILED`，问题码 `ungrounded_identity` / `request_field_mismatch` / `request_record_mismatch` / `evidence_record_mismatch` 等 + 字段路径（如 `arguments.customer_code`） | 脚本没有运行：智能体给脚本的该参数取不到可信来源。按字段路径定位脚本入参，核对对应对象是否有这个属性且已绑定数据（缺失 → A/C）、操作文档是否写清"先查哪个对象拿到该值再调脚本"（含混 → B）。**不是 E 类** |
| `execute_script` + 退出码非 0（附异常类型/详情） | 脚本本身或其入参口径有缺陷：核对操作文档对输入的定义（B）；脚本实现错误在演进报告中指引用 /ontology-codegen 重新生成该脚本 |
| `business_data_search` 等查数工具报表/列不存在、查询失败 | C 数据映射 |
| 超时、401/403、429、5xx、连接失败、无可用模型配置、下游数据库未就绪 | E 运行异常 |
| 「执行轨迹未取到」或没有「工具失败」小节 | 证据不足：按判分理由与环境信号判断；无法确定时在归因表里标"证据不足"并建议修复后重跑，不要臆测知识包缺陷 |

归因表的"拟修订"列对这类用例要引用所依据的工具失败行（工具名 + 错误码 + 字段路径）。

输出**编号归因表**（遵循 INTERACTION_CONVENTIONS 编号列表话术）：

```
| # | 用例（截断） | 判分 | 归因 | 拟修订 |
|---|---|---|---|---|
| 1 | 配送单 D002 是否已送达？ | fail | C 数据映射 | 配送单.md `# 数据来源` 补真实表名 |
```

然后用 AskUserQuestion 卡请专家确认处理范围。**卡片被拒绝或超时未应答时：不做任何修订，
按 Step 6 释放锁后正常结束（严禁留下 lock-evolution）**。选项：
- **全部修复**：按归因表逐条实施（E 类只给指引不改文件）；
- **自选编号**：专家回复编号列表（如 `1 3`），只处理选中项；
- **只出诊断报告**：跳过 Step 3–4，直接生成演进报告（归因结论 + 建议），不改任何文件。

## Step 3: 修订实施

**总则**：最小修订、单点归因单点改；`resource/` 只读；不碰他人锁定的文件；临时脚本一律写 `tmp/`
（workspace-rules 8.05）；始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。

**落点白名单（硬约束）**：反馈演进的知识修订**只能落在会进入知识包的目录**——
`ontology/object`、`ontology/operation`、`ontology/rule`、`ontology/metric`
（D 类另可改 `ontology/testcase`）。**`ontology/knowledge/` 是建模代理的上下文沉淀区，
不进知识包、consumer 永远看不到——严禁作为反馈演进的落点**（把知识写进那里做的是无用功，
分数不会有任何变化）。案例/方案类知识的正确形态是 `ontology/rule/` 下的**场景规则**：
触发特征 → 方案要点，并 wikilink 关联涉事对象。

**泛化性红线（硬约束）**：修订必须是**普适业务知识**的表达，严禁针对用例定制：
- 禁止把反馈里的问题或标准答案**原文照抄**进知识文档（连续 30 字以上雷同即视为应试）；
- 禁止"标准答案要点前置"、"检索关键词堆首行"之类为命中判分而做的排版技巧；
- 禁止在知识文档中引用反馈用例编号、题面或"评测""用例"之类的考核字样；
- 场景规则要覆盖**一类场景**而非一道题：触发特征写业务特征（行业/场景/痛点模式），
  方案要点写产品的普适适用逻辑与操作流程；
- 自检判据：把修订后的文档拿给**没见过这批用例的人**读，应当是自然、完整、
  站得住的业务知识——凡是"只有对着这道题才说得通"的表述都要重写。

按类别的修订手法：

- **A 知识缺失**：
  - 缺属性 → 在对象七列属性表中补行（中文名/英文名/描述/主键/类型/别名/规则齐全，无别名写 `无`）；
  - 缺对象/操作/规则 → 提示这是结构性缺口，建议走对应提取命令（/ontology-extract-object 等）补齐后再发布；
    专家坚持时可就地新建文档（沿用 `.claude/templates/` 模板），并同步维护 index 与交叉引用。
- **B 语义模糊**：
  - 在属性别名/对象别名中补充问题里出现的业务叫法（保持别名占位规范：无别名写 `无`，多个用 `, ` 分隔）；
  - 重写含混的描述——描述要能独立回答"这个字段/对象是什么、什么取值代表什么"。
- **C 数据映射**：
  - 有相关数据表时先跑 `python3 .claude/scripts/table_profile.py {数据表路径}` 拿三方对账差异（数据列 ↔ schema ↔ 属性英文名）；
  - 对象绑不到表时，在对象文档的 `# 数据来源` 段写**真实表名**（即数据表资料里的 Sheet 名，
    逐字相同）——该段是发布包「来源Sheet」列与 `mappings/object-data-mappings.json` 的唯一来源，
    留空或写对象名都会让这个对象在数据问答里查不到；纯概念、不落库的对象写 `无`；
    首行只写表名本身，补充说明另起一行以 `> ` 开头（首行写成句子会被整句当成表名）；
  - 列对不上时按差异修正对象属性表的英文名称列（英文名即真实列名）；改完复跑对账确认清零。
- **D 用例问题**：改 `ontology/testcase/` 中对应场景的预期行（不改知识包），并在演进报告中注明
  "标准答案修订"，供评测侧更新用例。
- **E 运行异常**：不改文件。在演进报告中给出指引（如"下游数据库未就绪，需管理员检查数据源"），
  并建议修复环境后由评测侧重跑。只有「工具失败」指向环境（或无工具失败证据）时才归入 E；
  指向脚本参数、数据映射的按上表归入 A/B/C 处理。

每改完一个文件，向专家展示一句话摘要（文件 + 修订点），不粘贴全文。

## Step 4: 一致性复检（全部必做，不通过不得进入 Step 5）

1. **落点自检**：`git status --porcelain` 列出本次全部改动文件——必须全部位于落点白名单目录；
   有越界文件立即回退该文件并重新按白名单落点实施。
2. `python3 .claude/scripts/ontology_checker.py`——硬错误必须清零；本次修订引入的 warning 要么修复要么向专家说明。
3. **增量校验（前置校验环节的复用，必做）**：对本次修改过的每个知识文档，按
   `/ontology-validate {涉事文件}` 的增量模式执行规则 + 语义两阶段校验；语义校验时
   额外核对两点：修订内容是否普适（无应试痕迹）、是否与既有知识/资料一致无矛盾。
   校验产出的错误项必须处理完毕。
4. **泛化性校验（必做，机械核对）**：
   ```bash
   python3 .claude/scripts/check_eval_generalization.py
   ```
   检出"与用例原文连续雷同 ≥30 字"或"出现标准答案/判分等应试措辞"即为硬错误——
   必须改写该文档（换成自己的业务表达、去掉考核语境），改完复跑至通过。
   **不得通过调大 `--threshold` 或跳过本步来规避。**
5. 若实施过 C 类修订：复跑 `table_profile.py {数据表路径}` 确认对账差异清零。

## Step 5: 演进报告

写入 `reports/evolution-{YYYYMMDD}-{HHMMSS}.md`（Asia/Shanghai），结构：

```markdown
# 知识包演进报告 — {日期时间}

> 依据反馈批次：{反馈文件名}（针对版本 {release_version 或「未注明」}，总分 {score}，{pass}✓/{fail}✗/{error}⊘）

## 失分归因
{Step 2 的归因表（含未处理项及原因）}

## 本次修订
| 文件 | 修订点 | 归因 |

## 预期与验证
- 预期改善：{逐条对应失分用例，说明修订如何回应判分理由}
- 验证方式：发布新版本后由评测侧重跑同一批用例，新一批反馈回来后对比分数（修订不保证提分，以实测为准）
```

## Step 6: 收尾

1. `git add -A && git commit`（Conventional Commits，如 `fix(ontology): 反馈演进——补配送单别名与来源Sheet 映射`）。
2. `python3 .claude/scripts/state_helper.py update-progress evolution_last_run {timestamp}`
3. `python3 .claude/scripts/state_helper.py release evolution`
4. 结束语（用户词表话术）："演进完成：修订 {N} 处，报告见 reports/evolution-*.md。
   发布新版本后请评测侧重跑验证效果——分数以实测为准。"

## 边界与守则

- 本命令**只优化知识包与用例**，不调用评测服务、不代跑评测（评测在 onto 之外，结果经反馈入口推送进来）；
- 一轮演进只处理一批反馈；专家想连续多轮时，每轮之间必须发布新版本、等新一批反馈；
- 与 /ontology-update 的分工：update 响应"资料变更"，evolution 响应"反馈失分"——两者都改知识包，
  但归因输入不同，不要混用；
- 全程遵循 `.claude/INTERACTION_CONVENTIONS.md`（错误恢复、跳步守卫、编号列表）与
  `.claude/VALIDATION_BOUNDARIES.md`（校验归口）。
