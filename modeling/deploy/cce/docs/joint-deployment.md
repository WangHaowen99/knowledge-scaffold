# Consumer 与 Modeling 联合部署和升级指导

本文说明如何把独立 Auth、Consumer 和 Modeling 的 CCE 离线包放在同一套联合配置下。配置 `auth` 时，联合入口负责三方首装，以及 Auth 所有权迁移和三方联合升级；不配置 `auth` 时保留原 Consumer → Modeling 流程。以下路径以部署机上的一个包外目录 `cce-joint-customer/` 为例；构建机与部署机不同时，先将交付包传到部署机。

联合入口不读取 Consumer 源码；配置 `auth` 时从独立 Auth 包执行包内 `auth-cce.py`，并从集群核对 Auth owner、Service UID 和目标镜像。

## 三方联合安装与升级

包外 `deployment.customer.yaml` 的模板已启用 `auth`，同时保留 `consumer`、`modeling` 两段：

```yaml
auth:
  package: ./onto-auth-example-amd64.tar.gz
  releaseName: auth
  namespace: onto-consumer
  values: ./auth-values.yaml
  image: registry.example.com/auth@sha256:0000000000000000000000000000000000000000000000000000000000000000
```

`auth-values.yaml` 从 Auth 包的 `cce/auth-values.example.yaml` 复制到包外。新环境先确定独立 Auth 的 issuer/audience、Auth 专用空 PVC、管理员凭据与初始密码 Secret、拉取 Secret，再让 Consumer 和 Modeling 分别按 Auth 的实际身份与 Service 配置客户端引用。旧环境迁移时以原 Auth 实际签发身份和迁移后独立 Auth 保留的 Service 为准；Service 名称沿用原 `<旧 Consumer releaseName>-auth`。新装 Service 名称为 `<Auth releaseName>-auth`。当前联合部署要求 Auth 与 Consumer 位于同一 namespace，Auth Service 端口为 `5056`，客户端协议为 `auth-v1`。镜像先上传到目标仓库，再把实际 manifest digest 填入 `auth.image`。

三方首次安装运行原联合 `install` 命令即可。入口先校验三份包及配置，调用 Auth 包内首装工具，确认独立 release 与 Service 就绪后，安装并验证 Consumer，最后安装 Modeling。三方升级运行原联合 `upgrade --scope both` 命令：若旧 Auth 仍归 Consumer 管理，联合入口先调用 Auth 包内 `migrate` 转移所有权并核对原 Service UID；随后预检两侧业务，升级 Auth 镜像，再升级 Consumer、Modeling。已有独立 Auth 时跳过所有权迁移；Auth 目标镜像与当前已记录镜像相同时跳过镜像升级。

Auth 使用自己的 Helm release 和升级状态；联合操作日志记录迁移与每一侧的完成阶段。Auth 迁移中断时，检查集群记录后使用原联合操作 ID 追加 `--resume`，联合入口将布尔 `--resume` 传给 Auth 迁移工具继续迁移。Auth 镜像升级中断时，先检查 Auth owner、快照与探针状态，并按 Auth 工具的恢复语义处理；联合入口不会盲目重试或替 Auth 恢复账号快照。迁移保留原 Service 名称和 UID，联合入口会核对 Consumer 留存的所有权记录。

`plan` 只调用两侧业务包的规划入口；配置 Auth 时还校验 Auth 包、架构与三侧引用。`preflight` 要求当前独立 Auth 已就绪，但允许镜像尚未升到目标 digest；旧 Consumer 内置 Auth 环境应直接运行联合 `upgrade`，由其先迁移所有权。`verify` 要求当前镜像等于目标 digest。从空环境部署三方时直接运行联合 `install`。联合 `uninstall` 的卸载配置包含 `auth` 时，按 Modeling → Consumer → Auth 顺序卸载；省略 `auth` 时只卸载两侧业务 release。

## 先看文件来源

| 最终文件 | 从哪里获取 | 如何处理 |
| --- | --- | --- |
| `consumer-cce-<版本>-<架构>.tar.gz` | Consumer CCE 构建命令的输出，或发布方交付的成品包 | 按实际所在路径复制到部署目录；不修改 |
| `onto-auth-<版本>-<架构>.tar.gz` | 独立 Auth 构建命令的输出，或发布方交付的成品包 | 三方编排时必需；填入联合配置的 `auth.package` |
| `modeling-cce-<版本>-<架构>.tar.gz` | Modeling CCE 构建命令的输出，或发布方交付的成品包 | 按实际所在路径复制到部署目录；不修改 |
| `auth-values.yaml` | **Auth 解压包** `cce/auth-values.example.yaml` | 三方编排时复制到包外，填写 Auth 专用 PVC、Secret 与身份 |
| `consumer-values.yaml` | **Consumer 解压包** `deploy/values.example.yaml` | 复制到包外，填写 Consumer 环境资源 |
| `consumer-images.yaml` | **Consumer 解压包** `deploy/resolved-images.example.yaml` | 复制到包外，填写 Consumer 镜像在目标仓库的 digest |
| `modeling-values.yaml` | **Modeling 解压包** `deploy/values.customer.example.yaml` | 复制到包外，填写 Modeling、Hub、Evaluation 环境资源 |
| `modeling-images.yaml` | **Modeling 解压包** `deploy/resolved-images.example.yaml` | 复制到包外，填写三张自有镜像和 JiuwenBox 的 digest |
| `deployment.customer.yaml` | **Modeling 解压包** `deploy/deployment.customer.example.yaml` | 复制到包外，填写 release 包、配置路径和身份；模板默认包含三方 `auth` 配置，两方部署时删除整个 `auth` 段 |
| `compatibility-verified.yaml` | Modeling 包内联合入口的 `prepare-compatibility` 操作 | 预览并确认后自动生成；不手写 |
| `customer-state/` | 部署方创建 | 保存操作记录和备份证据；失败续跑时保留 |

**源码仓与解压包的区别：**三个构建脚本分别位于 Consumer 仓的 `consumer/deploy/build_auth_release.sh`、`consumer/deploy/build_cce_release.sh` 和 Modeling 仓的 `modeling/deploy/cce/build-offline.sh`。三方部署使用三份离线包中的文件；下文复制模板的命令从解压包取文件，所有编辑都发生在包外文件。

## 1. 联合目录

下面的版本仅为合成示例，先按实际交付版本修改这些变量。`JOINT_DIR` 必须是部署机上的绝对路径；构建机与部署机不同时，两台机器的版本和架构值保持一致。

```bash
export CONSUMER_VERSION=0.1.0  # 改为实际 Consumer 包版本
export AUTH_VERSION=0.1.0      # 改为实际 Auth 包版本，可与 Consumer 不同
export MODELING_VERSION=0.1.0  # 改为实际 Modeling 包版本，两者可以不同
export ARCH=amd64              # ARM 节点改为 arm64
export JOINT_DIR=/path/to/cce-joint-customer  # 改为部署机上的实际绝对路径
```

需要自行构建时，分别在两个源码仓的根目录执行。将仓库路径改为本机实际位置；仅使用发布方交付的成品包时，跳过构建：

