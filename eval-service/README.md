# 独立知识包评测（eval1）

在 Hub2 之后接入：从 Hub 固定版本下载 → 在评测页面配置用例和数据源 → 生成 mapping → consumer 问答 → Judge 判分 → 按需发布派生版本。服务不读取建模工作区或 Core 数据库，不保存建模项目信息，不运行建模 Agent；任务中心与执行详情直接展示本轮知识包引用中的名称和本体标识。

## 本地运行

```bash
cd eval-service
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cd frontend
npm ci
npm run build
cd ..
# 在进程环境注入运行配置后启动
.venv/bin/python run.py
```

打开 `/evaluation/`；直接访问服务根路径（如 `http://127.0.0.1:8090/`）会跳转到该页面。开发前端可用 `npm run dev`，代理默认本机 8090，可通过 `EVAL_DEV_API_URL` 覆盖。单独启动开发端口不与建模共享浏览器存储，免重复登录需从建模页面以直连方式打开（见下文）。

## 评测工作台

- 登录后默认进入全宽任务中心，活动任务置顶；筛选、搜索与统计覆盖全部已加载分页。列表与执行详情每 5 秒更新，切回页面立即同步。
- 任务列表“任务 ID / 知识包 ID”列分别显示完整任务 ID 与知识包的本体标识；“知识包名称”列及执行详情中的同名字段显示本轮知识包名称，不依赖建模项目查询。搜索支持任务 ID、知识包 ID、知识包名称及评测集名称。
- 任务列表“启动时间”列按本地时间显示任务开始执行的时间（日期与时分秒），尚在排队的任务显示创建时间，便于区分同一知识包的多次评测。
- 新建评测按“固定 Hub 版本 → 选择数据源 → 选择或编辑评测集 → 准备与确认”执行。问题可逐条编辑或导入 JSON；无参考答案的用例保留为待人工判定。
- 任务地址为 `/evaluation/#/runs/<run_id>`。关闭页面或重新登录后，从任务中心或原地址重新进入。查看任务不触发恢复执行；待授权与已中断任务需要明确确认恢复，停止任务需要确认并等待远端对账。
- 执行详情可筛选判分结果、定位正在执行的用例、查看完整答案与事件索引；非活动任务可人工改判，原因必填并保留历史。映射派生版本的发布单独确认，正式回执来自 Hub。
- 数据源和评测集有独立管理入口；修改定义不会改写已有任务快照。桌面全宽、笔记本折叠导航、窄屏任务卡片和全屏详情侧栏使用同一套组件。

## 配置与权限

变量见 `.env.example`；该文件仅是占位说明，不是可直接运行的凭据文件。部署时的实际配置由部署脚本维护在 `deploy/.env`（权限 600，不入库），或通过 `EVAL_ENV_FILE` 从仓库和构建上下文之外注入，见下文“独立部署”。

- `EVAL_AUTH_URL`、`EVAL_AUTH_ISSUER`：与 Hub 使用同一可信账号及稳定身份命名空间。
- `EVAL_HUB_URL`、`EVAL_HUB_SERVICE_TOKEN`：对应 Hub 的 evaluation 服务身份（Hub 的 `HUB_EVALUATION_SERVICE_TOKEN`），不复用 modeling 凭据。
- `EVAL_CONSUMER_API_URL`、`EVAL_CONSUMER_SERVICE_TOKEN`：后者对应 consumer 的 `EVALUATION_SERVICE_TOKEN`，与其 modeling 密钥不同；用户身份仍必需。
- `EVAL_DATA_DIR`：独立卷。默认 `.runtime/` 仅用于合成数据本地开发，部署必须使用外部运行卷。
- `EVAL_ENCRYPTION_KEY`：Fernet 密钥，用于连接配置、页面保存的 Judge API Key、上传数据和执行数据快照。部署脚本首次部署时生成并保存在 `deploy/.env`，不在数据卷内；备份时须与数据卷分开、同样受控保管。变更或丢失密钥会导致已有加密数据无法解密：已有数据卷而缺少密钥时部署脚本拒绝生成新密钥。
- `EVAL_JUDGE_BASE_URL`、`EVAL_JUDGE_API_KEY`、`EVAL_JUDGE_MODEL`、`EVAL_JUDGE_CONFIG_REVISION`：临时 Judge 部署配置。问答模型在发起评测时选择：consumer 全局模型池中已启用的模型，或 consumer 默认模型；记录其非敏感配置摘要。此方案标记 **MODEL-CONFIG-01，待 eval2 整改**。
- 页面配置 Judge：登录后进入侧边“评测判分模型配置”。所有用户可查看生效来源、模型和配置版本，只有管理员（账号服务中 role 为 admin）可修改，因为该配置对所有用户的评测生效，并会收到题目、参考答案和回答。
  - 管理员填写网关地址、模型和 API Key，可先“测试连接”（与轮次预检相同的结构化判分检查）再保存。保存的配置整体替换部署配置，“恢复部署配置”删除它。
  - API Key 用 `EVAL_ENCRYPTION_KEY` 加密存入数据卷，接口和页面从不返回。网关不变时 Key 可留空沿用；更换网关必须重填，部署 Key 不会被发往新地址。
  - 配置版本自动生成；值变化后，排队及进行中的轮次会停止判分并提示重新创建轮次。可选 NL2SQL 使用默认 Judge 配置（不随个人配置变化）。
  - 管理员页面分两部分：“我的判分模型”与其他用户相同；“默认判分模型”维护供所有用户使用的默认配置，并带启用开关。停用后，没有个人配置的用户（含勾选“使用默认配置”的管理员）不能发起新评测，提示先填写自己的判分模型；这些用户排队及进行中的轮次停止判分并中断。重新启用不改变配置版本，中断的轮次可恢复。个人配置不受影响，NL2SQL 仍使用默认配置。开关缺省为启用。
