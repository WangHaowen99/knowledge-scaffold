# 独立知识包评测（eval1）

在 Hub2 之后接入：从 Hub 固定版本下载 → 在评测页面配置用例和数据源 → 生成 mapping → consumer 问答 → Judge 判分 → 按需发布派生版本。服务不读取建模工作区、项目或 Core 数据库，不运行建模 Agent。

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

打开 `/evaluation/`；直接访问服务根路径（如 `http://127.0.0.1:8090/`）会跳转到该页面。开发前端可用 `npm run dev`，代理默认本机 8090，可通过 `EVAL_DEV_API_URL` 覆盖。单独启动开发端口不与建模共享浏览器存储，免重复登录需配置同源代理，或从建模页面以直连方式打开（见下文）。

## 配置与权限

变量见 `.env.example`；该文件仅是占位说明，不是可直接运行的凭据文件。部署时的实际配置由部署脚本维护在 `deploy/.env`（权限 600，不入库），或通过 `EVAL_ENV_FILE` 从仓库和构建上下文之外注入，见下文“独立部署”。

- `EVAL_AUTH_URL`、`EVAL_AUTH_ISSUER`：与 Hub 使用同一可信账号及稳定身份命名空间。
- `EVAL_HUB_URL`、`EVAL_HUB_SERVICE_TOKEN`：对应 Hub 的 evaluation 服务身份（Hub 的 `HUB_EVALUATION_SERVICE_TOKEN`），不复用 modeling 凭据。
- `EVAL_CONSUMER_API_URL`、`EVAL_CONSUMER_SERVICE_TOKEN`：后者对应 consumer 的 `EVALUATION_SERVICE_TOKEN`，与其 modeling 密钥不同；用户身份仍必需。
- `EVAL_DATA_DIR`：独立卷。默认 `.runtime/` 仅用于合成数据本地开发，部署必须使用外部运行卷。
- `EVAL_ENCRYPTION_KEY`：Fernet 密钥，用于连接配置、页面保存的 Judge API Key、上传数据和执行数据快照。部署脚本首次部署时生成并保存在 `deploy/.env`，不在数据卷内；备份时须与数据卷分开、同样受控保管。变更或丢失密钥会导致已有加密数据无法解密：已有数据卷而缺少密钥时部署脚本拒绝生成新密钥。
- `EVAL_JUDGE_BASE_URL`、`EVAL_JUDGE_API_KEY`、`EVAL_JUDGE_MODEL`、`EVAL_JUDGE_CONFIG_REVISION`：临时 Judge 部署配置。问答沿用 consumer 默认 profile，记录其非敏感配置摘要。此方案标记 **MODEL-CONFIG-01，待 eval2 整改**。
- 页面配置 Judge：登录后点击右上角“评测判分模型配置”。所有用户可查看生效来源、模型和配置版本，只有管理员（账号服务中 role 为 admin）可修改，因为该配置对所有用户的评测生效，并会收到题目、参考答案和回答。
  - 管理员填写网关地址、模型和 API Key，可先“测试连接”（与轮次预检相同的结构化判分检查）再保存。保存的配置整体替换部署配置，“恢复部署配置”删除它。
  - API Key 用 `EVAL_ENCRYPTION_KEY` 加密存入数据卷，接口和页面从不返回。网关不变时 Key 可留空沿用；更换网关必须重填，部署 Key 不会被发往新地址。
  - 配置版本自动生成；值变化后，排队及进行中的轮次会停止判分并提示重新创建轮次。可选 NL2SQL 同样使用生效的 Judge 配置。
- dataset 可选配置：`EVAL_SQL_CONFIG` 为 MySQL 连接参数 JSON，`EVAL_NL2SQL_URL` 为 consumer 可达的评测查询入口，路径 `/evaluation/api/nl2sql`。每轮只管理 `eval_<run_id>` 数据库；查询使用绑定 scene 的 HMAC 凭据、SQL AST 检查及只读事务。数据库账号应按评测库前缀限定权限。
- `EVAL_TRUSTED_ORIGINS`：可选，允许向评测页面交接登录的建模页面来源，逗号分隔（如 `https://modeling.example.com`）。留空时只信任与评测页面主机名相同的建模页面；`localhost`、`127.0.0.1` 与 `[::1]` 视为同一台本机。

有效建模登录复用 `onto-access-token` 和 `onto-refresh-token`，刷新直连账号服务。不能通过 Onto 后端转发，也不能在跳转 URL 放 token。建模页面可用两种方式打开评测，入口地址只由建模部署变量 `ONTO_WEB_EVAL_PUBLIC_URL` 决定：

