# Modeling CCE 单独部署与升级指导

本文说明只使用 Modeling 离线包，在客户 CCE 上完成单独部署、验证、数据保留升级和失败排查。本文不安装 Consumer；但 Modeling 运行时仍需要已经存在的 Consumer Auth 和 Backend Service。

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
- 三个独立 PVC，分别给 Core、Agent、Onto Modeling；
- Modeling 镜像拉取 Secret；
- Modeling 服务令牌 Secret；
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

`global.auth.issuer` 和 `global.auth.audience` 必须与 Consumer 使用的值完全一致。`serviceTokenSecret` 必须引用 Modeling 命名空间中已创建的 Secret 和 key。

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
```

编辑字段：

| 字段 | 填写内容 |
|---|---|
| `global.access.mode` | `port` 表示通过 ELB IP 加端口访问；域名访问时使用 `host` 模式 |
| `global.access.elb.id` | 客户 ELB ID |
| `global.access.elb.class` | 客户 ELB 类别；如果控制器不要求可按客户规范填写 |
| `global.access.protocol` | `HTTP` 或 `HTTPS` |
| `global.access.ingressClassName` | CCE Standard 没有对应 IngressClass 时留空 |
| `global.access.listeners[*].listenerPort` | 每个端点的 ELB 监听端口，三者不能重复 |
| `global.access.listeners[*].endpointId` | 只能是 `modeling-core`、`onto-modeling`、`modeling-mcp` |
| `global.access.listeners[*].tlsSecretName` | HTTP 留空；HTTPS 填 TLS Secret 名称 |
| `global.access.routes` | `port` 模式保持 `[]` |

MCP 需要外部客户端接入时，填写：

```yaml
modeling:
  config:
    mcpPublicBaseUrl: http://<ELB_IP>:5188
```

不需要外部 MCP 客户端时可以留空。`<ELB_IP>` 只能替换为客户实际 ELB 地址，不要提交到 Git。

### 3.5 LLM、工作负载和 PVC

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
```

必须编辑：

- `modeling.config.llmBaseUrl`、`llmModel`：实际模型服务配置；
- `workloads.core.persistence.existingClaim`：Core PVC 名称；
- `workloads.agent.persistence.existingClaim`：Agent PVC 名称；
- `workloads.onto.persistence.existingClaim`：Onto Modeling PVC 名称；
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
    jiuwenbox: <REGISTRY>/jiuwenbox@sha256:<64位digest>
```

单独部署必须填写四个 key。不能填写 tag，例如 `repository:modeling-v2`。

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

`verify` 会等待 Core、Agent、Onto Modeling、sandbox-gateway（以及启用时的 NL2SQL）就绪，并检查 Onto 到 sandbox gateway、Core 到 JiuwenBox 的内部连通性。`status` 只读取 Helm release 状态和版本。

## 6. 单独升级并保留数据

升级前必须准备：

- 新版本 Modeling 离线包；
- 新的 `modeling-images.yaml`；
- 保留原三个 PVC 名称的 `modeling-values.yaml`；
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

安装器会统计升级前数据、停止三个写入 Deployment，等待 CSI `VolumeAttachment` 完成卸载后创建并等待三个 PVC 的 VolumeSnapshot，再执行 Helm 升级、恢复单副本并检查数据证据。若 EVS 暂时返回 `detaching`、`attaching` 或 `EVS.1070`，安装器只对当前操作创建的快照执行最多 5 次退避重试；非临时快照错误会立即失败。自动生成的备份记录保存在 `state-dir`，文件名类似：

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

备份记录必须包含三个 workload 的 PVC/PV 身份、VolumeSnapshot 身份、停止写入时间和 `dataEvidence`。升级前后不能改变 `existingClaim`。

升级失败时，安装器会尝试将三个业务 Deployment 恢复为单副本。如果 Helm 已切换到新版本，会先尝试回滚原有 Helm revision，再恢复副本数；返回 JSON 的 `recovery.status` 为 `ready` 才表示三个 Deployment 已重新就绪。`degraded` 或 `failed` 时按 `recovery.issues` 排查，并立即检查实际副本数和 Pod 状态。Helm 回滚只恢复部署清单，不能替代数据卷恢复。

升级失败后：

1. 保留 PVC、VolumeSnapshot、旧镜像和 `state-dir`；
2. 查看返回 JSON 的 `phase`、`errorCode`、`operationId` 和 `recovery`；
3. 确认三个 Deployment 均为 `1/1`，项目数据仍可读取；
4. 如自动备份只产生了部分快照，恢复服务后使用新的 operation ID 重试，以免复用失败的 VolumeSnapshot。

## 7. 验收命令

```bash
helm --kube-context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> list --all
kubectl --context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> get deployment,pod,svc,pvc
kubectl --context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> get volumesnapshot
```

还应从浏览器或合成测试客户端验证 Core、Onto Modeling 和需要时的 MCP 入口。Pod 就绪不代表业务流程已经验证。
