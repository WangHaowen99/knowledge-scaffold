"""插件产物登记与通用视图（详设 5.9.4、5.9.5；方案 C13）。

- registry：从已启用包的清单收集产物类型（格式、路径、字段、引用、状态映射）与视图声明；
- parsers：内核内置的格式解析器，不跑包的代码；
- flowchart：drawio ↔ 流程图模型（图示包登记的 `diagram.flowchart`）；
- index：按项目建的 SQLite 索引（字段筛选、排序、全文搜索），记引用条目的指纹用来判过期；
- service：通用读写接口（契约 AR-01～AR-09、MCP artifact_list / artifact_get）。
"""
