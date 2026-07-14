# `@onto-agent/sdk`

onto-agent 的 TypeScript SDK。

当前设计方向：

- 以 `/agentic/*` 作为主能力面
- 并行暴露 `/a2a/*` 兼容接口
- 提供统一的流式事件抽象

## 构建与打包

### 本地构建

```bash
cd sdk/typescript-sdk
./build.sh
```

### 生成可分发包

```bash
cd sdk/typescript-sdk
./package.sh
```

产物：

- `dist/`
- `onto-agent-sdk-<version>.tgz`

## 给其他项目使用

### 安装本地目录

```bash
npm install /path/to/onto-agent/sdk/typescript-sdk
```

### 安装本地打包产物

```bash
npm install /path/to/onto-agent/sdk/typescript-sdk/onto-agent-sdk-0.1.0.tgz
```

### 发布到 registry

```bash
npm publish
```

## 最小示例

```ts
import { createOntoAgentClient } from '@onto-agent/sdk';

const client = createOntoAgentClient({
  baseUrl: 'http://localhost:5055',
  token: process.env.ONTO_AGENT_TOKEN,
  workspaceId: 'demo-workspace',
});

const stream = await client.chat.send({
  sessionId: 'session-123',
  message: '请分析这个项目',
});

for await (const event of stream) {
  console.log(event.kind, event.payload);
}
```

`workspaceId` 会编码后写入每个请求的 `X-Workspace-Id`。`token` 必须是 access token。浏览器应用应自行保存登录响应里的 refresh token，并通过 `/auth/refresh` 单次刷新；通用 TypeScript SDK 不读取、不持久化 refresh token。

## 会话与运行续传

会话内容可用 `client.agentic.sessions.get(sessionId)` 或 `messages(sessionId)` 重新载入。网络中断后保存最后一个事件的 `sequence`，再连接同一 run：

```ts
let afterSeq = 0;
const resumed = await client.agentic.runs.resume('run-123', { afterSeq });

for await (const event of resumed) {
  afterSeq = event.sequence ?? afterSeq;
  console.log(event.kind, event.eventId);
}
```

排队中的 run 会返回 `409`，应先轮询 `runs.status()`；内存回放窗口已经释放时返回 `410`，此时改读 session 历史。`resume()` 同时发送 `after_seq` 和 `Last-Event-ID`，服务端只回放游标之后的事件。

## MCP 与 Graph

MCP 默认独立监听 `7488`：Streamable HTTP 地址为 `/search/mcp`、`/chat/mcp`。通过 `/agentic/mcp/tokens` 创建 workspace 专用 token，搜索和聊天分别要求 `mcp:search`、`mcp:chat` scope；不要复用 consumer access token。

Graph 连接按 workspace 配置并显式绑定 ontology。Neo4j 配置 `uri/database/username/password`，HugeGraph 配置 `url/graph/graph_space` 以及可选凭据；SDK 不从本地环境推断 Graph provider。

## 运行模式

后端运行模式通过 `context.agent_mode` 传入：

```ts
const stream = await client.chat.send({
  sessionId: 'session-123',
  message: '请分析这个项目',
  agentMode: 'multi_agent',
});
```

可选值：

- `quick`：默认值，关闭 reasoning，不暴露子 Agent 编排工具
- `thinking`：启用 reasoning，不暴露子 Agent 编排工具
- `multi_agent`：启用 reasoning，并允许 `spawn_sub_agent` 编排

也可以通过 `context: { agent_mode: 'multi_agent' }` 透传给底层接口。
