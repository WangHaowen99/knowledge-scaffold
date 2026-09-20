# 0003 会话池与离线部署链

## Context

每个活跃建模会话是一个 Claude Agent SDK 拉起的 CLI 子进程（均值约 400MB）。
原实现每项目一个常驻会话、无上限无回收，内存随"重启以来发过消息的项目数"
单调累积，8G 主机在几十个项目后必然 OOM。同时 onto-web 此前没有任何交付包
构建与部署脚本，方案第八节的离线交付、AVX 预检要求均未落地。
方案文档 4.1.1（2026-09-19 定）把会话池定为上线前置条件。

## Decision

1. 会话生命周期改为"活跃时存在、空闲即回收、resume 恢复上下文"：
   - 活跃子进程上限 `ONTO_WEB_MAX_SESSIONS`（默认 12，对应 8G 主机）；
     超限先驱逐最久空闲的会话，全部在跑则 FIFO 排队并向前端广播位次
     （`queue_position` 瞬时事件），打断按钮兼作取消排队出口。
   - 后台每 30 秒回收空闲超过 `ONTO_WEB_SESSION_IDLE_TTL`（默认 600 秒）的会话。
   - 从消息流捕获 SDK session id 落盘（`data/sessions/<pid>.meta.json`）；
     重连时带 `resume` 恢复模型上下文，顺带消除服务重启后模型失忆的缺口。
     显式"关闭会话"清除续接点（保留换配置重开的语义）；resume 失败自动清点重试。
   - `ONTO_WEB_CLI_PATH` 透传 `cli_path`，为无 AVX 环境的降级兼容包预留钩子。
2. 部署链按 consumer 同构模式落地：多阶段 Dockerfile（debian 系，禁 Alpine）、
   `build.sh <version> [amd|arm]` 产出镜像 tar + compose + 脚本的离线交付包、
   `preflight.sh`（AVX/内存/swap/磁盘/docker 预检）、`deploy.sh` 一键部署；
   容器 HOME 指向数据卷使 CLI transcript（resume 依赖）持久化。

## Alternatives considered

- 常驻会话 + 只加上限：不解决累积占用，上限内依旧被闲置项目占满。
- 回收但不 resume：实现最简，但回收即模型失忆，等于把重启缺口常态化。
- 每会话 cgroup 限额：容器内做子进程级 cgroup 需特权，改为 compose 容器级
  mem_limit + 主机 swap 兜底。

## Consequences

- 内存上限变为 `固定开销 + MAX_SESSIONS × 单进程峰值`，可按主机内存线性调参。
- 排队与回收对用户可见面最小：事件历史落盘回放 + resume 续接，唯一感知是
  高峰期的排队横幅。
- resume 依赖部署机 `~/.claude/projects/` 的 transcript；容器重建不丢
  （HOME 在数据卷），但清卷即失忆（历史回放仍在）。
- 降级兼容包组合（Node + 旧版 JS CLI）功能冻结，启用前必须在目标机实测
  关键路径，见 deploy/README.md。
