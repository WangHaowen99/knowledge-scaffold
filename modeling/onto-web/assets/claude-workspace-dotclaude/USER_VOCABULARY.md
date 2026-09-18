# 用户可见文案词汇表

本工具集面向 **非技术业务专家**。任何命令/agent 显示给用户的文本（提示、状态、错误、报告）**只能使用本词汇表中的表达**。

> 技术术语可以出现在代码、脚本注释、hook 日志里，但不能出现在用户对话里。

## 替换表

| ❌ 禁用（技术术语） | ✅ 推荐（用户可见） |
|---|---|
| commit / 提交 | 版本 / 保存版本 |
| git history / log | 版本历史 |
| push / pull / 拉取 / 推送 | 同步（如需提及；多数情况本地足够，无需展示） |
| revert / rollback | 回退 |
| branch | 分支（一般避免出现，用"版本"描述即可） |
| diff | 差异 / 对比 |
| mock data | 测试数据 |
| LLM / agent | （不出现；或改为"自动检查""智能校验"） |
| hook | （不出现；内部实现细节） |
| slash command / 斜杠命令 | 命令 |
| markdown / Obsidian / `.md` | 文档 / 文件 |
| `[[link]]` / Obsidian 链接 | 引用 / 链接（仅在必须解释时出现） |
| checkbox / 复选框 | （不出现；用"问题列表"代替） |
| json / 数组 / 字段 | 清单 / 条目 / 属性 |
| path / filepath | 文件位置（若必须展示具体路径则直接展示） |
| directory / dir / 目录 | 位置 / 文件夹 |
| 增量模式 / 批量模式 | 补充处理 / 批量处理（具体说明所做的事） |
| dispatch / subagent | （不出现；统一为"自动执行") |
| terminal / shell | （不出现；统一为"对话") |
| phase / 阶段编号 | 建模阶段（如"阶段 1/3：提取对象") |
| CI / build / pipeline | （不会出现在此工具中，禁止使用） |
| regression test | 回归检查 |
| API | （不出现；若需说明"调用"直接用"使用") |
| memory / knowledge base / 记忆库 | 业务知识 / 专家经验 |
| save knowledge / persist knowledge | 沉淀（动词，如"沉淀这条规则"） |
| frontmatter / yaml | （不出现；用"标签信息"或不提及） |

## 术语约定（必须使用完整名称）

| 完整名称 | 简写/别称 |
|---|---|
| **建模领域** | （禁止简称为"domain"） |
| **调研报告** | 位于 `research/`；不要说"研究资料" |
| **流程图** | `.drawio` 文件，位于 `ontology/diagrams/` |
| **流程图摘要** | 位于 `ontology/diagrams-summary/` |
| **业务对象 / 对象** | `ontology/object/*.md` |
| **业务逻辑 / 逻辑** | `ontology/logic/*.md` |
| **测试数据** | `ontology/data-mock/` |
| **生成代码** | `ontology/scripts/`；**不要说"codegen 产物"** |
| **校验报告** | `reports/` 下的文件 |
| **版本 N** | 等价于"第 N 次保存"；N = HEAD 上游 commit 数 |
| **业务知识** | 位于 `ontology/knowledge/`；含纠错/规范/术语/业务常识四类 |

## 动作语句模板

- "**已保存版本 {N}：{描述}**" — 成功保存后的唯一成功文案（由 hook 统一发出）。
- "**如需回退到上一版本，回复"回退"**" — 紧跟在保存文案后。
- "**下一步建议：**" — 所有命令结束前给出可选项清单的固定引导词。
- "**请先运行 `/ontology-XXX`**" — 缺失前置时的提示（括号内保留斜杠命令）。
- "**已跳过**" — 用户选择跳过时的唯一应答（不要说"已忽略""不处理"）。

## 错误信息模板

避免泄露技术细节：

- ❌ "FileNotFoundError: ontology/object/订单.md"
- ✅ "未找到业务对象文档：订单。请确认该对象是否已提取。"

- ❌ "subprocess exit code 1"
- ✅ "检查未能完成，请将上方信息告诉我以便排查。"

- ❌ "JSON parse error"
- ✅ "自动检查结果无法解析，已自动重试。若反复出现请告知。"

## 修改本词汇表

- 新增词汇：明确 `❌ 禁用` 与 `✅ 推荐` 两列。
- 变更现有词汇：先全局搜索旧表达是否还在被使用，一次改完再提交。
- 与 `INTERACTION_CONVENTIONS.md` 保持一致，二者描述的是同一规范的不同切面。
