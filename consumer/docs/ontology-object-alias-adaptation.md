# 对象别名必做适配方案

状态：已实施  
日期：2026-07-21  
关联设计：`consumer/docs/ontology-package-final-design.md`

## 1. 目标

当用户问题使用对象别名时，系统能够：

1. 从 `objects/index.md` 快速定位候选对象。
2. 将别名稳定归一化为正式 `object key`。
3. 使用正式 `object key` 加载对象详情和查询业务数据。
4. 在别名冲突时返回全部候选，不任意选择对象。
5. 保持不含别名列的旧本体包继续可用。

目标链路：

```text
用户问题出现对象别名
  -> objects/index.md 别名匹配
  -> 解析为正式 object key
  -> ontology_context 加载对象详情
  -> business_data_search 使用正式 object key
  -> mapping / provider 执行查询
```

## 2. 当前基线

当前代码已支持：

- 根上下文通用解析 Markdown 表格，`objects/index.md` 增加别名列后会自动进入 `objects_catalog`。
- `find_markdown_file()` 可以通过对象详情 frontmatter 中的 `key`、`canonical_key`、`title` 和 `aliases` 查找文件。
- 建模模板转换已能生成带“别名”列的对象目录。

当前缺口：

- `objects/index.md` 的别名只会展示给 Agent，后端不会使用该列解析对象。
- `find_markdown_file()` 已支持 `key`、`canonical_key`、`title`、`aliases` 和文件名匹配，但非直接文件名命中时会遍历对象文件，且不返回正式 key 和 `matched_by`。
- 别名请求加载成功后，`ontology_context` 仍可能把别名当作 `object_key` 返回。
- `business_data_search` 会把原始 `entity` 直接传给 Provider，mapping 只按正式 key 查找。
- 重复别名可能命中扫描到的第一个文件，没有歧义报告。
- 包导入没有校验目录别名与详情 `aliases` 的一致性。

## 3. 必做范围

### 3.1 补齐对象别名数据

`objects/index.md` 必须支持并填写别名列：

```markdown
| object key | 标题 | 别名 | 说明 |
| --- | --- | --- | --- |
| CHANGE_ORDER受影响对象 | CHANGE_ORDER受影响对象 | CHANGE_ORDER影响对象、设计变更影响对象 | 记录 CHANGE_ORDER 影响到的对象和版本变化 |
```

对象详情 frontmatter 必须同步声明同一组别名：

```yaml
resource: object
key: CHANGE_ORDER受影响对象
canonical_key: change_order_affected_object
title: CHANGE_ORDER受影响对象
aliases:
  - CHANGE_ORDER影响对象
  - 设计变更影响对象
```

约束：

- 对象详情 `aliases` 是完整定义的权威来源。
- 目录别名是详情别名的快速路由投影，两处应保持一致。
- 正式 `object key` 是对象详情 frontmatter 的 `key`；本期不改变 key 命名风格，不做中文到英文、拼音或 snake_case 转换。
- mapping 当前使用正式 `object key` 做精确查找，别名解析后必须传递 frontmatter `key` 的原始值。
- 文件名可以使用正式 `key`，也可以使用 `canonical_key`；不强制文件 stem 与正式 `key` 相同。
- 别名属于业务知识，不由运行时自动编造。
- 没有别名时，目录可以为 `-`，详情可以省略 `aliases` 或使用空数组。

`canonical_key` 是可选的 ASCII 工程标识，不要求现有对象补齐。如果填写，应使用路径安全的 ASCII 字母、数字、下划线或短横线，推荐使用小写 snake_case。它只用于跨系统引用、ASCII 文件名和兼容匹配：

- `canonical_key` 不是正式 `object key`。
- `canonical_key` 不进入 mapping，mapping 仍使用 frontmatter `key`。
- `canonical_key` 不作为 `ontology_context` 或 `business_data_search` 最终返回和传递的 `object_key`。
- 文件 stem 可以等于 `canonical_key`，但不要求二者必须相同。
- 同一本体包内的 `canonical_key` 应唯一，且不得与其他对象的 key、title 或 alias 冲突。

