# /ontology-extract-rule-metric — 抽取规则与指标实体

从已有的 `ontology/operation/*.md`（含旧目录 `ontology/logic/`）与 `ontology/object/*.md`
的属性规则列中，把**规则（rule）**与**指标（metric）**抽取为一等实体文档，写入
`ontology/rule/*.md` 与 `ontology/metric/*.md`。元信息对齐 modeling 知识包模型
（object / operation / rule / metric / script 五元组，ADR-0004）。

本命令是可选增强阶段：不修改既有 object/operation 文档正文（只允许在其中补
`[[规则名]]` / `[[指标名]]` 回链），产物可独立评审与回退。

## Step 0: 前置检查

1. `python3 .claude/scripts/state_helper.py check-lock extract-rule-metric`
2. 前置产物：`ontology/operation/`（或旧 `ontology/logic/`）至少有一个非 index 文档；
   否则提示先运行 `/ontology-extract-operation`，stop。
3. `python3 .claude/scripts/state_helper.py acquire extract-rule-metric rule-metric`

## 按切片建模

Step 0 之后读 `.claude/SLICE_MODE.md` 并照做：`python3 .claude/scripts/blueprint.py brief` 不是「全量模式」时，这一次只做 brief 里的那一片（派子代理时把 brief 原样放进 prompt），做完这一步就停下等用户确认生效。

## 业务知识识别（贯穿整个命令）

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。

## Step 1: 派发 rule-metric-extraction-agent

Read `.claude/agents/rule-metric-extraction-agent.md`. Dispatch it as a subagent。

变量（按行写入 prompt 主体）:

```
OBJECT_DIR=ontology/object/
OPERATION_DIR=ontology/operation/（若不存在则用 ontology/logic/）
RULE_DIR=ontology/rule/
METRIC_DIR=ontology/metric/
RULE_TEMPLATE=.claude/templates/rule-template.md
METRIC_TEMPLATE=.claude/templates/metric-template.md
```

Wait for the agent to return a JSON summary:
`{"rules": [名称列表], "metrics": [名称列表], "backlinks_added": 数量, "skipped": [说明]}`

## Step 2: 呈现摘要 + 用户确认

先 `python3 .claude/scripts/state_helper.py acquire extract-rule-metric confirm`，再输出：

```
规则与指标抽取完成：
- 规则：{n_rules} 条（{名称，顿号分隔}）
- 指标：{n_metrics} 个（{名称}）
- 回链补写：{backlinks_added} 处
- 跳过：{skipped 摘要，无则省略}
```

- 回复 **确认** → `acquire extract-rule-metric rule-metric`，进入 Step 3 提交
- 回复 **删除 名称1,名称2** → `acquire extract-rule-metric rule-metric`，删除指定实体文档及其回链后重新汇总
- 回复 **取消** → `git checkout -- ontology/ && python3 .claude/scripts/state_helper.py release extract-rule-metric`，stop

## Step 3: 校验并提交

```bash
# --files 只认文件名（不带 .md），不认目录；没有规则/指标文档时跳过
STEMS=$(find ontology/rule ontology/metric -maxdepth 1 -name '*.md' ! -iname 'index.md' -exec basename {} .md \; 2>/dev/null)
[ -n "$STEMS" ] && { python3 .claude/scripts/ontology_checker.py ontology --json --files $STEMS || true; }
git add ontology/rule/ ontology/metric/ ontology/object/ ontology/operation/
git commit -m "rule-metric: 抽取规则与指标实体"
python3 .claude/scripts/state_helper.py release extract-rule-metric
```

校验存在「错误」级问题时先 `acquire extract-rule-metric confirm`，向用户逐条展示，用户回复 **仍然提交** 才允许提交（回复后切回 `rule-metric`）。

释放锁后用两三句话小结：抽了多少规则和指标、还有什么要用户定；不列下一步菜单（界面的建议条会给出）。

## Note

- 同名冲突：拟抽取的规则/指标名与既有 object/operation 同名时，加后缀「规则」/「指标」消歧。
- 存量项目（ontology/logic/ 目录）同样支持；来源回链指向实际所在目录的文档。
