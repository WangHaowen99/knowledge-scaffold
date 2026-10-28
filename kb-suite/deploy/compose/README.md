# Docker Compose 部署（kb-suite）

入口：`http://<部署机>:5199`。这是 JiuwenSwarm 的 Web 界面，默认用 develop 源码构建的开发版（`kb-suite/swarm:dev`，提交号见镜像标签 `kb-suite.swarm-commit`），没有改源码。左侧导航里有两个插件页面：「知识库」是 kb-manager，「知识求索」是 da-analyst。DA 还以「三方 Agent」的身份接进了集群模式，可以当团队成员用。

回到 PyPI 发布版 0.2.7：`SWARM_TAG=0.2.7 SWARM_HOME_SUFFIX= ./kbsuite.sh up`。两个版本的工作区卷是分开的（`kb-suite_swarm-home` / `kb-suite_swarm-home-dev`），互不迁移。

## 组成

| 服务 | 镜像 | 端口 | 说明 |
|---|---|---|---|
| edge | `nginx:alpine` | 5199 → 8080 | 对外入口，反代到 swarm:5173；加 gzip，插件页带哈希的资源改长缓存（见下文「切换插件页」） |
| swarm | `kb-suite/swarm:dev`（或 `:0.2.7`） | 只在 compose 网络内（5173） | Swarm 的 Web、Gateway、AgentServer；镜像里带 `deepanalyze-harness`、`loopforest-harness` |
| kbs-api | `kb-suite/kb-service:dev` | 127.0.0.1:5198 → 8080 | 管理 API、DA 工具接口 `/v1/tools/*`、MCP `/mcp/` |
| kbs-worker | 同上 | — | 解析、切分、向量化，以及摘要、问题生成、Wiki、知识库描述等增强任务 |
| kbs-docreader | `kb-suite/docreader:current` | — | 以 WeKnora 发布镜像为底，覆盖了 WeKnora main 的解析源码，用来拿原文定位 |
| kbs-postgres | `paradedb/paradedb:v0.25.11-pg17` | — | pgvector + 中文 BM25 |
| da-engine | `kb-suite/deepanalyze-java:current` | 127.0.0.1:5197 → 8192 | DA-java，三个开发分支的源码构建，知识库工具接 kb-service |
| kbs-redis | `redis:7` | — | DA 的任务状态和 Todolist |
| lf-engine | `kb-suite/loop-forest:current` | 127.0.0.1:5196 → 8193 | Loop Forest 服务（第二个分析引擎），见下文 |

**模型**：都走阿里云 MaaS 的 OpenAI 兼容接口。向量模型用 `text-embedding-v4`（1024 维），重排用 `qwen3-rerank`；摘要、Swarm 主 agent 和 DA 都用 `qwen-plus`。知识库设置里可以给摘要、问题生成、Wiki、描述分别换模型，可选列表来自 `KBS_CHAT_MODELS`（compose 默认 `qwen-plus,qwen-max,qwen-turbo`）。

**并发**：`KBS_WORKER_CONCURRENCY`（默认 3）是 worker 同时跑的任务数，`KBS_LLM_CONCURRENCY`（默认 4）是同时发出的对话模型请求数。

## 密钥（不进仓库）

密钥放在 `~/.config/kb-suite/` 下，权限 600。

- **`secrets.env`**：`KBS_PG_PASSWORD`、`KBS_LLM_BASE_URL`、`KBS_LLM_API_KEY`、`KBS_RERANK_URL`、`SWARM_API_BASE`、`SWARM_API_KEY`、`DA_ENGINE_SECRET`。
- **`keys.env`**：第一次执行 `up` 时由 `bootstrap` 生成，包含三把服务密钥：
  - `KBS_PLUGIN_KEY`：能力 manage、ingest、retrieve；
  - `KBS_MCP_KEY`：能力 retrieve；
  - `KBS_DA_KEY`：能力 retrieve、reprocess，可以通过 `X-Tenant-Id` 指定租户。

## 操作

在 WSL 里，如果 `PATH` 里的 `docker` 是 Windows 的 `docker.exe`（走 npipe），WSL 路径的 bind mount 会挂成空目录：插件页面、构建脚本都会静默出错。脚本要用 WSL 集成的 Linux CLI 跑。

部分构建要从上游源码仓导出代码，用环境变量指定本地克隆的位置，没设时脚本直接报错退出：