```bash
export CONSUMER_REPO_DIR=/path/to/consumer-repository  # 包含 consumer/deploy/ 的仓库根目录
cd "$CONSUMER_REPO_DIR"
bash consumer/deploy/build_cce_release.sh --version "$CONSUMER_VERSION" --arch "$ARCH" --cpu-level v1
bash consumer/deploy/build_auth_release.sh --version "$AUTH_VERSION" --arch "$ARCH"
```

Modeling、Hub、Evaluation 在 Modeling 仓库根目录执行：

```bash
export MODELING_REPO_DIR=/path/to/modeling-repository  # 包含 modeling/deploy/cce/ 的仓库根目录
cd "$MODELING_REPO_DIR"
bash modeling/deploy/cce/build-offline.sh "$MODELING_VERSION" "$ARCH"
```

未指定输出路径时，脚本分别产出 `$CONSUMER_REPO_DIR/consumer/dist/cce/consumer-cce-<CONSUMER_VERSION>-<ARCH>.tar.gz`、`$CONSUMER_REPO_DIR/consumer/dist/onto-auth-<AUTH_VERSION>-<ARCH>.tar.gz` 和 `$MODELING_REPO_DIR/modeling/dist/modeling-cce-<MODELING_VERSION>-<ARCH>.tar.gz`。Consumer 和 Auth 可用 `--output-dir`、Modeling 可用第三个位置参数修改输出位置；以构建命令实际打印的包路径为准。三份包的架构必须一致。

在部署机将包的**实际路径**填入以下变量，再复制到包外目录。如果构建机不同，先把成品包传到部署机，变量填写传入后的路径；两方部署可跳过 Auth 包：

```bash
export CONSUMER_PACKAGE_SOURCE="/path/to/consumer-cce-${CONSUMER_VERSION}-${ARCH}.tar.gz"
export AUTH_PACKAGE_SOURCE="/path/to/onto-auth-${AUTH_VERSION}-${ARCH}.tar.gz"
export MODELING_PACKAGE_SOURCE="/path/to/modeling-cce-${MODELING_VERSION}-${ARCH}.tar.gz"
mkdir -p "$JOINT_DIR"
cp "$CONSUMER_PACKAGE_SOURCE" "$JOINT_DIR/"
cp "$AUTH_PACKAGE_SOURCE" "$JOINT_DIR/"
cp "$MODELING_PACKAGE_SOURCE" "$JOINT_DIR/"
```

在 CCE 管理机准备一个包外目录：

```text
cce-joint-customer/
├── onto-auth-<版本>-<架构>.tar.gz      # 三方编排时提供
├── onto-auth-<版本>-<架构>/             # 解压的 Auth 包；复制 values 模板和上传镜像时使用
├── consumer-cce-<版本>-<架构>.tar.gz
├── consumer-cce-<版本>-<架构>/       # 解压的 Consumer 包
├── modeling-cce-<版本>-<架构>.tar.gz
├── modeling-cce-<版本>-<架构>/       # 解压的 Modeling 包
├── auth-values.yaml                   # Auth 的账号卷、身份和 Secret 引用
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
tar -xzf "onto-auth-${AUTH_VERSION}-${ARCH}.tar.gz"
tar -xzf "modeling-cce-${MODELING_VERSION}-${ARCH}.tar.gz"
(cd "consumer-cce-${CONSUMER_VERSION}-${ARCH}" && sha256sum -c checksums.sha256)
(cd "modeling-cce-${MODELING_VERSION}-${ARCH}" && sha256sum -c checksums.sha256)
cp -n "onto-auth-${AUTH_VERSION}-${ARCH}/cce/auth-values.example.yaml" auth-values.yaml
```

校验失败时停止。Auth 包的文件清单和 SHA256 由联合入口在执行时验证。后续复制模板的命令在 `JOINT_DIR` 中执行；仅执行联合入口时进入 Modeling 解压目录。升级时另行解压并校验新包，保留旧包和包外配置，不用旧模板覆盖已编辑文件。

三个原则：

1. 离线包只读，不直接编辑包内配置；
2. `values`、镜像映射、兼容性清单和状态目录放在包外；
3. `customer-state` 不提交 Git，也不放入交付包。

## 2. CCE 侧前置资源

Auth 和 Consumer 是**同一 Consumer namespace 中的两个 Helm release**；Modeling 可以使用另一 namespace。先准备以下集群资源，再执行第 7～9 节的命令：

| 资源 | 首次三方安装 | 已有独立 Auth 升级 | 旧内置 Auth 迁移升级 |
| --- | --- | --- | --- |
| 命名空间 | 提前创建 Consumer、Modeling namespace；Auth 使用 Consumer namespace | 沿用原 namespace | 沿用原 Consumer namespace；迁移不跨 namespace |
| Auth 账号 PVC | **独立、已绑定且为空**的 `ReadWriteOnce` / `Filesystem` PVC，名称填 `auth-values.yaml` 的 `sharedAuth.persistence.existingClaim` | 沿用原 PVC，不能换卷或清空 | 沿用旧 Consumer Auth 的原 PVC；不得创建空卷替换或使用 Auth 首装入口 |
| Consumer 数据 PVC | 已绑定的 runtime PVC；启用求解器时另备求解器 PVC | 沿用原卷 | 沿用原卷；迁移只转移 Auth 所有权 |
| Modeling 数据 PVC | Onto、Hub、Evaluation 各自已绑定；旧版含 Core、Agent 时保留其原卷 | 沿用原卷 | 沿用原卷；旧 Core、Agent 的退役和迁移由 Modeling 升级处理 |
| Auth 凭据 Secret | 在 Consumer namespace 准备含管理令牌、初始管理员密码对应 key 的 `Opaque` Secret；名称和 key 分别填 `sharedAuth.adminTokenSecret`、`sharedAuth.bootstrapPasswordSecret` | 保留原凭据和值 | 保留旧 Auth 实际使用的凭据和值；迁移不重置账号或签名密钥 |
| Consumer 客户端凭据 | 在 Consumer namespace 准备管理接口令牌 `Opaque` Secret，填 `consumer.authAdminTokenSecret`；其值须与 Auth 管理令牌一致 | 保留引用与值 | 从旧配置核对并沿用；不得将 bootstrap 密码放入 Consumer 客户端 Secret |
| Modeling 客户端凭据 | 使用建模用户管理、自助改密或 Hub 成员同步时，在 Modeling namespace 准备与独立 Auth 管理令牌同值的 `Opaque` Secret，填 `modeling.secrets.authAdminToken` | 保留与 Auth 管理令牌一致的值 | 沿用 Auth 实际管理令牌；如原 CCE 未注入，升级时补齐 Secret 引用并验证功能 |
| 镜像拉取 Secret | Consumer namespace 中准备能拉取 Auth、Consumer 镜像的 `kubernetes.io/dockerconfigjson` Secret；Modeling namespace 另备可拉取 Modeling、JiuwenBox 镜像的同类 Secret | 保留并核对新 digest 可拉取 | 同左，另保留旧 Auth digest 供恢复 |
| 业务 Secret | Consumer 安装器按 values 中启用的用途初始化建模调用、MCP、Hub、评测和飞轮令牌；联合入口按条件补齐 Modeling 跨侧引用并导入源 Secret。Auth、TLS 和其他外部凭据仍需按各自要求准备 | 保留原引用与值；新增连接时核对两侧用途和 Secret 来源 | 保留原引用与值；新增连接时核对两侧用途和 Secret 来源 |
| 节点和入口 | 与三份包架构一致的可调度节点；ELB 和互不冲突的监听端口 | 核对目标镜像架构和端口 | 核对旧 Auth 原 Service 名称、端口和 UID；迁移沿用它们 |
| 快照能力 | 首装不创建升级快照；建议提前配置 Auth 的 `authSnapshotClass` 和 Consumer 的 `runtimeSnapshotClass` | Auth、Consumer、Modeling 升级前须有 VolumeSnapshot CRD、CSI 驱动、匹配源卷的 VolumeSnapshotClass 与权限 | **执行迁移前**原 Consumer 已安装 values 须有可用的 `authSnapshotClass`；业务升级还需 Consumer runtime、Modeling 各卷的快照能力 |

