# Consumer 与 Modeling 联合部署和升级指导

本文说明如何把两个独立代码仓生成的 CCE 离线包放在同一套联合配置下，由联合入口按 Consumer → Modeling 的顺序部署和升级。以下路径以部署机上的一个包外目录 `cce-joint-customer/` 为例；构建机与部署机不同时，先将两个 `.tar.gz` 传到部署机。

联合入口不读取 Consumer 源码，只读取两份离线包中的发布元数据、端点清单和安装脚本。

## 先看文件来源

| 最终文件 | 从哪里获取 | 如何处理 |
| --- | --- | --- |
| `consumer-cce-<版本>-<架构>.tar.gz` | Consumer CCE 构建命令的输出，或发布方交付的成品包 | 按实际所在路径复制到部署目录；不修改 |
| `modeling-cce-<版本>-<架构>.tar.gz` | Modeling CCE 构建命令的输出，或发布方交付的成品包 | 按实际所在路径复制到部署目录；不修改 |
| `consumer-values.yaml` | **Consumer 解压包** `deploy/values.example.yaml` | 复制到包外，填写 Consumer 环境资源 |
| `consumer-images.yaml` | **Consumer 解压包** `deploy/resolved-images.example.yaml` | 复制到包外，填写 Consumer 镜像在目标仓库的 digest |
| `modeling-values.yaml` | **Modeling 解压包** `deploy/values.customer.example.yaml` | 复制到包外，填写 Modeling、Hub、Evaluation 环境资源 |
| `modeling-images.yaml` | **Modeling 解压包** `deploy/resolved-images.example.yaml` | 复制到包外，填写五张自有镜像和 JiuwenBox 的 digest |
| `deployment.customer.yaml` | **Modeling 解压包** `deploy/deployment.customer.example.yaml` | 复制到包外，填写两份包、四份配置的路径及 release 身份 |
| `compatibility-verified.yaml` | Modeling 包内联合入口的 `prepare-compatibility` 操作 | 预览并确认后自动生成；不手写 |
| `customer-state/` | 部署方创建 | 保存操作记录和备份证据；失败续跑时保留 |

**源码仓与解压包的区别：**构建脚本分别位于 Consumer 仓的 `consumer/deploy/build_cce_release.sh` 和 Modeling 仓的 `modeling/deploy/cce/build-offline.sh`。部署使用两份离线包中的文件。下文复制模板的命令从解压包取文件，所有编辑都发生在包外文件。

## 1. 联合目录

下面的版本仅为合成示例，先按实际交付版本修改这四个变量。`JOINT_DIR` 必须是部署机上的绝对路径；构建机与部署机不同时，两台机器的版本和架构值保持一致。

```bash
export CONSUMER_VERSION=0.1.0  # 改为实际 Consumer 包版本
export MODELING_VERSION=0.1.0  # 改为实际 Modeling 包版本，两者可以不同
export ARCH=amd64              # ARM 节点改为 arm64
export JOINT_DIR=/path/to/cce-joint-customer  # 改为部署机上的实际绝对路径
```

需要自行构建时，分别在两个源码仓的根目录执行。将仓库路径改为本机实际位置；仅使用发布方交付的成品包时，跳过构建：

```bash
export CONSUMER_REPO_DIR=/path/to/consumer-repository  # 包含 consumer/deploy/ 的仓库根目录
cd "$CONSUMER_REPO_DIR"
bash consumer/deploy/build_cce_release.sh --version "$CONSUMER_VERSION" --arch "$ARCH" --cpu-level v1
```

Modeling、Hub、Evaluation 在 Modeling 仓库根目录执行：

```bash
export MODELING_REPO_DIR=/path/to/modeling-repository  # 包含 modeling/deploy/cce/ 的仓库根目录
cd "$MODELING_REPO_DIR"
bash modeling/deploy/cce/build-offline.sh "$MODELING_VERSION" "$ARCH"
```

未指定输出路径时，两个脚本默认分别产出 `$CONSUMER_REPO_DIR/consumer/dist/cce/consumer-cce-<CONSUMER_VERSION>-<ARCH>.tar.gz` 和 `$MODELING_REPO_DIR/modeling/dist/modeling-cce-<MODELING_VERSION>-<ARCH>.tar.gz`。Consumer 可用 `--output-dir`、Modeling 可用第三个位置参数修改输出位置；以构建命令实际打印的包路径为准。两份包的平台必须一致。

在部署机将两个包的**实际路径**填入以下变量，再复制到包外目录。如果构建机不同，先把成品包传到部署机，变量填写传入后的路径：