| 变量 | 仓库 | 用到的命令 |
|---|---|---|
| `SWARM_REPO` | jiuwenswarm | `build`、`build-swarm-dev` |
| `WEKNORA_REPO` | WeKnora | `build`、`build-docreader` |
| `DA_CORE_REPO`、`DA_RUNTIME_REPO`、`DA_SOLUTION_REPO` | agent-core-java、agent-runtime-java、agent-solution | `build-da`（`build-lf` 只用 `DA_SOLUTION_REPO`） |
| `AGENT_CORE_REPO` | agent-core | `harness/da-harness/e2e/run_contract.sh` |

```bash
cd deploy/compose
export PATH=/usr/bin:$PATH      # 让 docker 指向 /usr/bin/docker（Docker Desktop 的 WSL 集成）
./kbsuite.sh build            # kb-service、插件前端、docreader、Swarm（默认 develop 版；SWARM_TAG=0.2.7 构建发布版）
./kbsuite.sh build-kbs        # 只重建 kb-service 镜像
./kbsuite.sh build-plugin     # 只重建插件页面（frontend/dist），swarm 重启后生效
./kbsuite.sh build-swarm-dev  # 插件页面 + develop 版 Swarm 镜像（SWARM_REPO / SWARM_REF，默认 origin/develop）
./kbsuite.sh build-swarm      # 插件页面 + 0.2.7 版 Swarm 镜像
./kbsuite.sh build-docreader  # 只重建 docreader 覆盖镜像（WEKNORA_REPO / WEKNORA_REF）
./kbsuite.sh build-da         # 只构建 DA-java（见下）
./kbsuite.sh build-lf         # 只构建 Loop Forest 服务（要先 build-da，见下）
./kbsuite.sh up               # 启动；第一次会生成 keys.env；有 DA 镜像时一并启动 da-engine
./kbsuite.sh status | logs <服务> | restart <服务> | down
```

只更新 kb-service 或插件时不用整套重建：

```bash
./kbsuite.sh build-kbs && docker compose -p kb-suite --env-file ~/.config/kb-suite/secrets.env -f compose.yaml \
  up -d --no-deps --force-recreate kbs-api kbs-worker      # 迁移在 kbs-api 启动时自动执行
./kbsuite.sh build-plugin && docker restart kb-suite-swarm-1
```

## 知识库增强（问题生成、检索后处理、Wiki）

设计见 `docs/第二期-问答增强与Wiki-2026-10-27.md`。都在知识库「设置」里按库开关，默认只开摘要和检索后处理：

- **AI 问题生成**：每个分块生成几个问题，问题单独建向量和 BM25 索引，命中后返回原分块。
- **检索后处理**：重排阈值（含降级和兜底）、复合打分、MMR、相邻分块合并、短片段补上下文或父子分块、跨文档去重、查询扩展。
- **Wiki**：每篇文档一个摘要页，实体和概念各一页，多篇文档合并到同一页，页面之间用 `[[slug|名称]]` 链接；「Wiki」页签可以浏览、编辑、看图谱、重建。同一知识库的 Wiki 任务串行执行，7 篇文档约 9 分钟。
- **知识库描述**：汇总文档摘要生成，可以一键用作说明。
- **上传选项**：上传时可以覆盖分块、解析引擎、摘要、问题、Wiki 开关。

接口：Swarm 主 agent 的 MCP 新增 `search_wiki`、`read_wiki_page`，`search_knowledge` 会带上 Wiki 页面；DA 的 `kb_search` 支持 `pageTypes`（`entity`/`concept`/`overview`）、`linkedFrom` 和按 `relevance` 比较的 `minScore`，`wiki_browse` 能列出和读取 Wiki 页面（`pageId` 以 `wiki:` 开头）。

设置保存后只对新文档生效；已有文档到「设置 → 应用到已有文档」里批量重跑。

## docreader 覆盖镜像

WeKnora 发布的 docreader 镜像（09-24 版）不返回原文定位，main 分支的版本已经支持。两边的 `pyproject.toml` 和 `uv.lock` 完全相同，所以 `build-docreader` 不重新编译整个镜像，而是在发布镜像上用 `git archive` 覆盖 main 的 `docreader/` 源码，镜像标签用 WeKnora 的提交号。构建前脚本会比对 `uv.lock`，两边不一致就停下，提示改成从源码完整构建。

覆盖后，PDF 命中会带页码和 bbox，Excel 命中带 sheet 和行号。Word 仍然没有定位，因为 WeKnora 目前只给 PDF 和 Excel 输出定位。

## DA-java

