# 0098 正式版本以知识包 Hub 为准，求解验证移到评测服务

取代 ADR-0020 的「包出」中 X-01 与本地版本号、「验证回执入」整节；PB-05～PB-07 保留（反馈入 X-05 后由 ADR-0092 退役）。

## Context

**目标**：建模项目发布的每个正式版本，评测服务、consumer 导入、其他建模项目都拿到同一份字节、同一个版本号；
求解知识包的端到端验证在发布之后的独立评测里做，不再卡发布。成功标准：

- 版本号只有一个来源，两处发布不会撞号，同一个发布请求重试得到同一个版本；
- 评测服务、consumer 只认 Hub，不再按项目到 onto 拉包（X-01）；
- 历史页、发布记录、按版本下载照常可用；
- 求解包发布前只要静态检查通过，并由发布人确认「求解能力尚未经端到端验证」（同一修订已验证过的免确认）。

**基本约束**

1. Hub 是独立服务、按登录用户授权：onto 调 Hub 要带用户令牌（或用户签过的委托），不落盘用户凭据。
2. Hub 不可用时新发布必须失败，不能退回本地自算版本号（否则两边各有一套号）。
3. 发布只读提交导出的只读树（ADR-0023），工作区没提交的改动不进包；这条不能因为接 Hub 而改成读工作区。
4. 发布请求可能在任一步断开（预留、上传、提交）：重试要能接上，Hub 已提交而本地没记上的要能恢复。
5. Hub 包的根目录与 manifest id 是 Hub 包身份（项目 ID 的固定 UUIDv5），版本号写进 manifest，Hub 逐字核对。
6. 评测服务按原路径调 `GET /api/hub/package-projects`（用户令牌），路径不能变。

## Decision

**推导**

- 版本号由约束 2 只能由 Hub 分配：先向 Hub 预留请求拿到版本号，再按这个版本号渲染包、上传、提交。渲染依赖版本号，
  所以「冻结输入」要在预留之前、「渲染」在预留之后。
- 冻结的输入按约束 3 是提交导出的只读树：发布请求目录 `hub-tasks/packages/<项目>/<主体>/requests/<请求>/input/`
  保存这棵树的副本，摘要（文件、数据绑定、包名、降级、图模式）作为 Hub 的 `input_digest`。之后重试只读这份副本，
  工作区与 HEAD 再变也不影响这次发布（约束 4）。
- 门禁在预留之前跑完（建模阶段、数据绑定、求解静态检查、consumer 导入规则、发布确认），失败不占 Hub 版本号。
- 建模阶段（2026-11-12 起，与 develop 同规则）：资料分析、对象提取、操作提取（含旧 logic 目录）、质量检查四个必经阶段，
  阶段目录里有可见文件才算完成（index.md、隐藏文件、符号链接不算），资料分析验收没通过且对象没提取时不算。阶段按工作区判定
  （与 develop、项目页的阶段探针同口径；这些目录不在发布读取的只读树里）。网页、API、MCP 都不能绕过，缺阶段回 409
  `modeling_stages_incomplete`。第一轮曾以「插件流程的发布关卡替代」为由不做，第二轮按用户要求「行为与 develop 一致」改回。
- Hub 提交之后的本地记录是显示与缓存，不是权威：在发布的提交上打 `release/<Hub 版本>` 注释标签（标签说明带
  发布人、降级、`Hub：<包 ID> <版本 ID>`），把上传的那份包留存到 `releases/<项目>/<版本>.zip`，写最近一次发布。
  这一步失败只记日志：发布已在 Hub 生效，留存缺失时按版本下载会带登录令牌从 Hub 下回缓存，没有登录令牌
  （服务令牌下载）就按标签提交重新渲染并标 `backfilled`。
- 同一 request_id 重放得到同一个 Hub 版本；新的请求总是 Hub 的下一个版本，同一提交再发布也一样（不再「同一提交
  沿用原标签」：版本权威在 Hub，本地按提交去重会和 Hub 的记录对不上）。一个提交可以有多个发布标签。
- 求解验证：端到端验证需要运行中的 consumer 与求解服务，属于评测服务；发布门禁只保留静态检查，另要求发布人在
  发布时确认未验证（`solver_unverified_acknowledged`），确认人与冻结时的求解契约修订记在 Hub 发布记录旁。修订
  已在早先版本通过评测服务的求解验证时免确认（由评测对接提供判定）。求解契约修订把对象的数据绑定计入。
- MCP 发布：令牌含 `package.publish` 时签发即在 Hub 登记委托（不保存登录令牌），后台每步按委托签名调 Hub，
  每步前按令牌 ID 复核吊销、过期与所有者。

**退役**

- X-01 `GET /api/v1/contract/releases`、求解验证候选包 `GET /api/v1/projects/{id}/solver/candidate`、
  X-04 `POST /api/v1/projects/{id}/solver/receipts`、SV-04 `GET /api/v1/projects/{id}/solver/receipts`、
  服务令牌权限 `receipts.write`、本地验证记录（`services/solver_validation.py`）；
