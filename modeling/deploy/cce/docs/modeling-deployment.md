# Modeling CCE 单独部署与升级指导

本文说明使用包含 Onto、Hub、Evaluation 的 Modeling CCE 离线包单独安装、升级、验证和卸载 Modeling release。首次独立安装可先于 Auth 和 Consumer 完成；安装结果会标明待就绪的依赖。Onto 在 Auth 可用前保持等待，Hub 和 Evaluation 可先启动；完成业务验收前不要将健康探针视为鉴权和评测可用。本页中的单独安装器从 Modeling values 获取配置，不会读取 Consumer values 来自动补齐跨侧 Secret 引用；需要自动补齐时，使用[联合部署与升级指导](joint-deployment.md)。

## 1. 文件布局

建议在客户 CCE 管理机准备独立目录：

```text
modeling-cce-customer/
├── modeling-cce-<版本>-<架构>.tar.gz
├── modeling-values.yaml
├── modeling-images.yaml
└── cce-state/
```

解压离线包后，进入解压目录根目录执行下文的 `bash deploy/install-cce.sh` 命令：

```bash
tar -xzf modeling-cce-<版本>-<架构>.tar.gz
cd modeling-cce-<版本>-<架构>
```

`cce-state/` 必须放在离线包外，并由当前操作用户读写。它保存渲染结果、操作记录和自动备份记录。

## 2. CCE 侧前置资源

在执行集群预检或安装前准备：

- Modeling 命名空间；
- Auth 与 Consumer 计划使用的命名空间须已创建并可查询；对应 Service 可以稍后创建。若复用一个 namespace，只需预建一次；
- 三个独立 PVC，分别给 Onto、Hub、Evaluation；从旧版升级时原 Core、Agent PVC 继续保留；
- Modeling 镜像拉取 Secret；
- 按启用的连接准备 Consumer → Hub、Evaluation → Consumer、Consumer → Evaluation 三种不同用途的令牌；Modeling 先装时在 Modeling namespace 预建目标 Secret，Consumer 已安装时可受限导入其源 Secret。未启用的连接不需要对应 Secret。Judge Key 按需准备；Hub 内部令牌和评测加密密钥由 Modeling 安装器创建；
- 填写计划使用的 Auth Service 和 Consumer Backend Service 名称、namespace、端口。首次独立安装时两者可以尚不存在；已有部署的升级、正式预检和 `verify` 仍要求它们就绪。使用用户管理、自助改密或 Hub 成员同步时，还须在 Modeling namespace 预建与实际 Auth 管理令牌同值的 `Opaque` Secret，并在 `modeling.secrets.authAdminToken` 填写名称和 key；
- 目标架构可调度工作节点；
- ELB 和端口模式或域名模式的 Ingress 能力；
- 如果使用 HTTPS，准备 TLS Secret；
- 如果使用升级自动备份，确认 VolumeSnapshot CRD、CSI 快照驱动和 VolumeSnapshotClass 可用。

示例资源名称均为占位符，必须替换为客户实际创建的资源。

## 3. 编辑 `modeling-values.yaml`

复制包内示例文件到包外：

```bash
cp deploy/values.customer.example.yaml ../modeling-values.yaml
```

### 3.1 集群、架构和镜像拉取

```yaml
global:
  clusterFlavor: standard
  targetArch: amd64
  imageDelivery:
    pullSecretNames:
      - <MODELING_IMAGE_PULL_SECRET>
```

编辑字段：

| 字段 | 填写内容 |
|---|---|
| `global.clusterFlavor` | `standard` 或 `turbo`，与客户 CCE 集群一致 |
| `global.targetArch` | `amd64` 或 `arm64`，与工作节点一致 |
| `global.imageDelivery.pullSecretNames` | Modeling 命名空间中的镜像拉取 Secret 名称列表 |

### 3.2 Auth 身份

```yaml
global:
  auth:
    issuer: <AUTH_ISSUER>
    audience: <AUTH_AUDIENCE>
```