Auth 首装会用只读检查 Job 核对账号 PVC 为空，然后创建单副本 Auth；它不会创建 PVC、namespace 或 Secret。旧环境迁移会对原 Auth 卷做快照，转移原 Deployment、Service 的 Helm 所有权，保留原 PVC 及其 UID，不初始化第二份账号数据。迁移工具会从旧 Consumer 已安装 values 提取 Auth 身份、拉取配置、账号卷、凭据引用和 Auth 快照配置，写入独立 Auth release；它不会把这份包外 `auth-values.yaml` 重新应用到旧账号卷。Auth 的单副本升级和迁移期间，登录、刷新令牌等请求会短暂受影响，应安排维护窗口。

部署身份须能访问目标集群并具备 Helm、Deployment/Service/PVC/Secret、ConfigMap、Lease、Job、Pod 查询与操作权限；迁移和升级还需创建及读取 VolumeSnapshot，并能检查 PVC/PV 身份。部署机需有 Python 3.11+、PyYAML、jsonschema、Helm、kubectl 和包校验工具。权限和快照类按 **Consumer 包** `docs/auth-release.md`、`docs/upgrade-and-recovery.md` 核对；独立 Auth 包提供 `cce/auth-cce.py`、`cce/chart/` 和 `cce/auth-values.example.yaml`。

Secret 不能跨命名空间引用。Modeling 的 Auth 管理令牌以独立 Auth 实际使用的令牌为准，在 Modeling namespace 单独创建 Secret；Consumer 若调用 Auth 管理接口，也在其 namespace 中配置自己的客户端 Secret，两个客户端不需要使用同名 Secret。Auth 的管理令牌和初始化密码、Consumer 与 Modeling 的 Auth 客户端令牌均须预先准备；Consumer 安装器不会生成 Auth 凭据。Consumer 自有业务令牌按其 values 中的引用由 `preflight`、`install`、`upgrade` 首次生成并持续复用；未配置可选令牌引用时，不会为了关闭的连接生成 Secret。`preflight` 失败后也可能已写入 Secret 和 `<Consumer release>-credentials` ConfigMap，重试时必须保留。联合入口会在需要时从 Consumer 源 Secret 推导并导入 Modeling 的 Hub、评测和飞轮令牌；Modeling 安装器还会创建 Hub 内部令牌和 Evaluation 加密密钥。

### 2.1 编辑 `auth-values.yaml`

第 1 节已从 Auth 解压包复制 `cce/auth-values.example.yaml` 到包外。首次安装时编辑这个文件，至少核对以下字段；保留模板中的其他配置：

```yaml
global:
  auth: {issuer: <AUTH_ISSUER>, audience: <AUTH_AUDIENCE>}
  imageDelivery: {pullSecretNames: [<AUTH_IMAGE_PULL_SECRET>]}
sharedAuth:
  enabled: true
  persistence: {existingClaim: <EMPTY_AUTH_PVC>}
  adminTokenSecret: {name: <AUTH_SECRET>, key: admin-token}
  bootstrapPasswordSecret: {name: <AUTH_SECRET>, key: bootstrap-password}
upgradeBackup:
  authSnapshotClass: <AUTH_SNAPSHOT_CLASS>
```

独立 Auth 的 `issuer/audience` 是两侧客户端配置的基准：新装时先在这里确定签发身份，再填写 Consumer 和 Modeling 的 `global.auth`。`authSnapshotClass` 要匹配 Auth PVC 的 CSI 驱动；计划后续升级时在首装配置中一并填写。此文件**只保存 Secret 引用，不写明文凭据或镜像 digest**；目标 digest 填在 `deployment.customer.yaml` 的 `auth.image`。新装需要空 Auth PVC。旧内置 Auth 升级使用原账号卷和迁移后独立 Auth 的已安装配置，不用本示例重新初始化账号数据；包外文件中的身份字段也须与原 Auth 实际签发身份一致，不能借迁移更改 issuer/audience。联合入口会校验 Auth、Consumer、Modeling 三方身份一致。

三类数据卷的快照类配置方式不同：

| 数据卷 | 升级时的快照类来源 |
| --- | --- |
| 独立 Auth 账号卷 | 独立 Auth **已安装 values** 的 `upgradeBackup.authSnapshotClass`；首次安装时可在 `auth-values.yaml` 填好 |
| Consumer runtime 卷、可选 solver 卷 | Consumer 目标 `consumer-values.yaml` 的 `upgradeBackup.runtimeSnapshotClass`、`solverSnapshotClass` |
| Modeling 的 Onto、Hub、Evaluation 卷 | Modeling 安装器自动选集群唯一默认或唯一可用的 `VolumeSnapshotClass`，无需在 `modeling-values.yaml` 逐卷填写 |

旧内置 Auth 的所有权迁移从**原 Consumer 已安装 values** 读取 `upgradeBackup.authSnapshotClass`；仅修改这次的包外 `auth-values.yaml` 或目标 `consumer-values.yaml`，不会补上原安装记录中的缺失值。所有快照类都须与对应 PVC 所绑定 PV 的 CSI driver 相匹配。

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
- `global.auth.issuer`、`global.auth.audience` 与独立 Auth 的签发身份一致；联合校验也会比较 Modeling 的配置；
- 独立 Auth Service 的端口是 `5056`，Consumer values 的 `externalAuth.apiVersion` 为 `auth-v1`；
- Consumer 生成 Backend Service 的端口是 `5055`；
- Consumer 的 ELB 监听端口不与 Modeling 共用 ELB 的端口重复。

Consumer 模板中还需核对：`sharedAuth.enabled=false`、`externalAuth`、`consumer.authAdminTokenSecret`、`consumer.persistence.existingClaim`、`global.integration.serviceTokenSecret`、`consumer.mcp.tokenSecret` 及启用 HTTPS 时的 TLS Secret。`consumer.authAdminTokenSecret` 只供 Consumer 调用 Auth 管理接口，其值须匹配 Auth 的管理令牌；Auth 账号 PVC 和 bootstrap 密码只由 Auth owner 持有，不填入 Consumer 目标 values。`global.imageDelivery.pullSecretNames` 默认示例为 `default-secret`，须确认该 Secret 在 **Consumer namespace** 中确实可拉取目标镜像。启用 Hub 时填写 `hub.enabled/baseUrl/instanceId/tokenSecret`；需要 Evaluation 调用 Consumer 时配置 `global.integration.evaluationTokenSecret`；反馈连接填写 `flywheel.evaluationUrl/tokenSecret`，采集开关与工作空间范围由消费侧系统配置管理。Consumer 模板中这几项的启用示例是注释块，现有生效字段仍为空或关闭；按需启用时，须按实际地址、Secret 名称和 key 修改，保持示例为注释状态不会生成对应令牌。升级旧配置时，若 `consumer.environmentSecretName` 中含 Hub 或飞轮连接变量，先迁到显式 values 与 Secret 引用并保留原令牌；`hub.instanceId` 升级时保持稳定。

