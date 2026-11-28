# CCE 离线包构建指导

本文说明如何在开发侧构建 Modeling CCE 离线包，以及如何把镜像交付到客户侧镜像仓库。本文只涉及构建和镜像准备，不执行客户 CCE 安装。

## 1. 构建机要求

构建机需要具备：

- Docker 和 BuildKit/buildx；
- Python 3；
- `PyYAML`（组包脚本读取 YAML）；
- 可选的 `pip3` 和 `npm`；没有 `npm` 时 Drawio 编辑资源会缺失，但构建仍可继续；
- 构建目标架构对应的基础镜像，或构建机能访问批准的镜像源；
- 构建目标架构对应的 Python wheels，或构建机能访问批准的 Python 包源。

客户侧安装机不需要 Docker，但需要 Python 3.11 及以上、PyYAML、jsonschema、Helm 3 和 kubectl。

## 2. 构建 Modeling 镜像

从仓库根目录执行：

```bash
bash modeling/deploy/cce/build-offline.sh <版本> <amd64|arm64>
```

例如：

```bash
bash modeling/deploy/cce/build-offline.sh v2.0 amd64
```

脚本依次执行：

1. 调用 `modeling/deploy/cce/build-images.sh`；
2. 准备 Python wheels 和 Drawio 资源；
3. 构建 Onto Modeling、Knowledge Hub、Evaluation 三张镜像；
4. 将镜像加载到本地 Docker；
5. 调用 `package-cce.py --local` 生成离线包。

默认输出：

```text
modeling/dist/modeling-cce-<版本>-<架构>.tar.gz
```

也可以指定输出文件：

```bash
bash modeling/deploy/cce/build-offline.sh v2.0 amd64 \
  /path/to/modeling-cce-v2.0-amd64.tar.gz
```

如果希望离线包成功生成后删除本次构建的三个本地镜像，可追加 `--cleanup-images`：

```bash
bash modeling/deploy/cce/build-offline.sh v2.0 amd64 \
  /path/to/modeling-cce-v2.0-amd64.tar.gz --cleanup-images
```

该选项只执行以下四个精确标签的 `docker image rm`，不会执行 `docker prune`，也不会删除其他版本或其他仓库的镜像：

```text
modeling-offline/onto-modeling:v2.0
modeling-offline/onto-modeling-connector-gateway:v2.0
modeling-offline/knowledge-hub:v2.0
modeling-offline/evaluation:v2.0
```

清理发生在组包成功之后；构建或组包失败时不会执行清理。

### 2.1 Python wheels 缓存

Onto Modeling 的宿主 wheel 预下载与镜像内 `pip install` 使用同一个包源：优先使用
`ONTO_MODELING_PIP_INDEX_URL`，其次使用 `PIP_INDEX_URL`，默认使用
`https://repo.huaweicloud.com/repository/pypi/simple`，与虚拟机构建一致。

缓存目录是：

```text
modeling/onto-modeling/vendor/wheels/
```

`build-images.sh` 使用以下标记判断是否复用：

```text
vendor/wheels/.prepared-amd64
```

标记内容由以下文件的 SHA-256 和当前包源 URL 的 SHA-256 组成：

```text
modeling/onto-modeling/backend/requirements.txt
```

满足以下条件时复用 wheels：

- 对应架构的 `.prepared-<架构>` 存在；
- 标记内容与当前 `requirements.txt` 和包源 URL 的 SHA-256 一致；
- wheels 文件仍然存在且能满足 Dockerfile 的依赖解析。

以下情况会重新执行 `pip3 download`：

- 标记不存在；
- `requirements.txt` 发生任何变化；
- 包源 URL 发生变化；
- 切换 `amd64` 和 `arm64`；
- 清理或更换 `vendor/wheels` 目录。

如果下载失败，脚本会告警并继续构建；Dockerfile 随后会尝试从 `PIP_INDEX_URL` 安装缺少的依赖。需要严格离线构建时，应在构建前准备完整的目标架构 wheels，并确认镜像构建日志没有回退到包源。

### 2.2 Drawio 资源

资源目录：

```text
modeling/onto-modeling/vendor/drawio/webapp/
```

当 `index.html` 已存在时直接复用；不存在时，脚本尝试使用 `npm pack` 准备 Drawio。没有 npm 或下载失败时只告警，离线包仍会生成，但 Drawio 编辑功能不可用。

