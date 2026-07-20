# SDK 目录

统一的 SDK 代码放在 `sdk/` 目录下，按语言拆分为独立包：

- `typescript-sdk/`：TypeScript SDK
- `python-sdk/`：Python SDK

两种 SDK 都遵循同一客户端契约：

- `token` 只接收 access token；浏览器应用自行通过 `/auth/refresh` 管理 refresh token，通用 SDK 不保存或暴露 refresh token。
- 配置 `workspace_id` / `workspaceId` 后，每个业务请求都会发送 `X-Workspace-Id`。
- 会话可通过 sessions 接口重新读取；运行流可用 `runs.resume(..., after_seq/afterSeq)` 从最后消费的 `seq` 继续。
- MCP 客户端直接连接独立服务的 `7488` 端口，不复用本 SDK 的 access token；需通过 `/agentic/mcp/tokens` 创建带 `mcp:search` 或 `mcp:chat` scope 的专用 token。

## 常用命令

### 构建全部 SDK

```bash
cd sdk
./build.sh all
```

### 打包全部 SDK

```bash
cd sdk
./package.sh all
```

### 单独构建 TypeScript SDK

```bash
cd sdk/typescript-sdk
./build.sh
```

### 单独构建 Python SDK

```bash
cd sdk/python-sdk
./build.sh
```

## 对外使用文档

如果需要把 SDK 提供给其他项目使用，请优先参考：

- `sdk/PUBLISHING.md`