```bash
export CONSUMER_PACKAGE_SOURCE="/path/to/consumer-cce-${CONSUMER_VERSION}-${ARCH}.tar.gz"
export MODELING_PACKAGE_SOURCE="/path/to/modeling-cce-${MODELING_VERSION}-${ARCH}.tar.gz"
mkdir -p "$JOINT_DIR"
cp "$CONSUMER_PACKAGE_SOURCE" "$JOINT_DIR/"
cp "$MODELING_PACKAGE_SOURCE" "$JOINT_DIR/"
```

在 CCE 管理机准备一个包外目录：

```text
cce-joint-customer/
├── consumer-cce-<版本>-<架构>.tar.gz
├── consumer-cce-<版本>-<架构>/       # 解压的 Consumer 包
├── modeling-cce-<版本>-<架构>.tar.gz
├── modeling-cce-<版本>-<架构>/       # 解压的 Modeling 包
├── consumer-values.yaml
├── consumer-images.yaml
├── modeling-values.yaml
├── modeling-images.yaml
├── compatibility-verified.yaml             # 第 7 节确认后才生成
├── deployment.customer.yaml
└── customer-state/
```

首次安装时解压并分别校验包内文件：

```bash
cd "$JOINT_DIR"
tar -xzf "consumer-cce-${CONSUMER_VERSION}-${ARCH}.tar.gz"
tar -xzf "modeling-cce-${MODELING_VERSION}-${ARCH}.tar.gz"
(cd "consumer-cce-${CONSUMER_VERSION}-${ARCH}" && sha256sum -c checksums.sha256)
(cd "modeling-cce-${MODELING_VERSION}-${ARCH}" && sha256sum -c checksums.sha256)
```

校验失败时停止。后续复制模板的命令在 `JOINT_DIR` 中执行；仅执行联合入口时进入 Modeling 解压目录。升级时另行解压并校验两个新包，保留旧包和包外配置，不用旧模板覆盖已编辑文件。

三个原则：

1. 两份离线包只读，不直接编辑包内配置；
2. `values`、镜像映射、兼容性清单和状态目录放在包外；
3. `customer-state` 不提交 Git，也不放入交付包。

## 2. CCE 侧前置资源

提前创建：

- Consumer 命名空间和 Consumer PVC；
- Modeling 命名空间和五个 Modeling PVC（Core、Agent、Onto、Hub、Evaluation）；
- 两侧镜像拉取 Secret；
- Consumer 服务令牌 Secret；Modeling 服务令牌可预建，也可由安装器按 `sourceRef` 从 Consumer 源 Secret 导入；
- Consumer 所需的 Auth、Backend 和其他 Secret；
- Modeling 所需的可选 Secret；
- 与两份包的架构一致的可调度节点；
- ELB 和不冲突的监听端口；
- 升级场景需要 VolumeSnapshot CRD、CSI 驱动、唯一默认或唯一可用的 VolumeSnapshotClass，以及创建快照和读取 PVC/PV 的权限。

Secret 不能跨命名空间引用；两侧位于不同 namespace 时，同一令牌须分别存放在各自 namespace 的 Secret 中。当前 Consumer 安装器校验已存在的 Secret，不自动创建。Modeling 安装器可按包外 `sourceRef` 从受限源 Secret 导入目标 Secret，并创建 Hub 内部令牌和 Evaluation 加密密钥。

## 3. 编辑 `consumer-values.yaml`

先从 **Consumer 解压包**复制模板：

```bash
cd "$JOINT_DIR"
cp -n "consumer-cce-${CONSUMER_VERSION}-${ARCH}/deploy/values.example.yaml" consumer-values.yaml
```

然后用 `vi "$JOINT_DIR/consumer-values.yaml"` 编辑包外文件。完整字段以同一个 Consumer 包的 `docs/install.md` 和 `deploy/chart/values.schema.json` 为准。模板不是仅填写以下几项即可部署；Consumer 所需 PVC、认证、MCP、ELB 和 Secret 仍须逐项核对。联合校验尤其关注：

```yaml
global:
  clusterFlavor: standard
  auth:
    issuer: <AUTH_ISSUER>
    audience: <AUTH_AUDIENCE>
  imageDelivery:
    pullSecretNames:
      - <CONSUMER_IMAGE_PULL_SECRET>
```

必须确认：

- `global.clusterFlavor` 与 Modeling 相同；
- Consumer 包的 `release.yaml.platform` 与 Modeling 包的目标平台、`modeling-values.yaml` 的 `global.targetArch` 一致；Consumer values 模板没有 `global.targetArch` 字段；
- `global.auth.issuer` 与 Modeling 完全一致；
- `global.auth.audience` 与 Modeling 完全一致；
- Consumer 生成 Auth Service 的端口是 `5056`；
- Consumer 生成 Backend Service 的端口是 `5055`；
- Consumer 的 ELB 监听端口不与 Modeling 共用 ELB 的端口重复。

