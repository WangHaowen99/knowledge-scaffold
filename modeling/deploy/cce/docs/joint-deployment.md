# Consumer 与 Modeling 联合部署和升级指导

本文说明如何把两个独立代码仓生成的 CCE 离线包放在同一套联合配置下，由联合入口按 Consumer → Modeling 的顺序部署和升级。

联合入口不读取 Consumer 源码，只读取两份离线包中的发布元数据、端点清单和安装脚本。

## 1. 联合目录

在 CCE 管理机准备一个包外目录：

```text
cce-joint-customer/
├── consumer-cce-<版本>-<架构>.tar.gz
├── modeling-cce-<版本>-<架构>.tar.gz
├── consumer-values.yaml
├── consumer-images.yaml
├── modeling-values.yaml
├── modeling-images.yaml
├── compatibility-verified.yaml
├── deployment.customer.yaml
└── customer-state/
```

三个原则：

1. 两份离线包只读，不直接编辑包内配置；
2. `values`、镜像映射、兼容性清单和状态目录放在包外；
3. `customer-state` 不提交 Git，也不放入交付包。

## 2. CCE 侧前置资源

提前创建：

- Consumer 命名空间和 Consumer PVC；
- Modeling 命名空间和三个 Modeling PVC；
- 两侧镜像拉取 Secret；
- 两侧服务令牌 Secret；
- Consumer 所需的 Auth、Backend 和其他 Secret；
- Modeling 所需的可选 Secret；
- 与两侧架构一致的可调度节点；
- ELB 和不冲突的监听端口；
- 升级场景需要 VolumeSnapshot CRD、CSI 驱动、唯一默认或唯一可用的 VolumeSnapshotClass，以及创建快照和读取 PVC/PV 的权限。

Secret 不能跨命名空间引用，因此即使令牌值相同，也必须在各自命名空间分别创建 Secret。

## 3. 编辑 `consumer-values.yaml`

Consumer values 的完整字段以 Consumer 离线包自身示例和 schema 为准。联合校验至少关注：

```yaml
global:
  clusterFlavor: standard
  targetArch: amd64
  auth:
    issuer: <AUTH_ISSUER>
    audience: <AUTH_AUDIENCE>
  imageDelivery:
    pullSecretNames:
      - <CONSUMER_IMAGE_PULL_SECRET>
```

必须确认：

- `global.clusterFlavor` 与 Modeling 相同；
- `global.targetArch` 与 Modeling 相同；
- `global.auth.issuer` 与 Modeling 完全一致；
- `global.auth.audience` 与 Modeling 完全一致；
- Consumer 生成 Auth Service 的端口是 `5056`；
- Consumer 生成 Backend Service 的端口是 `5055`；
- Consumer 的 ELB 监听端口不与 Modeling 共用 ELB 的端口重复。

Modeling 示例文件将 `global.auth.issuer` 设为 `http://consumer-auth:5056`、`global.auth.audience` 设为 `knowledge-scaffold`。这两个值必须与 Consumer 实际签发的 JWT `iss`、`aud` 一致；`issuer` 不用于跨命名空间 Service 访问。Consumer 命名空间为 `onto-consumer` 时，`externalAuth.namespace` 和 `externalConsumer.namespace` 都应填写 `onto-consumer`，并与本文件 `consumer.namespace` 相同。

Consumer release 名称决定 Service 名称。例如：

```yaml
releaseName: consumer
```

会对应 Modeling values 中的：

```yaml
externalAuth:
  serviceName: consumer-auth
externalConsumer:
  serviceName: consumer-backend
```

## 4. 编辑 `modeling-values.yaml`

复制 Modeling 包中的示例：

```bash
tar -xzf modeling-cce-<版本>-<架构>.tar.gz
cp modeling-cce-*/deploy/values.customer.example.yaml ./modeling-values.yaml
```

重点编辑：

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

还要填写 Modeling 的三个 PVC：

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
```

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
```

## 5. 编辑两份镜像清单

### 5.1 `consumer-images.yaml`

内容以 Consumer 包要求为准，必须包含 Consumer 自有镜像和 JiuwenBox：

```yaml
global:
  resolvedImages:
    <consumer-image-id>: <REGISTRY>/<REPOSITORY>@sha256:<64位digest>
    jiuwenbox: <REGISTRY>/jiuwenbox@sha256:<64位digest>
```