`da/build.sh` 按 DA 的 `deploy-dev` 手册构建：

1. 对三个仓先 `fetch` 再 `git archive` 导出（不碰工作副本）：agent-core-java 和 agent-runtime-java 取 `develop`，agent-solution 取 `common`。
2. 用 Maven 依次安装 core、runtime、agent-solution 父 POM、core-ext、runtime-ext，最后打 DA 包。
   - 所有模块的编译目标都是 Java 17，所以用 `maven:3.9-eclipse-temurin-17` 构建。
   - Maven 依赖走华为云镜像，本地仓库缓存在 docker 卷 `kbs-m2`。
3. 用 `da/Dockerfile` 打镜像。它和 DA 的 `deploy-dev/Dockerfile` 一样，只是把 JRE 基础镜像做成了参数。部署机上拉 `eclipse-temurin:21` 很慢，所以设了 `DA_JRE_IMAGE=eclipse-temurin:17-jre`。
4. 用 `da/make_kb_spec.py` 从这次构建的 DA 源码里取出 5 个知识库工具的契约（`kb_search`、`expand`、`doc_grep`、`wiki_browse`、`reprocess_document`），生成 `da/build/knowledge-service.json`。

这次构建用的三仓提交号记录在镜像标签 `kb-suite.da-baseline` 里；`DA_CORE_REF` / `DA_RUNTIME_REF` / `DA_SOLUTION_REF` 可以锁定提交号重建。

**本地补丁**：`da/patches/*.patch` 会在编译前打到导出的源码上，打了哪些记录在镜像标签 `kb-suite.da-patches` 里。上游修复之后就删掉对应的补丁。

- `0001-runtime-mcp-registration-lifecycle`：动态 MCP（`runtimeConfig.mcpServers`）的全局注册。
  - 复用：DA 每个任务都新建 agent，第二个任务起重复注册时内核返回「id 已存在」，DA 把它当成不可用跳过，团队工具只在第一个任务里能用。现在改成复用已有注册。
  - 回收：原来注册从不注销，每个团队成员会话都会留下一份注册和一条连接，直到 DA 重启。现在按 agent 视图计数，最后一个卸载时注销并断开连接。
- `0002-keep-cancel-mark-while-busy`：DA 的 `/cancel` 会标记会话已取消，任务在下一次模型调用前收尾。但每个新请求在被内核以「会话忙」拒绝之前，都会先重新登记会话，顺手清掉这个标记，取消后紧接着重试（每个宿主的下一轮都会这样）就等于撤销了取消，任务一直跑到自然结束。现在会话还有任务在跑时不清标记。实测：修之前每秒重试，取消 90 秒后还在忙；修之后约 5 秒空出来。

**DA 怎么接 kb-service**：通过 Spring 的附加配置 `da/kbs-tools.yml`（`SPRING_CONFIG_ADDITIONAL_LOCATION`）重定义 `deepanalyze.tool-services`。其中 `base_url` 指向 kb-api，描述文件用上面生成的那份，`auth-headers` 里带 `KBS_DA_KEY`。DA 的 jar 和源码都不改。

**调用方式**：DA 的 `/api/agents/**` 要带三个请求头：`X-Engine-Secret`（取 `DA_ENGINE_SECRET`）、`X-Da-Tenant-Id: default`、`X-Da-Session-Id`；知识库范围放在请求体的 `scope.kbIds`。验证脚本：`python3 da/smoke.py "<问题>" [--tools kb_search]`，会打印工具调用和最终回答。

**一个坑**：Java 的 HttpClient 发出的 POST 会带 `Upgrade: h2c` 请求头，uvicorn 默认的 httptools 实现会在协议层直接回 400。所以 kb-api 改用 `--http h11` 启动。

## Swarm 侧的配置（由 `swarm/configure_swarm.py` 在容器启动时完成）

- 模型配置写进 `~/.jiuwenswarm/config/.env`。Swarm 加载 `.env` 时会覆盖环境变量，所以只设容器环境变量不生效。
- kb-service 登记成「已连接的连接器」，写在 `<workspace>/mcp/state.json`。密钥通过 Swarm 自带的 `CredentialStore` 加密保存。
- 用 Swarm 自己的 `create_agent_template` / `install_equipment_gated` 创建并安装「知识库问答」专家（id `kb-qa`），这个专家绑定了 kb-service 连接器。
- `swarm-kb-plugin/plugin/` 下每个带 `frontend/dist` 的插件目录（kb-manager、da-analyst）都复制到 `~/.jiuwenswarm/application_plugins/`。
- 开发版：把镜像里的 `deepanalyze_harness-*.whl` 用 Swarm 的 `install_custom_agent` 装成「三方 Agent」，和设置页上传走的是同一段代码；再把它写进默认团队的 `external_cli_agents`，同时配上 hybrid transport。这个开发版的设置页能上传 wheel，但「三方 Agent」开关列表里不显示自定义 agent，所以开关由脚本代为打开。