### 3.2 增加统一的对象引用解析

在 `consumer/backend/ontology/ontology_catalog.py` 中增加统一的对象引用解析能力。

目前本体包和建模转换产物存在多套目录表头。解析器必须按以下兼容集识别列：

| 语义 | 支持的列头 |
| --- | --- |
| 对象 key | `object key`、`object_key`、`key`、`中文名称` |
| 标题 | `标题`、`title` |
| 别名 | `别名`、`aliases`、`alias` |

列头匹配应忽略首尾空格和 ASCII 大小写。`中文名称` 作为对象 key 是对现有建模模板输出的兼容；该模板当前使用中文源文件 stem 作为 frontmatter `key`。目录对象 key 列的值必须与对应详情 frontmatter `key` 完全一致；建模模板如果用文件 stem 填充该列，就必须同时使用同一 stem 生成 frontmatter `key`。

说明、业务定义、`description` 和 `summary` 等列只由根上下文的通用 `parse_markdown_table()` 保留和展示，对象别名解析器不读取它们。不得在列头无法识别时无条件把第一列当作 key；应返回目录格式 warning，并回退到现有详情文件查找逻辑。

输入：

```text
ontology_dir + requested_object_name
```

解析结果至少应包含：

```text
requested_key
object_key
matched_by
path
```

`matched_by` 用于区分：

```text
key / canonical_key / title / alias / filename
```

匹配优先级必须固定为：

```text
key
  > canonical_key
  > title
  > alias
  > filename 兼容匹配
```

归一化规则：

- 比较键首先执行 `unicodedata.normalize("NFC", str(value).strip())`。
- 如规范化后的比较键全部为 ASCII，再执行 `casefold()`；非 ASCII 文本不做大小写转换。
- 比较键只用于匹配和去重，返回的正式 key 必须保留 frontmatter `key` 的原始值。
- 新包的别名列规范使用中文顿号“、”分隔；为兼容现有转换产物，解析时同时支持中英文逗号、分号和 `/`。
- `/` 是对象别名的标准分隔符，`SourceItem/TargetItem` 解析为 `SourceItem`、`TargetItem` 两个别名，不作为单个别名保存。目录单元格、frontmatter 字符串和 `aliases` 列表中的每个元素都使用同一拆分规则，导入校验与运行时解析必须保持一致。
- 不做模糊匹配、拼音匹配或向量召回。

解析顺序：

1. 直接匹配正式 key。
2. 使用 `objects/index.md` 的标题和别名匹配。
3. 使用对象详情 frontmatter 的 `canonical_key`、`title` 和 `aliases` 做兼容回退。
4. 使用现有文件名规则做旧包兼容。

当请求值或目录行已解析出正式 key 后，详情文件定位先尝试 `objects/<object_key>.md` 快速路径；如直接路径不存在，必须回退到扫描对象详情 frontmatter `key` 的精确匹配。因此当文件 stem 使用 `canonical_key`、而目录 key 列使用 frontmatter `key` 时，仍能定位到正确详情。实现可在单次目录扫描中建立 `frontmatter key -> path` 映射，避免对同一请求重复扫描。

运行时必须先找到当前最高命中优先级，再在该层级内判断歧义：

- 精确命中正式 key 时，正式 key 胜过其他对象的低优先级别名或标题，保证合法 key 调用不被旧包中的冲突别名破坏。
- 在同一最高命中层级匹配到多个对象时，必须返回明确的歧义结果和全部候选 `object key`，不得按文件排序选择第一个。
- 跨优先级的标识冲突应由包校验报告；未经校验的旧包在运行时仍按上述固定优先级解析。
- 对象引用未命中属于可恢复的路由结果，不标记为工具执行错误；结果应说明未找到并提供可用正式 key，供 Agent 修正后重试。目录缺失、读取失败、参数缺失和歧义仍按错误处理。