示例文件中的 `global.auth.issuer/audience` 须按实际 Auth 签发身份核对。Auth 尚未安装时先约定这两项，Auth 装好后必须与实际签发的 JWT `iss`、`aud` 一致。`issuer` 是验签身份，不是 Modeling Pod 访问 Auth Service 的地址。Consumer 仍持有自己的 `global.integration.serviceTokenSecret`，用于保护其 Modeling 专用接口；当前 Onto 未调用这些接口，Modeling 部署不再需要该令牌的副本。

从旧版 Modeling CCE 升级时，须从包外 `modeling-values.yaml` 删除原 `global.integration` 段；新 Chart 不再接受该字段。原 Modeling namespace 中的集成 Secret 不会被安装器删除，确认没有其他使用者后可按集群管理流程处理。

Onto Modeling 使用 Auth 管理接口时，客户 values 示例默认引用 `authAdminToken: {name: modeling-auth-client, key: admin-token}`。Chart 将它注入为 `ONTO_WEB_AUTH_ADMIN_TOKEN`；该 Secret 必须预建在 Modeling namespace，值与 Auth owner 实际管理令牌相同，安装器不会自动创建。安装器会比较两侧 Secret 的值，不一致时在升级前停止。用户管理、自助改密和 Hub 成员同步依赖此令牌；普通登录和 JWT 验签无需该令牌。无需管理功能时可将 `name`、`key` 都改为空字符串。

### 3.3 引用独立 Auth 与 Consumer Service

```yaml
externalAuth:
  apiVersion: auth-v1
  serviceName: <AUTH_SERVICE>
  namespace: <AUTH_NAMESPACE>
  servicePort: 5056

externalConsumer:
  serviceName: <CONSUMER_RELEASE>-backend
  namespace: <CONSUMER_NAMESPACE>
  servicePort: 5055
```

字段要求：

- `externalAuth` 按独立 Auth release 当前实际 Service 填写：新装通常是 `<AUTH_RELEASE>-auth`；旧 Consumer 内置 Auth 完成所有权迁移后，仍沿用原 `<CONSUMER_RELEASE>-auth` 名称和 UID。独立模式使用 `auth-v1`；若仅单独升级 Modeling、旧 Auth 尚未迁移且仍由 Consumer 管理，才使用 `v1` 和原 Service。三方联合升级的目标配置必须使用 `auth-v1`，由联合入口先迁移旧 Auth；
- `externalConsumer.serviceName` 必须是 Consumer release 生成的 Backend Service 名称；
- `namespace` 是各 Service 实际所在的命名空间；
- Auth Service 端口为 `5056`，以独立 Auth 实际 Service 为准；Consumer Backend 端口为 `5055`，按 Consumer 包的 `deploy/endpoints.yaml` 核对。

Chart 使用填写的 Service 名和 namespace 生成跨命名空间地址。首次独立 `install` 允许两项 Service 缺席，并记录 `waiting-dependencies`；Onto Pod 的等待容器先检查 Auth `/health`，再尝试 `/status`，旧 Auth 没有 `/status` 时仍可通过健康检查继续启动。Auth 上线后 Onto 自动启动。已存在的 Service 仍须通过端口及 Auth owner 身份检查；Service 存在但尚无可用端点时也记为待就绪。`preflight`、`verify`、`upgrade` 保持严格。后装 Consumer 与 Auth 可以按各自安装器支持的顺序执行；两者就绪后，先完成 Consumer 自身的 `verify`，再完成 Modeling 的 `verify`。旧 Auth 尚由 Consumer 管理时，单独升级 Modeling 继续使用 `v1` 契约。

### 3.4 对外访问

延续虚拟机的 IP 加端口方式：