- 发布门禁里的「求解契约回执」改为「求解契约」（只看静态检查，带 `requires_acknowledgement`）；
- `services/publish/versioning.py` 不再计算版本号，只管标签名与标签说明。

**接口**

- PB-03 `POST /api/v1/projects/{id}/releases`：要统一账号登录；请求体加 `request_id`（幂等键，缺省每次新请求）
  与 `solver_unverified_acknowledged`；响应多 `hub`（`ontology_id`、`release_id`、`package_digest`、`committed_at`）。
  鉴权关闭的本地模式没有 Hub 身份，返回 401。
- 发布记录（PB-04/05）多 `hub`；旧版本地发布的标签没有 Hub 身份，`hub` 为 null。
- 新增 `/api/v1/hub/packages`、`/packages/{id}/releases`、`/packages/{id}/releases/{rid}/download`、`POST /imports`；
  项目的 Hub 版本与共享资料 `/api/v1/projects/{id}/hub/releases`、`/hub/releases/{rid}/download`、
  `/hub/resources`（GET、PUT、`/status`、`/{file}/download`）；外部路径 `GET /api/hub/package-projects` 原样保留。

## Alternatives considered

- **Hub 只做留存，版本号仍由 onto 按主线计数**：两个建模项目发布同一个 Hub 包（成员共用身份）、或项目复制后
  各自发布，就会撞号；Hub 的版本连续性也没人保证。否决。
- **按工作区冻结（develop 的做法）**：会把没提交的改动带进包，和「发布只出提交」（ADR-0023）冲突，也让历史页的
  发布标签对不上内容。否决。
- **本地按提交去重，同一提交只发一次**：Hub 包被删除或换了关联后，本地仍以为已发布；去重应由 Hub 按请求做。否决。
- **Hub 不可用时先本地留存、恢复后补传**：本地要先编一个版本号，违反约束 2。否决。

## Consequences

- 发布依赖 Hub 与统一账号；本地开发不开鉴权时不能发布（门禁、预览、导出照常）。环境变量见 `ONTO_HUB_URL`、
  `ONTO_HUB_SERVICE_TOKEN` 等（部署文档）。
- 发布标签名沿用 `release/<版本>`；旧版本地发布的 `release/0.{n}.0` 标签照常列出。若旧版留下的 `release/0.1.N`
  恰好与 Hub 分配的版本同名且指向别的提交，旧标签不动、这次不另打标签（发布照常有效，记录在 Hub）。
- 评测服务改为从 Hub 取包；`releases.read` 服务令牌仍可读 PB-05～PB-07。
- 代价：发布多了一次网络往返和一份请求目录（发布成功后输入副本即删；未完成的 7 天后清理）。
- 与 develop 仍不同的地方（PR555 自身的架构决定）：发布按提交冻结（develop 按工作区）；单人所有权（develop 的 viewer、
  成员签发发布 token 按所有者处理）；接口在 `/api/v1`。界面元素与文字照 develop，摆放按新 UI（发布页是整页，发布检查是卡片）。

## 补充（2026-11-12）：知识包共享与删除项目时一并删除 Hub 包（develop !590）

**知识包共享**。develop 把「所有者加建模者（成员 role=editor）」同步成 Hub 授权用户。本分支项目只有所有者（单人所有权，
不恢复成员），用户口径是「知识包可以共享，项目不可以」，所以共享在知识包这一层管理：共享名单存在 Hub 任务目录
（`hub-tasks/packages/<项目>/members.json` 的 `shared`，随备份），不进项目元数据、不给项目权限。其余照 develop：

- Hub 授权语义：所有授权用户权限相同（全部版本读写、授权管理、整包删除），最后一人不能撤；
- 首次发布（含首次评测快照）时初始化：所有者加已设的共享用户整体授权一次；之后名单增删按增量排 pending 队列，逐个同步，
  授权在前、某个授权失败就停下；结果记 synced / failed，按用户重试；普通发布不全量恢复其他渠道撤销的授权；
- 授权前在统一账号目录核实账号（`auth.resolve_hub_member`，要 `ONTO_WEB_AUTH_ADMIN_TOKEN`），授权对象是可信 issuer + sub。

**换所有者**。管理员给项目换所有者（AD-07、删用户时移交）：给新所有者排一次授权，旧所有者留作共享用户，可以手动移除。
同步要用 Hub 上已授权用户的令牌，管理员通常不是，所以只尽力同步一次，没成功的留在 pending：新所有者要等某个已授权用户
（如旧所有者在 Hub 上）授权，或旧所有者在被移除前完成同步。这是单人所有权下的代价。

**删除项目时一并物理删除 Hub 包**。完全照 develop：P-06 带 `delete_hub_package` 与 `hub_usage_confirmed`，先在 Hub 项目锁内
同步完成远端 DELETE（整个本体），记删除进度（`deletion.json`）；远端失败保留项目和进度，可以重试；删除中或已删除时 Hub 发布、
共享修改一律 409。HB-10 给删除确认框看包名、本体标识与进度。
