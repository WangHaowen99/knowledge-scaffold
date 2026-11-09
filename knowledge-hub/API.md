# Hub API

所有包、授权与资料 API 均要求实际用户 Bearer 身份及 X-Hub-Service-Token。
Hub 在线校验用户身份，再检查 ontology_id 的授权集合；服务凭据不能替代用户权限。
manifest.yaml 继续使用 id，API 统一称 ontology_id，不接受旧字段。

## 发布

| 方法与路径 | 输入 | 输出 |
| --- | --- | --- |
| POST /api/package-publish-requests | request_id、ontology_id、change_type、kind、input_digest；派生带 base_release_id/base_digest；external_import 带 version | 201 新预留 / 200 原请求；release_id、version、状态和过期时间 |
| PUT /api/package-publish-requests/{request_id}/content | multipart file、X-Package-Digest | 校验后的 name、version、filename、摘要和大小 |
| POST /api/package-publish-requests/{request_id}/commit | 无 | committed 回执；重复版本 existing=true |
| GET /api/package-publish-requests/{request_id} | 无 | 原用户和原服务的请求状态 |

modeling 只允许 modeling_content，evaluation 只允许 mapping_update，consumer 只允许正式 external_import。
正式自动版本为 0.1.N，快照为基础版本-eval.N。基础版本无需最新，但 ID 和摘要必须一致。
mapping 仅允许修改 mapping 和清单 version。

同版本同 ZIP 摘要复用原 release_id；同版本不同摘要返回 409 version_conflict。
实际上传字节必须核验，始终先鉴权。新 ID 只有提交成功才获得初始授权，其他新建竞争者不能自动获得授权。
所有请求以用户、服务、request_id 幂等；改变输入报冲突。预留请求不对包列表公开。

## 查询与下载

| API | 输出 |
| --- | --- |
| GET /api/packages | 当前用户授权的包摘要分页 |
| GET /api/packages/{ontology_id} | 单包摘要 |
| GET /api/packages/{ontology_id}/releases | kind 默认 release；可取 evaluation_snapshot |
| GET /api/packages/{ontology_id}/releases/{release_id} | 固定版本回执 |
| GET /api/packages/{ontology_id}/releases/{release_id}/package | 原 ZIP、Content-Length、X-Package-Digest |

回执包含 ontology_id、name、version、release_id、kind、source_service、package_digest、size 和时间；派生另含基础引用。
新版本另含 creator 对象（issuer、sub），由 Hub 从已验证的发布用户身份生成，不接受客户端指定。发布回执、请求状态、版本详情和列表均返回该字段；每个版本保留自己的 creator，同版本同内容重复上传保留原版本的 creator。旧版本没有 creator 时省略该字段，不回填。
调用服务须将 creator.issuer 与 creator.sub 同时与当前用户的可信身份匹配，以区分本人发布和其他授权用户发布；issuer 须使用 Hub 的 HUB_AUTH_ISSUER（未设置时为 HUB_AUTH_URL 去除末尾斜杠），不能只比较 sub。缺少 creator 表示发布者未知；该字段不改变授权规则。
包摘要取最新提交正式版本，无正式版本取最新快照。列表采用 offset/limit（默认20、最大100），返回 items/total/next_offset。
包摘要中的 creator 属于所展示的最新版本，不表示整个包的首版创建者。
展示文件名为去连字符本体前12位-安全化包名-版本.zip；不改变归档字节和摘要。

## 授权与删除

| API | 输入和行为 |
| --- | --- |
| GET /api/packages/{ontology_id}/authorized-users | 分页返回 issuer、sub、subject_id |
| PUT /api/packages/{ontology_id}/authorized-users | users 数组（1至100项），各项 issuer+sub；全部身份通过格式与可信 issuer 校验后原子增加 |
| DELETE /api/packages/{ontology_id}/authorized-users/{subject_id} | 幂等撤销，最后一人返回 409 last_authorized_user |
| DELETE /api/packages/{ontology_id} | 整体逻辑删除；原授权用户重复调用成功 |

所有授权用户有相同权限；Hub 不识别项目角色或授权来源。
目标账号的选择和存在性由调用服务负责。Hub 不查询账号目录、不持有账号管理凭据；操作者仍须通过在线身份验证及包授权检查。账号 sub 应保持稳定且不可复用。
删除保留 ACL 和墓碑，全部版本与资料停止访问，相同 ID 不可重新创建。项目删除不调用此接口。

## 本体共享资料

| API | 输入和输出 |
| --- | --- |
| PUT /api/packages/{ontology_id}/resources/current | 明确选择的资料 ZIP；X-Resource-Generation、X-Resource-Digest；原子全量替换 |
| GET /api/packages/{ontology_id}/resources/current | offset/limit；ontology_id、generation、digest、files、total |
| GET /api/packages/{ontology_id}/resources/current/{file_id} | 当前文件原字节及摘要 |
| DELETE /api/packages/{ontology_id}/resources/current | X-Resource-Generation；推进代次并保留空集合标记 |

只有 modeling 服务可以写或清空，全部服务均可按本体授权读取；包必须已发布。
同代次同摘要幂等、不同摘要冲突，旧代次拒绝。清空也推进代次，旧上传不能复活资料。
资料仅为最新集合，与历史包版本无绑定；包没有资料时返回空清单。
旧项目资料路径、项目删除清理接口和建模权限回调均删除。

GET /api/health 保持原契约：ok、service、free_bytes。

`GET /api/internal/ready` 供部署方检查服务凭据，要求 `X-Hub-Service-Token`，无需用户令牌。
成功返回 `{"ok": true, "service": "knowledge-hub", "identity": "modeling"}`；identity 也可为 evaluation 或 consumer。
未知凭据返回 401，服务凭据重复返回 503。该接口不读写知识包，不验证用户授权或账号服务的可达性。
常见错误为401身份无效、404不存在或无权、410已删除、409冲突、422请求错误、503依赖不可用。