首版不引入持久化缓存。运行时使用有界的进程内对象引用索引，按对象目录下 Markdown 文件的名称、修改时间和大小签名自动失效；同一本体版本未发生变化时复用目录和详情 frontmatter 解析结果，避免每次别名请求重复读取全部对象详情。文件发生变化后必须重建索引，导入校验仍直接读取暂存目录，不依赖运行时缓存。目录或详情读取、UTF-8 解码、frontmatter 解析失败时不得缓存降级索引，下一次请求即使文件元数据未变化也必须重新读取。

### 3.3 `ontology_context` 必须返回正式 object key

修改 `consumer/backend/agent/tools/ontology_context.py`。

对象引用归一化在 `handle_ontology_context()` 所属的对象处理链路内完成。该链路应生成一份可在内部复用的解析结果，至少包含正式 key、命中方式和已解析路径。`_build_markdown_payload()` 和 `_build_read_record()` 必须复用同一解析结果，不得分别再调用 `find_markdown_file()` 或重复扫描对象目录。对外的工具输出仍保持字符串，内部可使用结构化结果在 payload 和 read record 之间传递。

当 `resource="object"` 时：

1. 先通过统一对象引用解析器处理 `key`。
2. 使用解析后的文件路径读取对象详情。
3. 输出中的 `object_key` 必须是正式 key，不得继续使用请求别名。
4. 通过别名或标题命中时，输出最小路由元数据。

期望结果示例：

```text
object_key: CHANGE_ORDER受影响对象
requested_key: 设计变更影响对象
matched_by: alias

content:
...
```

直接使用正式 key 时，可以省略 `requested_key` 和 `matched_by`，保持现有紧凑输出。

本期不修改 read ledger 的身份推断逻辑。当前 ledger 根据已解析的文件路径记录对象文档，别名不会直接进入该字段。文件 stem 与 frontmatter `key` 的身份统一属于独立议题，不在本次别名适配中改动。

### 3.4 `business_data_search` 必须在公共入口归一化 entity

修改 `consumer/backend/agent/tools/business_data_search.py`。

归一化必须在 `BusinessDataSearchTool.execute()` 的公共工具入口完成，且早于 `handle_business_data_search()` 调用和 `object_context_usage` 生成：

1. 使用 `ontology_id` 定位当前活动本体版本。
2. 使用统一对象引用解析器处理 `entity`。
3. 复制工具参数并将 `entity` 替换为正式 `object key`，再调用 `handle_business_data_search()`。
4. `handle_business_data_search()` 和 `BusinessDataSearchRequest` 只接收解析后的正式 key，Provider 因此不再看到请求别名。
5. `object_context_usage`、日志和证据中使用同一个正式 key；如需调试，可另行记录非敏感的原始请求值。

别名归一化只在公共工具入口实现一次。不得在 Neo4j、HugeGraph、NL2SQL 等 Provider 中分别复制同一逻辑，mapping 仍只使用正式 `object key`。

当别名有歧义时，必须在调用 Provider 之前终止查询并返回候选对象，不得尝试多个数据资源。

图数据源存在一个兼容例外：对象解析器明确返回 `not_found`，或旧包明确缺少 `objects/` 目录，且本体包没有同名精确 mapping 时，Neo4j 和 HugeGraph 可以把请求值作为图 Schema label 候选交给图运行时。图运行时必须先用预取 Schema 做精确匹配，并在实际查询中使用命中的 Schema label；其他 Provider 仍返回对象未命中错误。读取失败、UTF-8 解码失败、frontmatter 解析失败、参数错误和歧义必须直接终止，不得进入图兜底。mapping 文件损坏同样直接终止，并在导入阶段以 error 阻断安装。

这个候选值不是本体包正式 `object key`，因此不写入 `object_context_usage`；公共入口日志可以记录候选值，图运行时的查询和证据应记录最终命中的 Schema label。已解析的对象别名、歧义对象和同名精确 mapping 不走此例外。

