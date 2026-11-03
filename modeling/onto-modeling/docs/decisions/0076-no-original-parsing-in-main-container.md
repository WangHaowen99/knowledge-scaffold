# 0076 主容器里不解析资料原件、锁恢复不问人

## Context

ADR-0073 把 PDF、Word、Excel 的转换放进连接器网关，ADR-0074 让资料分析只读文本版。但主容器里还有几处在解析原件：

- 内测引擎跑在后端进程里，openjiuwen 自带的 `read_file` 遇到 PDF 会用 pdfplumber 在进程内解析。pdfplumber、
  python-docx 是 openjiuwen 的依赖，而且在模块加载时就导入，镜像里卸不掉；
- 稳定引擎的 Read 能直接读 PDF、图片；两个引擎的 Bash 都能临时写脚本、调 pdftotext 解析原件；
- 对象提取核对字段用的 schema 表头（`table_profile`）、发布时的数据绑定（`package_mapping`）用 openpyxl 读 Excel 原件；
- 资料分析核验遇到旧报告指向 Word、Excel 原件的出处时还会解析原件。

另外，所有命令开头遇到上次没做完的同名命令时，会让用户选「续执 / 清理重来 / 取消」——这是内部机制。

用户定的两条原则：解析不可信文件不在主容器里做；内部机制不问人。

## Decision

- **目标**：资料原件在主容器里一次都不被解析；助手和平台读资料只读文本版；用户不被锁这种内部状态打扰。
- **约束**：openjiuwen 的依赖卸不掉；两个引擎、Bash 都能读文件；旧项目里有指向原件的出处；锁恢复要安全（不能让两个
  会话同时改）。
- **推导与方案**：
  1. 卸不掉解析库，就在调用前拦：新内核守卫 `resource_read_guard`（与写守卫同一套 PreToolUse 协议）。稳定引擎的
     settings 基线加 `Read|NotebookRead` 和 Bash 两处；内测引擎把 `read_file` 映射成 `Read`，走同一个守卫脚本。
     读 `resource/` 下的 PDF、Office、图片等原件一律拒绝：有文本版指向文本版，没有就说明读不了、按未读处理；
     Bash 里提到原件、又不是只看元信息的命令（ls、wc、sha256sum、file 等）也拒绝。
  2. `table_profile` 与 `package_mapping` 读资料区 Excel 时改读文本版里的 Markdown 表（后端 `convert.md_tables`
     与工作区脚本同口径），没有文本版就说明字段核验未完成，不解析原件。资料区之外的表格（助手生成的模拟数据等）照旧。
  3. 资料分析核验：出处指向原件的，按它的文本版整篇核对编号；没有文本版就算核对不了。语料只读文本。
  4. 锁恢复：新子命令 `state_helper.py recover`。锁停在有名字的阶段就续做（锁归本会话），看不出做到哪（start、
     confirm、旧锁）就清理重来；命令只在工作过程里说一句业务话。
- **最强的反对理由**：守卫按工具调用的文本判断，防得住误用和常见写法，防不住有意绕过（比如把原件复制改名后再解析）。
  处理：这和写守卫的定位一样，真正挡住要靠按项目隔离 agent 的进程（P4）；本期先把助手正常会走的路全部堵上，并在
  命令和子代理说明里写明只读文本版。

## Alternatives considered

- **从镜像里卸掉 pdfplumber**：openjiuwen 在模块加载时就 import 它，卸了引擎起不来。
- **给 openjiuwen 打补丁去掉 PDF 分支**：要维护第三方库的补丁，升级就丢；守卫在我们自己的代码里。
- **锁恢复按持有者是否存活定**：`locked` 本来就是持有者还在或旧锁，按存活判断等于什么都不做；按阶段判断能续就续。

## Consequences

- 内核守卫多一个脚本、settings 基线多两处，所有项目重新装配一次。
- 发布从提交导出只读树，表格的文本版跟着导出（`publish.tree.is_package_source`）；刚上传的 Excel 要等数据接入转出
  文本版，才能参与数据绑定。
- 后端还有四处在主容器里解析原件，不在本 ADR 范围：资料页的 Excel 预览（`excel_preview`）、上传时的用途识别
  （`material_detect`）、出处解析遇到旧格式锚点时的抽取（`material_text.extract`）、评测移出的启动迁移读旧评测
  上传表的表头（`eval_retirement`，一次性，下个大版本删）。前三处建议改成读文本版。
