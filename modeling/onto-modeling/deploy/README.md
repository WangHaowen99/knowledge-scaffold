# Onto Modeling 部署说明

离线交付包部署流程，对应方案第八节与 4.1.1 容量规划。

## 交付包内容

```
onto-modeling/
  images/                     # onto-modeling 镜像 tar
  images.env                  # 镜像 tag 清单（deploy.sh 自动读取）
  docker-compose.release.yml
  deploy.sh                   # 预检 → 加载镜像 → 拉起
  dependency_probe.py         # 探测 Auth 与 Consumer 依赖
  preflight.sh                # 可单独运行的部署预检
  .env.example                # 环境变量说明（复制为 .env 后填写）
  README.md
```

## 部署步骤

```bash
tar -xzf onto-modeling-<version>-linux-<arch>.tar.gz
cd onto-modeling
cp .env.example .env
vi .env              # 默认 Auth 契约已填；实际契约不同时修改
./deploy.sh
```

首次安装可以先于 Auth 和 Consumer。Auth URL、共享网络、issuer、audience 默认分别为 `http://auth-server:5056`、`onto-auth-network`、`http://auth-server:5056`、`knowledge-scaffold`；旧 `.env` 中缺失或留空时，脚本会补齐。若实际 Auth 契约不同，须在首次安装前改成真实值。`deploy.sh` 在共享网络尚不存在时建立带 `auth.shared=true` 标签的网络，并将依赖状态写到本目录 `.dependencies.json`；若 Auth 尚未上线，Onto 进程会持续等待，页面暂不可用。Auth 和 Consumer 上线后执行 `bash deploy.sh --verify-dependencies`；该命令检查依赖及 Onto、Hub、评测三个 HTTP 服务，不重建容器。Consumer 安装时设置相同的 `AUTH_SHARED_NETWORK`。Hub、评测包各自的 `deploy.sh` 使用 `CONSUMER_NETWORK` 指向同一网络，允许它们在 Consumer 之前安装。三包首次独立安装可依次部署 Onto、Hub、评测；需预先对齐 Onto 的 `ONTO_HUB_SERVICE_TOKEN` 与 Hub 的 `HUB_SERVICE_TOKEN`、评测的 `EVAL_HUB_SERVICE_TOKEN` 与 Hub 的 `HUB_EVALUATION_SERVICE_TOKEN`、评测的 `EVAL_MODELING_SERVICE_TOKEN` 与 Onto 的 `ONTO_EVALUATION_SERVICE_TOKEN`。Consumer 安装时把 `EVALUATION_SERVICE_TOKEN` 设成评测的 `EVAL_CONSUMER_SERVICE_TOKEN`，其值不能与 Consumer 的 `MODELING_SERVICE_TOKEN` 相同。等 Consumer 安装并完成自身验收后，再执行建模依赖验证和一条真实评测用例。已有 Onto 容器或数据卷的升级仍要求 Auth 和 Consumer 预先就绪。

访问 `http://<部署机地址>:5100`（端口可用 `ONTO_WEB_PORT` 覆盖）；Auth 未就绪时等待启动，不能仅凭安装命令成功判断页面可用。

### 旧 Core 项目迁移

`deploy.sh` 检测到运行中的旧 `mw-modeling` 容器时，会调用
`migrate-from-modeling.sh` 将项目元数据、已解析资料和部分知识包文件迁入 Onto。
迁移按项目标记幂等；旧 Core 的 MCP Token、Agent 运行记录和评测历史不在迁移范围。
部署脚本不会自动配置旧 Core 的评测地址或 MCP 关联地址；若要退役旧 Core，须先处理
其余客户端与旧数据，再删除相关配置。`.env` 含运行凭据，不可提交到 Git 或打入发布包。

## 容量规划（方案 4.1.1）

- 每个活跃建模会话 = 一个 CLI 子进程，均值约 400MB、峰值 500MB+。
- 8G 主机：保持默认 `ONTO_WEB_MAX_SESSIONS=12`，约支撑 12–15 并发活跃会话、
  30–50 名在线用户（突发式交互节奏）。
- 几十用户同时建模为目标时建议 16G 主机，并按比例调高 `ONTO_WEB_MAX_SESSIONS`
  与 compose 里的 `ONTO_WEB_MEM_LIMIT`。