### 3.5 增加别名一致性校验

在本体包导入的临时目录或标准化目录安装到正式版本目录之前，执行别名一致性校验：

| 校验项 | 期望行为 |
| --- | --- |
| 目录对象 key 没有对应详情 | error |
| 目录对象 key 列的值与详情 frontmatter `key` 不一致 | error |
| 目录别名与详情 `aliases` 不一致 | error，指出 `objects/index.md` 中的对象名称和 key、对象定义文件路径及两侧别名值 |
| 别名与其他对象的 key 冲突 | error |
| 别名与其他对象的 `canonical_key` 或 title 冲突 | error |
| 同一别名指向多个对象 | error |
| 别名重复、为空或只有空格 | warning |
| 本体包没有别名列 | 允许，作为旧包兼容 |

目录别名与详情 `aliases` 的一致性比较必须基于分拆、归一化和去重后的集合，不按原始字符串、别名顺序或分隔符样式比较。

校验结果处理：

- 任何 error 都必须在安装本体版本之前阻断导入，导入 API 返回 HTTP 400 和可定位到对象、字段及冲突值的错误信息。
- warning 不阻断导入，但必须进入导入 API 现有返回体的 `warnings` 结构，不只写服务端日志。当前 `warnings` 是包含 `objects`、`objects_index` 和 `operations` 等列表的结构化对象，本期在同一对象下增加 `object_aliases: []`，不改变 `warnings` 的顶层类型。导入校验统一使用该分类返回别名 warning，不将其混入 `normalization_warnings`。
- 别名校验函数应同时返回 `errors` 和 `warnings`，由导入路由统一转换为阻断响应和成功响应中的 warning 分类。
- 校验必须遍历完整对象目录和详情集，聚合全部 errors 和 warnings 后再统一返回，不采用首个错误即短路的策略。为保持现有错误响应顶层形态，导入路由仍可返回单一 `error` 字符串，但必须按稳定顺序组合全部校验错误，便于使用者一次性修正。
- enable 切换本期不重复执行别名校验，避免在运行时状态切换中引入新的 warning 传播通道。对于绕过导入流程的历史包或本地包，仍由运行时固定优先级和歧义检测做安全降级。

当前运行时只有 `enabled` 开关，没有可执行的 release/publish 状态机制，因此本期不定义“released 状态阻断发布”规则。如后续引入发布状态和发布入口，再单独定义 warning 升级为阻断错误的策略。

校验结果不得更改或自动生成别名，只负责报告问题。

## 4. 端到端方案流程

### 4.1 总体流程和边界

方案分成“导入时治理”和“运行时解析”两部分。导入时负责尽早阻断不一致或损坏的包；运行时只消费已声明的信息，不自动修复、生成或猜测别名。

```text
本体包 ZIP
  -> 解压到临时目录
  -> 标准化为 v0.1 目录
  -> 校验对象目录、对象详情、别名和 mapping
  -> error: HTTP 400，正式版本目录不变
  -> warning: 保留到 warnings.object_aliases
  -> 校验通过: 安装版本

用户请求
  -> 定位 workspace 中的活动本体版本
  -> 构建或复用对象引用索引
  -> 解析 requested key/entity
  -> resolved: 返回正式 object key 和详情路径
  -> not_found/missing_directory: 仅 business_data_search 可进入兼容分支
  -> read/parse/ambiguous/invalid: 立即终止
```

职责边界如下：

| 层次 | 负责内容 | 不负责内容 |
| --- | --- | --- |
| 包导入 | 别名一致性、标识冲突、文件可读性、mapping 完整性 | 自动修改或补全业务别名 |
| 对象解析器 | 把 key、canonical key、title、alias、filename 解析为正式 key | 连接数据源、执行查询 |
| `ontology_context` | 使用正式 key 和已解析路径加载对象详情 | mapping 和图 Schema 兜底 |
| `business_data_search` | 决定正式对象、旧 mapping 或图 Schema 兼容路径 | 在 Provider 内重复实现别名解析 |
| Provider | 使用已经确定的对象或 Schema label 查询数据 | 猜测对象别名对应关系 |

