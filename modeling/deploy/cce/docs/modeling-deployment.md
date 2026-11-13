# Modeling CCE 单独部署与升级指导

本文说明使用包含 Core、Agent、Onto、Hub、Evaluation 的 Modeling CCE 离线包完成单独部署、验证和数据保留升级。Consumer 由独立开源仓交付；Modeling 仍需其 Auth 和 Backend Service。
## 1. 文件布局

建议在客户 CCE 管理机准备独立目录：

```text
modeling-cce-customer/
├── modeling-cce-<版本>-<架构>.tar.gz
├── modeling-values.yaml
├── modeling-images.yaml
└── cce-state/
```

解压离线包后，命令从解压目录的 `deploy/` 下执行：

```bash
tar -xzf modeling-cce-<版本>-<架构>.tar.gz
cd modeling-cce-<版本>-<架构>
```

`cce-state/` 必须放在离线包外，并由当前操作用户读写。它保存渲染结果、操作记录和自动备份记录。

## 2. CCE 侧前置资源

在执行安装器前准备：

- Modeling 命名空间；
- 五个独立 PVC，分别给 Core、Agent、Onto、Hub、Evaluation；
- Modeling 镜像拉取 Secret；
- Modeling 服务令牌 Secret，或可受限读取的开源 Consumer 源 Secret；
- 可选的 Consumer 评测令牌和 Judge Key 源 Secret；Hub 内部令牌和评测加密密钥由 Modeling 安装器创建；
- Consumer Auth Service 和 Backend Service；
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

### 3.2 共享认证和服务令牌

```yaml
global:
  auth:
    issuer: <AUTH_ISSUER>
    audience: <AUTH_AUDIENCE>
  integration:
    serviceTokenSecret:
      name: <MODELING_SERVICE_TOKEN_SECRET>
      key: <TOKEN_KEY>
```

示例文件中的 `global.auth.issuer` 为 `http://consumer-auth:5056`、`global.auth.audience` 为 `knowledge-scaffold`。部署前须确认这两个值与开源 Consumer 实际签发的 JWT `iss`、`aud` 完全一致；它们是验签参数，不是 Modeling Pod 访问 Auth Service 的地址。`serviceTokenSecret` 引用 Modeling 命名空间中的 Secret；目标对象不存在时可填写受限的 `sourceRef` 从开源 Consumer 所在 Namespace 导入。

### 3.3 引用 Consumer Service

```yaml
externalAuth:
  serviceName: <CONSUMER_RELEASE>-auth
  namespace: <CONSUMER_NAMESPACE>
  servicePort: 5056

externalConsumer:
  serviceName: <CONSUMER_RELEASE>-backend
  namespace: <CONSUMER_NAMESPACE>
  servicePort: 5055
```

字段要求：

- `externalAuth.serviceName` 必须是 Consumer release 生成的 Auth Service 名称；
- `externalConsumer.serviceName` 必须是 Consumer release 生成的 Backend Service 名称；
- `namespace` 是 Consumer 所在命名空间，不是 Modeling 命名空间；
- 端口固定分别为 `5056` 和 `5055`，并且必须与 Consumer 的 `deploy/endpoints.yaml` 一致。

示例文件使用 `onto-consumer` 作为 Consumer 命名空间。Chart 会据此生成跨命名空间 Service 地址，例如 `http://consumer-auth.onto-consumer.svc.cluster.local:5056`；须确认 Service 确实存在于该命名空间。

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
      - {id: core, protocol: HTTP, listenerPort: 8100, tlsSecretName: "", host: "", path: /, pathType: Prefix, endpointId: modeling-core}
      - {id: onto, protocol: HTTP, listenerPort: 5100, tlsSecretName: "", host: "", path: /, pathType: Prefix, endpointId: onto-modeling}
      - {id: mcp, protocol: HTTP, listenerPort: 5188, tlsSecretName: "", host: "", path: /, pathType: Prefix, endpointId: modeling-mcp}
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
| `global.access.listeners[*].listenerPort` | 每个端点的 ELB 监听端口，四者不能重复 |
| `global.access.listeners[*].endpointId` | `modeling-core`、`onto-modeling`、`modeling-mcp`、`evaluation` |
| `global.access.listeners[*].tlsSecretName` | HTTP 留空；HTTPS 填 TLS Secret 名称 |
| `global.access.routes` | `port` 模式保持 `[]` |

MCP 需要外部客户端接入时，填写：

```yaml
modeling:
  config:
    mcpPublicBaseUrl: http://<ELB_IP>:5188
```

