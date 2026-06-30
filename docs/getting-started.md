# Getting Started

本文档说明如何在本地运行 Meta Wise。读完后，你会得到一个本地可访问的后端服务和 React 前端。

## Prerequisites

- Python 3.11
- Node.js 18 或更高版本
- npm
- Docker 与 Docker Compose，可选
- 一个 OpenAI-compatible 模型服务，可在 `.env` 或系统设置页面中配置

如果在 Windows 上开发，推荐使用 WSL 运行后端、Docker 和脚本。前端可以在 Windows 或 WSL 中运行。

## Run the Backend

从仓库根目录进入后端目录：

```bash
cd platform/server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Windows PowerShell 激活虚拟环境：

```powershell
cd platform/server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python run.py
```

默认后端地址：

```text
http://localhost:5055
```

健康检查：

```bash
curl http://localhost:5055/health
```

## Configure the Model

后端不内置可用模型地址。你可以在 `platform/server/.env` 中配置：

```bash
LLM_BASE_URL=<openai-compatible-base-url>
LLM_MODEL=<model-id>
LLM_API_KEY=<api-key>
```

也可以先启动服务，再在前端系统设置页面中配置模型 profile。未配置模型时，聊天页会提示完成模型配置。

## Run the Frontend

打开新的终端：

```bash
cd platform/console
npm install
npm run dev
```

默认前端地址：

```text
http://localhost:6600
```

Vite 本地开发服务器会代理 `/auth`、`/agentic`、`/api` 和 `/health` 到 `http://127.0.0.1:5055`。通常不需要前端 `.env` 文件。

## Run with Docker Compose

从仓库根目录执行：

```bash
cp platform/server/.env.example platform/server/.env
cp platform/console/.env.example platform/console/.env
docker compose up --build
```

默认端口：

| 服务 | 地址 |
| --- | --- |
| 后端 | `http://localhost:5055` |
| 前端 | `http://localhost:8888` |

也可以使用封装脚本：

```bash
./run_docker.sh latest all
```

脚本会自动创建 `.onto-runtime/config`、`platform/server/logs` 和 `platform/console/logs/nginx` 等目录。

## Try an Ontology Example

示例场景位于 `examples/`：

- `examples/ontology-packages`

推荐流程：

1. 启动后端和前端。
2. 登录前端。
3. 在工作区中导入本体包。ZIP 包格式见 [Ontology Upload Template](ontology-upload-template.md)。
4. 在“数据连接”中创建 MindsDB、NL2SQL、Neo4j 或 HugeGraph 连接。
5. 在“本体绑定”中把当前 ontology 绑定到数据连接。
6. 在聊天页选择本体后提问。

示例业务 DOMAIN 场景的测试问题和用例在：

```text
```

## Verification

后端测试：

```bash
PYTHONPATH=platform/server:modeling/skills python -m pytest tests/backend
```

从 `platform/server` 目录运行：

```bash
PYTHONPATH=.:../../modeling/skills python -m pytest ../../tests/backend
```

前端验证：

```bash
cd platform/console
npm run lint
npm run build
```

## Troubleshooting

### Docker 拉取基础镜像超时

后端镜像基于 `python:3.11-slim`。如果访问 Docker Hub 超时，可以在 Docker Desktop 或 Docker daemon 中配置 registry mirror，或者先手动拉取并缓存基础镜像。

### Windows pip 路径过长

如果 `pip install` 报路径过长错误，以管理员身份打开 PowerShell，执行：

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

然后重新打开终端再安装依赖。

### 前端无法连接后端

确认后端健康检查可用：

```bash
curl http://localhost:5055/health
```

如果你没有使用默认端口，可以在 `platform/console/.env` 中配置：

```bash
VITE_API_BASE_URL=http://localhost:5055
```

### Agent 无法回答业务数据问题

确认三件事：

1. 当前 workspace 已创建数据连接。
2. 当前 ontology 已绑定该数据连接。
3. provider 对应的外部服务可达，例如 MindsDB、NL2SQL、Neo4j 或 HugeGraph。
