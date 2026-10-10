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

## 会话池（方案 4.1.1）

每个活跃建模会话 = 一个 CLI 子进程（均值约 400MB）。会话池按
`ONTO_WEB_MAX_SESSIONS`（默认 12，对应 8G 内存）限制并发：超限先驱逐最久
空闲的会话，全部在跑则排队并在前端显示位次；空闲超过
`ONTO_WEB_SESSION_IDLE_TTL`（默认 600 秒）的会话被回收，下次发消息用
resume 续接模型上下文，界面无感。

## 测试

```bash
cd backend && ./venv/bin/python -m pytest tests/
cd frontend && npm run build
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

## 当前状态

已有：项目生命周期（创建/导入向导（已发布知识包与工作区内容格式自动识别，识别不出
或解析为空时拒绝导入）/导出（含发布为知识包）/克隆/归档/软删，按用户隔离 +
成员共享与只读评审角色）、资料上传（分类/替换/变更提醒）、Agent 会话编排（会话池 +
排队 + 空闲回收 + resume 续接、流式消息、确认卡、打断）、hash 路由与深链、文件树
（树内搜索）/ markdown（语法高亮/代码复制/mermaid 主题联动）、drawio 预览与
在线编辑（官方 drawio 离线 webapp）、Excel 预览、HTML 沙箱预览、`[[链接]]`跳转、
文件编辑乐观并发（Web 保存即版本，冲突走合并对比）、版本时间线/回退/标记、
Studio 三视图（图/表/条目 + **结构化编辑：schema 表单、草稿区、30 分钟租约、
确定性检查分流**）、报告逐条评审（编号勾选/逐条修复）、走查分支卡、业务知识
沉淀（对话一键入库 + 知识库管理页）、六种反查面板、挂卡重复计数与取消出口、
单轮成本/缓存命中展示、回收站恢复、登录鉴权与账号改密、设置中心独立页
（模型/MCP 测试连接与按项目启用/权限模式/资产版本回滚）、运维中心（用量/审计/
备份定时调度/用户管理/会话池/健康页/回收站）、离线交付包构建与部署链、
consumer 同源色系。

未做（后续切片）：pixi.js 大图渲染完整路径（现为 xyflow + 大图降级模式）、
代码生成链评审页的专属候选格式、评测体系 API 化。
