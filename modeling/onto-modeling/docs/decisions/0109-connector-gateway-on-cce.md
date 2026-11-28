# 0109 CCE 交付带上连接器网关

## Context

**目标**：在 CCE（Helm）上部署的建模服务，上传的 PDF、Word、Excel 能转成文本版，项目成员能接入业务数据库，和 compose 部署一样。

**现状**：资料转文本、数据源采集都在连接器网关里做（ADR-0073、ADR-0104），compose 部署有这个服务。develop 的 CCE chart
（`modeling/deploy/cce`）只有 onto、sandbox-gateway、hub、evaluation，没有网关。网关地址没配时资料一直停在「等待转换」，
建模命令也等着，CCE 上的建模走不下去。

**基本约束**：

- 上传文件不可信：解析可能卡死、吃满内存，解析库不能进助手所在的容器（ADR-0073）。
- CCE 交付由安装器管理镜像清单、PVC 绑定、快照备份与升级校验（`install-cce.py`、`storage-inventory.yaml`），这套流程不归建模服务。
- 数据源凭据是密文加主密钥，丢了只能让用户重填，不会泄露。

## Decision

1. 第四个自有镜像 `connectorGateway`：同一个 Dockerfile 的 `connector-gateway` 目标，`build-images.sh` 单独构建，
   打包、发布校验、联合部署的镜像清单都加上它。
2. 单独的 Deployment 与 ClusterIP Service（8120）：65534 运行、只读根文件系统、无 capability、不挂服务账号令牌，
   内存上限 1.5Gi（和 compose 一致）。onto 经 `ONTO_WEB_CONNECTOR_GATEWAY_URL` 访问它，它从 onto 的 Service 取签票据的公钥。
3. 数据源出口：把本 Release 的各 Service 名和 Auth、Consumer 的 Service 名写进 `ONTO_CONNECTOR_EGRESS_DENY`，
   不让数据源连接指向平台内部服务（compose 里同一份清单是服务名）。
4. 插件卷、凭据卷用 emptyDir，不进 `storage-inventory.yaml`。安装器 `verify` 增加网关的 rollout 与经 onto 的健康检查。

## Alternatives considered

- **凭据卷用 PVC**：Pod 重建后数据源凭据还在。但要改安装器的 PVC 绑定、快照备份、升级比对，并让客户多建一个 PVC，
  改动落在 CCE 交付流程里；凭据丢失的后果只是重填。先不做，留给 CCE 交付负责人决定。
- **网关跑在 onto 镜像里**：省一个镜像，但解析库进了助手所在的镜像，违背 ADR-0073。
- **加 NetworkPolicy 隔离网关**：compose 里转换走内部网络、只有采集有出口。CCE chart 目前没有任何 NetworkPolicy，
  单给网关加会引入新的集群前提（CNI 支持）；网关自身对每个采集子进程按登记的主机端口放行，先依赖它。

## Consequences

- CCE 上资料转换、数据源接入可用；升级时多上传一张镜像，`resolved-images.yaml` 多一个 key。
- 网关 Pod 重建后，已接入数据源的凭据和主密钥丢失，要在资料页「修改连接」重新填写；已信任的连接器插件由内核 30 秒内重新交付。
- 网关 Pod 的网络出口不受集群策略限制，内部服务名单靠网关自查；集群里还有别的敏感服务时，要在部署时追加到出口禁止名单。
