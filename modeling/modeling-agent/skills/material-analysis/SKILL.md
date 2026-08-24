---
name: material-analysis
description: 完整阅读资料、识别解析问题并形成有来源的目标与知识缺口
---

# 资料分析

- 先列清单，逐份从 offset 0 读到 eof，不用摘要冒充全文。
- 表格先看数据字典，再按需读取样例；不要从少量样例推断稳定业务规则。
- 明确区分资料原文、你的推断和待专家确认的假设。
- 文档事实的 `write_file.evidence` 使用 `material:<document id>#<原文锚点或表名/字段>`；document id 必须从 `list_documents` 原样复制。不要把文件名放进 token，文件名可能含 `#`，会和锚点分隔符冲突。
- 候选正文里的来源栏同时写 `list_documents` 返回的完整文件名和同一个 `material:` token；文件名不得增删空格、改写标点或使用自造简称。尚未定位到具体章节时明确写“待深读定位”，不要编造章节。
- 扫描件、空文档和解析失败立即用 `notify` 反馈，不阻塞其他资料分析。
- 输出分析到 `.modeling/analysis/`，目标草案写 `.modeling/goal.md`；不要在目标确认前写知识包正文。
- 资料不足时形成具体缺口，说明缺口会影响哪个流程或判断，再发资料请求。