```yaml
global:
  access:
    mode: port
    elb:
      id: <ELB_ID>
      class: <ELB_CLASS>
    protocol: HTTP
    listenerPort: 80
    tlsSecretName: ""
    ingressClassName: ""
    routes: []
    listeners:
      - {id: onto, protocol: HTTP, listenerPort: 5100, tlsSecretName: "", host: "", path: /, pathType: Prefix, endpointId: onto-modeling}
      - {id: evaluation, protocol: HTTP, listenerPort: 5180, tlsSecretName: "", host: "", path: /evaluation/, pathType: Prefix, endpointId: evaluation}
```

编辑字段：

| 字段 | 填写内容 |
|---|---|
| `global.access.mode` | `port` 表示通过 ELB IP 加端口访问；域名访问时使用 `host` 模式 |
| `global.access.elb.id` | 客户 ELB ID |
| `global.access.elb.class` | 客户 ELB 类别；如果控制器不要求可按客户规范填写 |
| `global.access.protocol` | `HTTP` 或 `HTTPS` |
| `global.access.ingressClassName` | CCE Standard 没有对应 IngressClass 时留空 |
| `global.access.listeners[*].listenerPort` | 每个端点的 ELB 监听端口，两者不能重复；评测默认使用 5180，也可改为未占用的端口 |
| `global.access.listeners[*].endpointId` | `onto-modeling`、`evaluation` |
| `global.access.listeners[*].tlsSecretName` | HTTP 留空；HTTPS 填 TLS Secret 名称 |
| `global.access.routes` | `port` 模式保持 `[]` |

旧 Core MCP 的 5188 入口在目标版本下线。Onto 自有 MCP 使用 Onto Service 的现有入口，客户端须按其独立契约切换。

`modeling.config.evalPublicUrl` 不在客户 values 示例中；Chart 默认留空，Onto 页面会让浏览器尝试访问建模页面同主机的 HTTP 5180 端口及 `/evaluation/` 路径。若评测对外监听端口改为其他值，须在包外 `modeling-values.yaml` 的 `modeling.config` 中填写浏览器可达的完整评测地址。评测页面需要使用与 Onto 页面不同的来源；若 Onto 页面使用 HTTPS，评测地址也必须使用 HTTPS。集群内 Evaluation Service 的端口仍为 5180：

Onto 后端读取评测状态、报告和优化建议使用 Chart 内置的 `ONTO_WEB_EVAL_URL`，指向同一 release 的 Evaluation Service。这个集群内部地址与浏览器入口分开配置；不需要在客户 values 中填写。

```yaml
modeling:
  config:
    evalPublicUrl: https://eval.example.com:15180/evaluation/
```

### 3.5 LLM、工作负载、PVC 和 Secret

```yaml
modeling:
  config:
    ontoJiuwenBaseUrl: ""
    ontoJiuwenModel: ""
    ontoAnthropicBaseUrl: ""
  workloads:
    onto:
      replicas: 1
      nodeSelector: {}
      persistence:
        existingClaim: <ONTO_PVC>
    jiuwenbox:
      nodeSelector: {}
      runtimeClassName: ""
      resources: {}
    sandboxGateway:
      resources: {requests: {cpu: 100m, memory: 256Mi}}
hub:
  persistence: {existingClaim: <HUB_PVC>}
  secrets:
    internal: {name: modeling-hub-internal, modelingTokenKey: modeling-token, evaluationTokenKey: evaluation-token}
    consumerToken: {name: "", key: ""}
evaluation:
  persistence: {existingClaim: <EVALUATION_PVC>}
  feedback: {enabled: false, notifyRuns: false}
  secrets:
    encryptionKey: {name: modeling-evaluation-internal, key: encryption-key}
    notificationToken: {name: modeling-notify, key: service-token}
    consumerToken: {name: "", key: ""}
    flywheelToken: {name: "", key: ""}
```

必须编辑：

- `workloads.onto.persistence.existingClaim`：Onto Modeling PVC 名称；
- `hub.persistence.existingClaim` 和 `evaluation.persistence.existingClaim`：两块新增 PVC；
- `workloads.*.replicas`：单实例部署保持为 `1`。