- 个人判分模型：“评测判分模型配置”页面默认勾选“使用默认配置”。取消勾选后可填写自己的网关、模型和 API Key，保存后成为本人单独的配置，只覆盖本人新发起的评测；重新勾选即删除个人配置、切回默认配置。生效顺序为 个人配置 > 管理员页面配置 > 部署配置；默认配置停用时“使用默认配置”不可勾选。没有可用的判分模型时，新建向导的“开始评测”仍可点击，点击后提示未配置的原因并可前往配置。
  - 个人配置的 Key 同样加密存储、从不返回；留空时只沿用本人此前保存的 Key（同一网关），不会借用默认配置的 Key。
  - 判分时会把本人评测的题目、参考答案和回答发送到个人配置的网关；管理员修改默认配置不影响已设置个人配置的用户。
- 优化建议同样由该用户生效的判分模型生成。除失分用例的题目、参考答案、回答和判分理由外，还会把知识包文档节选，以及用例结算时记录的工具调用发送到判分模型网关。工具调用记录包括工具名、参数与结果的截断预览（脱敏后每项不超过 200 字）、错误码和错误信息；每个用例最多附 6 次调用。预览中可能包含从评测数据源查出的数据。工具调用记录单独存放在评测数据库中，每个用例最多保留 12 次调用，随评测记录一同删除。
- 失分用例按单次约 3.2 万字的输入分批分析，适配 32k 上下文的判分模型；与同一知识包文档相关的用例放在同一批，每批只带与本批相关的文档。文档按问题、参考答案、判分理由、工具调用和回答的相关度挑选：工具调用直接点名的脚本文档、所查数据表对应的对象文档优先；长文档保留开头和匹配的章节；各 index.md 合计不超过文档份额的 15%；数据映射超出份额时只保留所选文档对应的对象。网关返回“输入超出上下文”时自动按一半大小重新分批，无需配置。多批时再调用一次合并同一根因的条目，合并失败或丢失用例时直接拼接各批结果。单次生成最多预先规划 8 批、发起 14 次请求，超出上限或所在批次失败的用例会在建议开头列出。推理型判分模型可能思考数分钟，单次请求最多等待 10 分钟、整次生成最多 30 分钟；请求超时后按一半大小重新分批，不原样重发。生成期间执行详情显示当前批次与已等待时长；仍超时则提示判分模型思考时间过长，可稍后重新生成或换用响应更快的模型。
- dataset 可选配置：`EVAL_SQL_CONFIG` 为 MySQL 连接参数 JSON，`EVAL_NL2SQL_URL` 为 consumer 可达的评测查询入口，路径 `/evaluation/api/nl2sql`。每轮只管理 `eval_<run_id>` 数据库；查询使用绑定 scene 的 HMAC 凭据、SQL AST 检查及只读事务。数据库账号应按评测库前缀限定权限。
- `EVAL_TRUSTED_ORIGINS`：可选，允许向评测页面交接登录的建模页面来源，逗号分隔（如 `https://modeling.example.com`）。留空时只信任与评测页面主机名相同的建模页面；`localhost`、`127.0.0.1` 与 `[::1]` 视为同一台本机。
- `EVAL_MODELING_URL`：建模服务地址，用于发送评测完成通知，以及兼容接口 `GET /evaluation/api/modeling-projects` 按当前用户登录查询关联项目；任务中心与执行详情显示知识包名称时不调用该接口。置空则不查询、不发通知。
- `EVAL_MODELING_SERVICE_TOKEN`：发送评测完成通知的服务凭据，与 onto-modeling 的 `ONTO_EVALUATION_SERVICE_TOKEN` 相同，整栈与分离部署自动传入；留空时只告警、不发通知。

