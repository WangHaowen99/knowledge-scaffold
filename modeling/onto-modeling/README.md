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

## 场景、来源与评测结果核验

- 场景走查由 `trace_inputs.py` 统一读取已保存确认稿，保留字段类型与尾注，派遣前及报告前核对源文件修订和初始输入回执。解析或对账失败不计通过。
- 来源核验使用 `source_extract.py` 抽取 UTF-8 文本、DOCX 正文/表格和 XLSX 单元格，返回原文件摘要与定位锚点；无法读取或定位时明确标记“无法核验”，不以新增 ID 声明或历史基线免检。
- 页面及新报告不再展示覆盖率统计，仍展示具体缺项、推演通过率与数据评测分数；旧报告原文保留。
- 数据评测区分有效判分与技术异常：无有效结果显示“未形成有效评测”，混合结果标明仅为有效子集得分，不把技术异常当业务零分。

升级时需匹配工作区工具箱版本；旧回执保留但须重新核验。实现与兼容边界见
[`modeling-review-integrity.md`](../../docs/decisions/modeling-review-integrity.md)。

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