Consumer 模板中还需核对：`consumer.persistence.existingClaim`、`sharedAuth.persistence.existingClaim`、`global.integration.serviceTokenSecret`、`consumer.mcp.tokenSecret`、`sharedAuth.adminTokenSecret`、`sharedAuth.bootstrapPasswordSecret` 及启用 HTTPS 时的 TLS Secret。`global.imageDelivery.pullSecretNames` 默认示例为 `default-secret`，须确认该 Secret 在 **Consumer namespace** 中确实可拉取目标镜像。当前 `consumer.environmentSecretName` 会同时注入 Backend 和 MCP，不要将 Hub/Evaluation 专用令牌通过该字段注入。Consumer 包 `docs/install.md` 的资源表给出 Secret 类型和 key；模板名称只是示例。

Modeling 示例文件将 `global.auth.issuer` 设为 `http://consumer-auth:5056`、`global.auth.audience` 设为 `knowledge-scaffold`。这两个值必须与实际 Consumer 发布包签发的 JWT `iss`、`aud` 一致；不能因示例值存在就直接沿用。`issuer` 不用于跨命名空间 Service 访问。Consumer 的实际 namespace 须与 `deployment.customer.yaml` 的 `consumer.namespace` 以及 Modeling values 的两个外部 Service namespace 一致。

Consumer release 名称决定 Service 名称。例如：

```yaml
releaseName: consumer
```

按 Consumer 包 `deploy/endpoints.yaml` 和实际渲染结果核对后，会对应 Modeling values 中的：

```yaml
externalAuth:
  serviceName: consumer-auth
externalConsumer:
  serviceName: consumer-backend
```

## 4. 编辑 `modeling-values.yaml`

复制 Modeling 包中的示例：

```bash
cd "$JOINT_DIR"
cp -n "modeling-cce-${MODELING_VERSION}-${ARCH}/deploy/values.customer.example.yaml" modeling-values.yaml
```

用 `vi "$JOINT_DIR/modeling-values.yaml"` 编辑包外文件。以下片段说明关键字段，**不要用片段覆盖整个模板**，否则会丢失 Chart 需要的配置：

```yaml
global:
  clusterFlavor: standard
  targetArch: amd64
  auth:
    issuer: <与Consumer完全相同的AUTH_ISSUER>
    audience: <与Consumer完全相同的AUTH_AUDIENCE>
  imageDelivery:
    pullSecretNames:
      - <MODELING_IMAGE_PULL_SECRET>
  integration:
    serviceTokenSecret:
      name: <MODELING_SERVICE_TOKEN_SECRET>
      key: <TOKEN_KEY>

externalAuth:
  serviceName: <CONSUMER_RELEASE>-auth
  namespace: <CONSUMER_NAMESPACE>
  servicePort: 5056

externalConsumer:
  serviceName: <CONSUMER_RELEASE>-backend
  namespace: <CONSUMER_NAMESPACE>
  servicePort: 5055
```

`global.integration.serviceTokenSecret` 必须与 Consumer Backend 当前使用的服务令牌值一致。可在 Modeling namespace 预建目标 Secret；也可在该引用下增加 `sourceRef: {namespace: <CONSUMER_NAMESPACE>, name: <Consumer源Secret>, key: <源key>}`，让 Modeling 安装器受限读取并创建目标 Secret。值不写入 YAML。

例如 Consumer values 中的源 Secret 为 `integration-credentials`、key 为 `service-token`，Consumer namespace 为 `onto-consumer` 时，在 **Modeling values 原有的** `global.integration.serviceTokenSecret` 项下添加：

```yaml
global:
  integration:
    serviceTokenSecret:
      name: modeling-integration
      key: service-token
      sourceRef: {namespace: onto-consumer, name: integration-credentials, key: service-token}
```

以上只是该字段的填写示例，不要用它覆盖整个 values。执行安装器的身份需要读取 Consumer namespace 中这个源 Secret、在 Modeling namespace 创建目标 Secret 的权限；没有读取权限时先在 Modeling namespace 手工创建同值目标 Secret，再删去 `sourceRef`。

还要填写 Modeling 的五个 PVC，并核对新增 Secret 引用：

```yaml
modeling:
  workloads:
    core:
      replicas: 1
      persistence: {existingClaim: <CORE_PVC>}
    agent:
      replicas: 1
      persistence: {existingClaim: <AGENT_PVC>}
    onto:
      replicas: 1
      persistence: {existingClaim: <ONTO_PVC>}
hub:
  persistence: {existingClaim: <HUB_PVC>}
  secrets:
    internal: {name: modeling-hub-internal, modelingTokenKey: modeling-token, evaluationTokenKey: evaluation-token}
evaluation:
  persistence: {existingClaim: <EVALUATION_PVC>}
  secrets:
    encryptionKey: {name: modeling-evaluation-internal, key: encryption-key}
    notificationToken: {name: modeling-notify, key: service-token}
```

