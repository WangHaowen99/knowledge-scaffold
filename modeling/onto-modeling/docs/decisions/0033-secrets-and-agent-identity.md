# 0033 密钥移出 agent 可读范围，agent 以独立的非 root 用户和最小环境运行

## Context

方案第 0 章问题 5、E10（P1 提前）、第 6 章第 4、5 项：agent 以 root 运行，整个数据目录都能读：明文的
管理员模型 Key、个人 Key 的主密钥（能解密别人的 Key）、MCP token 库、服务令牌库。更严重的是两个引擎里
agent 自己执行的 bash 继承后端的完整环境：Claude 的 CLI 由 SDK 起，SDK 把后端的 `os.environ` 整个并进去；
Jiuwen 在进程内，命令执行直接拿 `os.environ`。agent 一条 `env` 就能看到 JWT 配置、auth-server 管理令牌、
主密钥。主密钥自动生成时和密文放在一起，备份里不能带它，异机恢复后个人 Key 解不开。

## Decision

1. **密钥目录**（`governance/secrets.py`）：管理员模型配置、个人 Key 密文、自动生成的主密钥、MCP token 库、
   服务令牌库放进 `ONTO_WEB_SECRETS_DIR`（默认数据目录下的 `secrets/`），目录 0700、文件 0600，归后端用户；
   启动时把数据目录根下的老文件（含 SQLite 伴生文件）搬进来。
2. **主密钥与密文分开**：要求配置 `ONTO_WEB_MODEL_SECRET_KEY`，没配时启动告警；备份只带密文，自动生成的
   主密钥在配置了 `ONTO_WEB_KEY_BACKUP_DIR` 时每次备份单独拷一份过去。
3. **最小环境**（`governance/agent_exec.py`，只用标准库，`bin/` 下两个薄壳）：
   - Claude：SDK 的 `cli_path` 指向 `onto-agent-cli`，它只留基础变量、模型网关配置（`ANTHROPIC_*`）和 CLI
     开关后 exec 真正的 CLI，并设 `CLAUDE_CODE_SHELL_PREFIX` 指向 `onto-agent-shell`，CLI 里的每条 bash
     与 hook 再经它去掉全部凭据类变量（包括模型 Key）；
   - Jiuwen：包一层 openjiuwen 的 `ShellOperation._create_subprocess`，命令改为经 `onto-agent-shell` 执行，
     环境换成同一份最小环境。
4. **独立用户**：镜像建 `onto-agent`（非 root），`ONTO_WEB_AGENT_USER` 指向它。后端仍以 root 运行（要能切
   用户），两个包装在 exec 前 `setgroups([agent 组])`、`setgid`、`setuid`、umask 007——不用 SDK 的 `user`
   参数，它只 setuid、保留 root 组。启动时（root 且配置了用户）：数据目录 0711（agent 只能穿过、不能列），
   工作区、Jiuwen 工件区、CLI 的 HOME 归 agent 组可写并 setgid；装配目录（项目 `.claude` 链到这里，含内核
   守卫）对 agent 只读（`share_read_only`，装配器每建一个装配目录也要调它）；数据目录其余条目去掉其他用户
   权限，后端进程 umask 改 007，原子写的新文件按 umask 给权限；镜像里给 git 配 `safe.directory`，agent 才能
   在归 root 的仓库里跑 git。
5. 引擎文件只做最小接入：Claude 拼选项时调 `agent_identity.claude_options`，Jiuwen 建 agent 时调
   `install_jiuwen_shell_isolation`。

## Alternatives considered

- **后端以非 root 运行**：切不到 agent 用户（要 CAP_SETUID），agent 就只能和后端同一个用户，读得到密钥。
- **只清后端自己的 `os.environ`**：CLI 调模型仍要 Key，环境最小化要在 CLI 与 bash 两层分别做，SDK 合并环境
  的行为也改不了，只能在 exec 前筛。
- **模型 Key 也不给 CLI，走后端代理**：属于网络隔离（E16，P3：模型网关走白名单代理），本阶段不做。

## Consequences

- agent 的 bash 里没有凭据变量，但 CLI 进程本身仍有当前会话用的模型 Key；同一用户的 agent 理论上能读
  CLI 进程的 `/proc/<pid>/environ`。彻底拿走要等 P3 的模型网关代理。
- 不同项目的 agent 是同一个系统用户，互相能读工作区；按项目隔离 agent 在 P4。
- 开发机（不是 root、没配用户）只做环境最小化，不切用户。
- Claude CLI 的 Bash 工具经 `CLAUDE_CODE_SHELL_PREFIX` 执行这一点按 CLI 文档实现，hook 与 MCP 走前缀已在
  CLI 代码里确认，Bash 工具需要用真实模型端到端确认一次。
