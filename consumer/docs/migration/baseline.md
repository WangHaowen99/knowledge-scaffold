# Consumer 迁移冻结基线

记录日期：2026-07-13（Asia/Shanghai）

## 目标仓基线

- 工作仓：`/Users/example/vscodePo/meta-wise`
- 迁移分支：`codex/consumer-main-full-migration`
- 基线分支：当前本地 `develop`
- 基线 commit：`1d07c1ee4905b89843b5158e5bbedd91fa3b9537`
- tree：`ff2fed75ce37093408796f992df788fd2cd4110e`
- commit 时间：`2026-07-13T12:00:43+08:00`
- commit 主题：`!20 merge sync/agent_eval-version1.0 into develop`

用户已明确冻结当前本地 `develop`。从该指令起禁止 fetch/pull `origin/develop`，也禁止把远端跟踪分支的新提交带入迁移。当前本地已有的 `origin/develop@40bb77adc5921eb20d70f79b5c83a0c604a077a8` 只记录为排除项，不是本次基线。

## 本地源码基线

- 来源仓：`/Users/example/vscodePo/onto-agent`
- 来源分支：`main`
- 基线 commit：`31fba9db4bd7103f4d3ab54da8f3031e50cef4d2`
- tree：`987e7562670cf62d3554a3963afe367bf19d73dd`
- commit 时间：`2026-07-10T16:28:06+08:00`
- commit 主题：`feat(frontend): resume runs through SSE cursors`
- 隔离快照：`/tmp/onto-agent-main-source-31fba9db`，detached HEAD

隔离快照由本地仓克隆并检出固定 commit，不读取来源工作区的未提交文件。迁移过程中只读使用该快照。

## 清单规模

| 清单 | 数量 |
|---|---:|
| meta-wise tracked files | 1470 |
| onto-agent main tracked files | 455 |
| meta-wise Python route declarations | 118 |
| onto-agent main Python route declarations | 114 |
| meta-wise test files | 215 |
| onto-agent main test files | 96 |

完整清单位于 `consumer/docs/migration/inventories/`。

## 工具版本

- Python：`3.11.13`
- Node.js：`v22.17.0`
- npm：`10.9.2`
- Docker CLI：`29.3.1`（build `c2be9cc`）
- Git：`2.50.1`

Phase 0 基线测试复用 `/Users/example/vscodePo/onto-agent/venv/bin/python`；该解释器包含当前测试所需依赖，不把虚拟环境写入目标仓。

## Git 提交身份

该仓库 local config 已固定为：

- `user.name=FighterInTheSun`
- `user.email=sunchengwu@example-brand.com`

每次提交前后都必须校验 author 和 committer。不得修改全局 Git 身份。

## 复核命令

```bash
git -C /Users/example/vscodePo/meta-wise rev-parse develop
git -C /Users/example/vscodePo/meta-wise rev-parse HEAD
git -C /Users/example/vscodePo/meta-wise merge-base --is-ancestor develop HEAD
git -C /tmp/onto-agent-main-source-31fba9db rev-parse HEAD
git -C /Users/example/vscodePo/meta-wise config --local --get user.name
git -C /Users/example/vscodePo/meta-wise config --local --get user.email
```

禁止把上述命令改成读取或刷新 `origin/develop`。
