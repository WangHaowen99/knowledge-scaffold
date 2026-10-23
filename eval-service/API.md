# Evaluation API

前缀 `/evaluation/api`。除 health、账号登录/刷新及受 scene 凭据保护的 NL2SQL 外，均要求 `Authorization: Bearer <access-token>`。身份在线验证并按 issuer/subject 隔离；不接受建模项目归属或请求体 owner。

| 接口 | 行为 |
| --- | --- |
| `GET /health` | 活性，不代表依赖就绪 |
| `GET /session` | 当前主体键与非敏感 Judge 配置 |
| `POST /auth/login`、`/auth/refresh` | 直连统一账号，响应不缓存；不保存用户令牌 |
| `GET /packages`、`/packages/{id}/releases` | 当前用户 Hub 包及正式版本列表 |
| `GET /packages/{id}/releases/{rid}` | 精确版本回执，用于跳转到历史版本 |
| `GET/POST /eval-sets`、`/data-sources` | 用户定义列表和创建 |
| `GET/PUT/DELETE /eval-sets/{id}`、`/data-sources/{id}` | 读取、带 revision 更新、删除定义；不改历史快照 |
| `POST /inputs` | multipart file，CSV/XLSX，返回受控句柄和摘要 |
| `POST /package-preparations` | request_id、package_ref、可选 data_source_id；冻结输入、生成 mapping、必要时发布快照 |
| `GET /package-preparations/{id}` | 状态、固定包引用及有界映射预览 |
| `POST /package-publications` | request_id、preparation_id；创建正式 mapping 派生版本，返回 Hub 回执 |
| `GET /package-publications` | 分页查询本地发布任务及 Hub 回执 |
| `GET /package-publications/{id}` | 本地持久发布任务状态；任务 ID 可由发布查询列表获得 |
| `POST /runs` | request_id、preparation_id、eval_set_id；持久化后返回 202 |
| `GET /runs`、`/runs/{id}` | 摘要、进度和统计 |
| `GET /runs/{id}/results` | 有界分页用例摘要 |
| `GET /runs/{id}/results/{case_id}/answer` | 单用例完整答案、判分及改判历史 |
| `GET /runs/{id}/results/{case_id}/evidence` | 从 consumer 按需读取有界执行事件索引，不复制完整工具报文 |
| `GET /runs/{id}/report` | 可重建的统计投影、包与模型引用 |
| `POST /runs/{id}/cancel`、`/resume`、`/credentials` | 取消意图、对账恢复或更新内存授权 |
| `POST /runs/{id}/results/{case_id}/override` | verdict、reason、revision；保留审计，不允许无答案或活动任务改判 |
| `DELETE /runs/{id}` | 终止并对账后清理自有资源，清理任务可重试 |

Hub 查询默认每页 20、最多 100；本地列表每页 20，按 next_offset 翻页。用例列表截短问题/理由，完整答案单独读取。映射预览最多 20 个对象，有 truncated 标志。consumer 单次响应最多 2 MiB、事件索引源最多 1 MiB/输出最多 100 项；超限明确失败，不伪装成完整结果。

普通请求体最多 1 MiB，数据上传最多 50 MiB，实际接收大小也检查；同源网关同步限制上传。Hub ZIP 最大 256 MiB、展开最大 1 GiB、成员最多 10000，拒绝路径穿越、符号链接、重复路径及过高压缩比。

错误格式 `code,message,phase,retryable`；依赖原始错误、连接地址和秘密不回显。鉴权失败 401/403，不存在或无权访问本地记录 404，并发或幂等冲突 409，过期 410，超限 413，格式错误 422，依赖不可用 503。

准备/发布/启动使用稳定 request_id，同键不同输入冲突；超时沿用原请求。Hub 发布与快照命名空间分开，服务不生成本地成功回执。数据库删除保留请求墓碑，不能通过重复请求重新创建已删轮次。

可选 NL2SQL：`POST /nl2sql/api/v1/query`、`/sql`。请求使用专属 scene HMAC 凭据，不使用用户登录；只允许本轮 eval 库的受限 SELECT、最多 1000 行。连接配置仅作为 consumer 运行设置，不进入 Hub 包。