**怎么在对话里用知识库**：

- **推荐**：在输入框点「+ → 专家 → 知识库问答」，kb-service 会自动挂上，不用再开连接器。
- **普通会话**：点「+ → 扩展 → 连接器」，手动打开 kb-service。

## 知识求索页（DA 作为 harness provider）

`harness/da-harness/` 是一个 openJiuwen HarnessProtocol 1.0 的 provider 包（`deepanalyze-harness`），把远端 DA-java 包成标准 harness：

- 一次 `send` 对应 DA 的一次 `run-stream`。文字、思考、工具调用、用量都映射成协议事件。
- DA 的 `ask_user` 映射成宿主的 `UserInputRequest` 或 `ToolApprovalRequest`，回答后调 `/answer` 续跑。
- `abort` 对应 `/cancel`，steer 对应 `/inject`。
- 断流时带 `Last-Event-ID` 重连；会话靠 checkpoint 恢复，checkpoint 里不含密钥。
- 包里声明了入口点 `openjiuwen.harness_providers: deepanalyze`，留给支持第三方 provider 的新版宿主用。

DA 有两种用法。

**「知识求索」页**（0.2.7 和开发版都有）：da-analyst 插件当宿主。

- 插件后端给每个对话起一个 `DeepAnalyzeHarness`，把 harness 事件转成简化的 JSON 推给页面；
- DA 的提问和审批在页面上以卡片形式作答；
- 页面支持多选知识库（一个都不选等于全部）、停止、执行中补充指令、新对话。
- **对话不随页面断开而停止**：在左侧导航切到别的页面会重建 iframe、断开插件 WS，正在跑的分析照常跑完；切回来时页面按会话 id 重新接上，补齐期间的输出。没人看着、也没在跑的对话 10 分钟后才停掉 harness。
- **历史对话**：页面左栏列出最近 50 个对话，可以打开、继续追问或删除。插件后端把每个对话的转录（用户输入加上页面用到的事件，流式输出合并成整段）存在 `~/.jiuwenswarm/da-analyst/history/<会话 id>.json`（`DA_ANALYST_HISTORY_DIR` 可改），在 swarm 的工作区卷里，重启不丢。打开一个已经停掉的对话时，用同一个会话 id 起新的 harness；DA 的会话 id 由它推出，所以只要 DA 还留着上下文，追问就能接上。Swarm 重启时正在跑的那一轮会显示为「已中断」。

插件后端的单元测试（在 Swarm 镜像里跑，需要镜像里的 jiuwenswarm 和 openjiuwen）：

```bash
docker run --rm --entrypoint sh -v "$PWD/swarm-kb-plugin:/src:ro" kb-suite/swarm:dev -c \
  "pip install -q pytest pytest-asyncio && cd /tmp && python -m pytest -q -p no:cacheprovider -o asyncio_mode=auto /src/tests"
```

**切换插件页**：Swarm 每次切到插件页都会重建 iframe，并且对插件资源回 `Cache-Control: no-cache`、不认条件请求，于是每次都整份重下未压缩的 JS（两个插件页各三百多 KB）。远程访问链路只有几百 KB/s 时，切一次要白屏 1 到 2 秒。edge 给响应加 gzip（插件 JS 压到约 1/3），并让带内容哈希的插件资源长缓存；入口 html 仍每次回源，所以重新构建插件后能拿到新文件名。插件 html 里还有一小段内联脚本，在主题同步前先沿用 Swarm 页面的背景色，避免闪白。回到直连 Swarm：删掉 compose 里的 `edge` 服务，把 swarm 的端口恢复成 `5199:5173`。

**集群模式的团队成员**（开发版）：openJiuwen 团队把 `cli_agent=deepanalyze` 的成员交给这个 provider，provider 接收团队传来的通用配置，DA 相关设置从环境变量读取。

