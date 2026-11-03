# 0026 守卫基线由内核注入，出错即拒，补常见绕过写法，受保护目录四层保护

## Context

详设 4.8 实测了 `resource_write_guard`：`python3 -c "open(...,'w')"`、`cd resource && touch a.md`、
`find ... -delete`、`git checkout -- resource/`、`D=resource; touch $D/a.md`、非法 JSON 输入都放行；脚本出错
或超时也放行，Jiuwen 侧注释写明「守卫失效不阻断建模」。守卫写在资产包的 `settings.json` 里，上传新资产包
就能删掉；hook 命令用 `$(git rev-parse …)` 拼路径，脚本缺失时返回 127 被当成放行。默认权限模式是
`bypassPermissions`（详设 14#8 暂时保持），守卫是唯一的硬约束。方案 D8～D10 给出改法。

## Decision

1. **内核注入**（D8）：8 个 hook 脚本与权限、hooks 基线（`settings.base.json`）移到 `backend/app/kernel_guards/`，
   随镜像发布（H3），装配时复制进每个项目装配目录的 `scripts/` 与 `settings.json`。包里有同名文件即装配失败；
   包只能在清单里追加 PreToolUse 守卫，同样带 `|| exit 2`。hook 命令改用 `$CLAUDE_PROJECT_DIR`，每条守卫加
   `|| exit 2`（脚本缺失、解释器起不来也阻断）。权限 deny 补上 `README.md`、`.onto/**` 与 Write。
2. **出错即拒**（D9）：四个守卫是同一份 `_guard_common.py` 的一行包装。输入为空、不是 JSON、`tool_input`
   不是对象、守卫自身抛异常，一律输出 deny。`pre_commit_check.sh` 读不出命令、找不到项目根也阻断。Jiuwen
   执行守卫时超时、非零退出、输出不是 JSON 一律拒绝；不认识但名字像写文件的工具默认拒绝；hook 输入带
   `cwd`，环境里给 `CLAUDE_PROJECT_DIR`。
3. **补常见绕过写法**（D10），共享一个 Bash 解析：
   - `cd X && …`、`pushd X`，以及 hook 输入的 `cwd`（Bash 工具会保留上次 cd 的目录）；
   - `python/perl/node/ruby -c|-e` 与 heredoc 内联代码：出现受保护路径**且**有写操作才拒（读资料的
     `python -c` 很常见，不能一律拒）；`perl -pi` 按原地改写处理；
   - `find … -delete|-fprint*`、`-exec` 跟写命令（`-exec grep` 这类读操作放行）、`… | xargs rm`；
   - `git checkout/restore/rm/clean/mv` 带路径，`git -C 目录` 改变工作目录，`git clean` 不带路径按整个仓库；
   - 破坏性操作的目标是受保护目录的祖先（`rm -rf .`、`git checkout .`）或通配符能匹配到它（`rm -rf *`）；
   - 路径带变量：同一条命令里赋过值的先代入，代不出来且命令里出现受保护目录名就拒；
   - `bash -c` / `sh -c` / `eval` 里的命令串递归解析；`mv` 的源也算写；`dd of=`、`tar -x -C`、`unzip -d`、
     `curl -o`、`wget -O`。
   - `pre_commit_check` 认 `git -C`、`git -c`、绝对路径调用的提交。
   详设 4.8 的实测表与上述写法做成参数化用例 `tests/test_guard_bypass.py`。
4. **受保护目录四层**（详设 4.8 目标 5）。受保护目录：`resource/`（源材料）、`.claude` 与 `CLAUDE.md`、
   `README.md`（装配目录）、`.onto/`（能力包锁文件）、`.git/`；装配目录与包仓库的真实路径同样拦。
   1. hook：上面的守卫拦常规写法；
   2. 内容哈希：装配目录每个文件的哈希在 MANIFEST.json，包仓库每个版本有校验和；启动核对发现被改过就重装，
      装配前核对包仓库，后端执行的工作区脚本只认哈希对得上的那份；
   3. 写入路径：提交检查拦暂存的 `resource/`、`.claude`、`.onto`、`CLAUDE.md`、`README.md`；网页写接口本来
      就拒 `.onto`、`.claude`（合入时的检查随统一变更事务 C1、变更集 C8 收口）；
   4. 进程隔离：agent 真正改不了要靠密钥移出（E10）与按项目隔离（P4），本阶段没有。

## Alternatives considered

- **守卫留在 core 包里，只校验包里带没带**：包的作者仍能改守卫逻辑；内核注入后包根本碰不到。
- **内联代码只要出现受保护路径就拒**（方案 D10 原文）：会挡住大量读 Excel、读资料的 `python -c`，用户会看到
  莫名其妙的拒绝；加「有写操作」这个条件，误拒和漏拦之间取前者更少的一边。
- **`git add -A && git commit` 合在一条命令里时拒绝**：PreToolUse 看到的是旧暂存区，这样写等于没检查；但
  拒掉会改变现有命令的写法。方案把这一项放在提交接口（C11，P2），这里不动。

## Consequences

- 守卫变成 Python 实现，每次工具调用多起 4 个 Python 进程（与原来每个守卫内嵌 Python 相同）。
- 少数合法写法会被拒（如 `find . -name x -delete`、在内联代码里既读资料又写派生产物），提示里给出改法。
- 守卫仍防不住有意绕过（把代码写进脚本文件再跑、编码路径），这类只能靠第 4 层。
- 测试要用装配目录（`tests/pack_helpers.py` 的 `assemble_real`）才能跑到真实的 hook。