### 4.2 导入与校验流程

导入校验发生在版本安装前，统一按以下口径执行：

1. 将上传包解压到临时目录，确定唯一 v0.1 包根目录。
2. 将 v0.1 包复制到暂存目录（`normalize_imported_package`），作为版本安装的候选内容。
3. 读取 `objects/index.md`，识别兼容的 key、title 和 alias 表头。
4. 逐个读取 `objects/*.md` 的 UTF-8 内容和 YAML frontmatter，建立详情定义集合。
5. 对目录与详情做一致性校验：
   - 目录 key 必须精确对应详情 frontmatter `key`。
   - 只有目录存在别名列时，才比较目录别名与详情 `aliases` 的归一化集合。
   - 校验 key、canonical key、title 和 alias 的跨对象冲突。
   - 重复或空别名生成 warning，不一致或歧义生成 error。
6. 校验 `mappings/object-data-mappings.json`：
   - 文件不存在按历史包兼容处理。
   - 文件存在时必须是 UTF-8、合法 JSON，并具有受支持的对象 mapping 结构。
7. 聚合全部 error 和 warning：
   - 任一 error：返回 HTTP 400，不执行版本安装。
   - 只有 warning：继续安装，并写入响应中的 `warnings.object_aliases`。
   - 无问题：安装新版本或覆盖目标版本。

```text
临时包
  |
  +-- objects/index.md 无别名列
  |     -> 允许
  |     -> 不执行“目录别名 = 详情 aliases”比较
  |     -> 详情 aliases 仍进入运行时索引
  |
  +-- objects/index.md 有别名列
  |     -> 拆分、归一化、去重
  |     -> 与详情 aliases 集合比较
  |     -> 不一致则 error
  |
  +-- mapping 不存在
  |     -> 兼容历史包
  |
  +-- mapping 存在但损坏
        -> error
        -> HTTP 400
```

版本安装是校验事务边界。错误发生时只删除临时目录，不应留下部分安装的版本，也不应修改当前活动版本。

### 4.3 运行时索引构建与缓存流程

对象引用索引以活动版本的 `objects/` 目录为输入，一次扫描生成以下查找表：

```text
normalized key           -> object definition[]
normalized canonical_key -> object definition[]
normalized title         -> object definition[]
normalized alias         -> object definition[]
normalized filename      -> object definition[]
```

具体流程：

1. 枚举 `objects/*.md`，忽略 `objects/index.md`。
2. 使用“文件名 + `mtime_ns` + 文件大小”生成目录签名。
3. 查询有界 LRU 缓存：
   - 命中相同版本路径和签名：复用索引。
   - 未命中：重新读取目录和全部详情 frontmatter。
4. 将详情 key 作为正式身份，将目录 title/alias 投影到对应详情定义。
5. 对每种标识分别建立归一化后的候选列表，不在构建阶段擅自选择冲突对象。
6. 索引构建成功后才进入缓存。

读取或解析失败采用“失败不缓存”：

```text
首次请求
  -> 文件签名 S
  -> 读取 object_a.md 失败
  -> 返回 read_error
  -> 不写入 LRU 缓存

第二次请求（文件签名仍为 S）
  -> 缓存中没有成功结果
  -> 再次读取 object_a.md
  -> 读取恢复后正常构建索引
```

这样可以避免短暂文件锁、权限抖动或 I/O 故障被固化成按文件名生成的错误对象定义。

### 4.4 对象引用解析流程

统一入口接收：

```text
ontology_dir + requested_key
```

解析器先判断包状态，再按固定优先级匹配。返回值是结构化状态，不要求调用方解析错误字符串。

