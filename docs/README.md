# Meta Wise Documentation

这里是 Meta Wise 的文档入口。根目录 README 只保留项目门面和最短启动路径，细节说明放在本目录中。

## Start Here

| 文档 | 类型 | 适合读者 |
| --- | --- | --- |
| [Getting Started](getting-started.md) | Tutorial / How-to | 第一次本地启动或用 Docker 启动的人 |
| [Architecture](architecture.md) | Explanation | 需要理解运行时、工具系统和目录拆分的人 |
| [Configuration](configuration.md) | Reference | 需要配置模型、数据连接、运行时目录和观测的人 |
| [API Reference](api.md) | Reference | 需要对接 HTTP API、SSE 事件和协议接口的人 |
| [Ontology Upload Template](ontology-upload-template.md) | Reference | 需要制作或导入本体 ZIP 包的人 |

## Runtime and Providers

| 文档 | 说明 |
| --- | --- |
| [Business Data Provider Extension](business-data-provider-extension.md) | 扩展 `business_data_search` provider 的方式 |
| [DataAgent Provider Design](dataagent-provider-design.md) | DataAgent provider 设计说明 |
| [NL2SQL Template Provider](nl2sql-template-provider.md) | NL2SQL Template provider 的匹配和回退机制 |
| [Operation Evolution](operation-evolution.md) | operation 演化流程 |
| [Reflection Evolution Optimization](reflection-evolution-optimization.md) | 反思与演化优化设计 |

## Protocols and Evaluation

| 文档 | 说明 |
| --- | --- |
| [ACP Test Guide](acp-test-guide.md) | ACP 协议测试指南 |
| [ACP Multilingual Migration Design](acp-multilingual-migration-design.md) | ACP 多语言迁移设计 |
| [Consumer Agent Specs](consumer-agent-specs.md) | 消费智能体规格与限制 |
| [Operation Clarity Evaluation](operation-clarity-evaluation.md) | operation 清晰度评测说明 |

## Deployment

发布包构建、部署、升级和回滚见 [platform/deployment/README.md](../platform/deployment/README.md)。