旧环境升级时，Core、Agent、Onto 的 PVC 名和原 PV 绑定必须沿用；首次加入 Hub、Evaluation 时另外准备两块独立、已绑定的 PVC。`hub.secrets.internal` 和 `evaluation.secrets.encryptionKey` 的引用应与模板一致，安装器首次创建对应 Secret；后续升级保留原名称、key 和值。

评测完成通知的 Secret 名称和 key 在 `evaluation.secrets.notificationToken` 中填写，示例为 `modeling-notify` / `service-token`。Chart 将同一凭据注入 Onto Modeling 的 `ONTO_EVALUATION_SERVICE_TOKEN` 和 Evaluation 的 `EVAL_MODELING_SERVICE_TOKEN`。安装器在目标 Secret 不存在时生成，已存在时检查并复用；部署账号无创建 Secret 权限时，由管理员预创建示例中填写的 Secret，并允许部署账号读取。values 中只填名称和 key，不填明文凭据。升级时保留原引用。

Onto Modeling 后端读取评测状态和报告使用 Chart 注入的 `ONTO_WEB_EVAL_URL`，指向集群内同一 Modeling release 的 Evaluation Service；这与浏览器访问的 `modeling.config.evalPublicUrl` 是两条独立路径。

`modeling.config.evalPublicUrl`、`evaluation.trustedOrigins`、`evaluation.judge`、`evaluation.secrets.judgeApiKey`、`evaluation.secrets.sqlConfig` 不在客户示例中，Chart 提供空默认值。需要其他浏览器入口、跨源登录、默认 Judge 或查询类评测时，再按 Modeling 包 `docs/modeling-deployment.md` 在包外 values 手动添加。

端口模式入口示例：

```yaml
global:
  access:
    mode: port
    elb: {id: <MODELING_ELB_ID>, class: <ELB_CLASS>}
    protocol: HTTP
    listenerPort: 80
    tlsSecretName: ""
    ingressClassName: ""
    routes: []
    listeners:
      - {id: core, protocol: HTTP, listenerPort: 8100, tlsSecretName: "", host: "", path: /, pathType: Prefix, endpointId: modeling-core}
      - {id: onto, protocol: HTTP, listenerPort: 5100, tlsSecretName: "", host: "", path: /, pathType: Prefix, endpointId: onto-modeling}
      - {id: mcp, protocol: HTTP, listenerPort: 5188, tlsSecretName: "", host: "", path: /, pathType: Prefix, endpointId: modeling-mcp}
      - {id: evaluation, protocol: HTTP, listenerPort: 8090, tlsSecretName: "", host: "", path: /evaluation/, pathType: Prefix, endpointId: evaluation}
```

## 5. 编辑两份镜像清单

### 5.1 `consumer-images.yaml`

先复制 **Consumer 解压包**的镜像模板：

```bash
cd "$JOINT_DIR"
cp -n "consumer-cce-${CONSUMER_VERSION}-${ARCH}/deploy/resolved-images.example.yaml" consumer-images.yaml
```

查看同一包 `release.yaml` 的 `images`、`archive`、`archiveRef`，按该包 `docs/import-images.md` 将 `images/` 下五张镜像上传到目标仓库，再用 `vi "$JOINT_DIR/consumer-images.yaml"` 将模板右侧值替换为**目标仓库返回的 manifest digest**。五个固定镜像 ID 是：

```yaml
global:
  resolvedImages:
    backend: <REGISTRY>/backend@sha256:<64位digest>
    frontend: <REGISTRY>/frontend@sha256:<64位digest>
    auth-server: <REGISTRY>/auth-server@sha256:<64位digest>
    jiuwenbox: <REGISTRY>/jiuwenbox@sha256:<64位digest>
    opsctl-runtime: <REGISTRY>/opsctl-runtime@sha256:<64位digest>
```

启用独立求解器时按 Consumer 包 `docs/import-images.md` 追加 `ortools-solver`。JiuwenBox 必须来自 Consumer 离线包；联合入口从此文件读取其 digest，供 Modeling 的 Core 和 sandbox-gateway 使用。

### 5.2 `modeling-images.yaml`

先复制 **Modeling 解压包**的镜像模板：

```bash
cd "$JOINT_DIR"
cp -n "modeling-cce-${MODELING_VERSION}-${ARCH}/deploy/resolved-images.example.yaml" modeling-images.yaml
```

同样按 Modeling 包 `release.yaml.images` 的 `archive` 和 `archiveRef` 上传五张自有镜像，再用 `vi "$JOINT_DIR/modeling-images.yaml"` 将目标仓库返回的 manifest digest 填入模板：