有效建模登录复用 `onto-access-token` 和 `onto-refresh-token`，刷新直连账号服务。不能通过 Onto 后端转发，也不能在跳转 URL 放 token。建模页面只通过独立地址打开评测，入口地址只由建模部署变量 `ONTO_WEB_EVAL_PUBLIC_URL` 决定：

- 直连（默认）：未设置该变量时，建模页面打开与自身同主机的 `http://<主机>:8090/evaluation/`；评测在其他主机、端口或经 HTTPS 时设为完整地址（如 `http://eval.example.com:8090/evaluation/`）。无需网关。
  - 点击评测时建模页面打开新窗口；评测页面就绪后，建模页面用 `postMessage` 把登录只发给该地址的来源。评测页面只接受打开它的窗口发来、且来源受 `EVAL_TRUSTED_ORIGINS` 信任的交接。
  - 评测端口默认开放所有网卡，供其他机器的浏览器访问。只允许本机访问时部署设置 `EVAL_BIND_ADDRESS=127.0.0.1`；它与 `EVAL_PORT`（默认 8090）都会记入 `deploy/.env`。
  - 评测页面保存的是登录副本，在建模页面退出不会同步清除。

当前权限为用户私有包；不复制项目成员共享权限。

## 输入和发布

评测集最多 50 个、每集 500 用例。用例格式是 JSON 数组，或包含 `cases` 数组的对象（`{"cases": [...]}`），每项含 `question` 和可选 `reference`（也接受 `groundTruth`）。数据源支持 none、dataset、connection，上传 CSV/XLSX，单文件及合并总量最多 50 MiB、每源最多 20 个文件。连接检查失败阻止用例执行。

- none 不要求数据库或 mapping。
- dataset 将数据写入本轮独立 MySQL 库，配置 consumer 的 NL2SQL 连接。
- connection 支持外部 NL2SQL 或 Neo4j 查询。外部 SQL 不执行覆盖导入；上传数据导入使用 dataset。Neo4j 导入须明确选择；配置范围存在旧图数据时，先确认替换，再删除并导入新数据。

NL2SQL 表单通过数据服务的 `POST /api/v1/sql` 执行固定的 `information_schema.columns` 元数据查询，按 Scene 限定数据库，每批最多 200 行，最多 100 张表、每表 256 个字段。前端仅浏览表和字段，不提供结构编辑。需要数据服务允许该只读元数据查询；不支持或无权限时显示读取失败并可重试。地址可填服务根路径或以 `/api/v1` 结尾，保存时规范化。读取结果返回绑定当前身份、Provider 与连接的 `inspection_id`，保存时校验未变更；修改连接后须重新读取。现有 API 客户端的 `tables` 输入契约仍兼容。

Neo4j 按 Provider 展开 URL、Database、User、Password 与 Workspace / Ontology 隔离字段，隔离字段默认 `d2o_workspace_id`、`d2o_ontology_id`（与 consumer 图数据一致）。Ontology 值由本轮固定知识包自动带入；查询已有图库无需手填表结构。图导入支持“不导入 / 示例数据 / 业务数据表”，未接入示例种子时置灰。选择业务数据表后上传 CSV/XLSX，保存后预览实体、实例、关系与字段匹配；预览不发布包、不写图库。导入使用配置的 Workspace 字段与对应值、Ontology 字段与当前知识包 ID，两组条件同时匹配。可在向导中检查旧图数据；开始评测时必须重新检查，发现旧节点及范围内关系后，用户确认才删除旧数据并导入新数据，取消保留原数据。跨范围关系会阻止替换；清理和导入在同一事务中，导入失败回滚。每个范围在图库保存独立导入回执，响应丢失后恢复先核对回执，避免重复清理；后续导入已替换该范围时，旧任务不能继续查询新数据。同一范围的活动评测互斥。检查最多 10 万个节点、20 万条关系，确认有效期 10 分钟；范围节点或关系已变化时中断，需重新创建并确认。旧版本按 `eval_<run_id>` 写入的图数据不会迁移到配置范围。删除评测任务仍不清理外部图数据。连接回读只返回允许展示的字段与凭据已配置状态；地址或账号变化时不能沿用旧凭据。