Consumer 升级前，还要在同一 `consumer-values.yaml` 中填写其**自有数据卷**的快照类；启用独立求解器时填写 solver 项：

```yaml
upgradeBackup:
  runtimeSnapshotClass: <RUNTIME_PVC_SNAPSHOT_CLASS>
  solverSnapshotClass: <SOLVER_PVC_SNAPSHOT_CLASS_OR_EMPTY>
  retainGroups: 2
```

Consumer 模板中的 `authSnapshotClass` 是旧内置 Auth 的兼容字段；目标包使用 `sharedAuth.enabled=false` 时，Consumer 整包升级不再备份 Auth 卷。独立 Auth 升级读取独立 Auth release 自己的已安装值，旧内置 Auth 迁移读取原 Consumer 的已安装值。

Consumer release 名称决定 Backend Service 名称。独立 Auth Service 在新装时为 `<AUTH_RELEASE>-auth`，在旧内置 Auth 迁移时沿用原 `<CONSUMER_RELEASE>-auth`。例如 Consumer release 为 `consumer`、独立 Auth release 为 `auth`：

```yaml
externalAuth:
  serviceName: auth-auth      # 新装；旧内置 Auth 迁移时改为 consumer-auth
  namespace: onto-consumer
  servicePort: 5056
  apiVersion: auth-v1
sharedAuth:
  enabled: false
```

Consumer 的 Auth 引用按独立 Auth 实际 Service 和身份填写。Backend 端点按 Consumer 包 `deploy/endpoints.yaml` 核对，其 Service 为 `<CONSUMER_RELEASE>-backend`。

## 4. 编辑 `modeling-values.yaml`

复制 Modeling 包中的示例：

```bash
cd "$JOINT_DIR"
cp -n "modeling-cce-${MODELING_VERSION}-${ARCH}/deploy/values.customer.example.yaml" modeling-values.yaml
```

用 `vi "$JOINT_DIR/modeling-values.yaml"` 编辑包外文件。以下片段说明关键字段，**不要用片段覆盖整个模板**，否则会丢失 Chart 需要的配置：

先从独立 Auth 的包外 `auth-values.yaml`（新装）或已安装 Auth release 的 values（迁移和升级）确认 `issuer/audience`。新装的 Service 名称由 Auth releaseName 决定；迁移和升级则从独立 Auth 实际 Service 确认名称、namespace 和端口。Modeling 示例模板中的 `http://consumer-auth:5056`、`knowledge-scaffold`、`consumer-auth` 只是默认示例，须按独立 Auth 配置替换。`issuer` 是 JWT 的 `iss`，不用于跨命名空间访问；Modeling 通过 `externalAuth` 访问 Auth Service。`externalConsumer` 只配置 Modeling 调用 Consumer Backend 的独立链路。

```yaml
global:
  clusterFlavor: standard
  targetArch: amd64
  auth:
    issuer: <与独立Auth签发身份一致的AUTH_ISSUER>
    audience: <与独立Auth签发身份一致的AUTH_AUDIENCE>
  imageDelivery:
    pullSecretNames:
      - <MODELING_IMAGE_PULL_SECRET>
  integration:
    serviceTokenSecret:
      name: <MODELING_SERVICE_TOKEN_SECRET>
      key: <TOKEN_KEY>

externalAuth:
  apiVersion: auth-v1
  serviceName: <AUTH_SERVICE>  # 填独立 Auth 实际 Service；旧环境迁移后名称可保留为原名称
  namespace: <AUTH_NAMESPACE>
  servicePort: 5056

externalConsumer:
  serviceName: <CONSUMER_RELEASE>-backend
  namespace: <CONSUMER_NAMESPACE>
  servicePort: 5055
```

新装 Auth 的 Service 通常为 `<AUTH_RELEASE>-auth`；旧内置 Auth 完成所有权迁移后仍是独立 Auth，但 Service 沿用原 `<CONSUMER_RELEASE>-auth` 名称和 UID。Modeling 的 `externalAuth` 始终按独立 Auth 当前实际 Service 填写。联合安装器还会校验 Consumer 客户端指向同一个 Auth Service；这只是两侧引用一致性校验。

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

联合入口按以下条件从 Consumer values 推导 Modeling 的跨侧 Secret 引用；它不会凭空生成固定的 Consumer 源名称：

| Modeling 目标引用 | Consumer 源引用 | 自动补齐条件 | 目标 Secret 默认名称 |
| --- | --- | --- | --- |
| `global.integration.serviceTokenSecret` | `global.integration.serviceTokenSecret` | 源已填写且 Modeling 目标引用为空 | `<Modeling release>-integration` |
| `hub.secrets.consumerToken` | `hub.tokenSecret` | `hub.enabled: true`，源已填写且目标为空 | `<Modeling release>-hub-consumer` |
| `evaluation.secrets.consumerToken` | `global.integration.evaluationTokenSecret` | Consumer 源名称已填写，源 key 完整且目标为空 | `<Modeling release>-evaluation-consumer` |
| `evaluation.secrets.flywheelToken` | `flywheel.tokenSecret` | Modeling 的 `evaluation.feedback.enabled: true`，源已填写且目标为空 | `<Modeling release>-flywheel-consumer` |

补齐时目标 key 沿用源 key，并加 `sourceRef` 指向实际 Consumer namespace、源 Secret 名称和 key。联合安装先执行 Consumer，再执行 Modeling，因此 Consumer 已按其 values 初始化所需业务令牌后，Modeling 才导入同值令牌。三种可选连接使用不同用途的令牌。Modeling 模板中这三项保留空引用；即使把它们注释掉，Chart 默认值仍为空，联合入口在满足条件时也能自动补齐。不满足条件时保持为空，不要求额外 Secret。

若 Modeling 目标已显式填写 `name/key`，联合入口会保留原值，**不会自动增加 `sourceRef`**；目标 Secret 须事先存在，或者在包外 values 中显式写入与 Consumer 实际源一致的 `sourceRef`。联合入口会核对已有 `sourceRef` 的 namespace、源名称与 key。目标 Secret 已存在时，Modeling 安装器还会核对源、目标的值；没有跨 namespace 读取权限时，可预建同值目标 Secret 并省略 `sourceRef`。如先单独部署 Modeling，单独安装器不会读取 Consumer values，必须自行准备对应的目标 Secret 和引用。

例如，只有在 Consumer values 确实使用 `consumer-demo` namespace、`flywheel.tokenSecret: {name: consumer-flywheel-credentials, key: service-token}`，且已配置反馈地址时，Modeling 才可显式填写以下引用：

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

