#!/usr/bin/env bash
# PreToolUse 内核守卫：禁止手工编辑 .ontology-state 下由工具管理的锁文件与脚本验证记录。
# 由内核注入装配目录，包删不掉（详设 4.8）。实现与绕过写法的处理见同目录 _guard_common.py；
# 解释器起不来时 exec 失败、非零退出，settings.json 里的 `|| exit 2` 把它变成拒绝。
exec python3 "$(dirname "${BASH_SOURCE[0]}")/_guard_common.py" lock
