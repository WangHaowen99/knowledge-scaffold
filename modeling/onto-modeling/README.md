# Onto Modeling — 本体建模工具 Web 版

基于 Claude Agent SDK 的本体建模 Web 工作台。设计方案见
`/mnt/jianguoyun/meta/design/本体建模工具Web版-方案设计-20260918.md`。

## 结构

```
onto-modeling/
  backend/    FastAPI 服务：项目/工作区管理、Agent 会话编排（会话池 + SSE）、文件与版本 API
  frontend/   React 19 + Vite 7 + antd + @ant-design/x 三栏工作台
  assets/     提示词资产（同步自本体建模工具集，挂载进每个任务工作区）
  deploy/     离线交付包构建、预检与部署脚本
  docs/       架构决策记录
```

## 启动（开发）

后端（默认 127.0.0.1:8100）：

```bash
cd backend
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python run.py
```

前端（默认 127.0.0.1:5180，/api 代理到后端）：

```bash
cd frontend
npm install
npm run dev
```

模型接入通过环境变量注入（见 `.env.example`），不落盘、不进仓库。

模型上下文约束按 配置中心「模型接入 → 上下文与自动压缩」> `.env` 中的 `ONTO_WEB_CONTEXT_*`
（见 `.env.example`，变量名与会话引擎无关）> 内置默认 逐项生效。配置页只填模型真实窗口和最大输出，
内置默认分别为 200000 和 32000；压缩窗口与触发百分比只能在 `.env` 配置，两者都没配时服务端按生效的
窗口与最大输出自动推算，使压缩前的请求仍给模型窗口留至少 10% 余量（压缩窗口向下取 10000 的倍数，
低于 CLI 下限 100000 时改推触发百分比，向下取 5 的倍数），配了任一项就完全按 `.env`。稳定引擎把
生效值转成 CLI 的环境变量注入会话（Claude 官方模型名不读窗口变量，仍按 CLI 内置窗口）。接入窗口
小于生效窗口的模型时必须配置真实窗口，否则自动压缩来不及触发，请求会因超出模型窗口被网关拒绝；
窗口更大的模型可配置以用满窗口。配置页按输入框里的值实时提示自动压缩的触发点。`.env` 改动需重建
容器（`docker compose up -d`）才生效，非法值会被忽略并在服务日志中告警。

配置页填写模型名后会查模型上下文推荐表：命中时填入窗口和最大输出，可手改，与网关配置一起保存；
没命中时清空这两项（原值属于之前的模型）；改回已保存的模型时恢复已保存的值。推荐表只做预填，
不参与上面的生效顺序，打开配置页也不会自动改已保存的值。匹配规则：不区分大小写，忽略 `provider/`
前缀、`.` 与 `_` 写法差异和网关追加的 6 或 8 位日期后缀，其余须与 id 或表中别名完全一致。

推荐表分两层：内置表（`backend/app/services/model_context_presets.json`，随版本发布，数值取自厂商
官方文档，最大输出取官方默认值，思考与非思考默认值不同时取思考模式的）和数据目录下的自定义表
`model-context-presets.json`（同 id 整条替换内置条目，新 id 追加）。管理员可在配置页「查看或编辑
推荐表」中覆盖内置条目、恢复内置、新增或删除模型；运维也可参照数据目录下的
`model-context-presets.example.json`（每次启动按内置表重写，本身不生效）直接编辑自定义表。
自定义表改动在下次查表时生效，无需重启；文件有误时整份不生效、沿用上一次合法的结果，页面显示错误且
不能编辑。页面保存带上读取时的版本号，文件已被改过时拒绝保存，每次保存前把上一版存为同目录的
`model-context-presets.json.prev`。「停用自定义表」把文件改名为 `model-context-presets.json.bak-日期-时间`
备份，改回原名即重新启用。
聊天面板顶部的上下文环显示稳定引擎最近一次模型调用的已用量、
模型窗口、剩余量和距自动压缩的余量；发生压缩时对话里插入「上下文已自动压缩」提示。

