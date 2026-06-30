# `onto-agent-sdk`

onto-agent 的 Python SDK。

## 构建与打包

### 本地开发安装

```bash
cd platform/protocols/sdk/python-sdk
pip install -e .
```

### 生成 wheel 与 sdist

```bash
cd platform/protocols/sdk/python-sdk
./build.sh
```

产物：

- `dist/*.whl`
- `dist/*.tar.gz`

## 给其他项目使用

### 安装本地目录

```bash
pip install /path/to/onto-agent/platform/protocols/sdk/python-sdk
```

### 安装本地 wheel

```bash
pip install /path/to/onto-agent/platform/protocols/sdk/python-sdk/dist/onto_agent_sdk-0.1.0-py3-none-any.whl
```

### 从 Git 仓库子目录安装

```bash
pip install "git+ssh://git@your-git-host/your-org/onto-agent.git@main#subdirectory=platform/protocols/sdk/python-sdk"
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