- 建议主机配置 2–4G swap 或 zram 吸收峰值。
- 数据评测每轮在评测专用线程池里执行，`ONTO_WEB_EVAL_MAX_CONCURRENT_ROUNDS`
  （默认 16）限制同时执行的轮数，超出的轮次排队并在状态里显示位次；评测不占用
  上传、git、建模发消息共用的默认线程池。每轮在 modeling 侧按用例并发调用
  consumer，调高上限前先确认 consumer 与模型网关的承载。
- 空闲会话默认 10 分钟回收（`ONTO_WEB_SESSION_IDLE_TTL`），回收后靠 resume
  续接模型上下文，用户无感；会话 transcript 持久化在数据卷 `/data/home/.claude/`。
- 运行中的会话连续 30 分钟没有任何模型或工具响应（`ONTO_WEB_SESSION_STALL_TIMEOUT`，
  单位秒，0 关闭）即判定执行超时：终止引擎、释放槽位，对话里给出提示，用户可以直接发消息
  继续。等待确认卡的时间不计入。模型调用失败重试期间也没有响应，调小阈值前先确认模型网关
  的单次超时与重试次数。

## 子代理同步执行（CLI 后台任务开关）

平台在每个 Claude 引擎会话的 CLI 子进程中注入环境变量
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`，子代理和 Shell 命令一律同步执行。原因：CLI
默认把子代理放到后台运行，而平台以主轮结束判定会话空闲，后台运行的子代理会被误示为完成，
还可能随空闲回收被静默终止。

- 该变量由平台自动注入，无需手工配置。不要在容器内 `/data/home/.claude/settings.json`
  的 `env` 中设置它：CLI 启动后会读取那里的值，覆盖平台注入的值。
- 平台还会注入 `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1` 与
  `CLAUDE_CODE_BG_TASKS_REPORT_RUNNING=1`，让 CLI 上报自身的会话状态：后台工作未结束时，
  会话保持"运行中"，不会被误判为空闲、提前通知或被回收。这两个变量同样不要在上述
  `settings.json` 中覆盖。
- 命令超过本次调用的超时（默认 2 分钟，模型可以延长，最长 10 分钟）会直接失败，
  不再转入后台。
- 镜像内的 `claude-agent-sdk` 没有锁定版本，打包的 CLI 随构建时间变化。每次部署和升级后
  执行以下两项检查：

  1. 打包的 CLI 仍识别该变量，输出大于 0 即正常：

     ```bash
     docker exec onto-modeling python3 -c "import claude_agent_sdk, pathlib; cli = pathlib.Path(claude_agent_sdk.__file__).parent / '_bundled' / 'claude'; print(cli.read_bytes().count(b'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'))"
     ```

     输出为 0 说明该版本 CLI 已不识别此变量，应暂停升级并反馈研发。使用降级兼容包
     （`ONTO_WEB_CLI_PATH`）时本项不适用，按下文降级包的关键路径实测。
  2. 用户级配置没有覆盖该变量，没有输出即正常：

     ```bash
     docker exec onto-modeling sh -c 'grep -n CLAUDE_CODE_DISABLE_BACKGROUND_TASKS /data/home/.claude/settings.json 2>/dev/null'
     ```

## AVX 指令集要求（x86_64）

捆绑的 Claude Code CLI 是 Bun 编译的原生二进制，要求 CPU 暴露 AVX，否则子进程
SIGILL 崩溃。`preflight.sh` 会检查；未通过时按优先级整改：

1. **虚拟化层整改**：CPU 模型改 host-passthrough 或任一带 AVX2 的具体型号（零代码成本）。
2. **换 ARM 交付包**：aarch64 无 AVX 概念，无此限制。
3. **降级兼容包（最后手段）**：在目标机准备 Node ≥18 运行时和旧版 JS 交付的
   CLI（2.0.76，纯 JS 无 Bun/AVX 依赖），将其挂载进容器并设
   `ONTO_WEB_CLI_PATH=/path/to/cli.js`。注意：降级组合功能落后且冻结维护，
   必须在目标虚机实测关键路径（确认卡、hooks、子代理（含同步执行）、会话恢复）后才能放行，
   界面无"兼容模式"标识前不建议长期使用。
4. 以上均不可行则明确不支持该环境。

## 数据与备份

- 建模业务数据保存在 docker 卷 `onto-web-data`；账号数据由独立 Auth owner 保存。
- 运维中心提供手动备份（保留最近 3 份）；建议由两个 owner 分别备份其数据卷。

## 升级与回滚

- 升级：解包新版本交付包，重复部署步骤；`docker compose up -d` 滚动替换容器，
  数据卷保留。
- 升级后按「子代理同步执行（CLI 后台任务开关）」一节执行两项检查。
- 回滚：用旧版本交付包重复部署步骤即可（镜像 tag 随版本走，互不覆盖）。

## 接入既有账号中心（单点账号）

Onto 包不包含 Auth。先装 Onto 时可沿用默认契约；Auth 装好后实际签发身份必须与预期值一致。自定义网络或签发身份须在首次安装前修改 `.env`；升级时沿用原身份值：

```
ONTO_MODELING_AUTH_URL=http://auth-server:5056
ONTO_MODELING_EXTERNAL_AUTH_NETWORK=onto-auth-network
ONTO_MODELING_CONSUMER_URL=http://backend:5055
ONTO_WEB_AUTH_ISSUER=http://auth-server:5056
ONTO_WEB_AUTH_AUDIENCE=knowledge-scaffold
```

`deploy.sh` 先探测依赖：缺席时记录等待状态，已响应但身份或协议不匹配时停止部署。`--verify-dependencies` 会重新检查 Auth 健康、发现文档、RS256 JWKS、Consumer `/ready` 和 Onto、Hub、评测的 HTTP 服务；实际账号登录和历史会话连续性仍需业务验收。
若旧 Onto 自带 Auth 容器仍存在，脚本会停止并要求先核对账号归属，保留旧账号卷。

账号首次登录或管理员重置密码后，Onto 会查询 Auth 的 `/userinfo`。待改密账号只可进入强制改密页面，业务 API 返回 403；改密使用 Auth 自助接口，成功后须用新密码重新登录。账号状态查询失败时，业务 API 不放行。Onto 的个人设置里修改密码也会退出当前登录。两种改密都需要 Onto 配置与 Auth 相同的 `ONTO_WEB_AUTH_ADMIN_TOKEN`；普通登录及待改密状态检查不依赖此令牌。

## 脚本运行验证的沙箱（jiuwenbox）

代码生成的脚本必须跑通才能提交（`/ontology-codegen` Step 4.6）。验证在 consumer 的
`onto-agent-jiuwenbox` 沙箱里执行，脚本随请求送进一次性沙箱（禁止出网、仅 `/tmp` 可写）。

jiuwenbox 的管理 socket 只挂给脚本验证网关 `onto-modeling-sandbox-gateway`（compose 服务
`sandbox-gateway`），不挂进 onto-modeling 容器：建模 agent 在 onto-modeling 容器里能执行任意命令，
拿到 socket 就能自己定沙箱策略。网关与 onto-modeling 同一镜像，以非 root、只读根文件系统、无
capability 运行，不读 `.env`、不挂数据卷、不发布端口；它只按固定策略运行脚本，onto-modeling 经
`ONTO_WEB_SANDBOX_GATEWAY_URL=http://sandbox-gateway:8110` 调用它。

