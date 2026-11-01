# 0036 启动恢复注册表：各模块登记恢复步骤，启动过程就是恢复过程

## Context

详设 8.3 的原则是「内核进程随时可以死；磁盘状态在任何时刻都自洽；启动过程就是恢复过程」，措施 1 要求把
现有的启动恢复动作收成一个扩展点（方案 B8、第 3 章 `governance/startup_recovery.py`）。现状：lifespan 里
依次手写启动自检（残留锁、写入意图、未提交改动）、几项存量迁移、插件运行时启动核对、后台任务收敛、清扫
删到一半的目录；对话历史的幽灵运行态等到读历史时才修；O-35 只有三项（gaps B2 第 8 条、B3b 第 7 条）。

## Decision

1. `governance/startup_recovery.py`：`register(id, title, run, order=, background=)`；`run(manager)` 返回
   O-35 的一项，或 None（只做事、不进报告，如存量迁移）。lifespan 在任何会话开始之前按顺序跑前台步骤，
   做完才就绪（C-04、`/api/health/ready`）；后台步骤不挡就绪，做完补进报告。单步出错只记日志、报告里记
   failed，不挡其余步骤与启动。
2. 内核的步骤实现留在各自模块，`_load_defaults` 按顺序登记：
   | 顺序 | 步骤 | 报告项 |
   |---|---|---|
   | 10～30 | 清残留 git 锁、补提交写入意图、列未提交改动 | index_lock、write_intents、uncommitted |
   | 40～70 | 工具状态停止跟踪、单人所有权、补提交旧版上传、评测数据迁移 | — |
   | 80 | 插件运行时核对装配目录 | assembly |
   | 85 | agent 提交钩子发布并装到各项目（ADR-0037） | 失败时才有 |
   | 90 | 对话历史停在运行中的补中断与空闲；没回复的确认卡、决策卡补失效 | history_repair |
   | 100 | 还开着的执行改判中断并放续跑卡；进程内后台任务（求解验证）收敛 | interrupted_execs |
   | 300（后台） | 探测外部依赖 | dependencies |
   | 310（后台） | 清扫删到一半的项目目录 | purge |
3. 报告仍由 `services/startup_report` 存取（写日志、留存到数据目录）；`startup_report.run` 同步跑全部步骤
   （含后台的），给运维脚本与测试用。退出原因与重启次数仍由 `governance/lifecycle` 记。

## Alternatives considered

- **各模块在 import 时自己登记**：登记顺序取决于 import 顺序，测试替换模块后也不好控；改为注册表按固定
  顺序调各模块的 `register_steps`，插件或以后的模块再用 `register` 追加。
- **把对话历史自愈留在读历史时做**：首页、运维总览不读历史就一直看到「运行中」；启动时一次修完，
  读历史时的自愈保留作兜底。

## Consequences

- O-35 的项按契约补全：assembly、dependencies、interrupted_execs、history_repair、purge（write_intents 是 B3b
  加的）。外部依赖探测在后台，启动后很快就绪但报告里稍晚才有这一项。
- 原 lifespan 里的 `background_jobs.run_startup_sweeps` 与 `purge_leftovers` 后台任务并进注册表；
  定期备份、运维监听仍在 lifespan。
