# `@onto-agent/sdk`

onto-agent 的 TypeScript SDK。

当前设计方向：

- 以 `/agentic/*` 作为主能力面
- 并行暴露 `/a2a/*` 兼容接口
- 提供统一的流式事件抽象

## 构建与打包

### 本地构建

```bash
cd platform/protocols/sdk/typescript-sdk
./build.sh
```

### 生成可分发包

```bash
cd platform/protocols/sdk/typescript-sdk
./package.sh
```

产物：

- `dist/`
- `onto-agent-sdk-<version>.tgz`

## 给其他项目使用

### 安装本地目录

```bash
npm install /path/to/onto-agent/platform/protocols/sdk/typescript-sdk
```

### 安装本地打包产物

```bash
npm install /path/to/onto-agent/platform/protocols/sdk/typescript-sdk/onto-agent-sdk-0.1.0.tgz
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