- 整栈部署（根目录 `deploy/deploy.sh`、`install-consumer-modeling*.sh`）会探测 consumer 的
  socket 卷，写入 `.env`：`ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME=<卷名>`；deploy.sh 据此生成
  `docker-compose.jiuwenbox.yml`，把卷只读挂给网关。`redeploy-app.sh` 沿用该文件并一起替换网关。
- 单独部署、或探测不到 consumer 的沙箱时不挂载，网关报 jiuwenbox 不可用，脚本验证在 onto-modeling
  容器内执行。
- 管理员可在「配置中心 → 沙箱」查看是否已连接、沙箱内 Python 版本与包列表，并重新检测。

## 从原 modeling 服务迁移数据

deploy.sh 部署完成后会自动检测 `mw-modeling` 容器（可用
`ONTO_MODELING_MIGRATE_FROM=<容器名>` 覆盖），存在即执行
`migrate-from-modeling.sh` 把项目/资料/知识包迁入本服务；迁移幂等，
已迁移的项目自动跳过，也可随时手动重跑该脚本。
Workbench 数据库处于 WAL 模式，脚本在 `mw-modeling` 容器内用 `VACUUM INTO` 导出一致性快照再迁移，
因此需要该容器在运行中。迁移失败不影响 Onto Modeling 本身，deploy.sh 会打印 `[WARN]` 和重跑命令。