启用登录鉴权后，成员可在「设置 → 我的模型配置」填写自己的 API Key（可选再填网关地址、
模型，留空沿用管理员配置）。个人 Key 只用于本人发送的消息：同一项目换人发消息且配置来源
不同时，会话断开后用 resume 续接；本轮未结束时拒绝换人（409）。没有个人 Key 的成员、
未启用鉴权的部署，以及管理员关闭个人 Key 后，行为都与管理员统一配置时一致。

## 会话池（方案 4.1.1）

每个活跃建模会话 = 一个 CLI 子进程（均值约 400MB）。会话池按
`ONTO_WEB_MAX_SESSIONS`（默认 12，对应 8G 内存）限制并发：超限先驱逐最久
空闲的会话，全部在跑则排队并在前端显示位次；空闲超过
`ONTO_WEB_SESSION_IDLE_TTL`（默认 600 秒）的会话被回收，下次发消息用
resume 续接模型上下文，界面无感。

每个项目可有多条会话（聊天面板顶部下拉：新建、切换续聊、重命名、删除），各自保存历史与模型
上下文，但同一时刻只有一个在线引擎。往另一条会话发消息时，原会话空闲就换绑，忙则返回 409。
存储与取舍见 `docs/decisions/0013-multi-conversation-sessions.md`。

## 数据评测前的对象关系检查

启动数据评测前，服务检查包内关系的源对象、目标对象及自关联。结构错误会返回 HTTP 422
和 `EVALUATION_PACKAGE_INVALID`，并给出对象文件及行号，不创建后台评测任务或新轮次。
检查适用于所有数据源模式，允许对象未绑定数据表也不能跳过关系结构检查。