可选 Secret 引用位于：

```yaml
modeling:
  secrets:
    anthropicAuthToken: {name: <SECRET_NAME_OR_EMPTY>, key: <KEY_OR_EMPTY>}
```

未启用对应功能时，`name` 和 `key` 都留空；填写后 Secret 必须已存在于 Modeling 命名空间。

在 `evaluation.secrets.notificationToken` 中填写通知 Secret 的名称和 key；示例为 `modeling-notify` / `service-token`。Chart 将同一凭据注入 Onto Modeling 和 Evaluation 两个容器。安装器在目标 Secret 不存在时生成，已存在时检查并复用。部署账号无创建 Secret 权限时，由管理员预创建该 Secret，并允许部署账号读取。values 中只填引用，不填明文令牌；升级时保持引用不变。

`hub.secrets.internal` 配置同一个 Opaque Secret 的名称及 Modeling、Evaluation 各自使用的 key；`evaluation.secrets.encryptionKey` 配置另一个 Opaque Secret 的名称和 key。它们只填写引用，不填写密钥正文。安装器在 Helm 写入前自动创建缺失的内部 Secret，已有且完整的对象直接复用。如果安装器没有创建 Secret 的权限，平台管理员应先在 Modeling Namespace 创建与 values 中名称和 key 一致的对象；安装器仍需读取权限以校验和复用。创建失败且对象仍不存在时，安装会在 Helm 之前停止。升级已有 Hub/Evaluation 时不能更换内部 Secret 名称或 key；评测已有数据而原加密 Secret 缺失时，须恢复原密钥，不能重新生成。

当前实际使用的跨侧令牌在两侧 values 中各有一个引用，Secret 不能跨 namespace 直接引用：

| Modeling 中的目标引用 | 对应 Consumer 源引用 | 何时需要 |
| --- | --- | --- |
| `hub.secrets.consumerToken` | `hub.tokenSecret` | Consumer 启用 Hub 时 |
| `evaluation.secrets.consumerToken` | `global.integration.evaluationTokenSecret` | Consumer 配置评测调用令牌时 |
| `evaluation.secrets.flywheelToken` | `flywheel.tokenSecret` | Modeling 开启 `evaluation.feedback.enabled`、接收 Consumer 反馈时 |

这三项在客户 values 模板中是空引用，Chart 也默认为空；保持空值表示不使用该连接。仅把配置行注释掉也会采用 Chart 的空默认值，不会自动填入固定名称。**单独安装器不会读取 Consumer values 或推导这三项。**填写后，`name/key` 指向 Modeling namespace 中的目标 Secret；目标不存在时，可加完整 `sourceRef` 从 Consumer namespace 导入。导入要求部署身份能读取源 Secret、创建目标 Secret。有 `sourceRef` 且目标已存在时，安装器会核对源、目标 key 的值；无源读取权限时，预建同值目标 Secret 并不填写 `sourceRef`，但安装器无法比较两侧值。升级时沿用原引用和值。

例如 Consumer 已在 `consumer-demo` namespace 中使用 `flywheel.tokenSecret: {name: consumer-flywheel-credentials, key: service-token}`，Modeling 可在包外 values 中填写：

```yaml
evaluation:
  feedback: {enabled: true, notifyRuns: false}
  secrets:
    flywheelToken:
      name: evaluation-flywheel-credentials
      key: service-token
      sourceRef:
        namespace: consumer-demo
        name: consumer-flywheel-credentials
        key: service-token
```

