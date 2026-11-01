# 0030 元模型只定义一份：实体类型、目录、章节、链接语法与文件名编码

## Context

详设 4.4 与后端改造方案 C3～C5：实体类型与目录表在后端（Studio、MCP、兼容中间件、文件接口）和工具箱
（规则检查、调用关系、字段语义、查询、影响分析）里各写一份，维基链接正则约 20 处，分两族：一族遇 `#`
截断（Studio、脚本参数形态、MCP 文档模型），一族把 `#` 当名字的一部分（规则检查、调用关系、查询）。
导入的 `1#泵站` 这类对象，检查器认、Studio 图谱断边。文件名编码（`report_reconcile.safe_document_stem`）
不转义 `#`、`&`，新建的条目名会和链接语法冲突。

## Decision

1. **唯一定义放在工具箱脚本 `assets/workspace-toolkit/scripts/onto_metamodel.py`**（只用标准库）：
   实体类型（object、operation、rule、metric、script）、主目录与只读别名（`ontology/logic`）、章节结构
   （Studio 表单用的五类 schema 搬到这里）、章节别名、链接语法、关系语法、key 规则、文件名编码，以及
   `as_dict()` 对外视图。建模 agent 在工作区 `.claude/scripts/` 下直接 import；后端经
   `app/kernel/metamodel.py` 按文件加载同一份（按原名注册进 `sys.modules`，脚本与后端拿到同一个模块对象）。
2. **链接解析统一为 `parse_links` → `Link(target, anchor, alias, span)`**：`#` 一律是锚点，`|`（表格里的
   `\|` 也认）是别名，路径前缀与 `.md` 去掉。解析到条目时按「全名（含 `#锚点`）→ 名称 → 两者编码后的
   文件名」顺序匹配，兼容存量文件名里带 `#` 的条目。手里没有条目名单的地方（调用关系、检查器加载阶段）
   保留全名，到比对条目时再 `resolve`。
3. **改用它的地方**：Studio（图谱边、正文链接、反链、确定性检查、条目 schema、目录表）、MCP 文档模型（目录、
   链接、key 规则）、发布包 frontmatter 的 `uses.objects`、`package_mapping` 与检查器的关系端点、兼容中间件
   的条目 ID、文件与 Studio 路由的目录表；工具箱的 `ontology_checker`、`operation_relations`、
   `script_argument_shape`、`field_semantics`、`query`、`impact_analysis` 只改引用元模型的那几行，不搬迁。
4. **文件名编码搬进元模型**：`encode_filename` 在原来的文件系统保留字符之外，还编码 `# | [ ] &`；
   `report_reconcile.safe_document_stem` 改为调用它。`key_problem` 给出新条目 key 是否合法（MCP 用它，
   导入遇到非法 key 进待处理属于 P2）。
5. **`GET /api/v1/metamodel`（契约 C-05）** 返回 `as_dict()`，前端据此生成条目表单、解析链接。

## Alternatives considered

- **元模型放后端、工具箱经 HTTP 取**：工具箱脚本在 agent 的工作区里离线运行，不能依赖后端接口。
- **放进契约库（D5）按版本加载**：契约库属于插件运行时（B3a 并行在做）；先放在工具箱目录、由后端单独
  加载，契约库落地后把 `onto_metamodel` 并进它的加载清单即可，模块名不变。
- **`#` 继续当名字的一部分**：和锚点语义（详设 5.8.5）冲突；全名优先匹配已经能兼容存量文件名。

## Consequences

- 检查结果有变化：`[[对象#锚点]]` 指向已有对象时不再报「无对应文件」；关系端点 `[[对象#锚点]]` 归到对象
  本身（与发布侧原有口径一致）；`[[1#泵站]]` 在 Studio、发布、MCP 里都能连上。
- 新建条目名里有 `#`、`&`、`[`、`]` 时文件名会被百分号编码，链接里写原名仍能解析到。
- 前端 `CenterPane.jsx` 里还有一份链接正则，新前端改读 C-05（前端线）。
- 插件运行时把工具箱迁成 core 包时，`onto_metamodel.py` 随目录一起搬，`app/kernel/metamodel.py` 的路径
  跟着改（或改由契约库加载）。
