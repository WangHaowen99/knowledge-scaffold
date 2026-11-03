"""连接器网关（详设 5.8.2；ADR-0073）：第一版只做文档转换，跑在单独的无特权容器里。

- 不接业务网络、不存凭据、没有出口，不挂任何数据卷；
- 转换器随网关镜像交付，在网关自己的只读目录里（`registry`）；
- 每个任务一个子进程（`runner` 起 `worker`），有超时、内存上限、输出上限，崩了不影响网关本身；
- 每个请求带内核签发的一次性票据，网关用内核的公钥验签（`app.evidence.tickets`）。

启动：`python -m app.connector_gateway`（单独的镜像，见 Dockerfile 的 connector-gateway 目标）。
"""
