# 0071 内测引擎的文件工具只读访问助手副本里的资料链接

## Context

助手在变更集副本里干活（方案 C7）。资料区受保护、不进变更集，副本里的 `resource` 是指向主线资料区的符号链接
（`kernel/changesets/worktree._ensure_links`）。内测引擎的文件沙箱只有三根：项目区（副本）、私有工件区、装配目录；
openjiuwen 按 resolve 之后的路径判定，链接目标在主线工作区，于是 read_file、list、glob 读 `resource/` 都报
「Path … outside sandbox」，Grep 拼出的命令也因引用了沙箱外的绝对路径被拒。真实模型的资料分析一开始就读不了资料，
助手改用 bash 跑脚本才读到（测试部署现场容器日志 9 次越界）。

## Decision

在建 agent 之前（`JiuwenEngine._build_agent`）包一层框架（`engines/jiuwen_sandbox.install`，幂等，和
`agent_identity.install_jiuwen_shell_isolation` 同样的做法）：

- 只在读的时候放行：`FsOperation` 的 `read_file`、`list_files`、`list_directories`、`search_files` 与 `GrepTool.invoke`
  执行期间置一个上下文标记；`write_file`、上传、其他命令不置。
- 放行的只有一个目录：沙箱第一根是助手副本、副本顶层 `resource` 是链接、而且 resolve 后正是这个副本对应的主线资料区
  （`worktree.main_root(副本)/resource`）。路径 resolve 后落在这个目录里才放；资料区里指向别处的链接 resolve 后在外面，
  照旧越界；副本的 `resource` 被换成指向别处的链接时一概不放。
- 框架原来的判定先跑，不越界的照旧；越界、正在读、落在上面那个目录里才改成放行。建上级目录的写一律不放。

## Alternatives considered

- **把主线资料区加进沙箱根**：文件写和命令执行的越界判定也一起放开了，写资料区只剩守卫一层。
- **副本里放一份资料的拷贝或硬链接**：资料可能很大，各副本不一致；V 里助手自己这样做过，被守卫挡掉。
- **注册一个自己的 fs 操作类替换框架的**：同样依赖框架的内部结构，还要复制读写各方法，包一层改动更小。

## Consequences

- 依赖 openjiuwen 0.1.20 的内部方法（`FsOperation._resolve_path`、`ShellOperation._check_shell_sandbox`、
  `_extract_abs_paths`、`_get_sandbox_roots`）；升级框架时 `test_f2_jiuwen_resource_read.py` 会先报。
- glob、list 返回的是主线资料区的绝对路径，助手拿这个路径去写仍会被拒。
- 稳定引擎不受影响（不走 openjiuwen 的沙箱）。