原包 mapping 可被替换，仅派生副本改变。mapping 变化先提交 Hub 内部评测快照；同一基础版本加相同 mapping 复用已提交的快照。快照由 Hub 命名为 `<基础版本>-eval.<n>`（如 `0.1.10-eval.2`），不占用正式版本号；此前已提交的快照保留原版本号。任务列表“知识包版本”只显示所选发布版本，评测快照版本在执行详情中单独列出。正式“发布知识包版本”由用户显式触发，创建新版本，不重标历史快照，不自动激活。未修改 mapping 时直接运行原 Hub 版本。

接口统一在 `/evaluation/api` 下。主要资源为 `/packages`、`/eval-sets`、`/data-sources`、`/inputs`、`/package-preparations`、`/package-publications`、`/runs`。定义使用 revision 乐观并发，准备、发布和运行使用稳定 request_id。输入中不接受建模 project_id。响应限额和明细规则见 `API.md`。

## 独立部署

```bash
# 从仓库根执行；consumer 与知识包 Hub 需已部署
bash deploy/deploy.sh --eval-only
```

- 运行配置：未设置 `EVAL_ENV_FILE` 时，`deploy/deploy.sh` 读写 `deploy/.env`，只保存凭据和显式配置的值，取值顺序为 进程环境 > `.env` 已有值：
  - 首次部署生成 `EVAL_ENCRYPTION_KEY`。
  - 评测凭据沿用 Hub 的 `HUB_EVALUATION_SERVICE_TOKEN` 与 consumer 的 `EVALUATION_SERVICE_TOKEN`，由根目录 `deploy/deploy.sh` 读取后注入，两者分别由 Hub 部署脚本和 consumer 部署生成。
- 默认地址：账号、Hub、consumer 地址的默认值 `http://auth-server:5056`、`http://knowledge-hub:8200`、`http://backend:5055` 写在 `deploy/docker-compose.eval-service.yml` 中，compose 文件不含任何凭据。部署脚本用 `--env-file` 把运行配置交给 compose，配置中写了同名地址时优先生效；手动执行 compose 并使用外部配置时，也需要加上 `--env-file <配置文件>`。
- 网络：`CONSUMER_NETWORK` 必填，评测容器加入该网络，按服务名访问依赖。根目录部署脚本会自动探测并注入，本地 `.env` 会记住它，所以在 `deploy/` 下手动执行 `docker compose` 时无需另设变量。不在整栈旁部署时，指定任一已有网络，并在 `.env` 中把依赖地址改为该网络可达的地址。
- Judge：`EVAL_JUDGE_BASE_URL`、`EVAL_JUDGE_API_KEY`、`EVAL_JUDGE_MODEL`、`EVAL_JUDGE_CONFIG_REVISION` 是部署默认值（OpenAI 兼容 `/chat/completions`，需支持工具调用），已在 `deploy/docker-compose.eval-service.yml` 中声明但不含值。缺失时部署只告警；写入 `deploy/.env` 或进程环境后重新部署，或由管理员在评测页面保存 Judge 配置，即可开始评测。
- 外部配置：设置 `EVAL_ENV_FILE` 指向仓库之外、仅运行账号可读的配置文件时，脚本原样使用、不改写，也不自动补齐凭据。
- 镜像源：构建时 npm 默认 `registry.npmmirror.com`，PyPI 默认华为云镜像。可用 `EVAL_NPM_REGISTRY`、`EVAL_PIP_INDEX_URL`（或 `NPM_REGISTRY`、`PIP_INDEX_URL`）覆盖，源码部署和离线包构建都生效；未设置时不传构建参数，沿用 Dockerfile 中的默认值。基础镜像 `node:24-bookworm-slim`、`python:3.12-slim-bookworm` 仍从 Docker Hub 拉取，需要加速时在 Docker 守护进程中配置 registry mirror。

已有服务使用独立 Docker 网络时，可设置 `EVAL_COMPOSE_OVERRIDE` 指向受保护的 Compose 覆盖文件，接入共享网络；不添加同源评测网关。

独立 Compose 不启动账号、Hub、consumer 或建模依赖。默认全栈 `bash deploy/deploy.sh` 会在其他服务之后部署评测；`--onto-modeling-only` 不包含评测。默认宿主端口 8090（所有网卡），网关按实际网络配置访问；直连方式见“配置与权限”。`/evaluation/api/health` 只报告进程活性，不能替代依赖与模型协议验证。

