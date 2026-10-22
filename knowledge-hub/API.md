# Hub1 API

所有用户操作带 `Authorization: Bearer <access-token>`，身份转发到 Onto；服务写操作还须带 `X-Hub-Service-Token`。后者只证明调用服务，不授予用户读取权限。请求体中的角色或主体不作为权限依据。无效身份 401、无权限 403、项目/版本不存在 404、内容冲突 409、已删除项目 410、超限 413、依赖不可用 503。

| 方法和路径 | 行为 |
| --- | --- |
| `POST /api/projects/{pid}/release-requests` | `{request_id, source_digest, source_commit, publish_options, kind}`，分配不可复用 `0.1.N`；相同请求/身份/内容幂等，变化返回 409 |
| `PUT .../release-requests/{request_id}/content` | multipart `file`，`X-Package-Digest` SHA-256；校验包根、manifest ID/版本/格式、索引、路径和 ZIP 完整性 |
| `POST .../release-requests/{request_id}/commit` | 重新检查权限，完整包和 Git 成功后返回 committed 回执 |
| `GET .../release-requests/{request_id}` | 原用户查请求状态；pending 不出现在发布列表 |
| `GET /api/projects/{pid}/releases?offset=0&limit=20` | 仅正式版本，倒序；limit 1–100 |
| `GET .../releases/{release_id}` | 回执，含版本、摘要、大小、发布时间、来源和发布选项 |
| `GET .../releases/{release_id}/package` | 原样流式下载，`X-Package-Digest` 校验 |
| `PUT /api/projects/{pid}/resources/current` | Onto 后台同步专用；multipart file，`X-Resource-Generation` 和 `X-Resource-Digest`；低序号/同序号不同摘要 409 |
| `GET .../resources/current?offset=0&limit=20` | 当前资料分页清单，含句柄、相对路径、大小、摘要；不返回正文 |
| `GET .../resources/current/{file_id}` | 当前资料下载；不能传任意磁盘路径 |
| `DELETE /api/internal/projects/{pid}/artifacts` | 服务专用；Onto 必须确认项目不存在且有删除任务，再建立 tombstone 并永久删除包、资料、快照和 Git |

`kind=release` 为正式发布；`evaluation_snapshot` 为内部评测快照，另存目录、不进入发布列表、不更新正式发布记录，要求项目写权限。下载仍按源项目读权限校验。

最新资料 ZIP 只包含 `resource/{diagrams,excel,others}/` 与 `resource/materials.json`。空集合保留空元数据文件即可；元数据不能引用不存在的资料。资料不进入知识包 ZIP，不保存在发布回执中，不进入 Hub Git。相同资料 generation 的重试要求相同 ZIP 字节。

Onto 对浏览器提供 `POST /api/projects/{pid}/package/releases` 和相应列表、指定版本下载接口。发布请求 ID 在浏览器本地保存至发布且下载成功；超时重试沿用 ID。下载失败提示“已发布，下载失败”，可从已发布列表重试。旧 `export?scope=package` 入口按用户与输入摘要生成稳定请求 ID，同样必须先保存到 Hub。