JiuwenBox 必须来自 Consumer 离线包。联合入口会从这里读取 JiuwenBox digest，供 Modeling 的 Core 和 sandbox-gateway 使用。

### 5.2 `modeling-images.yaml`

```yaml
global:
  resolvedImages:
    modeling: <REGISTRY>/modeling@sha256:<64位digest>
    modelingAgent: <REGISTRY>/modeling-agent@sha256:<64位digest>
    ontoModeling: <REGISTRY>/onto-modeling@sha256:<64位digest>
```

联合入口会生成临时的 Modeling 有效镜像映射，将 Consumer 的 JiuwenBox digest 注入 Modeling。也可以显式填写 `jiuwenbox`，但必须与 Consumer 完全相同。

## 6. 编辑 `deployment.customer.yaml`

参考 Modeling 包中的 `deploy/deployment.customer.example.yaml`：

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
| `modeling.package` | Modeling 离线包路径 |
| `modeling.releaseName` | Modeling Helm release 名称 |
| `modeling.namespace` | Modeling 命名空间 |

两个 release 的 `releaseName + namespace` 不能完全相同。所有路径都相对于 `deployment.customer.yaml` 所在目录解析。

## 7. 生成 `compatibility-verified.yaml`

兼容性文件由 Modeling 离线包内的联合入口生成。脚本会校验两份包的 `checksums.sha256`，从各自 `release.yaml` 读取发布版本、Chart 版本和目标平台，从 Consumer `deploy/endpoints.yaml` 读取 Auth 与 Backend API 版本，并生成唯一组合 ID。客户无需为填写兼容性字段额外解压离线包、查询这些字段或手写 YAML。

生成命令读取第 6 节的 `deployment.customer.yaml`，输出到其中 `compatibilityFile` 指向的位置。**没有确认参数时只预览，不写文件。** 请由发布方提供联调与数据兼容性验收结果，客户核对候选组合与验收记录后再确认。

### 7.1 首次联合部署

从 Modeling 包解压目录运行：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config ../deployment.customer.yaml
```

检查输出中的 `targetCombinationId`、`targetCombination` 和目标包版本。确认这两个版本已完成联合验证后执行：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config ../deployment.customer.yaml \
  --confirm-verified
```

脚本在 `compatibilityFile` 指向的路径新建文件，自动写入一个目标组合和 `transitions: []`。如果文件已经存在，命令会拒绝覆盖；保留原文件并检查 `deployment.customer.yaml` 指向是否正确。这里的确认只声明该组合已有联调结论，脚本读取版本号本身不能证明业务兼容。

### 7.2 联合升级

保留上次安装时的 `compatibility-verified.yaml`，将 `deployment.customer.yaml` 中两侧 `package` 指向新包，再预览候选升级路径：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config ../deployment.customer.yaml \
  --upgrade-candidate
```

脚本从现有兼容性清单和集群里两个已安装的 Helm release 识别旧组合；从新包识别目标组合；自动生成“新 Consumer + 旧 Modeling”的中间组合、`from`、`to`、`intermediateCombinationId` 和 `transitionId`。此时仍不会修改文件。

确认新旧组合及中间组合已联调，且新 Modeling 能不经数据迁移读取原 PVC 后执行：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config ../deployment.customer.yaml \
  --upgrade-candidate \
  --confirm-verified \
  --confirm-no-modeling-migration
```

脚本在原兼容性清单中保留旧记录并加入目标组合、中间组合与迁移记录。联合升级会先读取集群当前两侧版本，按当前组合筛选 `from`，再匹配本次离线包对应的 `to`；因此正常情况下会自动得到唯一 transition。只有同一“当前组合 → 目标组合”被记录了多条不同迁移策略时，才需要从预览结果取得 `transitionId`，并通过 `--transition-id` 显式指定。其他旧版本到同一目标版本的路径不会干扰当前升级。若旧清单丢失、集群实际版本与旧组合不符、两个新包平台不一致或端点不符合契约，脚本会拒绝生成。不要仅凭脚本成功就确认数据兼容性。

## 8. 联合首次部署

从 Modeling 包解压目录执行：

