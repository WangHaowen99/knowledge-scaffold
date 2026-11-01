# 0037 agent 提交收口：git 提交钩子在提交那一刻检查真实暂存区并写来源 trailer

## Context

详设 4.3「agent 提交收口」、P2 验收「写入都带来源 trailer」、方案 C11：agent 在工作区里自己 `git commit`，
提交说明不带 `Onto-Source` 等 trailer（版本列表把它们都猜成助手）；提交检查 `pre_commit_check.sh` 是
PreToolUse hook，在整条 bash 命令执行之前跑——`git add -A && git commit` 写在一行时它看到的是旧暂存区，
等于没查（B3a 遗留，gaps B3a 第 4 条）。变更集副本（C7）在 B4b，此前 agent 仍直接提交到主线。

## Decision

1. 内核发布两个 git 钩子（`app/kernel_guards/githooks/`，启动时复制到 `DATA_DIR/githooks`，agent 只读可执行），
   各项目的 `core.hooksPath` 指向它（启动恢复步骤、每次连接引擎前各确认一次）：
   - `pre-commit`：在提交那一刻跑内核守卫里的 `pre_commit_check.sh`（`ONTO_COMMIT_HOOK=1` 模式），看到的是
     真实暂存区；检查出错、脚本缺失、结果读不出来一律拒绝；
   - `commit-msg`：去掉 agent 自己写的 `Onto-*` 行，写 `Onto-Source: agent`、`Onto-Author`（项目所有者，取自
     引擎标记）、`Onto-Packs`（生效中的装配清单）、`Onto-Node`（当前执行的节点与版本，取自 `.onto/run/exec.json`）。
2. 钩子只管 agent 的提交：命令环境里有会话身份 `ONTO_RUN_ENGINE`（ADR-0034）。人工在终端里的提交不经它；
   后端自己的 git 命令一律带 `-c core.hooksPath=/dev/null`（`gitops._run_git`），写入事务用 commit-tree，都不经钩子。
3. PreToolUse 的 `pre_commit_check.sh` 改为：拒绝跳过提交检查的写法（`--no-verify`、`-n`、改 `core.hooksPath`、
   `GIT_CONFIG_*`）；钩子就绪（有会话身份、hooksPath 指向的目录里两个钩子可执行）时放行，检查交给 git 钩子；
   没就绪时退回在 PreToolUse 里检查（旧行为）。
4. MCP 写入不再因为 agent 有暂存改动回「项目忙」：写入事务用临时索引只提交本次路径，agent 的暂存原样留着
   （方案 C10）。「助手运行中整项目忙」的判断要换成写入范围锁，留给 B4b。

## Alternatives considered

- **提交检查搬进后端接口、脚本调本机 HTTP**（方案 C11 原文「脚本只调本机接口」）：agent 的命令以独立用户、
  最小环境运行，拿不到调用凭据；git 钩子在同一时刻、同一份暂存区上跑，效果相同，不需要新的鉴权面。
- **在 PreToolUse 里预测 `git add` 之后的暂存区**：要模拟 add 的路径规则与忽略文件，容易漏判。
- **trailer 只写在变更集的合入提交上**（方案 C11）：变更集在 B4b；在那之前 agent 的提交直接进主线，
  先在每个提交上写。变更集上线后合入提交另写一份 trailer，副本里的提交照旧带。

## Consequences

- 有意绕过（`unset ONTO_RUN_ENGINE`、直接 commit-tree）仍然防不住，防线与其他守卫一致：防误改与常见写法，
  真正改不了靠进程隔离（详设 4.8 第 4 层，P4 按项目隔离）。
- 钩子目录用绝对路径写进各项目的 `.git/config`：数据目录换了位置，启动恢复步骤会改写过来。
- 版本列表按 trailer 认出 agent 的提交（`Onto-Source: agent` → assistant），不再靠提交说明前缀猜。