```yaml
global:
  resolvedImages:
    modeling: <REGISTRY>/modeling@sha256:<64位digest>
    modelingAgent: <REGISTRY>/modeling-agent@sha256:<64位digest>
    ontoModeling: <REGISTRY>/onto-modeling@sha256:<64位digest>
    knowledgeHub: <REGISTRY>/knowledge-hub@sha256:<64位digest>
    evaluation: <REGISTRY>/evaluation@sha256:<64位digest>
    jiuwenbox: <REGISTRY>/jiuwenbox@sha256:<64位digest>
```

Modeling 模板包含 `jiuwenbox`；将它填写为与 `consumer-images.yaml` **完全相同的 digest 引用**。联合入口会生成临时有效镜像映射并校验两侧引用一致。不要使用 tag，也不要将包内的 `sourceManifestDigest` 当作目标仓库 digest。

## 6. 编辑 `deployment.customer.yaml`

从 **Modeling 解压包**复制联合模板，并创建包外状态目录：

```bash
cd "$JOINT_DIR"
cp -n "modeling-cce-${MODELING_VERSION}-${ARCH}/deploy/deployment.customer.example.yaml" deployment.customer.yaml
mkdir -p customer-state
```

用 `vi "$JOINT_DIR/deployment.customer.yaml"` 编辑包外文件。下例中的包名、namespace、release 和 context 都要与实际值一致：

```yaml
schemaVersion: cce-joint/v1
cluster:
  context: <KUBE_CONTEXT>
stateDir: ./customer-state
compatibilityFile: ./compatibility-verified.yaml
timeoutSeconds: 900
consumer:
  package: ./consumer-cce-<版本>-<架构>.tar.gz
  releaseName: <CONSUMER_RELEASE>
  namespace: <CONSUMER_NAMESPACE>
  values: ./consumer-values.yaml
  resolvedImages: ./consumer-images.yaml
modeling:
  package: ./modeling-cce-<版本>-<架构>.tar.gz
  releaseName: <MODELING_RELEASE>
  namespace: <MODELING_NAMESPACE>
  values: ./modeling-values.yaml
  resolvedImages: ./modeling-images.yaml
```

字段要求：

| 字段 | 说明 |
|---|---|
| `cluster.context` | CCE 管理机 kubeconfig 中的 context |
| `stateDir` | 联合 Journal、临时镜像清单和操作状态目录 |
| `compatibilityFile` | 第 7 节生成的兼容性清单；首次执行生成命令时允许文件尚不存在 |
| `consumer.package` | Consumer 离线包路径 |
| `consumer.releaseName` | Consumer Helm release 名称 |
| `consumer.namespace` | Consumer 命名空间 |
| `consumer.values/resolvedImages` | 第 3、5.1 节准备的两个包外文件 |
| `modeling.package` | Modeling 离线包路径 |
| `modeling.releaseName` | Modeling Helm release 名称 |
| `modeling.namespace` | Modeling 命名空间 |
| `modeling.values/resolvedImages` | 第 4、5.2 节准备的两个包外文件 |

两个 release 的 `releaseName + namespace` 不能完全相同；它们可以使用同一 namespace，也可以使用不同 namespace。所有路径都相对于 **`deployment.customer.yaml` 所在目录**解析，不是相对于当前 shell 目录。首次安装时 `compatibility-verified.yaml` 尚不存在是正常的，第 7 节会生成它。

## 7. 生成 `compatibility-verified.yaml`

兼容性文件由 Modeling 离线包内的联合入口生成。脚本会校验两份包的 `checksums.sha256`，从各自 `release.yaml` 读取发布版本、Chart 版本和目标平台，从 Consumer `deploy/endpoints.yaml` 读取 Auth 与 Backend API 版本，并生成唯一组合 ID。客户无需为填写兼容性字段额外解压离线包、查询这些字段或手写 YAML。

生成命令读取第 6 节的 `deployment.customer.yaml`，输出到其中 `compatibilityFile` 指向的位置。**没有确认参数时只预览，不写文件。** 请由发布方提供联调与数据兼容性验收结果，客户核对候选组合与验收记录后再确认。

### 7.1 首次联合部署

从 Modeling 包解压目录运行。`JOINT_DIR`、`MODELING_VERSION` 和 `ARCH` 已在第 1 节设置；如果新开了 Shell，先按该节重新设置为部署机上的实际值。之后所有联合命令都沿用它们，避免把 `../` 误解为包外目录：

```bash
export MODELING_PACKAGE_DIR="$JOINT_DIR/modeling-cce-${MODELING_VERSION}-${ARCH}"
cd "$MODELING_PACKAGE_DIR"
```

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config "$JOINT_DIR/deployment.customer.yaml"
```

检查输出中的 `targetCombinationId`、`targetCombination` 和目标包版本。确认这两个版本已完成联合验证后执行：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config "$JOINT_DIR/deployment.customer.yaml" \
  --confirm-verified
```