不需要外部 MCP 客户端时可以留空。`<ELB_IP>` 只能替换为客户实际 ELB 地址，不要提交到 Git。

`modeling.config.evalPublicUrl` 不在客户 values 示例中；Chart 默认留空，Onto 页面会让浏览器尝试访问建模页面同主机的 HTTP 5180 端口及 `/evaluation/` 路径。若实际评测入口不同，可在包外 `modeling-values.yaml` 的 `modeling.config` 下手动添加浏览器可达的完整地址，或同源代理路径 `/evaluation/`：

Onto 后端读取评测状态、报告和优化建议使用 Chart 内置的 `ONTO_WEB_EVAL_URL`，指向同一 release 的 Evaluation Service。这个集群内部地址与浏览器入口分开配置；不需要在客户 values 中填写。

```yaml
modeling:
  config:
    evalPublicUrl: https://eval.example.com/evaluation/
```

### 3.5 LLM、工作负载、PVC 和 Secret

```yaml
modeling:
  config:
    llmBaseUrl: <LLM_BASE_URL>
    llmModel: <LLM_MODEL>
    taskLlmBaseUrl: ""
    taskLlmModel: ""
    mcpPublicBaseUrl: ""
    ontoJiuwenBaseUrl: ""
    ontoJiuwenModel: ""
    ontoAnthropicBaseUrl: ""
  workloads:
    core:
      replicas: 1
      nodeSelector: {}
      persistence:
        existingClaim: <CORE_PVC>
    agent:
      replicas: 1
      nodeSelector: {}
      persistence:
        existingClaim: <AGENT_PVC>
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
  secrets:
    encryptionKey: {name: modeling-evaluation-internal, key: encryption-key}
    notificationToken: {name: modeling-notify, key: service-token}
    consumerToken: {name: "", key: ""}
```

必须编辑：

- `modeling.config.llmBaseUrl`、`llmModel`：实际模型服务配置；
- `workloads.core.persistence.existingClaim`：Core PVC 名称；
- `workloads.agent.persistence.existingClaim`：Agent PVC 名称；
- `workloads.onto.persistence.existingClaim`：Onto Modeling PVC 名称；
- `hub.persistence.existingClaim` 和 `evaluation.persistence.existingClaim`：两块新增 PVC；
- `workloads.*.replicas`：单实例部署保持为 `1`。

可选 Secret 引用位于：

```yaml
modeling:
  secrets:
    llmApiKey: {name: <SECRET_NAME_OR_EMPTY>, key: <KEY_OR_EMPTY>}
    evalDbUrl: {name: <SECRET_NAME_OR_EMPTY>, key: <KEY_OR_EMPTY>}
    cleanupToken: {name: <SECRET_NAME_OR_EMPTY>, key: <KEY_OR_EMPTY>}
    anthropicAuthToken: {name: <SECRET_NAME_OR_EMPTY>, key: <KEY_OR_EMPTY>}
```

未启用对应功能时，`name` 和 `key` 都留空；填写后 Secret 必须已存在于 Modeling 命名空间。

在 `evaluation.secrets.notificationToken` 中填写通知 Secret 的名称和 key；示例为 `modeling-notify` / `service-token`。Chart 将同一凭据注入 Onto Modeling 和 Evaluation 两个容器。安装器在目标 Secret 不存在时生成，已存在时检查并复用。部署账号无创建 Secret 权限时，由管理员预创建该 Secret，并允许部署账号读取。values 中只填引用，不填明文令牌；升级时保持引用不变。

`hub.secrets.internal` 配置同一个 Opaque Secret 的名称及 Modeling、Evaluation 各自使用的 key；`evaluation.secrets.encryptionKey` 配置另一个 Opaque Secret 的名称和 key。它们只填写引用，不填写密钥正文。安装器在 Helm 写入前自动创建缺失的内部 Secret，已有且完整的对象直接复用。如果安装器没有创建 Secret 的权限，平台管理员应先在 Modeling Namespace 创建与 values 中名称和 key 一致的对象；安装器仍需读取权限以校验和复用。创建失败且对象仍不存在时，安装会在 Helm 之前停止。升级已有 Hub/Evaluation 时不能更换内部 Secret 名称或 key；评测已有数据而原加密 Secret 缺失时，须恢复原密钥，不能重新生成。