这段只是需要接收反馈时的填写示例，不能覆盖其他 `evaluation` 配置；示例名称和 namespace 必须按 Consumer 实际配置替换。不要把它设为始终生效的默认值：即使反馈关闭，只要 `flywheelToken.name` 非空，安装器仍会检查、导入该 Secret，Chart 也会注入它。Consumer 未安装时不能引用尚不存在的源 Secret，应先在 Modeling namespace 预建目标令牌，并在 Consumer 首装前把同值令牌预建到其 `flywheel.tokenSecret` 指定的 Secret；Consumer 安装器会检查并复用已有值。Hub 和评测令牌也遵循相同的预建或导入规则，各用途使用不同令牌。

Consumer → Hub 还需在 Consumer values 中设置 `hub.enabled: true`、`hub.baseUrl`、`hub.instanceId` 和 `hub.tokenSecret`。Evaluation → Consumer 需配置 Consumer 的 `global.integration.evaluationTokenSecret`，并在 Modeling 中填写相同用途的目标令牌引用。若先安装 Modeling 且之后要运行评测，应在安装前预建该目标 Secret；两侧值不同会导致业务调用失败。

消费反馈由两侧分别控制：Consumer 的 `flywheel.evaluationUrl/tokenSecret` 配置连接，采集开关和工作空间范围由 Consumer 系统配置管理；Modeling 的 `evaluation.feedback.enabled: true` 允许 Evaluation 接收，`notifyRuns: true` 可额外通知 Onto Modeling 已确认的反馈轮次。建模侧两个开关默认均为 `false`，只配置 Consumer 地址不会开启 Evaluation 接收。`enabled: true` 必须有 `flywheelToken`；`notifyRuns: true` 要求同时开启 `enabled`。飞轮令牌至少 32 字符，须与 Consumer 的令牌同值，并与其他服务令牌不同。Consumer 的 `flywheel.evaluationUrl` 填 Evaluation Service 的基础地址，不追加 `/evaluation/api`，客户端会追加路径。

`evaluation.secrets.judgeApiKey` 和 `evaluation.secrets.sqlConfig` 也是可选外部引用；未使用时保持空值，需要时按相同的目标 Secret / `sourceRef` 方式填写。

`evaluation.trustedOrigins`、`evaluation.judge`、`evaluation.secrets.judgeApiKey` 和 `evaluation.secrets.sqlConfig` 不在客户 values 示例中，Chart 分别默认为空列表或空值。跨源登录、部署默认判分模型、查询类评测需要时，可在包外 values 手动添加，例如：

```yaml
evaluation:
  trustedOrigins: [https://onto.example.com]
  judge: {baseUrl: https://judge.example.com, model: example-model, configRevision: v1}
  secrets:
    judgeApiKey: {name: judge-api-key, key: api-key}
    sqlConfig: {name: evaluation-sql, key: connection}
```

这里只填写非敏感参数及 Secret 引用；省略这些字段时服务仍可启动，但没有部署默认 Judge 配置，隔离数据集的查询类评测也不可用。

## 4. 编辑 `modeling-images.yaml`

复制并修改：

```bash
cp deploy/resolved-images.example.yaml ../modeling-images.yaml
```

内容必须是完整 digest 引用：

```yaml
global:
  resolvedImages:
    ontoModeling: <REGISTRY>/onto-modeling@sha256:<64位digest>
    knowledgeHub: <REGISTRY>/knowledge-hub@sha256:<64位digest>
    evaluation: <REGISTRY>/evaluation@sha256:<64位digest>
    jiuwenbox: <REGISTRY>/jiuwenbox@sha256:<64位digest>
```

单独部署必须填写三个自有镜像和 JiuwenBox 共四个 key。不能填写 tag，例如 `repository:onto-v2`。先装 Modeling 时，JiuwenBox 镜像仍需预先推送到客户镜像仓库并填写 digest；它源自 Consumer 的交付镜像，但无需先安装 Consumer release。若尚未取得该镜像，先完成镜像交付后再执行 Modeling `install`。

## 5. 单独部署命令

### 5.1 规划

```bash
bash deploy/install-cce.sh plan \
  --context <KUBE_CONTEXT> \
  --namespace <MODELING_NAMESPACE> \
  --release <MODELING_RELEASE> \
  --values ../modeling-values.yaml \
  --resolved-images ../modeling-images.yaml \
  --state-dir ../cce-state
```

