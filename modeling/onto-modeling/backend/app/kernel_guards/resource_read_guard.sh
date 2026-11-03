#!/usr/bin/env bash
# PreToolUse 内核守卫：不在主容器里解析资料原件（PDF、Word、Excel、PPT、图片等），改读数据接入转出的文本版
# （F4c，ADR-0076）。读文件工具（稳定引擎 Read、内测引擎 read_file）和 Bash 里的解析库、转换命令都过它。
# 实现见同目录 _guard_common.py；解释器起不来时 exec 失败、非零退出，settings.json 里的 `|| exit 2` 把它变成拒绝。
exec python3 "$(dirname "${BASH_SOURCE[0]}")/_guard_common.py" resource_read
