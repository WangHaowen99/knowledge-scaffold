# SDK 发布与外部项目接入说明

本文说明如何将 `sdk/typescript-sdk` 与 `sdk/python-sdk` 构建成可供其他项目使用的交付物，并给出推荐的接入方式。

## 包概览

| 语言 | 目录 | 包名 | 推荐给外部项目的形式 |
| --- | --- | --- | --- |
| TypeScript | `sdk/typescript-sdk` | `@onto-agent/sdk` | npm registry、私有 registry、`.tgz` 包 |
| Python | `sdk/python-sdk` | `onto-agent-sdk` | PyPI / 私有 PyPI、wheel、源码路径 |

## 统一入口

在仓库根目录下可以直接使用：

```bash
cd sdk
./build.sh all
./package.sh all
```

支持的目标：

- `all`
- `typescript`
- `python`

例如：

```bash
cd sdk
./build.sh typescript
./package.sh python
```

## TypeScript SDK

### 构建

```bash
cd sdk/typescript-sdk
./build.sh
```

输出：

- `dist/`：编译后的 JS 与类型声明

### 打包

```bash
cd sdk/typescript-sdk
./package.sh
```

输出：

- `onto-agent-sdk-<version>.tgz`

### 外部项目接入方式

#### 方式 1：安装本地打包产物

```bash
npm install /path/to/onto-agent/sdk/typescript-sdk/onto-agent-sdk-0.1.0.tgz
```

#### 方式 2：安装本地目录

```bash
npm install /path/to/onto-agent/sdk/typescript-sdk
```

> 该包已配置 `prepack` / `prepare`，会在需要时自动构建 `dist/`。

#### 方式 3：发布到 npm 或私有 registry

推荐长期使用这种方式：

```bash
npm publish
```

如果使用私有源，请按团队 registry 配置执行发布。

## Python SDK

### 构建

```bash
cd sdk/python-sdk
./build.sh
```

输出：

- `dist/*.whl`
- `dist/*.tar.gz`

脚本会在 `sdk/python-sdk/.venv-build/` 中自动准备构建环境，不污染业务项目的虚拟环境。

### 打包

```bash
cd sdk/python-sdk
./package.sh
```

`package.sh` 当前等价于执行 `build.sh`，产出 wheel 与 sdist。

### 外部项目接入方式

#### 方式 1：安装本地 wheel

```bash
pip install /path/to/onto-agent/sdk/python-sdk/dist/onto_agent_sdk-0.1.0-py3-none-any.whl
```

#### 方式 2：安装本地源码目录

```bash
pip install /path/to/onto-agent/sdk/python-sdk
```

#### 方式 3：从 Git 仓库子目录安装

```bash
pip install "git+ssh://git@your-git-host/your-org/onto-agent.git@main#subdirectory=sdk/python-sdk"
```

#### 方式 4：发布到 PyPI 或私有 PyPI

推荐长期使用这种方式：

```bash
python3 -m twine upload dist/*
```

## 推荐的对外交付策略

- **团队内部临时联调**：优先使用本地路径或本地打包产物
- **多个项目长期复用**：优先发布到私有 registry / 私有 PyPI
- **对外正式发布**：采用版本号、changelog、构建产物校验后再发布

## 发布前检查清单

### TypeScript SDK

```bash
cd sdk/typescript-sdk
npm run typecheck
./build.sh
./package.sh
```

### Python SDK

```bash
cd sdk/python-sdk
python3 -m compileall src
./build.sh
```