`plan` 只校验输入并渲染 Chart，不创建 Kubernetes 资源。检查 `cce-state/` 中生成的 rendered YAML；它不检查目标集群中的 Secret、PVC 或 Service，也不验证跨侧令牌值。`plan` 会在包外状态目录写入渲染文件。

### 5.2 预检

```bash
bash deploy/install-cce.sh preflight \
  --context <KUBE_CONTEXT> --namespace <MODELING_NAMESPACE> \
  --release <MODELING_RELEASE> \
  --values ../modeling-values.yaml \
  --resolved-images ../modeling-images.yaml \
  --state-dir ../cce-state \
  --operation-id modeling-install-<ID>
```

预检会检查 PVC、Secret、Consumer Backend Service、节点架构、Chart 配置、旧 Consumer 内置 Auth 或独立 Auth 的 owner、Service UID 与 Helm release 状态，
并从 Modeling namespace 中的临时 Pod 检查 Auth 健康、发现文档和 JWKS。临时 Pod 用后删除。
该探针验证服务连通、签发身份与 JWKS；实际账号登录和历史会话连续性在业务验收时验证。
从空环境先装 Modeling 时，正式 `preflight` 会因 Auth/Consumer 尚未就绪而失败；可先执行 `plan` 检查包和渲染，再执行首次独立 `install`。这项等待能力不适用于升级或旧 Auth 所有权迁移。

### 5.3 安装

```bash
bash deploy/install-cce.sh install \
  --context <KUBE_CONTEXT> --namespace <MODELING_NAMESPACE> \
  --release <MODELING_RELEASE> \
  --values ../modeling-values.yaml \
  --resolved-images ../modeling-images.yaml \
  --state-dir ../cce-state \
  --operation-id modeling-install-<ID> \
  --timeout-seconds 900
```

首次安装必须确认 release 不存在。若 Auth 或 Consumer 未就绪，安装返回成功并标记 `waiting-dependencies`，`pendingDependencies` 列出当前等待项；Hub 与 Evaluation 已启动，Onto 在 Auth 就绪前不提供页面。随后安装独立 Auth，再安装 Consumer 并完成 Consumer 自身的 `verify`，最后执行下节 Modeling `verify`；成功才转为 `complete`。先装 Modeling 时，须在 Consumer 首装前按其 values 引用预建与 Modeling 对应令牌同值的源 Secret，供 Consumer 安装器复用；需要启用 Hub、评测或飞轮而首装时尚未配置的，应在两侧配置及 Secret 就绪后升级 Modeling，再做业务验收。已存在的 release 应使用 `upgrade`，不要重复执行首次安装。

### 5.4 验证和状态

```bash
bash deploy/install-cce.sh verify \
  --context <KUBE_CONTEXT> --namespace <MODELING_NAMESPACE> \
  --release <MODELING_RELEASE> \
  --values ../modeling-values.yaml \
  --resolved-images ../modeling-images.yaml \
  --state-dir ../cce-state \
  --operation-id modeling-install-<ID> \
  --timeout-seconds 900

bash deploy/install-cce.sh status \
  --context <KUBE_CONTEXT> --namespace <MODELING_NAMESPACE> \
  --release <MODELING_RELEASE>
```

`verify` 要求 Auth Service 和 Consumer Backend Service 已存在，并检查 Auth owner、签发身份及 JWKS；随后等待 Onto、Hub、Evaluation、sandbox-gateway 并检查本地健康接口。若此前独立安装留下依赖状态记录，还会从 Evaluation Pod 检查 Consumer `/ready`，成功后清空等待项；`status` 可显示 `dependencyStage` 和 `pendingDependencies`。健康与端点检查完成后，仍需用合成测试账号验收登录；启用相应连接时，分别验证 Consumer → Hub、Evaluation → Consumer 的调用，以及 Consumer → Evaluation 的反馈提交与接收状态。只有启用 `notifyRuns` 时还需验证轮次通知；未启用的连接不要求这些业务调用。

