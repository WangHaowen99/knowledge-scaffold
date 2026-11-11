# 0091 正式版本以知识包 Hub 为准，求解验证移到评测服务

取代 ADR-0020 的「包出」中 X-01 与本地版本号、「验证回执入」整节；反馈入 X-05 与 PB-05～PB-07 保留。

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
- 门禁在预留之前跑完（数据绑定、求解静态检查、consumer 导入规则、发布确认），失败不占 Hub 版本号。
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