- 直连（默认）：未设置该变量时，建模页面打开与自身同主机的 `http://<主机>:8090/evaluation/`；评测在其他主机、端口或经 HTTPS 时设为完整地址（如 `http://eval.example.com:8090/evaluation/`）。无需网关。
  - 点击评测时建模页面打开新窗口；评测页面就绪后，建模页面用 `postMessage` 把登录只发给该地址的来源。评测页面只接受打开它的窗口发来、且来源受 `EVAL_TRUSTED_ORIGINS` 信任的交接。
  - 评测端口默认开放所有网卡，供其他机器的浏览器访问。只允许本机访问时部署设置 `EVAL_BIND_ADDRESS=127.0.0.1`；它与 `EVAL_PORT`（默认 8090）都会记入 `deploy/.env`。
  - 评测页面保存的是登录副本，在建模页面退出不会同步清除。
- 同源网关：将独立服务路径接入**已有同源独立网关**，参考 `deploy/nginx.conf.example`，建模设置 `ONTO_WEB_EVAL_PUBLIC_URL=/evaluation/`。两边页面同源，直接共享浏览器中的登录。

当前权限为用户私有包；不复制项目成员共享权限。

## 输入和发布

评测集最多 50 个、每集 500 用例。用例格式是 JSON 数组，每项含 `question` 和可选 `reference`。数据源支持 none、dataset、connection，上传 CSV/XLSX，单文件及合并总量最多 50 MiB、每源最多 20 个文件。外部连接提供表字段定义，连接检查失败阻止用例执行。

- none 不要求数据库或 mapping。
- dataset 将数据写入本轮独立 MySQL 库，配置 consumer 的 NL2SQL 连接。
- connection 支持外部 NL2SQL 或 Neo4j 查询。外部 SQL 不执行覆盖导入；上传数据导入使用 dataset。Neo4j 导入须明确选择，节点/边使用本轮隔离 scope，不删除外部图数据。

原包 mapping 可被替换，仅派生副本改变。mapping 变化先提交 Hub 内部评测快照；正式“发布知识包版本”由用户显式触发，创建新版本，不重标历史快照，不自动激活。未修改 mapping 时直接运行原 Hub 版本。

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

已有服务使用独立 Docker 网络或需要同源网关时，可设置 `EVAL_COMPOSE_OVERRIDE` 指向仓库外的受保护 Compose 覆盖文件，加入外部网络和网关服务。部署脚本会将其叠加到 `deploy/docker-compose.eval-service.yml`；再次部署应继续提供相同覆盖文件。网关接管建模原入口端口时，建模服务应取消重复的宿主端口绑定，后续建模重部署也须保留这项覆盖。

独立 Compose 不启动账号、Hub、consumer 或建模依赖。默认全栈 `bash deploy/deploy.sh` 会在其他服务之后部署评测；`--onto-modeling-only` 不包含评测。默认宿主端口 8090（所有网卡），网关按实际网络配置访问；直连方式见“配置与权限”。`/evaluation/api/health` 只报告进程活性，不能替代依赖与模型协议验证。

离线包：`bash eval-service/build.sh <version> [amd|arm]`。产物包含镜像、Compose、部署脚本和说明；目标机设置 `EVAL_IMAGE=knowledge-evaluation:<version>` 后执行包内 `deploy/deploy.sh`，运行配置规则同上。不包含运行配置和数据。根目录 `bash deploy/build.sh <version> [amd|arm]` 生成的完整离线包也包含评测镜像、部署脚本及网关配置示例。

## 恢复与清理

SQLite 请求记录及独立文件锁保证单实例写入；两个后台线程、每用户一个活动任务、最多 16 个排队/执行任务。用户令牌只在内存，重启后轮次标记中断，用户通过有效登录授权恢复。远端提交未知时复用原 session/幂等键对账，不新建会话。Judge 失败只重判，不重复问答。

完整工具轨迹由 consumer 保存，本地提供消费 run_id 和证据引用，明确显示 `not_collected`。人工改判保留原判定、原因、操作者身份键和时间。

删除轮次前须终止并对账；删除清理进度落库，失败后重新执行删除继续处理。只删除自有消费空间和 dataset 库，不删除外部图/SQL。建模项目删除不影响独立评测和 Hub 包。未引用上传 24 小时后清理，被定义/准备/运行引用的文件保留。

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