## 6. 单独升级并保留数据

升级前必须准备：

- 新版本 Modeling 离线包；
- 新的 `modeling-images.yaml`；
- 保留 Onto PVC 名称，并填写 Hub、评测 PVC 的 `modeling-values.yaml`；原 Core、Agent PVC 仍由运维保留；
- VolumeSnapshot CRD、CSI 驱动、VolumeSnapshotClass；
- 备份恢复权限和足够的 `/data` 剩余空间。

Modeling 的 `global.auth.issuer/audience` 在升级时保持与已安装 release 一致，并继续匹配实际 Auth 签发身份。若旧 Auth 尚归 Consumer 管理，可按 3.3 节使用旧 `v1` 契约先单独升级 Modeling；后续迁移 Auth 时再把客户端切换到 `auth-v1`。联合升级的顺序和配置要求见[联合升级](joint-deployment.md#9-联合升级)。

### 6.1 自动创建快照（推荐）

```bash
bash deploy/install-cce.sh upgrade \
  --context <KUBE_CONTEXT> --namespace <MODELING_NAMESPACE> \
  --release <MODELING_RELEASE> \
  --values ../modeling-values.yaml \
  --resolved-images ../modeling-images.yaml \
  --state-dir ../cce-state \
  --operation-id modeling-upgrade-<ID> \
  --auto-backup \
  --snapshot-retention 2 \
  --min-free-percent 15 \
  --timeout-seconds 900
```

安装器从已安装的 Helm values 自动识别旧三服务、旧五服务或新三服务，无需额外退役参数。旧三服务首次升级时快照 Core、Agent、Onto 三卷；原五服务 release 升级时快照五卷；新三服务后续升级仅快照 Onto、Hub、Evaluation 三卷。旧三服务升级所需的 Hub、评测 PVC 必须预先创建且为 Bound 状态，并确保未被其他 Pod 挂载。安装器等待 CSI `VolumeAttachment` 完成卸载，再执行 Helm 升级并恢复单副本。原 Core、Agent PVC/PV 和快照不会自动删除，供必要时回退。旧 Core/MCP/Agent 入口随 Helm 升级删除，使用它们的客户端须先切换。

检测到旧 Core 时，安装器在旧 Pod 停止并完成快照后，用新版 Onto 镜像创建临时迁移 Job，分别挂载只读 Core PVC 与可写 Onto PVC。Job 在 Onto 卷上生成一致的 SQLite 副本，调用与虚拟机相同的 `migrate_from_modeling.py`，迁移 Core 项目元数据、已解析资料及部分知识包文件；按 `migrated_from` 标记跳过已迁移项目。安装器核对源项目数与迁移/跳过数，删除 Job 及 Pod 并释放卷后才启动新版 Onto。Job 失败或数量不符时停止升级并尝试回滚；已写入 Onto 卷的项目不会由 Helm 回滚清除，重试依赖迁移标记避免重复创建。旧 Agent 运行记录、Core MCP Token 与旧评测历史不在此迁移范围，仍需按业务要求另行处理，旧卷和快照保留。

新 CCE Chart 为 Onto 设置 `ONTO_WEB_MODELING_CORE_RETIRED=true`。旧项目的 `modeling_project_id` 等元数据不会自动删除；Onto 的成员修改与项目删除不再访问已退役的 Core，历史 Core 清理任务停止重试并保留记录。升级前盘点仍需使用旧 MCP Token、Agent 记录或旧评测数据的调用方，并安排这些数据的迁移或替代。

自动生成的备份记录保存在 `state-dir`，文件名类似：

```text
<state-dir>/<release>-<operation-id>-modeling-backup.json
```

### 6.2 使用已有备份记录

```bash
bash deploy/install-cce.sh upgrade \
  --context <KUBE_CONTEXT> --namespace <MODELING_NAMESPACE> \
  --release <MODELING_RELEASE> \
  --values ../modeling-values.yaml \
  --resolved-images ../modeling-images.yaml \
  --state-dir ../cce-state \
  --operation-id modeling-upgrade-<ID> \
  --backup-record <BACKUP_RECORD_JSON> \
  --timeout-seconds 900
```

备份记录必须覆盖升级前实际存在的工作负载 PVC/PV 身份、VolumeSnapshot 身份、停止写入时间和 `dataEvidence`。旧 Core 项目按上文迁移，Core 与 Agent 原卷及快照继续保留；目标版核对迁移数量及仍运行的 Onto、Hub、评测卷身份和数据。目标 values 不得继续包含旧 Core/Agent 字段及镜像映射；安装器执行 Helm 升级时使用 `--reset-values`，避免旧字段自动继承。

升级失败时，安装器会尝试将升级前实际存在的三个或五个业务 Deployment 恢复为单副本。如果 Helm 已切换到新版本，会先尝试回滚原有 Helm revision，再恢复副本数；返回 JSON 的 `recovery.status` 为 `ready` 才表示原有 Deployment 已重新就绪。`degraded` 或 `failed` 时按 `recovery.issues` 排查，并立即检查实际副本数和 Pod 状态。Helm 回滚只恢复部署清单，不能替代数据卷恢复。

升级会尝试将操作阶段写入包外 `*-operation.json` 和命名空间内的 `<release>-cce-state` ConfigMap；写入失败会在结果中告警，不改变升级结果。续跑时仅对同一操作、尚未开始 Helm 变更且本地与集群阶段记录一致的状态放行；结果不明确时须先核实，不能通过删除状态绕过检查。

升级失败后：

1. 保留 PVC、VolumeSnapshot、旧镜像和 `state-dir`；
2. 查看返回 JSON 的 `phase`、`errorCode`、`operationId` 和 `recovery`；
3. 确认升级前已有的三个或五个 Deployment 均为 `1/1`，项目数据仍可读取；
4. 如自动备份只产生了部分快照，恢复服务后使用新的 operation ID 重试，以免复用失败的 VolumeSnapshot。

## 7. 部署验收命令

安装或升级完成后执行以下检查：

```bash
helm --kube-context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> list --all
kubectl --context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> get deployment,pod,svc,pvc
kubectl --context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> get volumesnapshot
```

还应从浏览器或合成测试客户端验证 Onto、Hub、独立评测和 Onto 自有 MCP。旧 Core/MCP/Agent 入口在目标版不存在；Pod 就绪不代表业务流程已经验证。

## 8. 单独卸载 Modeling

使用**当前已安装版本**的 Modeling 解压包执行卸载；安装器会核对该包与集群中 Modeling release 的发布版本和 Chart 版本。`state-dir` 放在包外，并保留卸载操作记录：

```bash
bash deploy/install-cce.sh uninstall \
  --context <KUBE_CONTEXT> \
  --namespace <MODELING_NAMESPACE> \
  --release <MODELING_RELEASE> \
  --state-dir ../cce-state \
  --operation-id modeling-uninstall-<ID>
```

卸载只移除 Modeling Helm release；Auth 和 Consumer release 不受影响，Modeling 的 PVC、Secret、快照和包外备份记录继续保留。若卸载中断，先核对 Helm 中的实际状态，再用相同解压包、`state-dir` 和 `operation-id` 追加 `--resume modeling-uninstall-<ID>`。若要连同 Consumer、独立 Auth 一起卸载，使用[联合卸载](joint-deployment.md#10-联合卸载)，其顺序为 Modeling → Consumer → Auth，且必须在卸载配置中填写 `auth`。卸载后检查 Modeling release 和工作负载已移除，并确认需要保留的 PVC、Secret、快照仍在。