离线包：`bash eval-service/build.sh <version> [amd|arm]`。产物包含镜像、Compose、部署脚本和说明；目标机设置 `EVAL_IMAGE=knowledge-evaluation:<version>` 后执行包内 `deploy/deploy.sh`，运行配置规则同上。不包含运行配置和数据。根目录 `bash deploy/build.sh <version> [amd|arm]` 生成的完整离线包也包含评测镜像、部署脚本及网关配置示例。

## 恢复与清理

SQLite 请求记录及独立文件锁保证单实例写入；两个后台线程、每用户一个活动任务、最多 16 个排队/执行任务。每轮按发起时设置的并发数（1 到 8，默认 4）同时向 consumer 提问，每条用例只更新自己的结果；某条用例失败时尚未开始的用例不再提交，已提交的用例继续对账完成。用户令牌只在内存，重启后轮次标记中断，用户通过有效登录授权恢复。发起、恢复和停止评测时，页面一并交出 refresh token（请求头 `X-Refresh-Token`），同样只在内存：执行引擎在 access token 到期前 2 分钟内自动换新，身份每分钟最多向账号服务核验一次，因此评测时长不受 access token 15 分钟有效期限制，关闭页面也不会中断。用户退出登录（refresh token 被吊销）或 refresh token 过期后，令牌用尽时轮次进入待授权，提示“登录凭证已过期，请重新登录后点击‘授权并继续’”；判分模型网关拒绝 API Key 时提示检查判分模型配置，不当作登录过期。恢复时轮次立即显示为准备中，不等预检与数据检查完成。远端提交未知时复用原 session/幂等键对账，不新建会话。consumer 以 `SESSION_PREPARE_FAILED`（会话运行状态准备失败，常见于同一工作空间并发提问时撞上会话整理）结束运行时，问题并未真正作答，该用例按 2、4、8…秒（上限 30 秒，含随机抖动）退避后换新会话重新提交，连同首次最多 6 次；重试次数记在用例上，对账时按当次的 session/幂等键重放，等待期间停止评测立即生效；仍失败时执行异常说明中注明已重试次数。其他运行失败不重试。Judge 失败只重判，不重复问答。

完整工具轨迹由 consumer 保存，本地提供消费 run_id 和证据引用，明确显示 `not_collected`。人工改判保留原判定、原因、操作者身份键和时间。

任务中心“···”菜单和执行详情提供“删除评测记录”：已结束的轮次可直接删除；中断或待授权的轮次在 consumer 侧没有执行中或待对账的问题时可删除，否则须先停止评测并完成对账；排队或执行中的轮次须先停止。删除清理进度落库，失败后重新执行删除继续处理。只删除自有消费空间和 dataset 库，不删除外部图/SQL。外部连接数据源的轮次在数据源指定的 consumer 工作空间中执行（Neo4j 取“Workspace 对应值”，NL2SQL 取“评测工作空间 ID”，与原建模评测一致），以当前用户权限调用 consumer 的通用工作空间接口：不存在时创建，名称与 ID 一致（计入该用户的工作空间数量上限）；已存在时只复用本人所有且未共享的空间，并在其中导入、激活本轮知识包版本（同版本以 Hub 内容覆盖）；该空间跨轮次复用，删除轮次时不删除。建模项目删除不影响独立评测和 Hub 包。未引用上传 24 小时后清理，被定义/准备/运行引用的文件保留。

```bash
# 下列路径为通用运维示例；快照目标必须不存在
python -m evaluation.maintenance backup --data-dir /var/lib/evaluation --snapshot /backup/evaluation.sqlite
# 停止新服务后恢复到空目录；必须提供最新删除记录库
python -m evaluation.maintenance restore --data-dir /restore/evaluation --snapshot /backup/evaluation.sqlite --deletion-records /var/lib/evaluation/evaluation.sqlite
```

数据库包含加密的输入、连接配置与页面保存的 Judge API Key，备份应同样受控。恢复重放删除标记、执行完整性检查，running 转 interrupted 等待重新授权；不能通过旧备份复活已删轮次。

## 验证

```bash
PYTHONPATH=eval-service python -m pytest eval-service/tests -q
cd eval-service/frontend
npm test
npm run build
```

全部测试资料合成。Hub 集成测试调用仓库真实 Hub2 存储和派生校验；consumer 和模型交互使用合成替身，另测试实际 consumer 预置路由的权限边界。真实外部数据库、模型效果、容器镜像和网关部署不由替身测试替代。实施验证记录见 `VERIFICATION.md`。

评测入口必须与建模页面来源不同，不接受相对路径；HTTPS 建模页面必须配置 HTTPS 评测地址。