这只是启用示例，不是固定默认值。源 Secret 的名称、key、namespace 须与 Consumer values 及联合配置相符。若实际目标名称无需指定，保留模板中的空引用即可由联合入口生成；显式填写以上目标名称意味着需要同时提供匹配的 `sourceRef` 或预建目标 Secret。即使 `evaluation.feedback.enabled: false`，只要 `flywheelToken.name` 非空，联合入口仍要求 Consumer 配置对应的源引用，Modeling 安装器也会检查或导入该 Secret，因此关闭连接时应保持令牌引用为空。

Consumer 的 `hub.baseUrl` 和 `flywheel.evaluationUrl` 填集群内可达的 Hub、Evaluation Service 基础地址；以下仅用保留域名示意地址格式：`http://hub.example.net:8200`、`http://evaluation.example.net:5180`。部署时要按实际 Modeling release、namespace 和 Service 改为可达地址。飞轮地址不要追加 `/evaluation/api`，Consumer 客户端会追加路径。Modeling 的 `evaluation.feedback.enabled` 控制 Evaluation 是否接收反馈，`notifyRuns` 控制是否向 Onto Modeling 通知反馈轮次；两项默认均为 `false`。仅填写 Consumer 地址不会开启 Evaluation 接收，也不会自动补齐飞轮令牌；开启反馈时必须有 Consumer 地址和令牌源引用。Consumer 的采集开关另由其系统配置控制。`notifyRuns: true` 还要求 `enabled: true`。

还要填写 Modeling 的三个 PVC，并核对 Secret 引用：

```yaml
modeling:
  secrets:
    authAdminToken: {name: modeling-auth-client, key: admin-token}
  workloads:
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

旧环境升级时，Onto 的 PVC 名和原 PV 绑定必须沿用；Core、Agent 的旧 PVC/PV 与快照保留，不写入目标 values。首次加入 Hub、Evaluation 时另外准备两块独立、已绑定的 PVC。`hub.secrets.internal` 和 `evaluation.secrets.encryptionKey` 的引用应与模板一致，安装器首次创建对应 Secret；后续升级保留原名称、key 和值。`hub.secrets.consumerToken`、`evaluation.secrets.consumerToken`、`evaluation.secrets.flywheelToken` 可保持模板中的空引用，由上面的联合入口按实际启用条件补齐。

评测完成通知的 Secret 名称和 key 在 `evaluation.secrets.notificationToken` 中填写，示例为 `modeling-notify` / `service-token`。Chart 将同一凭据注入 Onto Modeling 的 `ONTO_EVALUATION_SERVICE_TOKEN` 和 Evaluation 的 `EVAL_MODELING_SERVICE_TOKEN`。安装器在目标 Secret 不存在时生成，已存在时检查并复用；部署账号无创建 Secret 权限时，由管理员预创建示例中填写的 Secret，并允许部署账号读取。values 中只填名称和 key，不填明文凭据。升级时保留原引用。

`modeling.secrets.authAdminToken` 对普通登录和 JWT 验签是可选的；Onto Modeling 要代理 Auth 管理接口时必须填写。示例引用 Modeling namespace 中的 `modeling-auth-client` Secret 的 `admin-token` 键，安装器不会自动创建该 Secret。它会作为 `ONTO_WEB_AUTH_ADMIN_TOKEN` 注入 Onto Pod，用于用户管理、自助改密和同步 Hub 成员。Secret 值以独立 Auth 实际管理令牌为准；不要把初始管理员密码复制过来。不启用这些功能时可把 `name`、`key` 都改为空字符串，相关操作会报告不可用。

Onto Modeling 后端读取评测状态和报告使用 Chart 注入的 `ONTO_WEB_EVAL_URL`，指向集群内同一 Modeling release 的 Evaluation Service；这与浏览器访问的 `modeling.config.evalPublicUrl` 是两条独立路径。

`modeling.config.evalPublicUrl`、`evaluation.trustedOrigins`、`evaluation.judge`、`evaluation.secrets.judgeApiKey`、`evaluation.secrets.sqlConfig` 不在客户示例中，Chart 提供空默认值。评测的对外监听端口默认是 5180；若改用其他端口，须在 Modeling values 中填写指向该独立入口的完整 `modeling.config.evalPublicUrl`。默认 Judge 或查询类评测等其他配置按 Modeling 包 `docs/modeling-deployment.md` 在包外 values 手动添加。

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
      - {id: onto, protocol: HTTP, listenerPort: 5100, tlsSecretName: "", host: "", path: /, pathType: Prefix, endpointId: onto-modeling}
      - {id: evaluation, protocol: HTTP, listenerPort: 5180, tlsSecretName: "", host: "", path: /evaluation/, pathType: Prefix, endpointId: evaluation}
```

## 5. 上传 Auth 镜像并编辑两份业务镜像清单

三方部署先处理 Auth 包的 `images/auth-server.tar`：导入归档，推送到目标仓库，记录仓库返回的完整 `repository@sha256:<64位摘要>`，填入第 6 节 `deployment.customer.yaml` 的 `auth.image`。例如在可访问仓库的上传机执行：

```bash
export AUTH_PACKAGE_DIR="$JOINT_DIR/onto-auth-${AUTH_VERSION}-${ARCH}"
docker load -i "$AUTH_PACKAGE_DIR/images/auth-server.tar"
# 将 SOURCE_AUTH_IMAGE 填成 docker load 输出的原镜像名称与标签。
export SOURCE_AUTH_IMAGE=<LOAD_OUTPUT_IMAGE_REF>
export TARGET_AUTH_IMAGE=<REGISTRY>/<REPOSITORY>/auth-server:<AUTH_VERSION>
docker tag "$SOURCE_AUTH_IMAGE" "$TARGET_AUTH_IMAGE"
docker push "$TARGET_AUTH_IMAGE"
```

`docker push` 输出的 `digest: sha256:...` 与目标仓库名称组合成 `auth.image`；也可从仓库控制台读取同一架构的 manifest digest。Auth 包 `auth-release.json` 提供架构、image ID 和文件哈希，**不提供上传后的仓库 digest**。Auth 镜像不写入 `consumer-images.yaml` 或 `modeling-images.yaml`，本地 image ID 和 tag 也不能代替 `auth.image`。目标仓库的拉取 Secret 必须在 Consumer namespace 可用。更多镜像核对方法见 **Consumer 包** `docs/import-images.md` 的 Auth 镜像章节。

旧内置 Auth 迁移升级仍提供独立 Auth 包和目标镜像 digest。迁移先保留原 Auth 镜像和账号数据，再由联合入口按 `auth.image` 升级；如果仅迁移所有权、不更换镜像，`auth.image` 填当前运行镜像的完整 digest。旧 digest 应保留到迁移和业务验收完成。

### 5.1 `consumer-images.yaml`

先复制 **Consumer 解压包**的镜像模板：

```bash
cd "$JOINT_DIR"
cp -n "consumer-cce-${CONSUMER_VERSION}-${ARCH}/deploy/resolved-images.example.yaml" consumer-images.yaml
```

查看同一包 `release.yaml` 的 `images`、`archive`、`archiveRef`，按该包 `docs/import-images.md` 将 `images/` 下四张镜像上传到目标仓库，再用 `vi "$JOINT_DIR/consumer-images.yaml"` 将模板右侧值替换为**目标仓库返回的 manifest digest**。四个固定镜像 ID 是：