脚本在 `compatibilityFile` 指向的路径新建文件，自动写入一个目标组合和 `transitions: []`。如果文件已经存在，命令会拒绝覆盖；保留原文件并检查 `deployment.customer.yaml` 指向是否正确。这里的确认只声明该组合已有联调结论，脚本读取版本号本身不能证明业务兼容。

### 7.2 联合升级

优先保留上次安装时的 `compatibility-verified.yaml`。先将 `CONSUMER_VERSION`、`MODELING_VERSION` 改为新包版本，按第 1 节把两个新包复制到 `JOINT_DIR`、解压并校验；再将 `deployment.customer.yaml` 中两侧 `package` 指向新包。从**新 Modeling 包**目录执行下面的预览与确认命令：

```bash
export MODELING_PACKAGE_DIR="$JOINT_DIR/modeling-cce-${MODELING_VERSION}-${ARCH}"
cd "$MODELING_PACKAGE_DIR"
```

预览候选升级路径：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config "$JOINT_DIR/deployment.customer.yaml" \
  --upgrade-candidate
```

清单完整时沿用原有已验证组合及其 ID。若清单丢失或缺少当前已安装组合，脚本从 Helm 读取两侧实际版本，并从 Consumer 命名空间中与 Helm 版本一致、处于 `ready` 的 `<release>-cce-state` 读取旧端点 API 版本，再核对实际 Auth、Backend Service 端口，自动生成旧组合 ID；从目标包生成目标组合，并生成“新 Consumer + 旧 Modeling”的中间组合及 `transitionId`。预览结果的 `reconstructedOldCombination` 和 `oldCombinationSource` 会指出是否重建旧组合及其来源。此时不会写文件；若集群旧契约缺失或不一致，必须恢复可信记录，不能推测 API 版本。

确认新旧组合及中间组合已联调，且新 Modeling 能不经数据迁移读取原 PVC 后执行：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config "$JOINT_DIR/deployment.customer.yaml" \
  --upgrade-candidate \
  --confirm-verified \
  --confirm-no-modeling-migration
```

清单存在时，脚本保留原有记录并补齐缺失组合、迁移记录；清单不存在时，在确认后新建文件。联合升级会先读取集群当前两侧版本，按当前组合筛选 `from`，再匹配本次离线包对应的 `to`；因此正常情况下会自动得到唯一 transition。只有同一“当前组合 → 目标组合”被记录了多条不同迁移策略时，才需要从预览结果取得 `transitionId`，并通过 `--transition-id` 显式指定。其他旧版本到同一目标版本的路径不会干扰当前升级。若集群实际版本与旧契约不符、两个新包平台不一致或端点不符合契约，脚本会拒绝生成。**自动重建只是恢复字段来源，不代表已完成旧→目标版本的联调或数据兼容验证**；运行确认命令前仍须取得发布方的验证结论。

### 7.3 同版本联合升级

当已安装组合与两份目标包的发布版本、Chart 版本、平台和接口版本完全相同时，仍按第 7.2 节的预览与确认命令操作。脚本会自动识别同版本，在预览结果中显示 `sameVersion: true`，并生成 `from`、`intermediateCombinationId` 和 `to` 指向同一个组合的路径。重复生成时沿用同一条记录。确认前仍需核对两侧联调结果，以及 Modeling 原数据无需迁移。先按第 6 节确认包、values 和镜像清单指向本次要部署的内容。

## 8. 联合首次部署

从 Modeling 包解压目录执行，沿用第 7.1 节的 `JOINT_DIR`、`MODELING_PACKAGE_DIR` 和 `cd "$MODELING_PACKAGE_DIR"`：

```bash
bash deploy/install-consumer-modeling.sh cce plan \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce preflight \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce install \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce verify \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce status \
  --config "$JOINT_DIR/deployment.customer.yaml"
```

执行顺序和作用：

1. `plan`：校验两份包、镜像映射、兼容性组合、跨侧 values，并渲染检查；
2. `preflight`：检查集群、Secret、PVC、节点、Consumer 端点和 ELB 端口；首次安装时允许 Consumer release 尚不存在；
3. `install`：先安装 Consumer，确认 Auth/Backend Service 后再安装 Modeling；
4. `verify`：验证两侧工作负载、内部健康接口和 JiuwenBox 连接；
5. `status`：读取两侧 Helm release 的状态和 Chart。

每一步都应输出 `status: succeeded`。失败时保留 `customer-state`，不要立即删除一侧 release 或 PVC。

## 9. 联合升级

联合升级要求：

- 当前集群两侧 release 与 `from` 组合一致；
- 新离线包与 `to` 组合一致；
- `intermediateCombinationId` 是新 Consumer 加旧 Modeling；
- 新版本 Modeling values 中 Core/Agent/Onto 的 PVC 名与当前安装版本一致，Hub/评测 PVC 已 `Bound`；五服务后续升级还须保留 Hub/评测 PVC 名；
- 集群有唯一默认或唯一可用的 VolumeSnapshotClass，升级前已存在的三或五个 Modeling 数据文件系统有足够剩余空间；
- Consumer 侧升级入口支持其自身的数据保留策略。

