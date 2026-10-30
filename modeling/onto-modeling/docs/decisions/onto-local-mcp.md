# Onto 项目级本地 MCP

## 背景

转发 Modeling MCP 需要远端项目及知识包同步，无法保证工具读取的是 Onto 当前建模结果。

## 决策

由 Onto 提供对象、操作、规则的 15 个 MCP 工具，保留页面管理接口，新增本地 HTTP/SSE
端点。文档仍是数据源，以内容摘要作为当前 revision，修改与网页写入共用项目锁，
并将目标文件提交到项目 Git。Token 摘要存放于工作区之外的 SQLite 文件，不随知识包导出。
每次调用检查项目权限、Token 有效期和工具 scope。

新建、导入、克隆不再因 MCP 创建远端项目。原有评测、历史关联清理仍保留。

## 替代方案

继续同步到 Modeling 会引入双份状态和同步失败重试，因此未采用。
复用 Modeling 的工具名称及调用形式，但持久化和校验由本地工作区实现。

## 影响与边界

- 原 Modeling Token 不能用于新端点，需重新签发本地 Token。
- 读取基于当前工作区；传入过期 revision 会被拒绝，不提供任意历史快照读取。
- 版本回退通过 `list_project_versions`、`preview_project_version_revert`、
  `revert_project_version` 三个工具提供；预览凭据绑定项目、Token、用户、目标版本和
  预览时 HEAD，默认十分钟过期，确认调用可幂等重放。
- 版本列表、回退预览和回退执行统一使用 `project.version.revert` scope；预览与执行仅允许项目 Owner/Admin。
- 预览凭据保存在当前服务进程内，服务重启后需要重新预览；这避免将待执行的破坏性操作
  持久化到项目文件或知识包中。
- 列表每页最多 200 项，请求体最大 1 MiB；详情返回结构化文档，大小随文档变化。
- SSE 会话保存在单进程内存，服务重启后客户端需要重连。
- `report_name` 用于业务名称展示和导出再导入，文件名与内部 ID 保持稳定。
- 校验基于本地文档、引用、编辑状态及 revision；不调用 Modeling 的发布检查。

## 验证

`backend/tests/test_onto_mcp.py` 覆盖读写、校验、Token 权限、冲突、回滚和实际 SSE
传输。归档与 Studio 测试覆盖名称保留、旧格式图关系及图边去重。
