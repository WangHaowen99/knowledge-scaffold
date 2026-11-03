# 0038 Jiuwen 会话放进独立 worker 子进程

## Context

详设 4.2 目标 6、8.3 措施 5（缩小爆炸半径）与方案 B11：Jiuwen 引擎在后端进程内运行（全局 Runner、
进程内的 DeepAgent），框架出错、阻塞或内存失控会拖垮整个内核，和它一起没的还有全部 Claude 会话与接口。

## Decision

1. `engines/jiuwen_worker.py` 是 worker 进程（`python -m app.services.engines.jiuwen_worker`），一个 worker
   承载全部 Jiuwen 会话；stdin / stdout 各一行一个 JSON。stdout 只给协议用：启动时复制一份描述符，再把 1 号
   描述符指到 stderr，库自己的 print 不会搅乱协议。
2. `engines/jiuwen_remote.py` 的 `RemoteJiuwenEngine` 实现同一个引擎协议，会话层不用改；`WorkerClient` 第一次
   连接时拉起 worker。回调经协议回到内核：确认卡（worker 发 rpc，内核答复）、续接点存取、错误留档；
   连接时把续接 token 与事件历史快照带过去。模型配置指纹、上下文窗口在内核这边算。
3. worker 退出或崩溃：它上面的每个会话都收到 `_engine_end`（出错），会话层按引擎异常结束处理（出错卡、执行
   记为中断并放续跑卡）；等着的操作一律失败；下一条消息连接时重拉一个新 worker。停机时会话都断开之后关
   worker 的 stdin，worker 跟着退。
4. `ONTO_WEB_JIUWEN_WORKER`（默认 true）；设 false 退回进程内运行（排障、单测直接构造引擎时用）。
   引擎类可换（`--engine 模块:类`），单测在 worker 里跑假引擎。

## Alternatives considered

- **每个会话一个子进程**：Jiuwen 的优势就是每会话约 3MB；每会话一个 Python 进程要多付几十 MB 与几秒冷启动。
- **线程隔离**：拦不住框架里的阻塞调用与内存失控，崩溃仍是整个进程。

## Consequences

- 宿主注入的 `Injector` 在 worker 里建（代码与内容同一份，ADR-0034）；agent 的命令隔离（最小环境、agent
  用户）在 worker 里照旧装上。
- 多一个常驻进程（只在有 Jiuwen 会话时拉起）；worker 崩溃时同时在线的 Jiuwen 会话一起结束，但 Claude 会话
  与接口不受影响。真实模型下的端到端还没有跑过（见 gaps）。
