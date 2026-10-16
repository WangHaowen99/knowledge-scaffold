# 升级与恢复

升级必须保留 Core、Agent 和 Onto Modeling 的全部已有数据。三个预创建 PVC 由客户负责，不属于本 Helm 发布；Chart 会以相同路径挂载它们，并使用单副本 `Recreate` 更新，不会创建、初始化、替换或删除 PVC。共享 Consumer 认证数据也必须由 Consumer 负责。

对于不迁移数据的升级路径，安装器会：

1. 读取已安装发布，记录每个工作负载的 PVC claim 名称、命名空间、绑定 PV 身份和数据格式版本。缺失、为空、已替换或绑定关系变化的 claim 会被拒绝。除非明确请求并单独测试数据迁移，否则目标 values 必须保留这些绑定。
2. 将已安装版本和目标版本匹配到一个经过验证的兼容性迁移。联合升级始终先升级 Consumer 并等待验证通过，再升级 Modeling。验证新 Consumer 能否与旧 Modeling 共存；否则在维护期间持续停止跨侧请求。停止相关写入，等待旧写入进程退出后，维护任务才能挂载同一卷。整个过程中 Core、Agent 和 Onto Modeling 保持单副本。
3. 要求每个 Modeling PVC 都有 VolumeSnapshot，并验证快照已就绪、指向同一 claim、UID 与记录一致，且创建时间晚于停止写入的时间。备份记录应存放在客户控制的运维区域，不得放入两个发布包。存储驱动必须支持可恢复的 VolumeSnapshot，并通过恢复测试验证。
4. 只接受明确标记为 `modelingDataMigration: none` 且 `preserveModelingData: true` 的迁移。这些标记必须来自使用已有数据完成测试的目标二进制与数据格式组合。会改变数据的迁移需要独立实现；不能用空卷初始化代替原有数据。
5. 在开始 Modeling 升级前先更新并验证 Consumer Helm 发布，然后验证新的 Modeling Pod 仍挂载原 claim 和 PV，且已有项目、会话和其他持久化记录仍可访问。如果 Consumer 失败，不得开始 Modeling。如果 Consumer 成功后 Modeling 失败，应保留已升级的 Consumer 和维护状态，待核对实际版本与数据后处理；不得自动回滚 Consumer。验收完成前保留旧镜像和已验证的备份。

执行升级前，停止传入写入，将三个 Modeling Deployment 缩容到 0，并等待旧 Pod 消失。为每个 PVC 创建一个快照并记录不可变身份。客户控制的备份记录形如以下合成示例：

```json
{
  "schemaVersion": "cce-backup/v1",
  "context": "customer-demo",
  "namespace": "modeling-demo",
  "releaseName": "modeling",
  "stoppedAt": "2026-10-15T08:00:00Z",
  "dataEvidence": {"core": 3, "agent": 2, "onto": 1},
  "claims": {
    "core": {"claimName": "modeling-core-data", "claimUid": "synthetic-core-uid", "volumeName": "synthetic-core-pv", "snapshotName": "core-snapshot", "snapshotUid": "synthetic-core-snapshot-uid"},
    "agent": {"claimName": "modeling-agent-data", "claimUid": "synthetic-agent-uid", "volumeName": "synthetic-agent-pv", "snapshotName": "agent-snapshot", "snapshotUid": "synthetic-agent-snapshot-uid"},
    "onto": {"claimName": "modeling-onto-data", "claimUid": "synthetic-onto-uid", "volumeName": "synthetic-onto-pv", "snapshotName": "onto-snapshot", "snapshotUid": "synthetic-onto-snapshot-uid"}
  }
}
```

安装器会检查 Kubernetes VolumeSnapshot 对象和实时 PVC/PV 绑定，但不会创建快照或测试恢复。备份记录本身不能证明备份可恢复。安装器还会检查旧 Deployment 已缩容为 0 且旧 Pod 已消失，之后才更新 Chart。`dataEvidence` 包含升级前 Core 项目数、Agent 运行记录数和 Onto Modeling 工作区数量。这些数据应由经过批准的只读维护流程从已停止的卷中采集。启动后，安装器会检查数据库可读性，并验证这些数量没有减少。数量检查不能替代业务级数据校验或快照恢复测试。

Modeling 侧 `deploy/install-cce.sh upgrade` 由联合入口调用，并接收通常的 context、namespace、release、values、resolved-images 和 state-dir 参数，以及 `--transition-id`、`--backup-record`、`--transition-record` 和委派的操作 Lease；兼容清单由联合入口校验后以内部 transition 记录传递。没有经过验证的操作 Lease 时，单独调用会被拒绝。联合入口接受：

```text
cce upgrade --scope both --config ... --transition-id ... \
  --backup-record <Modeling record>
```

它会先调用 Consumer 软件包的升级入口，等待成功后再调用 Modeling。Consumer 软件包根据自身 values 中的 `maintenance.backupExistingClaim` 执行备份与数据保留检查，联合入口不传递外部备份记录。这些命令必须在真实 CCE 集群及两个软件包上完成演练后，才能用于客户生产环境。

Modeling Chart 首先以三个写数据 Deployment 均为 0 副本的状态升级。安装器等待旧 Pod 消失，再次检查 PVC/PV 身份，然后以单副本应用目标 Chart 并检查数据证据。卷检查失败时，工作负载保持停止状态。更新后的 Chart 和备份快照应保留，以便受控恢复。

如果步骤失败，请检查操作 ID 和客户状态目录。`--resume <operation-id>` 只有在软件包与配置校验和一致、且实际 Helm 版本与日志一致时才会恢复。无法验证实际版本的半完成步骤必须转人工恢复。Helm 回滚只改变 manifest，不能恢复已修改的数据。卸载发布不得删除 PVC。

如果第二个发布安装失败，应保留第一个发布，检查实际集群状态；只有确认失败阶段后，才可使用同一操作 ID 重试。不得自动卸载已成功的一侧或清空任一侧的数据。
