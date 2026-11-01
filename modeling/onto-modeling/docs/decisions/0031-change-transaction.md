# 0031 统一变更事务 ChangeTx：拿锁、校验版本、写入、派生、只提交本次路径并带来源 trailer

## Context

详设 4.3 与方案 C1、C2、C6：网页保存、Studio、MCP、导入各走一条路。`write_file` 是非原子写；MCP 用
`commit --only`；`revert_to`、`seed_example` 用 `git add -A` 把 agent 还没提交的改动一起提交；`tag_version`
不拿锁；目录 `index.md` 由读接口顺带刷新；提交里没有来源，版本列表只能按提交说明前缀猜；网页或 MCP
写完文件没来得及提交就停机，改动会在同一文件下次保存时被一起提交。

## Decision

1. **`app/kernel/writepath/tx.py` 的 ChangeTx** 是唯一的写入事务：
   - `__enter__` 拿项目锁（同一线程可嵌套，文件锁只在最外层加），记下基准 HEAD；
   - `check_revision` 校验内容版本号（不符 `409 revision_conflict`，details 带谁、何时改的）；
   - `write` / `remove` 原子写入或删除，`touch` 登记由脚本改动的路径；第一次改动前写一条写入意图
     （`DATA_DIR/writepath/intents/`）；异常时按快照恢复；
   - `commit` 先刷新派生文件（被改动目录的 `index.md`，进同一个提交），再只提交本次路径：在临时索引上
     从 HEAD 构造提交、提交说明末尾写 trailer、`update-ref` 比对旧 HEAD 后移动（agent 在中间提交了就基于
     新 HEAD 重做），最后把真实索引里这几个路径对齐到新提交。真实索引被别的 git 操作占着时宁可不提交
     （对不齐索引，agent 下一次提交会把旧内容带回去）；
   - 提交成功后发 `workspace.changed`（`kernel/writepath/events.py`，进程内订阅）。
2. **trailer**：`Onto-Source`（agent、web、studio、mcp、autobuild、import、plugin、revert）、`Onto-Author`，
   有值时加 `Onto-Changeset`、`Onto-Node`；`Onto-Packs` 由插件运行时经 `register_trailer_provider` 提供。
3. **入口**：网页保存（F-03）、Studio 提交（ST-08，来源 studio）、MCP 写入、资料上传 / 用途 / 删除、知识条目
   （K-03～K-05）、求解契约保存、整树回退（V-06）、示例注入都改走 ChangeTx。导入、克隆、建项目是新仓库的
   整仓提交，只在提交说明上盖 trailer（`stamped`）。旧的 `_commit_paths` 保留为兼容入口，内部走同一个提交
   实现，按说明前缀补来源。
4. **回退与示例注入只提交相关路径**：回退取目标版本与 HEAD 之间不同的路径，恢复这些、删掉之后新增的，
   只提交它们；示例注入比较脚本运行前后的未提交路径，只提交新出现的。agent 未提交的改动不再被卷进去。
5. **崩溃恢复**：启动自检（O-35）在清残留锁之后处理写入意图，已落盘没提交的补提交（`recover:` 说明，
   原来源），报告项 `write_intents`；`lifecycle` 记下上次是否正常退出，填 `last_exit`。
6. **扩展点**：提交目标是 `ChangeTarget`（`kind`、`root`、`head()`、`commit()`）。P1 只有 `MainlineTarget`；
   P2 的变更集副本实现同一接口，事务代码不变。

## Alternatives considered

- **继续用 `git commit -- paths`**：会和 agent 抢真实索引的锁，HEAD 在读和写之间被 agent 移动时也没有比对；
  临时索引 + `update-ref` 的写法在「停止跟踪元数据」的迁移里已经用过。
- **写入意图里存全文以便回滚**：资料可能很大；文件本身是原子写，停机后盘上的内容就是要保存的内容，
  补提交即可。
- **派生文件仍由读接口刷新**：读接口有写副作用、无锁提交（方案 C6 指出的问题）。

## Consequences

- 网页保存新条目时 `index.md` 跟着进同一个提交；版本列表的来源从 trailer 读，不再靠前缀猜（存量提交仍按前缀）。
- agent 自己的提交还没收口（C11，P2），仍不带 trailer；版本列表把没有 trailer 的提交记为助手。
- MCP 写入仍在 agent 有暂存改动时返回 `PROJECT_EDITING_BUSY`（整项目忙的判断随写入范围锁 C10 在 P2 删除）。