| 状态 | 触发条件 | 是否可继续 |
| --- | --- | --- |
| `resolved` | 某一优先级唯一命中 | 返回正式 key、路径和 `matched_by` |
| `not_found` | 对象目录和详情可读，但没有任何命中 | `ontology_context` 终止；业务查询可检查兼容路径 |
| `missing_directory` | 旧包没有 `objects/` | 只有业务查询可检查 mapping/图 Schema |
| `invalid_request` | 请求值为空 | 立即终止 |
| `ambiguous` | 最高命中优先级存在多个候选 | 返回全部候选并立即终止 |
| `read_error` | 目录或详情读取、UTF-8 解码失败 | 立即终止，不缓存 |
| `invalid_package` | 详情 frontmatter 未闭合、YAML 非法或不是映射 | 立即终止，不缓存 |

匹配过程：

```text
requested_key
  -> trim + NFC
  -> 全 ASCII 时 casefold
  -> 查询 key 候选
       -> 有候选：只在 key 层判断唯一或歧义
       -> 无候选：查询 canonical_key
       -> 无候选：查询 title
       -> 无候选：查询 alias
       -> 无候选：查询 filename
       -> 全部无候选：not_found
```

一旦高优先级存在候选，就不再查看低优先级。例如请求值精确等于对象 A 的正式 key，同时也是对象 B 的 alias，运行时选择对象 A；包导入校验负责报告这类跨对象冲突。

成功结果示例：

```text
status: resolved
requested_key: 设计变更影响对象
object_key: CHANGE_ORDER受影响对象
matched_by: alias
path: objects/change_order_affected_object.md
```

### 4.5 `ontology_context` 消费流程

`ontology_context(resource="object")` 不包含 mapping 或 Provider 兼容逻辑：

1. 根据 `ontology_id` 定位活动版本目录。
2. 调用统一对象解析器。
3. 只有 `resolved` 才读取结果中的 `path`。
4. 使用 `read_required_text()` 再次读取完整对象内容。
5. 输出正式 `object_key`；别名、title 或 canonical key 命中时同时输出 `requested_key` 和 `matched_by`。
6. read ledger 使用解析后的详情路径记录读取事实。
7. 其他状态直接返回解析器错误，不尝试图 Schema 或 mapping。

```text
ontology_context(object, "设计变更影响对象")
  -> resolve: matched_by=alias
  -> formal key=CHANGE_ORDER受影响对象
  -> read objects/change_order_affected_object.md
  -> payload.object_key=CHANGE_ORDER受影响对象
  -> ledger.source_path=objects/change_order_affected_object.md
```

### 4.6 `business_data_search` 决策流程

对象归一化必须在 `BusinessDataSearchTool.execute()` 中完成。完整决策顺序如下：

```text
entity
  -> resolve_object_reference_result()
       |
       +-- resolved
       |     -> entity 替换为正式 object key
       |     -> 调用 Provider
       |     -> object_context_usage 记录正式 key
       |
       +-- invalid_request / ambiguous / read_error / invalid_package
       |     -> 直接返回错误
       |     -> 不读取 mapping
       |     -> 不解析数据源
       |     -> 不调用 Provider
       |
       +-- not_found / missing_directory
             -> 精确查询 object-data-mappings.json
                  |
                  +-- found
                  |     -> 按精确 mapping key 调用 Provider
                  |
                  +-- invalid
                  |     -> 直接返回 mapping 错误
                  |
                  +-- not_found / file_missing
                        -> 解析数据源
                             |
                             +-- Neo4j / HugeGraph
                             |     -> 将原值作为 Schema label 候选
                             |     -> 图运行时必须精确命中预取 Schema
                             |     -> 不写 object_context_usage
                             |
                             +-- 其他 Provider 或数据源不可用
                                   -> 返回原对象解析错误
```

mapping 查询状态用于区分“没有配置”和“配置损坏”：

| mapping 状态 | 含义 | 运行时行为 |
| --- | --- | --- |
| `found` | 存在当前对象的精确 mapping | 使用该 mapping |
| `not_found` | mapping 文件合法，但没有当前对象 | 可检查图 Schema 兼容路径 |
| `file_missing` | 历史包没有 mapping 文件 | 可检查图 Schema 兼容路径 |
| `invalid` | 文件不可读、非 UTF-8、JSON 非法或结构错误 | 立即终止，不允许图兜底 |