- 用法：输入框切到「集群模式」，让 leader 调 `spawn_teammate` 时填 `cli_agent` 为 `deepanalyze`。leader 不会自己把「deepanalyze」和这个参数对上，要么在需求里写明，要么它会改用专家。
- 团队工具：团队给成员的是本地 stdio 的 `openjiuwen-team-mcp`。DA 在另一个容器里用不了它，所以 provider 在 Swarm 容器里起这个进程，用 streamable HTTP 转出去（`http://swarm:<随机端口>/<随机令牌>/...`，只在 compose 网络内可达），再通过 `runtimeConfig.mcpServers` 交给 DA。DA 看到的工具名是 `mcp_openjiuwen-team_send_message` 这样的形式。
  - DA 调工具前会往参数里加 `__da_session`、`__da_tenant`，再按工具的 schema 校验，团队工具不允许多余字段，校验过不去。所以桥接把 schema 放宽，转发前再去掉这两个字段。
  - DA 的默认权限策略（`tiered_policy:defaults.*`）对每个 MCP 工具都要确认。宿主自己挂进来的工具由 provider 直接批准，代价是每次调用多一轮「中断、续跑」，大约 3 秒。
  - 桥接对 GET 回 405，不开服务端推送的长连接。
- 角色提示：DA 的接口没有 system prompt，团队给成员的角色说明会拼在新会话第一轮输入的前面，后面再附一段说明，告诉 DA 写出的文件团队看不到、结果要写进消息正文。
- 知识库范围：DA 没有知识库范围时会隐藏知识库工具。团队成员没有选库这一步，所以 provider 每轮开始前读 kb-service 的 `/v1/kbs`（用只读的 `KBS_MCP_KEY`），把租户下全部知识库作为范围。
- 提问和审批：团队宿主不提供提问和审批通道，DA 的提问按「无法询问，请继续」作答；除了上面说的宿主工具，其余审批一律拒绝（DA 只认「允许」等批准词，其他回答都按拒绝处理）。

实测（10-26）：leader 起了 DA 队友，DA 检索知识库后用 `send_message` 把结果发给 leader，leader 被唤醒后整理成表格回复用户。

需要的环境变量（compose 已配好）：`DA_BASE_URL`、`DA_TENANT_ID`、`DA_KB_CATALOG_URL`、`DA_KB_CATALOG_KEY_ENV`、`DA_MCP_BRIDGE_HOST`，以及 `secrets.env` 里的 `DA_ENGINE_SECRET`。

测试：

- `harness/da-harness/tests` 是单元测试，用 `httpx.MockTransport` 模拟 DA。团队成员相关的用例里，MCP 桥接起的是真实的 stdio MCP 进程，走 streamable HTTP 调用。
- `harness/da-harness/e2e/run_contract.sh` 拿 agent-core 自带的 harness 契约用例跑真实 DA，需要 da-engine 在运行。默认用 0.2.7 镜像和 v0.1.19 的用例；开发版用 `SWARM_IMAGE=kb-suite/swarm:dev AGENT_CORE_REF=<develop 锁定的 agent-core 提交>`。

## Loop Forest（第二个分析引擎）

agent-solution 里的 `loop-forest` 是一个 Java 库：在 agent-core 的 ReActAgent 外面加一套「循环纪律」rail，包括目标重锚、否决、分叉与收敛、预算、token 上限、完成校验、续跑。它本身不是服务，所以 kb-suite 加了两样东西：

- **`service/lf-service/`（Java 服务）**：用 JDK 自带的 HTTP server 提供 DA `/api/agents` 的兼容子集，包括 `run-stream`（SSE）、`stream/{taskId}`（按 Last-Event-ID 重放）、`status`、`cancel`、`inject`。请求头、事件名和错误码（`ENGINE_BUSY`）都和 DA 一致。
  - 每次运行照 `LoopForestAgent` 的配方装配：ReActAgent 加上 `LoopForestSuite`（分叉、token 上限、预算）。区别是在服务里自己装配，这样才能流式输出、注入系统提示、挂 MCP、取消和补充指令。
  - 知识库工具用 kb-service 的 MCP，请求里的 `scope.kbIds` 通过 `X-KB-Scope` 请求头强制限定（kb-service 的 MCP 支持这个头，交集为空时直接拒绝）。请求里的 `runtimeConfig.mcpServers`（团队工具）同样挂上。每次运行的注册在结束时注销。
  - 分叉：模型调用分叉工具时，服务用同一个模型和同一套知识库工具起一个子 agent 去完成子目标（最多 `LF_FORK_MAX_ITERATIONS` 轮），把结论返回给主循环。
  - 多轮：服务按会话保存最近 `LF_HISTORY_TURNS` 轮问答，拼进下一轮的输入。
  - 取消：标记后，任务在下一次模型调用前收尾；补充指令推进 Loop Forest 的 steering 队列。
  - Loop Forest 不会向用户提问，`/answer` 返回 404。
  - **同一时刻只跑一个任务**：Loop Forest 的工具（分叉工具）用固定 id 注册在进程全局，并发运行会冲突。其他请求收到 `ENGINE_BUSY`，provider 会自动重试，最多等 180 秒。
