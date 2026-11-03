Reverse-lookup and free-text search over the ontology. Query (if any): $ARGUMENTS

## Step 0: 前置检查

只读命令，不需要锁。按 `.claude/STEP0_TEMPLATE.md` 只做第 2 步（阶段前置）：

```bash
python3 .claude/scripts/state_helper.py check-prereq query
```

- 输出 `ok` → 继续。
- 输出 `missing:对象已提取,...` → 提示"当前缺少 {阶段列表}，反查需要先有对象文档。回复 **先做 对象提取**（/ontology-extract-object）/ **继续**（结果可能为空）/ **取消**。"

若最终仍是空仓库，可回退提示："当前无本体数据，无法查询。回复 **资料分析** 开始建模。"并停止。

## Step 1: Parse $ARGUMENTS

If empty, show this menu and wait for reply:

```
请选择查询类型：

- 回复 **引用 <名称>** → 查询谁引用了这个对象/逻辑（如 `引用 订单`）
- 回复 **搜索 <关键词>** → 全本体文本搜索（如 `搜索 计算总价`）
- 回复 **孤立** → 列出所有没被任何文档引用的孤立对象/逻辑
- 回复 **未用对象** → 列出没被任何逻辑使用的对象
- 回复 **失效链接** → 列出所有指向不存在文档的 [[链接]]
- 回复 **知识 <关键词>** → 搜索已沉淀的业务知识（如 `知识 退款`）
```

Otherwise parse $ARGUMENTS as one of:
- `引用 X` or `used-by X` → mode=used-by, target=X
- `搜索 X` or `search X` → mode=search, keyword=X
- `孤立` or `orphans` → mode=orphans
- `未用对象` or `unused-objects` → mode=unused-objects
- `失效链接` or `dangling` → mode=dangling
- `知识 X` or `knowledge X` → mode=knowledge, keyword=X

If $ARGUMENTS doesn't match any pattern, treat it as `搜索 $ARGUMENTS` (fallback to search).

## Step 2: Run query

Map the mode to script flag:

| 模式 | 命令 |
|------|------|
| used-by | `python3 .claude/scripts/query.py --used-by "{TARGET}"` |
| search | `python3 .claude/scripts/query.py --search "{KEYWORD}"` |
| orphans | `python3 .claude/scripts/query.py --orphans` |
| unused-objects | `python3 .claude/scripts/query.py --unused-objects` |
| dangling | `python3 .claude/scripts/query.py --dangling` |
| knowledge | `python3 .claude/scripts/query.py --knowledge "{KEYWORD}"` |

## Step 3: Present result

Print the script's markdown output directly.

## Step 4: Suggest next step

有结果时用一两句话点出要紧的发现；没有结果时直说没查到。不列下一步菜单（界面的建议条会给出）。

用户之后在对话里说「影响 <名称>」，读 `.claude/commands/ontology-impact.md` 照做；说「再查询」或给出新的关键词，回到 Step 1。
