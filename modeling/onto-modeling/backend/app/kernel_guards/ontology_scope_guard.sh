#!/usr/bin/env bash
# PreToolUse 内核守卫：ontology/ 下不许自创目录、不许直接写求解契约受管目录，反馈演进期只能写会打包的目录。
# 由内核注入装配目录，包删不掉（详设 4.8）。实现与绕过写法的处理见同目录 _guard_common.py；
# 解释器起不来时 exec 失败、非零退出，settings.json 里的 `|| exit 2` 把它变成拒绝。
exec python3 "$(dirname "${BASH_SOURCE[0]}")/_guard_common.py" scope