图 Schema 路径不是模糊别名解析。请求值必须由图运行时精确命中预取的 label；未命中时返回图映射错误，不能选取相似 label。为了避免两次读取数据源配置，公共入口判定图 Provider 后会把同一连接对象传给后续查询处理。

### 4.7 典型场景

#### 场景 A：目录别名正常命中

```text
请求 entity = "设计变更影响对象"
目录 alias -> CHANGE_ORDER受影响对象
详情 key   -> CHANGE_ORDER受影响对象
Provider entity = CHANGE_ORDER受影响对象
object_context_usage.entity_name = CHANGE_ORDER受影响对象
```

#### 场景 B：旧包没有别名列

```text
objects/index.md 只有 key/title
详情 frontmatter 有 aliases -> 仍可通过详情索引命中
详情也没有 aliases          -> key/title/filename 继续工作
导入结果                    -> 不因缺少别名列报错
```

#### 场景 C：同一别名指向多个对象

```text
alias "制造件" -> ObjectA, ObjectB
解析状态       -> ambiguous
返回候选       -> ObjectA, ObjectB
Provider 调用  -> 不发生
```

#### 场景 D：对象目录损坏但配置了图数据源

```text
objects/index.md 非 UTF-8
解析状态 -> read_error
mapping  -> 不读取
图兜底   -> 不执行
结果     -> 返回稳定的目录解码错误
```

#### 场景 E：对象仅存在于图 Schema

```text
对象解析 -> not_found
mapping  -> not_found
Provider -> Neo4j
Schema   -> 精确存在 label "SchemaOnlyEntity"
结果     -> 允许图查询
usage    -> 不写入本体对象 object_context_usage
```

## 5. 兼容策略

- 没有别名列的旧包仍按正式 key、标题和文件名工作。
- 详情 frontmatter 已有 `aliases`、但目录没有别名的旧包，继续使用详情扫描回退。
- 新包应优先通过 `objects/index.md` 完成别名路由，不为每次用户问题遍历全部对象详情。
- 不修改 `mappings/object-data-mappings.json` 结构，mapping key 始终是正式 `object key`。
- 不要求前端增加新交互才能使用别名。

## 6. 必须增加的测试

### 6.1 对象目录和上下文

1. `objects/index.md` 的别名列进入根 `objects_catalog`。
2. 通过目录别名加载到正确对象详情。
3. 通过别名加载后，返回正式 `object key`。
4. 正式 key、`canonical_key`、title、alias 和兼容文件名分别按固定优先级解析。
5. 中文分隔符、英文大小写和 Unicode 等价文本正确归一化。
6. `object key`、`object_key`、`key` 和“中文名称”表头都能识别正式 key，未知表头不会静默使用第一列。
7. 不填写 `canonical_key` 的现有对象仍能正常路由；使用 `canonical_key` 命中时最终仍返回 frontmatter `key`。

### 6.2 歧义和校验

1. 同一别名指向多个对象时返回全部候选，不加载任意一个详情。
2. 别名与其他对象 key、`canonical_key` 或 title 冲突时校验失败。
3. 请求同时命中一个对象的正式 key 和另一对象的低优先级别名时，正式 key 稳定胜出，同时包校验报告冲突。该用例必须显式构造一个未通过包校验的测试包，只验证运行时降级优先级；校验阻断由第 2 条单独覆盖。
4. 目录别名与详情 `aliases` 只有顺序或分隔符差异时视为一致；归一化后集合不一致时校验失败，错误信息包含对象 key 及两侧别名值。
5. 目录对象 key 列与详情 frontmatter `key` 不一致时阻断导入。
6. 同一包中存在多个别名校验 error 时，导入返回 HTTP 400，且 `error` 字符串按稳定顺序包含全部错误，不只返回第一个。
7. 只存在别名重复或空值等 warning 时导入成功，返回体保持 `warnings` 为对象，并包含 `warnings.object_aliases`。
8. 没有别名列的旧包仍能加载和查询。

