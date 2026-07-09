# 知识包建模工具

`modeling/` 下的独立 Web 服务——基于 [pi](https://github.com/earendil-works/pi) agent 的动态 workflow 交互式建模，把领域资料结构化为知识包并经 consumer 实测、优化器迭代。

- 设计文档：[docs/design.md](docs/design.md)
- pi 集成验证：[docs/spike-notes.md](docs/spike-notes.md)

## 状态

**M1（服务骨架 + 认证 + 项目/上传/解析 + workflow 引擎与恢复）已完成**，验收「传入样例资料走到节点 3」通过。当前为后端；前端与后续里程碑（M2 访谈/生成、M3 评测闭环、M4 优化循环）见设计文档。

已实现节点：① 项目创建 · ② 资料上传与解析（Excel→数据字典、md/txt/word/pdf→正文）· ③ 初步分析（pi agent run 产出领域概览/候选对象/开放问题，快照可序列化恢复）。

## 技术栈

Fastify 5 + `node:sqlite`（零原生编译）+ pi-agent-core/pi-ai + xlsx/mammoth/pdf-parse。**需 Node ≥ 22**（`node:sqlite`）。

## 本地开发

```bash
cp .env.example .env      # 填 MODELING_LLM_* 与 AUTH_ISSUER_URL
npm install
# node:sqlite 在 Node 22 需实验开关；Node 24+ 可省略
NODE_OPTIONS=--experimental-sqlite node --env-file=.env node_modules/.bin/tsx src/server.ts
```

服务默认 `http://localhost:5100`。认证 issuer 指向 consumer（需 consumer 在运行以登录）。

## 验证

```bash
# M1 冒烟：用 faux LLM（无需真实 key）跑通节点 1→3
DATA_DIR=./data/smoke MODELING_LLM_BASE_URL=x MODELING_LLM_MODEL=x npx tsx scripts/smoke-nodes.ts

npm run typecheck
npm run build
```

## 主要接口（M1）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/login` | 转发 issuer 登录 |
| GET | `/api/me` | 当前用户 |
| GET/POST | `/api/projects` | 项目列表 / 新建（节点 1）|
| GET | `/api/projects/:id` | 项目详情（含文档、workflow 节点）|
| POST | `/api/projects/:id/documents` | 上传并解析资料（节点 2，multipart）|
| POST | `/api/projects/:id/analyze` | 触发初步分析（节点 3）|

除 `/health` 与 `/auth/login` 外均需 `Authorization: Bearer <token>`（经 AuthClient 向 issuer introspect 校验）。
