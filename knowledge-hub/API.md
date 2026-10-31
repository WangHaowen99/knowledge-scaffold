# Knowledge Hub API

Hub2 没有 Hub1 存量兼容：原项目级包 release-requests、releases 和 ZIP 下载路由已删除。当前包操作只使用以下通用接口。建模服务已接入；评测服务在后续 eval1 接入，消费服务在后续阶段适配。

## 鉴权

所有包请求同时携带 `Authorization: Bearer <access-token>` 和 `X-Hub-Service-Token`。Hub 根据不同服务凭据识别 modeling/evaluation/consumer；通过配置的统一账号服务 `/userinfo` 在线验证访问令牌及撤销状态，不回查建模项目。账号服务检查签名、issuer、audience、access 类型和 token version。用户所有权来自可信 issuer 命名空间与 subject，不接受请求体 owner、角色、项目或空间。

同一用户可通过三个服务访问自己的包；服务密钥不能代替用户权限。服务写入类型：modeling=`modeling_content`，evaluation=`mapping_update`，consumer=`external_import`。只有 modeling/evaluation 可创建 `evaluation_snapshot`。生产未接入的服务凭据可留空。

## 发布与读取

| API | 调用方 | 输入 | 输出 |
| --- | --- | --- | --- |
| `POST /api/package-publish-requests` | 三种服务 | request_id、ontology_id、change_type、input_digest、kind；已有包指定 hub_package_id；派生指定 base_release_id/base_digest；外部导入必须给 version | 首次 201，幂等 200；包 ID、预留版本 ID/版本号、状态、过期时间 |
| `PUT /api/package-publish-requests/{request_id}/content` | 原用户和原服务 | multipart file、X-Package-Digest | uploaded 状态、摘要、大小 |
| `POST /api/package-publish-requests/{request_id}/commit` | 原用户和原服务 | 无额外业务参数 | committed 回执 |
| `GET /api/package-publish-requests/{request_id}` | 原用户和原服务 | 请求 ID | pending/uploaded/committed/expired 回执 |
| `GET /api/packages` | 三种服务 | offset/limit | 当前用户可见的包摘要 |
| `GET /api/packages/{id}/releases` | 三种服务 | kind，默认 release；offset/limit | 已提交版本摘要 |
| `GET /api/packages/{id}/releases/{rid}` | 三种服务 | 精确包和版本 | 版本回执 |
| `GET /api/packages/{id}/releases/{rid}/package` | 三种服务 | 精确包和版本 | 原 ZIP、Content-Length、X-Package-Digest |

列表返回 `items,total,offset,limit,next_offset`，limit 默认 20、上限 100；响应有 64 KiB 边界，按 next_offset 继续。回执包含 `hub_package_id,release_id,ontology_id,version,kind,source_service,change_type,package_digest,size,created_at,committed_at`，派生包另含基础版本及摘要。响应不含 owner、项目、空间或磁盘路径。

版本号由 Hub 分配（外部导入除外，自带版本）：正式版本 `release` 在包内按 `0.1.N` 递增；评测快照 `evaluation_snapshot` 命名为 `<基础版本>-eval.<n>`，如 `0.1.10-eval.2`，n 按同一基础版本计数，基础版本本身是快照时沿用其原始基础版本，没有基础版本时以 `0.1.0` 为基础。快照不占用正式版本号；本规则之前已提交的快照保留原版本号。

新建包省略 hub_package_id，由 Hub 分配。建模/评测按包分配不可复用 `0.1.N`；外部导入保留其版本和原 ZIP 字节。同包版本（含快照）唯一。幂等键是用户、服务、request_id，改变输入返回 409；响应丢失先查状态，不换请求 ID。

当前支持 `ontology-package` schema `0.1` / `0.2`（`operation-centric.v1`），单包根为 ontology_id（manifest.yaml.id）。校验 ZIP、路径、大小、manifest、索引引用及 mapping。`mapping_update` 必须基于同包已提交版本，只允许增加/替换 `mappings/object-data-mappings.json` 和修改 manifest version；其他文件、manifest 字段、Markdown 索引保持不变。Hub 不生成 mapping，不连接评测数据源。未知格式或变更拒绝。

错误返回 `code,phase,retryable,message`：401 身份无效，403 服务能力不足，404 资源不存在/不属于当前用户，409 输入/版本/摘要冲突，410 请求过期，413 超限，422 请求字段错误，503 认证或存储不可用。不会回显请求体中的秘密或上游内部异常。

## 项目最新资料（独立业务）

资料按项目授权，仍由建模服务的授权回调处理；它与通用包权限无关。

| API | 调用方 | 说明 |
| --- | --- | --- |
| `PUT /api/projects/{pid}/resources/current` | 建模后台 | multipart file、X-Resource-Generation、X-Resource-Digest；当前资料全量替换 |
| `GET /api/projects/{pid}/resources/current` | 有项目读取权限的用户 | 分页当前资料清单 |
| `GET /api/projects/{pid}/resources/current/{file_id}` | 同上 | 当前资料下载 |
| `DELETE /api/internal/projects/{pid}/artifacts` | 建模后台 | 回查已持久化删除任务与项目不存在，清理项目资料；不删除独立包 |

资料只保留当前状态，不入包、不入归档 Git。低 generation 或同 generation 异摘要返回冲突。

## 建模业务入口

建模本地 `POST /api/projects/{pid}/package/releases` 直接使用 Hub2，输入 request_id、allow_unbound；项目只用于本地权限及编辑内容，不传给 Hub。发布入口继续提示调研、对象、操作、校验阶段；成功后打开「已发布版本」列表，不自动下载。对应 GET 列表与指定版本下载按当前用户的本地包关联读取。

`GET /api/hub/packages`、包版本列表和下载用于个人包浏览；`POST /api/hub/imports` 接收 request_id、name、hub_package_id、release_id、package_digest，创建本地建模项目并保留包身份与基础版本。GET `export?scope=package` 不再隐式发布。没有旧版本复制接口或发布目标开关。

字段命名：API、回执及持久化记录统一使用 `ontology_id`，其值等于知识包 `manifest.yaml` 的 `id`。清单格式保持不变，不接受旧字段名；`hub_package_id` 和 `release_id` 仍分别表示 Hub 包与发布版本。