```yaml
global:
  resolvedImages:
    backend: <REGISTRY>/backend@sha256:<64位digest>
    frontend: <REGISTRY>/frontend@sha256:<64位digest>
    jiuwenbox: <REGISTRY>/jiuwenbox@sha256:<64位digest>
    opsctl-runtime: <REGISTRY>/opsctl-runtime@sha256:<64位digest>
```

启用独立求解器时按 Consumer 包 `docs/import-images.md` 追加 `ortools-solver`。JiuwenBox 必须来自 Consumer 离线包；联合入口从此文件读取其 digest，供 Modeling 的 sandbox-gateway 使用。

### 5.2 `modeling-images.yaml`

先复制 **Modeling 解压包**的镜像模板：

```bash
cd "$JOINT_DIR"
cp -n "modeling-cce-${MODELING_VERSION}-${ARCH}/deploy/resolved-images.example.yaml" modeling-images.yaml
```

同样按 Modeling 包 `release.yaml.images` 的 `archive` 和 `archiveRef` 上传三张自有镜像，再用 `vi "$JOINT_DIR/modeling-images.yaml"` 将目标仓库返回的 manifest digest 填入模板：

```yaml
global:
  resolvedImages:
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
auth:                         # 三方自动编排时填写；两方部署时省略
  package: ./onto-auth-<版本>-<架构>.tar.gz
  releaseName: <AUTH_RELEASE>
  namespace: <CONSUMER_NAMESPACE>
  values: ./auth-values.yaml
  image: <AUTH_REGISTRY_IMAGE_AT_SHA256_DIGEST>
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
| `auth.package/values/image` | 三方编排使用的 Auth 包、包外配置和目标仓库 digest；旧环境升级保留原 Service 名称 |
| `consumer.package` | Consumer 离线包路径 |
| `consumer.releaseName` | Consumer Helm release 名称 |
| `consumer.namespace` | Consumer 命名空间 |
| `consumer.values/resolvedImages` | 第 3、5.1 节准备的两个包外文件 |
| `modeling.package` | Modeling 离线包路径 |
| `modeling.releaseName` | Modeling Helm release 名称 |
| `modeling.namespace` | Modeling 命名空间 |
| `modeling.values/resolvedImages` | 第 4、5.2 节准备的两个包外文件 |

当前联合入口要求 Auth 与 Consumer 在同一 namespace，且 Helm release 名称不同；Modeling release 的名称与 namespace 组合也不能与前两者冲突。Consumer、Modeling 各自的 `issuer/audience` 和 `externalAuth` 须与独立 Auth 的身份、实际 Service 一致；联合入口会再核对两侧引用一致。所有路径都相对于 **`deployment.customer.yaml` 所在目录**解析，不是相对于当前 shell 目录。首次安装时 `compatibility-verified.yaml` 尚不存在是正常的，第 7 节会生成它。

## 7. 生成 `compatibility-verified.yaml`

兼容性文件由 Modeling 离线包内的联合入口生成。脚本会校验 Consumer、Modeling 两份包的 `checksums.sha256` 和 Auth 包 `auth-release.json` 的文件清单及哈希；从业务包 `release.yaml` 读取发布版本、Chart 版本和目标平台，并核对 Auth 包架构。Backend API 版本取自 Consumer `deploy/endpoints.yaml`；独立 Auth 的客户端协议为 `auth-v1`，联合入口校验 Consumer、Modeling 的 `externalAuth.apiVersion` 均为该值，首次安装后由 Auth 首装入口核对独立 owner 和 Service。脚本生成唯一组合 ID，客户无需手写兼容性 YAML。

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

检查输出中的 `targetCombinationId`、`targetCombination` 和目标包版本。确认 Auth、Consumer、Modeling 的目标组合已完成联合验证后执行：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config "$JOINT_DIR/deployment.customer.yaml" \
  --confirm-verified
```

脚本在 `compatibilityFile` 指向的路径新建文件，自动写入一个目标组合和 `transitions: []`。如果文件已经存在，命令会拒绝覆盖；保留原文件并检查 `deployment.customer.yaml` 指向是否正确。这里的确认只声明该组合已有联调结论，脚本读取版本号本身不能证明业务兼容。

### 7.2 联合升级

优先保留上次安装时的 `compatibility-verified.yaml`。先将 `AUTH_VERSION`、`CONSUMER_VERSION`、`MODELING_VERSION` 改为本次实际包版本，按第 1 节把三份目标包复制到 `JOINT_DIR`、解压并校验；再将 `deployment.customer.yaml` 中 `auth.package`、`consumer.package`、`modeling.package` 指向目标包，`auth.image` 指向目标仓库 digest。需要独立 Auth 镜像升级时，已安装 Auth values 中的 `upgradeBackup.authSnapshotClass` 必须可用；仅修改包外 `auth-values.yaml` 不会更新独立 owner 的已安装配置。从**新 Modeling 包**目录执行下面的预览与确认命令：

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

确认新旧组合及中间组合已联调。预览中的 `modelingDataMigration` 由已安装的 Modeling Helm values 决定：有旧 Core 时为 `required`，部署器会在升级过程中自动迁移 Core 项目；仅有 Onto、Hub、评测时为 `none`，需确认新版本可直接读取现有数据。旧 Core 场景执行：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config "$JOINT_DIR/deployment.customer.yaml" \
  --upgrade-candidate \
  --confirm-verified
```

无旧 Core 场景执行：

```bash
bash deploy/install-consumer-modeling.sh cce prepare-compatibility \
  --config "$JOINT_DIR/deployment.customer.yaml" \
  --upgrade-candidate \
  --confirm-verified \
  --confirm-no-modeling-migration
```

清单存在时，脚本保留原有记录并补齐缺失组合、迁移记录；如同一路径原先记录了 `none`，但集群当前仍有旧 Core，会更新为 `required`。清单不存在时，在确认后新建文件。联合升级会先读取集群当前两侧版本，按当前组合筛选 `from`，再匹配本次离线包对应的 `to`；因此正常情况下会自动得到唯一 transition。其他旧版本到同一目标版本的路径不会干扰当前升级。若集群实际版本与旧契约不符、两个新包平台不一致或端点不符合契约，脚本会拒绝生成。**自动重建只是恢复字段来源，不代表已完成旧→目标版本的联调或数据兼容验证**；运行确认命令前仍须取得发布方的验证结论。

### 7.3 同版本联合升级

当已安装组合与两份目标包的发布版本、Chart 版本、平台和接口版本完全相同时，仍按第 7.2 节的预览与对应的确认命令操作。脚本会自动识别同版本，在预览结果中显示 `sameVersion: true`，并生成 `from`、`intermediateCombinationId` 和 `to` 指向同一个组合的路径。重复生成时沿用同一条记录。确认前仍需核对两侧联调结果与迁移策略。先按第 6 节确认包、values 和镜像清单指向本次要部署的内容。

## 8. 联合首次部署

从 Modeling 包解压目录执行，沿用第 7.1 节的 `JOINT_DIR`、`MODELING_PACKAGE_DIR` 和 `cd "$MODELING_PACKAGE_DIR"`：

```bash
bash deploy/install-consumer-modeling.sh cce plan \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce install \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce verify \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce status \
  --config "$JOINT_DIR/deployment.customer.yaml"