### 2.3 直接调用 `build-images.sh`

只构建镜像、不组离线包时：

```bash
bash modeling/deploy/cce/build-images.sh <版本> <amd64|arm64>
```

该命令把四个镜像加载为本地标签（连接器网关是同一个 Dockerfile 的 `connector-gateway` 目标）：

```text
modeling-offline/onto-modeling:<版本>
modeling-offline/onto-modeling-connector-gateway:<版本>
modeling-offline/knowledge-hub:<版本>
modeling-offline/evaluation:<版本>
```

本地标签不包含架构后缀。构建另一种架构时，同名标签会被覆盖，应在每次构建后立即组包。

## 3. 使用已推送镜像组包

开发侧如果已经把四个镜像推送到镜像仓库，可以不使用 `--local`，改为按 digest 组包：

```bash
python3 modeling/deploy/cce/package-cce.py \
  --version <版本> \
  --arch <amd64|arm64> \
  --image 'ontoModeling=<仓库>/onto-modeling@sha256:<64位digest>' \
  --image 'connectorGateway=<仓库>/onto-modeling-connector-gateway@sha256:<64位digest>' \
  --image 'knowledgeHub=<仓库>/knowledge-hub@sha256:<64位digest>' \
  --image 'evaluation=<仓库>/evaluation@sha256:<64位digest>' \
  --output modeling/dist/modeling-cce-<版本>-<架构>.tar.gz
```

每个 `--image` 都必须：

- 使用 `id=repository@sha256:digest` 格式；
- 使用完整 64 位 digest；
- 与 `--arch` 的目标架构一致。

组包脚本会把镜像拉到本地、校验架构、记录源 manifest digest 和镜像归档 SHA-256。不要把 tag 当作 digest 使用。

## 4. 离线包内容

离线包根目录通常包含：

```text
modeling-cce-<版本>-<架构>/
├── release.yaml
├── checksums.sha256
├── images/
├── deploy/
│   ├── chart/
│   ├── install-cce.sh
│   ├── install-cce.py
│   ├── joint-cce.py
│   ├── deployment.customer.example.yaml
│   ├── values.customer.example.yaml
│   ├── resolved-images.example.yaml
│   └── ...
└── docs/
    ├── offline-package-build.md
    ├── modeling-deployment.md
    └── joint-deployment.md
```

`release.yaml` 是发布元数据，至少查看：

| 字段 | 用途 |
|---|---|
| `releaseVersion` | 产品发布版本 |
| `chartVersion` | Helm Chart 版本 |
| `platform` | `linux-amd64` 或 `linux-arm64` |
| `images` | 镜像归档、归档 SHA-256、配置 digest |
| `imageDependencies.jiuwenbox` | 声明 JiuwenBox 由 Consumer 提供 |

## 5. 组包检查

组包完成后检查文件：

```bash
tar -tzf modeling/dist/modeling-cce-<版本>-<架构>.tar.gz | head
```

在交付前保存离线包 SHA-256：

```bash
sha256sum modeling/dist/modeling-cce-<版本>-<架构>.tar.gz
```

客户侧解压后，安装脚本会自动校验 `checksums.sha256`。不要在离线包解压目录中直接编辑文件；客户配置应放在包外。

## 6. 客户侧镜像导入

客户侧使用批准的镜像导入工具加载 `images/*.tar`，再推送到客户镜像仓库。推送完成后，必须查询客户仓库生成的 manifest digest，因为仓库转换后 digest 可能不同。

客户侧最终填写 `resolved-images.yaml`：

```yaml
global:
  resolvedImages:
    ontoModeling: <客户仓库>/onto-modeling@sha256:<64位digest>
    connectorGateway: <客户仓库>/onto-modeling-connector-gateway@sha256:<64位digest>
    knowledgeHub: <客户仓库>/knowledge-hub@sha256:<64位digest>
    evaluation: <客户仓库>/evaluation@sha256:<64位digest>
    jiuwenbox: <客户仓库>/jiuwenbox@sha256:<64位digest>
```

当前离线包包含三个自有镜像；JiuwenBox 继续由 Consumer 镜像清单作为外部依赖提供。

离线包不得包含 SWR 凭据、kubeconfig、Secret 值或真实环境地址。