外部凭据通过 `hub.secrets.consumerToken`、`evaluation.secrets.consumerToken`、`evaluation.secrets.judgeApiKey` 或 `evaluation.secrets.sqlConfig` 的 `name/key` 引用 Modeling Namespace 中已存在的 Secret；目标 Secret 缺失时可补 `sourceRef: {namespace: ..., name: ..., key: ...}`，由安装器受限导入。当前开源 Consumer 不提供评测专用接口和令牌，只有部署新的 Consumer 包后才能完成评测调用验收。

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
    modeling: <REGISTRY>/modeling@sha256:<64位digest>
    modelingAgent: <REGISTRY>/modeling-agent@sha256:<64位digest>
    ontoModeling: <REGISTRY>/onto-modeling@sha256:<64位digest>
    knowledgeHub: <REGISTRY>/knowledge-hub@sha256:<64位digest>
    evaluation: <REGISTRY>/evaluation@sha256:<64位digest>
    jiuwenbox: <REGISTRY>/jiuwenbox@sha256:<64位digest>
```

单独部署必须填写六个 key。不能填写 tag，例如 `repository:modeling-v2`。

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

`plan` 只渲染和校验，不创建 Kubernetes 资源。检查 `cce-state/` 中生成的 rendered YAML。

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

预检会检查 PVC、Secret、Consumer Service、节点架构、Chart 配置和外部服务端口。

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

首次安装必须确认 release 不存在。已存在的 release 应使用 `upgrade`，不要重复执行首次安装。

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

`verify` 会等待 Core、Agent、Onto、Hub、Evaluation、sandbox-gateway（以及启用时的 NL2SQL）就绪，并检查 Onto 到 sandbox gateway、Core 到 JiuwenBox 的内部连通性。`status` 只读取 Helm release 状态和版本。

## 6. 单独升级并保留数据

升级前必须准备：

- 新版本 Modeling 离线包；
- 新的 `modeling-images.yaml`；
- 保留原三个 PVC 名称、并填写 Hub/评测 PVC 的 `modeling-values.yaml`；五服务后续升级还须保留新增两卷名称；
- VolumeSnapshot CRD、CSI 驱动、VolumeSnapshotClass；
- 备份恢复权限和足够的 `/data` 剩余空间。

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

旧三服务 release 首次扩展时，安装器只统计并快照已有的三卷；Hub/评测新 PVC 必须为空且未被其他工作负载使用。五服务后续升级时会停止并快照五卷，随后校验五卷身份与数据证据。安装器等待 CSI `VolumeAttachment` 完成卸载，再执行 Helm 升级并恢复单副本。若 EVS 暂时返回 `detaching`、`attaching` 或 `EVS.1070`，安装器只对当前操作创建的快照执行最多 5 次退避重试；非临时快照错误会立即失败。自动生成的备份记录保存在 `state-dir`，文件名类似：

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

备份记录必须覆盖升级前实际存在的三个或五个 workload 的 PVC/PV 身份、VolumeSnapshot 身份、停止写入时间和 `dataEvidence`。升级前后不能改变 `existingClaim`。

升级失败时，安装器会尝试将升级前实际存在的三个或五个业务 Deployment 恢复为单副本。如果 Helm 已切换到新版本，会先尝试回滚原有 Helm revision，再恢复副本数；返回 JSON 的 `recovery.status` 为 `ready` 才表示原有 Deployment 已重新就绪。`degraded` 或 `failed` 时按 `recovery.issues` 排查，并立即检查实际副本数和 Pod 状态。Helm 回滚只恢复部署清单，不能替代数据卷恢复。

升级会尝试将操作阶段写入包外 `*-operation.json` 和命名空间内的 `<release>-cce-state` ConfigMap；写入失败会在结果中告警，不改变升级结果。续跑时仅对同一操作、尚未开始 Helm 变更且本地与集群阶段记录一致的状态放行；结果不明确时须先核实，不能通过删除状态绕过检查。

升级失败后：

1. 保留 PVC、VolumeSnapshot、旧镜像和 `state-dir`；
2. 查看返回 JSON 的 `phase`、`errorCode`、`operationId` 和 `recovery`；
3. 确认升级前已有的三个或五个 Deployment 均为 `1/1`，项目数据仍可读取；
4. 如自动备份只产生了部分快照，恢复服务后使用新的 operation ID 重试，以免复用失败的 VolumeSnapshot。

## 7. 验收命令

```bash
helm --kube-context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> list --all
kubectl --context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> get deployment,pod,svc,pvc
kubectl --context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> get volumesnapshot
```

还应从浏览器或合成测试客户端验证 Core、Onto Modeling 和需要时的 MCP 入口。Pod 就绪不代表业务流程已经验证。