```

三方空环境直接运行上述 `install`，由联合入口先创建 Auth。两方部署若已单独安装 Auth，可以在 `install` 前执行 `preflight`。各命令的作用：

1. `plan`：校验三份包、镜像映射、兼容性组合、三侧 Auth 引用与身份，并渲染检查；
2. `preflight`：检查集群、Secret、PVC、节点、Consumer 端点和 ELB 端口；三方空环境的 Auth 尚不存在，先运行 `install`；
3. `install`：三方配置先安装 Auth，再安装 Consumer；联合入口在配置的 `timeoutSeconds` 内等待 Consumer 工作负载就绪，通过严格验证后才安装 Modeling。超时会指出未就绪的 Deployment，核对 Pod 状态后使用原操作 ID 续跑；两方配置沿用 Consumer → Modeling；
4. `verify`：验证两侧工作负载、内部健康接口和 JiuwenBox 连接；
5. `status`：读取两侧 Helm release 的状态和 Chart；三方配置同时核对 Auth owner 状态。

每一步都应输出 `status: succeeded`。失败时保留 `customer-state`，不要立即删除一侧 release 或 PVC。

## 9. 联合升级

联合升级要求：

- 当前集群两侧 release 与 `from` 组合一致；
- 新离线包与 `to` 组合一致；
- `intermediateCombinationId` 是新 Consumer 加旧 Modeling；
- 新版本 Modeling values 中 Onto、Hub、评测 PVC 与已安装业务保持一致；从旧版升级时保留原 Core/Agent PVC/PV 和快照，不在目标 values 中继续声明；
- 集群有唯一默认或唯一可用的 VolumeSnapshotClass，升级前已存在的三或五个 Modeling 数据文件系统有足够剩余空间；
- Consumer 侧升级入口支持其自身的数据保留策略。

按第 7.2 节准备并校验三份目标包，将 `deployment.customer.yaml` 中 `auth.package`、`consumer.package`、`modeling.package` 指向目标包；更新 `auth.image` 和两份业务镜像清单中的目标 digest，并核对三份 values。保留原 PVC 名称、Auth issuer/audience 和业务 Secret 引用；新增 Hub、评测或飞轮连接时，先核对 Consumer 源引用、Modeling 目标引用和开关，已在使用的令牌须沿用原值。按第 7.2 节生成旧、中间、新三个组合及升级记录。不要再次复制模板覆盖现有包外 YAML。

三方编排还需提供本次 Auth 包，在 `auth.package` 填入其 tar 路径，在 `auth.image` 填入目标镜像 digest。旧 Consumer 内置 Auth 环境须将 Consumer、Modeling 的 `externalAuth.serviceName` 都设为原 `<CONSUMER_RELEASE>-auth`，并保持原 namespace、issuer、audience、账号 PVC 与 Secret 引用；原 Consumer 已安装 values 中的 `upgradeBackup.authSnapshotClass` 须满足迁移快照要求。目标 Consumer 包必须声明 external 模式；第 7.2 节生成并确认的过渡组合须使用 `auth-v1`。联合入口自动识别旧 owner，执行快照、所有权迁移、迁移后预检，然后升级 Auth 镜像；已有独立 Auth 则从升级预检开始。Auth 镜像升级中断或回滚需先按 Auth 工具记录恢复，不自动继续两侧业务升级。

升级时将 `MODELING_PACKAGE_DIR` 指向**新 Modeling 包**解压目录并进入该目录；`JOINT_DIR` 仍指同一个包外目录。无需手工创建 Modeling 快照或备份 JSON：

```bash
export MODELING_PACKAGE_DIR="$JOINT_DIR/modeling-cce-${MODELING_VERSION}-${ARCH}"
cd "$MODELING_PACKAGE_DIR"
```

```bash
bash deploy/install-consumer-modeling.sh cce plan \
  --config "$JOINT_DIR/deployment.customer.yaml"

bash deploy/install-consumer-modeling.sh cce upgrade \
  --scope both \
  --config "$JOINT_DIR/deployment.customer.yaml" \
  --snapshot-retention 2 \
  --min-free-percent 15
```

已有独立 Auth 的环境可在联合 `upgrade` 前单独运行 `preflight`；旧内置 Auth 环境不要先执行目标 external 配置的 `preflight`，它会在所有权迁移前因独立 owner 尚不存在而失败。

建模安装器会从已安装的 Helm values 自动识别是否含 Core/Agent；旧版升级时快照旧卷、删除旧 Deployment/Service/Ingress，并保留退役卷与快照，无需额外退役参数。`--snapshot-retention` 是新三业务成功快照组的保留数，默认 `2`；`--min-free-percent` 是每个 Modeling 数据文件系统的最低剩余空间百分比，默认 `15`。

升级前盘点旧 Core 项目、MCP Token、旧评测数据及外部调用方。目标版 Onto 会保留历史项目元数据，但不再请求 Core；未完成的 Core 清理任务保留记录且停止重试。需要迁移的业务数据须先完成迁移与对账。

同版本联合升级先按第 7.3 节生成路径，再执行本节的 `plan`、`upgrade` 命令；已有独立 Auth 时可增加 `preflight`。恢复时使用原有 `--resume <operation-id>` 参数。同版本升级仍执行 Consumer 升级与验证，然后对 Modeling 自动创建快照、升级与验证；恢复时会核对已完成 release 的本次操作 ID 和目标镜像引用。必须使用包含同版本支持的新 Modeling 离线包；Consumer 的安装器来自其独立离线包，执行前还需确认该包的 `upgrade` 命令允许同版本调用。

联合入口的实际顺序：

```text
旧内置 Auth：Auth migrate → 校验原 Service/账号卷/owner → Consumer preflight → Modeling preflight
已有独立 Auth：Consumer preflight → Modeling preflight
→ Auth upgrade、owner 与镜像校验（目标 digest 相同则跳过升级）
→ Consumer upgrade → Consumer verify
→ Modeling upgrade 内部 preflight
→ 统计原数据、检查空间、停止旧 release 已存在的三或五个写入 Pod
→ 为旧 release 已存在的三或五个 PVC 自动创建并等待 VolumeSnapshot
→ 在 stateDir 自动写入备份记录
→ 校验快照和 PVC/PV 身份 → Modeling upgrade → Modeling verify
```

前两步用于检查目标包和集群基础条件，不会创建快照；`upgrade` 才开始自动备份。Modeling 安装器在成功后将本次快照组标记为 `successful`；只清理超出保留数量的新三业务完整快照组，旧 Core/Agent 快照组保留。升级结果的 `results.modeling.artifacts` 会列出自动生成的备份 JSON 路径；文件保存在 `stateDir`，权限为 `0600`。客户无需填写或创建该文件。

自动快照发生在 Consumer 升级成功之后。如果快照创建失败，联合入口会停止在 Modeling 阶段，Consumer 已升级且不会被自动回滚；应保留快照、PVC、状态目录和旧镜像，检查实际工作负载状态后再恢复。快照就绪与备份记录生成不等于恢复演练，交付前仍应验证从快照恢复数据的能力。

## 10. 联合卸载

确认两侧业务已停止，且没有其他客户端继续使用这套 Auth 后，可在任一有 `kubectl`、Helm、Python 3.11+、PyYAML、jsonschema 和集群访问权限的管理机运行。管理机只需新版 Modeling 联合入口脚本及其相邻的 `common.py`、`upgrade.py`，不需要原离线包。三方联合卸载配置可以只包含以下字段；不卸载 Auth 时省略 `auth` 段：

```yaml
schemaVersion: cce-joint/v1
cluster: {context: <KUBE_CONTEXT>}
stateDir: ./customer-state
auth: {releaseName: <AUTH_RELEASE>, namespace: <AUTH_NAMESPACE>}
consumer: {releaseName: <CONSUMER_RELEASE>, namespace: <CONSUMER_NAMESPACE>}
modeling: {releaseName: <MODELING_RELEASE>, namespace: <MODELING_NAMESPACE>}
```

从 Modeling 源码或包含新版联合入口的部署目录运行。下面以已进入 Modeling 源码仓根目录、`JOINT_DIR` 指向卸载配置所在目录为例：

```bash
bash deploy/install-consumer-modeling.sh cce uninstall \
  --config "$JOINT_DIR/deployment.uninstall.yaml"