### 6.3 业务数据查询

1. `business_data_search` 接收别名时，Provider 最终收到正式 `object key`。
2. mapping 使用正式 key 正常命中。
3. 使用别名直接查找 `objects.get(entity)` 时不命中 mapping，经公共入口归一化为正式 key 后命中同一 mapping 条目。
4. 别名有歧义时不调用 Provider。
5. `object_context_usage`、日志和证据记录正式 key，不记录请求别名。
6. 对象和精确 mapping 都未命中时，仅图 Provider 可把请求值作为 Schema label 候选继续查询；该路径不写入 `object_context_usage`，且只解析一次数据源。

建议优先扩展：

- `consumer/backend/tests/test_tool_registry.py`
- `consumer/backend/tests/test_package_import.py`
- 业务数据查询入参与 Provider mock 相关测试

## 7. 影响文件

| 文件 | 必做改动 |
| --- | --- |
| `consumer/backend/ontology/ontology_catalog.py` | 增加对象目录别名解析、归一化、歧义检测和正式 key 结果 |
| `consumer/backend/agent/tools/ontology_context.py` | 对象加载前解析别名，返回正式 key |
| `consumer/backend/agent/tools/business_data_search.py` | Provider 调用前将 entity 归一化为正式 key |
| `consumer/backend/ontology/package_import.py` 及导入校验代码 | 增加目录与详情别名一致性、冲突和引用校验，区分 errors 与 warnings |
| `consumer/backend/api/routes/ontologies.py` | 在安装版本前阻断别名校验错误，将其他 warning 写入 `warnings.object_aliases` |
| `consumer/backend/tests/test_tool_registry.py` | 增加别名路由、正式 key 返回和旧包兼容测试 |
| `consumer/backend/tests/test_package_import.py` | 增加别名一致性和冲突校验测试 |
| `consumer/backend/tests/test_business_data_search_resolution.py` | 增加 entity 归一化、mapping 命中和 `object_context_usage` 测试 |
| 各本体包 `objects/index.md` | 增加并填写别名列 |
| 各本体包 `objects/<object_key>.md` | 在 frontmatter 中同步 `aliases` |

`consumer/backend/skill_parser/modeling_template.py` 已同时生成目录“别名”列和详情 frontmatter `aliases`，本期不强制修改其列头，由统一解析器兼容“中文名称”等现有列头。`legacy_package_converter.py` 不自动编造旧包中不存在的业务别名。Provider 层由于入参已在公共工具入口归一化，不需要修改别名逻辑。

## 8. 非本期范围

本期不做：

- 字段别名的统一解析。
- 图谱节点增加 `aliases` 属性。
- 前端别名编辑或展示交互。
- 别名的模糊匹配、拼音匹配、分词扩展或向量召回。
- 基于模型自动生成别名。
- 修改 mapping 格式以保存别名。
- 在各个 Provider 内重复实现别名解析。
- 在没有性能数据前引入持久化别名缓存。

## 9. 验收标准

以下条件全部满足后，本需求才算完成：

1. 根上下文能一次性看到对象 key、标题、别名和说明。
2. 用户使用唯一别名提问时，能加载正确对象详情。
3. `ontology_context` 和 `business_data_search` 的后续链路使用正式 `object key`。
4. 别名有歧义时返回全部候选，不加载详情、不调用 Provider。
5. 包校验能发现目录与详情别名不一致及各类冲突。
6. 不含别名的旧包和现有正式 key 调用不受影响。
7. 新增及相关回归测试全部通过。
8. 使用现有中文 key 本体包回归时，正式 key 调用和别名调用最终命中同一 mapping 条目，不发生中英文或 snake_case 形态转换。
9. 别名调用下的 `object_context_usage` 记录正式 `object key`，不记录请求别名。