按第 7.2 节准备并校验两个新包，将 `deployment.customer.yaml` 中 `consumer.package`、`modeling.package` 指向新包；更新该文件的 `consumer.resolvedImages`、`modeling.resolvedImages` 所指向的两份镜像清单中的 digest，并更新 `consumer.values`、`modeling.values` 所指向的配置文件。保留原 Modeling PVC 名称、共享认证 issuer 和 audience。按第 7.2 节生成旧、中间、新三个组合及升级记录。不要再次复制模板覆盖现有包外 YAML。

升级时将 `MODELING_PACKAGE_DIR` 指向**新 Modeling 包**解压目录并进入该目录；`JOINT_DIR` 仍指同一个包外目录。无需手工创建 Modeling 快照或备份 JSON：

```bash
export MODELING_PACKAGE_DIR="$JOINT_DIR/modeling-cce-${MODELING_VERSION}-${ARCH}"
cd "$MODELING_PACKAGE_DIR"
```

```bash
bash deploy/install-consumer-modeling.sh cce plan \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce preflight \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce upgrade \
  --scope both \
  --config "$JOINT_DIR/deployment.customer.yaml" \
  --snapshot-retention 2 \
  --min-free-percent 15
```

`--snapshot-retention` 是保留的成功快照组数，默认 `2`；`--min-free-percent` 是每个 Modeling `/data` 文件系统的最低剩余空间百分比，默认 `15`。省略这两个参数也会使用默认值。

同版本联合升级先按第 7.3 节生成路径，再执行本节相同的 `plan`、`preflight`、`upgrade` 命令；恢复时使用原有 `--resume <operation-id>` 参数。同版本升级仍执行 Consumer 升级与验证，然后对 Modeling 自动创建快照、升级与验证；恢复时会核对已完成 release 的本次操作 ID 和目标镜像引用。必须使用包含同版本支持的新 Modeling 离线包；Consumer 的安装器来自其独立离线包，执行前还需确认该包的 `upgrade` 命令允许同版本调用。

联合入口的实际顺序：

```text
Consumer preflight → Modeling 基础 preflight
→ Consumer upgrade → Consumer verify
→ Modeling upgrade 内部 preflight
→ 统计原数据、检查空间、停止旧 release 已存在的三或五个写入 Pod
→ 为旧 release 已存在的三或五个 PVC 自动创建并等待 VolumeSnapshot
→ 在 stateDir 自动写入备份记录
→ 校验快照和 PVC/PV 身份 → Modeling upgrade → Modeling verify
```

前两步用于检查目标包和集群基础条件，不会创建快照；`upgrade` 才开始自动备份。Modeling 安装器在成功后将本次快照组标记为 `successful`，并清理超出保留数量的旧成功快照组。升级结果的 `results.modeling.artifacts` 会列出自动生成的备份 JSON 路径；文件保存在 `stateDir`，权限为 `0600`。客户无需填写或创建该文件。

自动快照发生在 Consumer 升级成功之后。如果快照创建失败，联合入口会停止在 Modeling 阶段，Consumer 已升级且不会被自动回滚；应保留快照、PVC、状态目录和旧镜像，检查实际工作负载状态后再恢复。快照就绪与备份记录生成不等于恢复演练，交付前仍应验证从快照恢复数据的能力。

## 10. 联合卸载

确认业务已停止后，可在任一有 `kubectl`、Helm、Python 3.11+、PyYAML、jsonschema 和集群访问权限的管理机运行。管理机只需新版 Modeling 联合入口脚本及其相邻的 `common.py`、`upgrade.py`，不需要原离线包。卸载配置可以只包含以下字段：

```yaml
schemaVersion: cce-joint/v1
cluster: {context: <KUBE_CONTEXT>}
stateDir: ./customer-state
consumer: {releaseName: <CONSUMER_RELEASE>, namespace: <CONSUMER_NAMESPACE>}
modeling: {releaseName: <MODELING_RELEASE>, namespace: <MODELING_NAMESPACE>}
```

从 Modeling 源码或包含新版联合入口的部署目录运行。下面以已进入 Modeling 源码仓根目录、`JOINT_DIR` 指向卸载配置所在目录为例：

```bash
bash deploy/install-consumer-modeling.sh cce uninstall \
  --config "$JOINT_DIR/deployment.uninstall.yaml"
```

在新管理机上也可以直接运行 `python3 -B modeling/deploy/cce/joint-cce.py uninstall --config <配置绝对路径>`，无需复制历史安装包目录。

