# `onto-agent-sdk`

onto-agent 的 Python SDK。

## 构建与打包

### 本地开发安装

```bash
cd sdk/python-sdk
pip install -e .
```

### 生成 wheel 与 sdist

```bash
cd sdk/python-sdk
./build.sh
```

产物：

- `dist/*.whl`
- `dist/*.tar.gz`

## 给其他项目使用

### 安装本地目录

```bash
pip install /path/to/onto-agent/sdk/python-sdk
```

### 安装本地 wheel

```bash
pip install /path/to/onto-agent/sdk/python-sdk/dist/onto_agent_sdk-0.1.0-py3-none-any.whl
```

### 从 Git 仓库子目录安装

```bash
pip install "git+ssh://git@your-git-host/your-org/onto-agent.git@main#subdirectory=sdk/python-sdk"
```

### 发布到 PyPI / 私有 PyPI

```bash
python3 -m twine upload dist/*
```

## 最小示例

```python
from onto_agent_sdk import OntoAgentClient

with OntoAgentClient(
    base_url="http://localhost:5055",
    token="YOUR_TOKEN",
    workspace_id="demo-workspace",
) as client:
    session = client.chat.create_session(title="demo")
    session_id = session.get("id") or session.get("session_id")

    with client.chat.send(
        session_id=session_id,
        message="请分析这个项目",
    ) as stream:
        for event in stream:
            print(event.kind, event.payload)
```

`workspace_id` 会编码后写入每个请求的 `X-Workspace-Id`。`token` 必须是 access token。登录响应里的 refresh token 由浏览器/BFF 保存并通过 `/auth/refresh` 轮换；Python SDK 不读取、不持久化 refresh token。

## 会话与运行续传

会话内容可用 `client.agentic.sessions.get(session_id)` 或 `messages(session_id)` 重新载入。网络中断后保存最后一个事件的 `sequence`，再连接同一 run：

```python
last_seq = 0
with client.agentic.runs.resume("run-123", after_seq=last_seq) as stream:
    for event in stream:
        if event.sequence is not None:
            last_seq = event.sequence
        print(event.kind, event.event_id)
```

排队中的 run 会返回 `409`，应先轮询 `runs.status()`；内存回放窗口已经释放时返回 `410`，此时改读 session 历史。`resume()` 同时发送 `after_seq` 和 `Last-Event-ID`，服务端只回放游标之后的事件。

## MCP 与 Graph

MCP 是独立服务，地址包含工作空间专属的持久化路由标识，例如 `http://<host>:7488/<mcp_endpoint_id>/search/mcp` 和 `http://<host>:7488/<mcp_endpoint_id>/chat/mcp`。通过 `/agentic/mcp/runtime/status` 读取当前 workspace 的完整 URL，再通过 `/agentic/mcp/tokens` 创建 MCP token。搜索端点要求 `mcp:search`，聊天端点要求 `mcp:chat`；URL Endpoint 与 token workspace 必须一致，不要传 consumer access token。

Graph 数据源通过 ontology data-source API 直接配置。Neo4j 使用 `uri/database/username/password`，HugeGraph 使用 `url/graph/graph_space` 以及可选的 `username/password/token`；每个 ontology 保存一份独立配置，SDK 不从环境变量推断 Graph 连接。

## 运行模式

后端运行模式通过 `context.agent_mode` 传入：

```python
with client.chat.send(
    session_id=session_id,
    message="请分析这个项目",
    agent_mode="multi_agent",
) as stream:
    for event in stream:
        print(event.kind, event.payload)
```

可选值：

- `quick`：默认值，关闭 reasoning，不暴露子 Agent 编排工具
- `thinking`：启用 reasoning，不暴露子 Agent 编排工具
- `multi_agent`：启用 reasoning，并允许 `spawn_sub_agent` 编排

也可以通过 `context={"agent_mode": "multi_agent"}` 透传给底层接口。