正常关系会在导出、导入后保留；存量关系名称包含端点名称或基数时仍允许评测。
后台导出使用已检查的对象内容快照；对象内容变化后不会自动续接旧轮次。
实现和验证范围见 [issue #367 修复记录](../../docs/issue-367-regression-review.md)。

## 测试

```bash
cd backend && ./venv/bin/python -m pytest tests/
cd frontend && npm test && npm run build
```

## 构建与部署

```bash
./build.sh <version> [amd|arm]     # 产出 dist/onto-modeling-<version>-linux-<arch>.tar.gz
docker compose up --build          # 本地从源码起全栈（onto-modeling + auth-server）验证
```

依赖复用（需 BuildKit，Docker Desktop / buildx 默认开启）：

- pip 索引源默认华为云 PyPI 镜像，可用 `ONTO_MODELING_PIP_INDEX_URL`（或 `PIP_INDEX_URL`）覆盖，
  例如 `ONTO_MODELING_PIP_INDEX_URL=https://pypi.example.com/simple ./build.sh <version>`；
- 宿主 pip 运行在 Python 3.11 及以上时，构建脚本先把目标平台 wheel 供应到 `vendor/wheels/`
  （含约 100MB 的 claude-agent-sdk，命中宿主缓存则零下载），镜像内经 bind mount 作为 find-links，
  索引不可达时兜底；宿主 Python 更低时环境标记无法按 3.11 求值、供应不完整，脚本跳过这一步，
  避免大包在宿主和镜像内各下一遍；
- 运行镜像基于 `python:3.11-slim`、前端构建用 `node:22-slim`，与 auth-server /
  consumer / modeling 同基础镜像，复用已拉取的层；
- npm 与 pip 均挂 BuildKit 缓存，同机重复构建不再重新下载。

目标机部署流程与容量规划（含 AVX 预检、降级兼容包）见 `deploy/README.md`。

## 项目级本地 MCP

在项目设置的 MCP 页面创建 Token 并复制 Read / Change 地址。MCP 直接读写当前
Onto 工作区，新建、导入和克隆项目无需创建 Modeling 关联。旧 Modeling MCP
地址及 Token 不会自动迁移，请为本地端点重新创建 Token。

- HTTP：`/api/onto-mcp/{project_id}/{read|change}/mcp`。
- SSE：`/api/onto-mcp/{project_id}/{read|change}/sse`。
- Token 通过 `Authorization: Bearer <token>` 传递，按项目隔离；读、校验、保存
  分别要求 `ontology.read`、`ontology.change.validate`、`ontology.change.apply`。
- 写操作必须提供最新 `expected_revision`；修改保存到本项目的 Git 工作区。
- 默认按访问工作台的地址生成 MCP 地址。反向代理部署可选用
  `ONTO_WEB_MCP_PUBLIC_BASE_URL` 指定公开地址，例如 `https://example.com`。
- 本次独立化仅针对 MCP；原有 Modeling 评测配置仍按原流程使用。

设计与兼容边界见 [本地 MCP 设计说明](docs/decisions/onto-local-mcp.md)。

## 当前状态

已有：项目生命周期（创建/导入向导（已发布知识包与工作区内容格式自动识别，识别不出
或解析为空时拒绝导入）/导出（含发布为知识包）/克隆/归档/永久删除，按用户隔离 +
成员共享与只读评审角色；新建/导入/重命名/克隆时校验任务名称，重名给出候选名）、
资料上传（分类/替换/变更提醒）、Agent 会话编排（会话池 +
排队 + 空闲回收 + resume 续接、流式消息、确认卡、打断）、hash 路由与深链、文件树
（树内搜索）/ markdown（语法高亮/代码复制/mermaid 主题联动）、drawio 预览与
在线编辑（官方 drawio 离线 webapp）、Excel 预览、HTML 沙箱预览、`[[链接]]`跳转、
文件编辑乐观并发（Web 保存即版本，冲突走合并对比）、版本时间线/回退/标记、
Studio 三视图（图/表/条目 + **结构化编辑：schema 表单、草稿区、30 分钟租约、
确定性检查分流**）、报告逐条评审（编号勾选/逐条修复）、走查分支卡、业务知识
沉淀（对话一键入库 + 知识库管理页）、六种反查面板、挂卡重复计数与取消出口、
单轮成本/缓存命中展示、回收站恢复（仅旧版软删除留下的存量条目）、登录鉴权与账号改密、设置中心独立页
（模型/MCP 测试连接与按项目启用/权限模式/资产版本回滚/jiuwenbox 沙箱连接状态）、
代码生成脚本必做运行验证（经脚本验证网关在 jiuwenbox 沙箱中执行，不可用时在容器内执行，未验证的脚本不能提交）、
运维中心（用量/审计/
备份定时调度/用户管理/会话池/健康页/回收站）、离线交付包构建与部署链、
consumer 同源色系。

未做（后续切片）：pixi.js 大图渲染完整路径（现为 xyflow + 大图降级模式）、
代码生成链评审页的专属候选格式、评测体系 API 化。

## Hub2 知识包

建模发布统一保存到当前用户所属的独立 Hub 包，项目成员不会自动继承个人包权限；删除建模项目不删除 Hub 包。项目进度区和首页菜单中的「已发布版本」按当前用户查询并下载指定版本，发布成功不自动下载。工作区 Git 历史仍独立展示。

首页「我的 Hub 知识包」可分页选择包及固定版本，导入为本地建模项目并继续修改；后续发布保留包身份和基础版本。所有包操作要求统一账号登录，无鉴权开发模式不能访问包 API。没有旧项目包接口、迁移、复制或发布位置选择。

Hub 不可用时发布失败；Hub 已提交、本地关联尚未写入时提示待恢复，并由定时任务或原请求重试收敛。最新项目资料独立同步、失败后台重试；当前建模内评测使用 Hub2 快照，执行编排及 MCP 草稿不变。独立 eval1 和消费侧接入后续实施。

部署配置 `ONTO_HUB_URL`、`ONTO_HUB_SERVICE_TOKEN`（对应 Hub `HUB_SERVICE_TOKEN`）和统一账号认证；包操作和项目资料共用建模服务身份，部署脚本沿用原有 token 自动生成逻辑。Hub 不在本项目的源码/发布 Compose 中，而是独立 compose 项目（`knowledge-hub/deploy/docker-compose.knowledge-hub.yml`、独立离线包），需与本服务同处一个 Docker 网络；整栈 `deploy/deploy.sh` 与安装脚本先部署 Hub，再把本服务的 `ONTO_HUB_SERVICE_TOKEN` 对齐为 Hub 实际使用的值。评测服务凭据由 Hub 部署脚本生成并由评测部署沿用，消费服务凭据按后续接入配置。启动、部署、API、限制与备份见 [Hub README](../../knowledge-hub/README.md)。

建模备份包含 `hub-tasks/` 中的主体包关联、冻结发布输入及恢复记录，不保存用户令牌；Hub 卷单独备份。发布门槛继续提示资料分析、对象提取、操作提取和质量检查；示例数据、场景验证及可选步骤不阻塞发布。带发布副作用的 GET `export?scope=package` 已取消，应先 POST 发布，再下载指定版本。


## 独立评测入口（eval1）

本地开发时，默认直接打开本机 8090 的评测服务。建模后端设置 `ONTO_WEB_EVAL_PUBLIC_URL=/evaluation/` 时改走同源路径，由建模 Vite 将 `/evaluation/`（页面和 API）代理到 `http://127.0.0.1:8090`，可在启动 Vite 时通过 `EVAL_DEV_API_URL` 覆盖。需要先构建评测前端并独立启动评测服务，步骤见 [评测服务说明](../../eval-service/README.md)。修改代理环境变量后须重启 Vite。

评测入口地址只由部署变量 `ONTO_WEB_EVAL_PUBLIC_URL`（写在 `.env`）决定，页面不提供修改入口。这个地址由用户浏览器访问，不是容器网络地址；修改后须重启建模服务。

- 直连（默认）：未设置时，浏览器打开与建模页面同主机的 `http://<主机>:8090/evaluation/`，整栈同机部署无需配置。评测在其他主机、端口或经 HTTPS 时设为完整地址（如 `http://eval.example.com:8090/evaluation/`），须为 http(s)、不含账号和查询参数，路径固定为 `/evaluation/`。点击评测时打开新窗口，登录经 `postMessage` 只交给该地址的来源；评测侧的来源限制见评测服务说明。
- 同源网关：在建模页面所在的同源独立网关接入 `/evaluation/`，保留完整路径并转发至评测服务，参考 [网关配置示例](../../eval-service/deploy/nginx.conf.example)，并设置 `ONTO_WEB_EVAL_PUBLIC_URL=/evaluation/`。此时在当前窗口跳转并共享浏览器中的登录。

默认全栈 `bash deploy/deploy.sh` 包含评测；仅部署建模不会启动评测。也可从仓库根执行 `bash deploy/deploy.sh --eval-only` 独立部署评测，并按说明注入受保护的运行配置。健康检查返回 404 时检查路由，502/503 时检查评测上游，返回 HTML 时检查是否误入建模页面回退。正确响应应为 `{"status":"ok","service":"evaluation"}`。

进度栏不再提供“数据评测（可选）”和数据源编辑。项目已发布版本及“我的 Hub 知识包”的版本列表新增“评测”，仅携带 Hub 包/版本引用跳转生效的评测入口，不自动执行，URL 中不含令牌或项目 ID。未配置独立评测时明确提示，不回退到 Core。

评测服务自行管理用例、数据源、mapping 与运行结果，直接访问账号、Hub 和 consumer。任务中心显示建模项目名称时，评测以当前用户登录调用只读接口 `GET /api/hub/package-projects?hub_package_id=...`（每次 1 到 20 个包），返回本人可访问且本人关联到这些包的项目名称，每包最多 5 个并附总数。同源时登录通过浏览器存储复用、刷新协调使用共享锁，直连时由建模窗口交接；独立网关不依赖建模容器。部署见 `../../eval-service/README.md`，使用 `--eval-only`，不随 `--onto-modeling-only` 启动。

旧版评测记录按明确的历史入口查看；旧任务停止和对账接口保留。新的旧评测创建、用例/数据源修改接口返回 410。旧工作台和 MCP 的 Core 调用仍保留，不能据此下线整个 Core。评测服务不接管历史项目成员关系，也不随建模项目删除独立记录。
