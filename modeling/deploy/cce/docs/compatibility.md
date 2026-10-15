# 兼容性记录

软件包默认包含不含任何组合的 `compatibility.yaml`。Consumer 与 Modeling 团队必须共同记录经过验证的精确组合，包括发布版本、Chart 版本、平台、认证与 Consumer API 版本、操作协议、端点协议以及 JiuwenBox 镜像组合。安装器会拒绝空 manifest 或不匹配的 manifest，避免在共享 CCE 环境尚未验证时宣称跨仓库兼容。

完成联合升级验证后，记录 `from`、`to` 和 `intermediateCombinationId` 组合 ID，并设置 `steps: [consumer, modeling]`。中间组合表示新 Consumer 搭配旧 Modeling。即使维护期间已停止跨侧流量，安装器仍要求列出该组合。只有在使用已有数据完成目标二进制测试后，才可记录 `modelingDataMigration: none` 和 `preserveModelingData: true`。任何会改变数据的迁移仍不受支持。单侧升级使用 `steps: [modeling]`，且不能修改 Consumer 版本。

请将此 manifest 与验收证据、源镜像到客户镜像的 digest 映射一同保存。发布包中不得包含环境地址、凭据或真实数据。