- **`harness/lf-harness/`（provider，入口点名 `loopforest`）**：直接复用 `deepanalyze-harness` 的实现，只换了 card（不声明提问和审批）、引擎名和变量前缀 `LF_`。wheel 里带一份同版本的 `deepanalyze_harness`。

**构建**：`./kbsuite.sh build-lf`，需要先跑过 `build-da`（core、runtime、react-rails 要已经在 `kbs-m2` 里）。脚本按 `LF_SOLUTION_REF`（默认同 `DA_SOLUTION_REF`）导出 agent-solution 的 loop-forest 模块，装进本地 Maven 仓库，再打出 `lf-service.jar`（含单测）和镜像。

**配置**（`lf-engine` 容器）：
- 密钥、模型、kb-service 密钥默认取 `DA_ENGINE_SECRET`、`KBS_LLM_BASE_URL`、`KBS_LLM_API_KEY`、`KBS_MCP_KEY`；也可以分别用 `LF_ENGINE_SECRET`、`LF_MODEL_BASE_URL`、`LF_MODEL_API_KEY`、`LF_KB_MCP_KEY` 覆盖。
- 可调项：`LF_MODEL_NAME`（qwen-plus）、`LF_TEMPERATURE`（0.7）、`LF_MAX_TOKENS`（8000）、`LF_MAX_ITERATIONS`（36）、`LF_FORK_ENABLED`（true）、`LF_FORK_MAX_ITERATIONS`（12）、`LF_CONTEXT_BUDGET`（true）、`LF_GOVERNOR_CAP_TOKENS`（200000）、`LF_HISTORY_TURNS`（6）、`LF_SYSTEM_PROMPT`。

**在 Swarm 里怎么用**：
- 「知识求索」页右上角可以切换引擎（DeepAnalyze / Loop Forest），每个引擎各自保留会话。
- 集群模式下让 leader 用 `spawn_teammate` 时把 `cli_agent` 填成 `loopforest`。Swarm 侧的变量是 `LF_BASE_URL`、`LF_ENGINE_SECRET`、`LF_TENANT_ID`、`LF_KB_CATALOG_URL`、`LF_KB_CATALOG_KEY_ENV`、`LF_MCP_BRIDGE_HOST`，compose 已经配好。

## 已知限制

- Word 的命中没有页码，原因见上文 docreader 一节。
- 图片要接入 OCR 或 VLM 才能入库，目前会直接拒绝图片文件；所以 WeKnora 的多模态、图片向量、ASR 相关设置都没有做。
- WeKnora 的知识图谱（Neo4j）、FAQ 知识库、标签和自动打标签、数据源同步、共享，以及智能体（对话）设置都没有做；对话侧的设置在 Swarm 里。
- Wiki 没有目录树（WeKnora 的 folders）、问题标记（issues）和 agent 写页面的工具；页面按类型分组浏览。
- 只有一个租户：`default`。
- 开发版 Swarm 的单 Agent 模式（主 agent）还不能挂第三方 provider，DA 只能当团队成员或在「知识求索」页里用。专家模板的外部运行时也只认 codex 和 claudecode。
- DA 写的文件留在 DA 容器里，团队工作区里看不到，结果只能通过消息传回来。
- leader 不会自己把「deepanalyze」和 `cli_agent` 参数对上，没写明时会改用专家（实测第一次就是这样）。
- 每次团队工具调用都多一轮 DA 的确认续跑（约 3 秒）；每个 DA 任务开始时重连一次团队 MCP（几十毫秒）。
- 停止后，正在进行的那一次模型调用会跑完（几秒），这期间下一轮会收到 `ENGINE_BUSY`，provider 在没有输出时自动重试。
- 页面会话的 checkpoint 存在插件进程的内存里，Swarm 重启后没法走 resume。不过 DA 的会话 id 是从页面会话 id 推出来的，只要 DA 那边还留着上下文，就会接着用；对话的转录另存在工作区卷里，历史列表不受影响。
