# 0096 数据 Schema 的 Sheet 范围与表头预检：行为同 develop，读网关的输出

## Context

**目标**：一份标成「数据 Schema」的表格里，常混着封面、说明、字段字典、术语表这类 Sheet。它们被当成物理表，对象提取就会
从说明页里造字段，发布的数据绑定也会绑到不存在的表上。develop（提交 bee5e00a、a4328d1a、dfffdaef）的做法是：

- 上传或标为 schema 时按结构算出范围（哪些 Sheet 是数据表），连同文件 sha256 存进 `materials.json`；
- 用户在资料区看每个 Sheet 的表头问题（第一行为空、合并、缺列名、重名），勾选范围后存成人工范围；
- 文件换了，人工范围要重新确认，不悄悄换成现算的结果；对象提取、字段核对、数据绑定只认范围内的 Sheet。

第二轮用户要求：除评测优化外，行为和界面元素都与 develop 一致。

**约束**：

- ADR-0076：主服务不解析资料原件，只读连接器网关转出的文本版。它只约束实现方式，不能拿来删减行为。
- 工作区脚本（table_profile）跑在助手的环境里，导入不了后端代码；后端和脚本必须是同一份规则。
- Excel 的文本版是上传之后异步转出的；上传那一刻还没有。
- 旧项目的 `materials.json` 里只有文件级的 `schema` 标注，读的时候不能改写它。

## Decision

1. **规则只有一份，照搬 develop 的 `table_sources.py`**，放在工作区脚本里（只用标准库），后端 `services/table_sources.py`
   按路径加载，`material_detect` 用同一组名字转出。判定、范围、缓存（按文件 sha256，`force` 重扫，检查期间文件变了报错）、
   问题文字都与 develop 相同。
2. **只换读法**：develop 用 openpyxl 读原件；这里 Excel 读文本版，它带网关写的原件锚点（ADR-0093：每行的 Excel 行号、
   合并区，另请网关加整表最右一列 `cols`），由此算出物理第一行、第一行是否合并、有内容的最右一列，和 develop 读原件的结果一致。
   老文本版没有锚点时按表格本身推断（第一行就是表头、宽度按转出的行），启动迁移会重转补上锚点。CSV 照 develop 直接读。
3. **范围的存法同 develop**：`{version: 1, mode, included_sheets, reference_sheets, file_sha256}`。CSV 上传或标为 schema 时当场存；
   Excel 上传那刻还没有文本版，在网关转出文本版的同一个提交里存（`record_schema_scope`），标为 schema 时文本版已在就当场存。
4. **接口**：R-01 的 `Material.schema_scope`、`Material.schema_candidate` 就是 develop resource-meta 的 `schema_scopes[path]`、
   `schema_candidates[path]`；R-16 `GET …/schema-preview?force=` 对应 develop 的 POST（读接口用 GET，权限要能写，单人所有权下即所有者且未归档）；
   R-03 的 `schema_selection` 同 develop。范围错误 409 `schema_selection_invalid`。
5. **Excel 还在转文本版**时（develop 没有这种状态）：预检回 409 `material_text_pending`，弹窗里显示，不能确认；转换失败或暂不支持时按
   develop「文件无法解析为数据表」报；数据绑定、字段核对读不到这张表，与其他还没文本版的资料一样列为未读。

上传那一刻判用途（`material_detect.detect_material`）和「疑似数据表」候选仍读原件，是 ADR-0076 记下的遗留点，本 ADR 不扩大它。

## Alternatives considered

- **照搬 develop，在主服务里解析原件**：违背 ADR-0076。
- **只存人工范围、现算的不存**（第一轮的做法）：结果相同，但 `materials.json` 的内容与 develop 不同，用户要求一致，已改回。
- **Excel 上传时同步等网关转完再存范围**：上传要多等一次转换；转换本来就会提交一次，在那个提交里存，内容与 develop 相同。

## Consequences

- Excel 标为 schema 后，范围要等文本版转出才存下（通常几秒）；之前资料行不显示范围，改用途为 schema 的预检提示稍后再查。
- 一个工作簿最多 200 个 Sheet（同 develop），多了要拆分。
- 整表最右一列靠网关的 `cols` 行；网关还没给时只看转出的前 2000 行，更靠后的「有数据、没列名」查不出来。
- 最强的反对理由：范围的计算依赖网关的锚点，网关和工作区脚本两处口径要一直对齐。处理：锚点格式只有一份（ADR-0093），
  这里的解析与后端 `convert.parse_anchor_index` 同口径，并按第一个工作表标记对齐行号，不依赖文本版头部的写法。