```bash
bash deploy/install-consumer-modeling.sh cce plan \
  --config ../deployment.customer.yaml

bash deploy/install-consumer-modeling.sh cce preflight \
  --config ../deployment.customer.yaml

bash deploy/install-consumer-modeling.sh cce install \
  --config ../deployment.customer.yaml

bash deploy/install-consumer-modeling.sh cce verify \
  --config ../deployment.customer.yaml

bash deploy/install-consumer-modeling.sh cce status \
  --config ../deployment.customer.yaml
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
- 新版本 Modeling values 中 `modeling.workloads.core/agent/onto.persistence.existingClaim` 与当前安装版本一致，三个 PVC 保持 `Bound`；
- 集群有唯一默认或唯一可用的 VolumeSnapshotClass，三个 Modeling `/data` 文件系统有足够剩余空间；
- Consumer 侧升级入口支持其自身的数据保留策略。

先把 `deployment.customer.yaml` 中 `consumer.package`、`modeling.package` 指向两个新包；更新该文件的 `consumer.resolvedImages`、`modeling.resolvedImages` 所指向的两份镜像清单中的 digest，并更新 `consumer.values`、`modeling.values` 所指向的配置文件。保留原 Modeling PVC 名称、共享认证 issuer 和 audience。按第 7.2 节生成旧、中间、新三个组合及升级记录。

从新 Modeling 包的解压目录执行；无需手工创建 Modeling 快照或备份 JSON：

```bash
bash deploy/install-consumer-modeling.sh cce plan \
  --config ../deployment.customer.yaml

bash deploy/install-consumer-modeling.sh cce preflight \
  --config ../deployment.customer.yaml

bash deploy/install-consumer-modeling.sh cce upgrade \
  --scope both \
  --config ../deployment.customer.yaml \
  --snapshot-retention 2 \
  --min-free-percent 15
```

`--snapshot-retention` 是保留的成功快照组数，默认 `2`；`--min-free-percent` 是每个 Modeling `/data` 文件系统的最低剩余空间百分比，默认 `15`。省略这两个参数也会使用默认值。

联合入口的实际顺序：

```text
Consumer preflight → Modeling 基础 preflight
→ Consumer upgrade → Consumer verify
→ Modeling upgrade 内部 preflight
→ 统计原数据、检查空间、停止三个写入 Pod
→ 为三个 PVC 自动创建并等待 VolumeSnapshot
→ 在 stateDir 自动写入备份记录
→ 校验快照和 PVC/PV 身份 → Modeling upgrade → Modeling verify
```

前两步用于检查目标包和集群基础条件，不会创建快照；`upgrade` 才开始自动备份。Modeling 安装器在成功后将本次快照组标记为 `successful`，并清理超出保留数量的旧成功快照组。升级结果的 `results.modeling.artifacts` 会列出自动生成的备份 JSON 路径；文件保存在 `stateDir`，权限为 `0600`。客户无需填写或创建该文件。

自动快照发生在 Consumer 升级成功之后。如果快照创建失败，联合入口会停止在 Modeling 阶段，Consumer 已升级且不会被自动回滚；应保留快照、PVC、状态目录和旧镜像，检查实际工作负载状态后再恢复。快照就绪与备份记录生成不等于恢复演练，交付前仍应验证从快照恢复数据的能力。

## 10. 联合失败恢复

联合操作会在 `stateDir` 写入 `<operation-id>.json` Journal。失败后：

1. 查看 JSON 的 `phase`、`errorCode`、`results`；
2. 检查 Consumer 和 Modeling 的实际 Helm release、Deployment、Pod 和 PVC；
3. 如果某一侧已经完成，不要卸载或清空该侧；
4. 只有 Journal、实际 release 版本和输入文件完全一致时，才使用 `--resume <operation-id>`；
5. 如果某侧升级结果不确定，先人工确认，不能直接重试；
6. 保留旧镜像、PVC、快照和备份记录，直到业务验收完成。

## 11. 联合验收

```bash
helm --kube-context <KUBE_CONTEXT> -n <CONSUMER_NAMESPACE> list --all
helm --kube-context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> list --all
kubectl --context <KUBE_CONTEXT> -n <CONSUMER_NAMESPACE> get deployment,pod,svc
kubectl --context <KUBE_CONTEXT> -n <MODELING_NAMESPACE> get deployment,pod,svc,pvc
```

此外应完成合成业务验收：共享登录、Consumer 页面、Modeling Core、Onto Modeling 页面、Consumer 与 Modeling 的关联调用，以及需要时的 MCP 外部调用。Helm 和 Pod 状态正常不能替代业务验收。