```

在新管理机上也可以直接运行 `python3 -B modeling/deploy/cce/joint-cce.py uninstall --config <配置绝对路径>`，无需复制历史安装包目录。

联合入口先取得配置中各 release 的操作锁，读取集群里的 Helm release 身份和操作状态，并核对待卸载清单只包含应用工作负载及其 Service、Ingress、ConfigMap、Job。配置 `auth` 时，还要求 Consumer 已使用 external Auth，并核对独立 Auth 状态、Deployment/Service 归属，以及 Helm 清单只有 Auth Deployment 和 Service；按 **Modeling → Consumer → Auth** 顺序卸载。旧版 Consumer 仍拥有 Auth 时，先完成所有权迁移，再使用三方卸载。省略 `auth` 时，按 **Modeling → Consumer** 顺序卸载，保留独立 Auth。确认每侧 Helm release 已移除后，还会按归属标签或注解、名称范围清理残留的 Deployment、Service、Ingress、Job、ReplicaSet；归属不明确的资源会阻止操作。随后删除该侧的 `<release>-cce-state` ConfigMap，并将完成进度写入本地 Journal；操作 Lease 只释放、不删除。PVC、PV、Secret、VolumeSnapshot 和 `stateDir` 中的备份记录均保留；命名空间和外部镜像仓库也不删除。卸载 Auth 后，保留的账号卷不能按新环境空卷流程重新初始化。

如果中途失败，先查看返回的 `operationId`、`phase`、`results`，核对配置中各 release 的实际 Helm 状态，再使用同一操作 ID 续跑：

```bash
bash deploy/install-consumer-modeling.sh cce uninstall \
  --config "$JOINT_DIR/deployment.uninstall.yaml" \
  --resume <原操作ID>
```

续跑要求原操作的 Journal 仍在 `stateDir`，集群 context、配置中的 Auth、Consumer、Modeling release 名称和 namespace 与原操作一致；已完成的 release 不会再次卸载。换管理机续跑时，需将原 Journal 一并带到新管理机。不要在其他操作仍运行时执行卸载。

## 11. 联合失败恢复

联合操作会在 `stateDir` 写入 `<operation-id>.json` Journal。失败后：

1. 查看 JSON 的 `phase`、`errorCode`、`failedSide`、`failedAction` 和 `results`；子安装器失败时，`results.<side>` 保留其 `phase`、`message`、`retryable` 和 `recovery` 等结构化结果，`nextAction` 提示下一步核对方向；
2. 检查 Auth、Consumer、Modeling 的实际 Helm release、Deployment、Service、Pod 和 PVC；Auth 迁移或升级还要核对快照、`authOwnership`、`authRelease` 与 Service UID；
3. 如果某一侧已经完成，不要卸载或清空该侧；
4. 只有 Journal、实际 release 版本和输入文件完全一致时，才使用 `--resume <operation-id>`；
5. Auth 所有权迁移中断且集群阶段与 Journal 一致时，联合 `--resume` 会调用 Auth 迁移工具的布尔 `--resume`；Auth 镜像升级中断时，先按 Consumer 包 `docs/auth-release.md` 检查其恢复语义，再决定联合操作能否续跑；某侧升级结果不确定时不能直接重试；
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
export AUTH_RELEASE=auth
export AUTH_SERVICE=auth-auth  # 旧内置 Auth 迁移后填写原 consumer-auth
helm --kube-context "$KUBE_CONTEXT" -n "$CONSUMER_NAMESPACE" list --all
helm --kube-context "$KUBE_CONTEXT" -n "$MODELING_NAMESPACE" list --all
kubectl --context "$KUBE_CONTEXT" -n "$CONSUMER_NAMESPACE" get deployment,pod,svc,pvc
kubectl --context "$KUBE_CONTEXT" -n "$CONSUMER_NAMESPACE" get svc "$AUTH_SERVICE"
kubectl --context "$KUBE_CONTEXT" -n "$CONSUMER_NAMESPACE" get configmap "${AUTH_RELEASE}-cce-state"
kubectl --context "$KUBE_CONTEXT" -n "$MODELING_NAMESPACE" get deployment,pod,svc,pvc
```

三方配置的联合 `verify` 要求 Auth owner 为 `ready`、Auth Deployment 实际镜像等于 `auth.image`，并核对 Auth owner 记录的 Service UID（若有）与当前 Service 一致。旧内置 Auth 的所有权迁移阶段仍须确认原 Auth Service 名称、UID 和账号 PVC 未变化；迁移完成后的日常验收不再以 Consumer 留存的历史 UID 为准。随后完成合成业务验收：共享登录、Consumer 页面、Onto Modeling、Knowledge Hub、独立评测，以及仍保留的 Onto 自有 MCP 调用。启用跨侧连接时，分别验证 Consumer → Hub、Evaluation → Consumer 的业务请求和 Consumer → Evaluation 的反馈提交、接收状态；`notifyRuns` 开启时再验证轮次通知。未启用的连接无需执行相应验收。旧 Core、Agent 和 Core MCP 入口应已从客户端与运维探针中移除。Helm 和 Pod 状态正常不能替代业务验收。

## 13. 按包查更详细的说明

| 包 | 解压包内的说明 | 主要内容 |
| --- | --- | --- |
| Auth | `cce/auth-values.example.yaml`、`cce/auth-cce.py`、`auth-release.json` | Auth 专用 values、首装/迁移/升级入口和包内文件清单 |
| Consumer | `docs/auth-release.md`、`docs/install.md`、`docs/import-images.md`、`docs/upgrade-and-recovery.md`、`docs/command-interface.md` | Auth 所有权和快照条件、Consumer Secret/PVC、镜像上传与续跑 |
| Modeling | `docs/modeling-deployment.md`、`docs/offline-package-build.md`、`docs/joint-deployment.md` | Modeling/Hub/Evaluation values、镜像及联合部署 |

Hub/Evaluation Deployment 和健康检查通过后，仍须验收已启用的跨侧业务调用；健康检查不能证明跨服务令牌与用户鉴权均正确。
