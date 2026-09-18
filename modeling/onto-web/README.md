# Onto Web — 本体建模工具 Web 版

基于 Claude Agent SDK 的本体建模 Web 工作台。设计方案见
`/mnt/jianguoyun/meta/design/本体建模工具Web版-方案设计-20260918.md`（第一片垂直切片）。

## 结构

```
onto-web/
  backend/    FastAPI 服务：项目/工作区管理、Agent 会话编排（SSE）、文件与版本 API
  frontend/   React 19 + Vite 7 + antd + @ant-design/x 三栏工作台
  assets/     提示词资产（同步自本体建模工具集，挂载进每个任务工作区）
  docs/       架构决策记录
```

## 启动

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

## 测试

```bash
cd backend && ./venv/bin/python -m pytest tests/
cd frontend && npm run build
```

## 当前状态（第一片）

已有：项目创建（独立 git 工作区 + 资产软链）、资料上传、Agent 会话编排
（流式消息、确认卡、打断）、文件树 / markdown+mermaid 预览、`[[链接]]`跳转、
版本时间线、consumer 同源色系。

未做（后续切片）：Studio、diff 视图、drawio/Excel 预览、代码生成链、
评测体系、会话池与预算闸门、多用户。