联合入口先取得两侧操作锁，读取集群里的 Helm release 身份和操作状态，并核对待卸载清单只包含应用工作负载及其 Service、Ingress、ConfigMap、Job，再按 **Modeling → Consumer** 顺序卸载对应 Helm release。确认每侧 Helm release 已移除后，还会按 release 标签和名称前缀清理残留的 Deployment、Service、Ingress、Job、ReplicaSet；不匹配归属的资源会阻止操作。随后删除该侧的 `<release>-cce-state` ConfigMap，并将完成进度写入本地 Journal；操作 Lease 只释放、不删除。PVC、PV、Secret、VolumeSnapshot 和 `stateDir` 中的备份记录均保留；命名空间和外部镜像仓库也不删除。

如果中途失败，先查看返回的 `operationId`、`phase`、`results`，核对两侧实际 Helm 状态，再使用同一操作 ID 续跑：

```bash
bash deploy/install-consumer-modeling.sh cce uninstall \
  --config "$JOINT_DIR/deployment.uninstall.yaml" \
  --resume <原操作ID>
```

续跑要求原操作的 Journal 仍在 `stateDir`，集群 context、两侧 release 名称和 namespace 与原操作一致；已完成的一侧不会再次卸载。换管理机续跑时，需将原 Journal 一并带到新管理机。不要在其他操作仍运行时执行卸载。

## 11. 联合失败恢复

联合操作会在 `stateDir` 写入 `<operation-id>.json` Journal。失败后：

1. 查看 JSON 的 `phase`、`errorCode`、`failedSide`、`failedAction` 和 `results`；子安装器失败时，`results.<side>` 保留其 `phase`、`message`、`retryable` 和 `recovery` 等结构化结果，`nextAction` 提示下一步核对方向；
2. 检查 Consumer 和 Modeling 的实际 Helm release、Deployment、Pod 和 PVC；
3. 如果某一侧已经完成，不要卸载或清空该侧；
4. 只有 Journal、实际 release 版本和输入文件完全一致时，才使用 `--resume <operation-id>`；
5. 如果某侧升级结果不确定，先人工确认，不能直接重试；
6. 保留旧镜像、PVC、快照和备份记录，直到业务验收完成。


**明确可续跑且输入文件未变**时，从本次 Modeling 包解压目录执行。先把失败输出中的 `operationId` 填入变量，沿用原 ID，不要新建 ID：

```bash
export ORIGINAL_OPERATION_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  # 改为失败输出中的 operationId
bash deploy/install-consumer-modeling.sh cce upgrade \
  --scope both \
  --config "$JOINT_DIR/deployment.customer.yaml" \
  --resume "$ORIGINAL_OPERATION_ID" \
  --snapshot-retention 2 \
  --min-free-percent 15
```

首次安装失败且确认可续跑时，将上面的 `upgrade --scope both` 改为 `install`，仍使用原 `--resume`。若返回“某侧 upgrade outcome is uncertain”，需先核对该侧实际 Helm revision 与 Journal，不能仅重复命令；Consumer 的独立恢复细节以其包内 `docs/upgrade-and-recovery.md` 为准。

## 12. 联合验收

将以下三个变量改为 `deployment.customer.yaml` 中的实际值，再执行验收命令：

```bash
export KUBE_CONTEXT=customer-demo
export CONSUMER_NAMESPACE=onto-consumer
export MODELING_NAMESPACE=onto-modeling
helm --kube-context "$KUBE_CONTEXT" -n "$CONSUMER_NAMESPACE" list --all
helm --kube-context "$KUBE_CONTEXT" -n "$MODELING_NAMESPACE" list --all
kubectl --context "$KUBE_CONTEXT" -n "$CONSUMER_NAMESPACE" get deployment,pod,svc
kubectl --context "$KUBE_CONTEXT" -n "$MODELING_NAMESPACE" get deployment,pod,svc,pvc
```

此外应完成合成业务验收：共享登录、Consumer 页面、Modeling Core、Onto Modeling 页面、Consumer 与 Modeling 的关联调用，以及需要时的 MCP 外部调用。Helm 和 Pod 状态正常不能替代业务验收。

## 13. 按包查更详细的说明

| 包 | 解压包内的说明 | 主要内容 |
| --- | --- | --- |
| Consumer | `docs/install.md`、`docs/import-images.md`、`docs/upgrade-and-recovery.md`、`docs/command-interface.md` | Consumer Secret/PVC、镜像上传、独立升级与续跑 |
| Modeling | `docs/modeling-deployment.md`、`docs/offline-package-build.md`、`docs/joint-deployment.md` | Modeling/Hub/Evaluation values、镜像及联合部署 |

当前开源 Consumer 包尚未实现评测专用工作空间接口，以及 Consumer → Hub/Evaluation 的业务调用。Hub/Evaluation Deployment 和健康检查通过时，仍需单独确认这些跨仓业务能力是否已交付，不能将健康检查等同于完整评测链路验收。
