# 0093 规则的原始资料出处用原件锚点，网关文本版带原件位置；Studio 最后更新时间进 ST-03

## Context

develop（!589）给规则加了「原始资料来源」节：出处写资料原件路径加锚点（`#L12`、`#p:12`、`#table:1/row:2/cell:3`、
`#sheet:规则!B3`），资料分析报告的来源位置和「详细信息」出处同样写法，`research_checker` 用 `source_extract` 直接读原件
核对；点出处打开原文并定位高亮（Word 段落与表格单元格、Excel 单元格与合并区、文本行号），PDF、图片在平台内打开，
其他格式给下载，预览上方有「返回 <文档名>」。Studio 表视图加「最后更新时间」列（`GET /studio/updates`）。

本分支的事实：

- ADR-0076：主容器里不解析资料原件；ADR-0074：资料分析只读多源连接器转出的文本版（`resource/text/…`）。
- 第一轮（2026-11-11）因此改成出处写文本版行号、在文本版里按行高亮，做不到单元格、合并区、PDF 原样打开，记了差距。
- 用户 2026-11-12 要求：除评测优化外，行为和页面元素都与 develop 一致；ADR-0076 只约束实现方式，不能拿来删减行为。

**目标**：出处、核验、预览和 develop 一模一样；主容器照旧不解析原件。
**成功标准**：同一份 Word、Excel，经文本版得到的段与锚点和 develop 直接读原件逐段相同（文字、编号都一样）；develop 的
`test_source_preview`、`sourceLocation`、`studioUpdates` 测试按原断言通过。

**约束**

1. 不解析原件：主服务、助手都只读网关的输出；需要原件结构就让网关的转换输出带上位置。
2. 编号要和 develop 的 `source_extract` 一致：Word 段落按正文里的段落元素数（空段也占号），表格按表格、行、单元格元素数；
   Excel 按真实行号、列字母；转换器只转有字的行，行号必须单独记。
3. 转换输出照旧给人看、给 schema 表头解析用：不能改变正文、表格的写法。
4. 已经转好的文本版要能补上位置。

## Decision

**推导**

- 位置放在哪：放进正文（每行加注释）会改表格行，schema 表头、选出处列表、文本版页签都要改解析，违反约束 3。放到文本版
  **文末一段 HTML 注释**里（`<!-- onto:anchors 1 … -->`，一行「正文行号 锚点」），渲染时整段不显示，Markdown 表格和位置
  标记的解析都不受影响。写法：Word `p:<n>`（带标题、列表记号长度，读回原文时去掉）、`table:<t>/row:<r>`（第 k 列就是
  `cell:k`）；Excel `sheet:<名>`、`row:<Excel 行号>`、`merge <名> A1:C1 …`、`cols <名> <n>`（整张表有值的最右一列，
  给 P5a 的 Sheet 范围预检）；PDF 用已有的页标记，锚点 `page:<n>`（develop 读不了 PDF，这是本分支多出来的）。
- 谁来还原：`source_extract.extract(资料区里的 Word、Excel、PDF)` 读文本版和锚点注释，还原出和直接读原件同样的段与锚点；
  只读原件字节算哈希，并核对文本版文件头记的原件修订，资料改了还没重转就判无法核验。资料区以外的文件照 develop 直接读
  （测试、离线用法）。这样 `research_checker`、R-06、EV-29、来源预览不用各自改，develop 的出处写法、核验规则原样成立。
- 助手怎么写出锚点：按 `--sources` 给的 `read` 读文本版，`source_extract.py <文本版>` 回 `cite`（原件路径）和每段的原件锚点，
  出处写 `cite#anchor`；不用自己数，也不碰原件路径（读守卫照旧拦原件）。
- 补老数据：Word、Excel 的文本版没有锚点注释时，启动迁移强制重转一次（convert-docx 1.1.0、convert-xlsx 1.2.0）。
- 来源预览：照 develop 加 R-17 `GET /resources/source-preview`（窗口、上限、提示都照搬）；前端照 develop 的 SourcePreview、
  `lib/sourceLocation.js` 做在资料区的预览卡里，资料芯片、「查看原文」带锚点跳来时用，上方「返回 <文档名>」。PDF、图片由
  F-04 原样输出给浏览器打开，不算解析。Excel 走资料区的表格预览（`excel_preview`，ADR-0076 已列的例外，复用不新增）。
- 最后更新时间：本分支条目表就是 ST-03，时间放进行里（`updated_at`）、`sort=updated`；遍历历史按 HEAD 缓存，每次只多一条
  `git status`。`studio.updates()` 的行为与 develop 测试一致；develop 的 `/studio/updates` 没有调用方，不另开。

**最强的反对理由**：文本版和原件之间多了一层，转换器一旦算错编号，出处就悄悄指错位置。处理：编号规则和 develop 抽取器逐条对齐，
测试用同一份 Word、Excel 比对「经文本版」与「直接读原件」的段，锚点、文字逐段相等；文本版文件头带原件修订，原件改了还没重转就
判无法核验，不会拿旧位置充数。代价：网关转换输出多一段注释（每个段落、表格行一行）；老项目升级后要等重转完才有位置。

## Alternatives considered

- **网关开一个按需抽取接口，主服务要位置时现解析**：每次核验、预览都要过网关，网关不在时整条出处链路不可用；文本版已经在
  资料区，带上位置就够了。
- **出处继续写文本版行号（第一轮）**：和 develop 的出处写法、核验、预览都不一致，用户不接受。
- **位置写在每行末尾的注释里**：要改所有读 Markdown 表格的地方（schema 表头、选出处、文本版页签），风险大。

## Consequences

- 规则多一节「原始资料来源」；资料分析、规则提取的说明与 develop 一致；`research_checker` 照 develop 核对清单与「详细信息」出处。
- 早先写文本版行号（`resource/text/…#L12`）的出处照旧能核对，资料区里在文本版里定位。
- `source_extract` 抽取器版本 2；结果多 `text_version`，Excel 多 `merges`。
- 文字差异（只影响 ID 溯源的文字，锚点不受影响）：Excel 取公式的缓存值（develop 读公式本身）；Word 单元格里的多个段落用空格连
  （develop 用换行）。
- 剩下的差距见 `progress/gaps.md`「2026-11-11 · P5b」。
