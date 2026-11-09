#!/usr/bin/env bash
# PreToolUse 守卫（ADR-0087）：插件开发对话只放行写项目根下 plugins/ 里的草稿，项目里别的路径、数据目录下的
# 其他项目一律拒绝。只挂在插件开发装配目录的 settings.json 里，建模的装配不引用它。
# 实现见同目录 _guard_common.py；解释器起不来时 exec 失败、非零退出，settings.json 里的 `|| exit 2` 把它变成拒绝。
exec python3 "$(dirname "${BASH_SOURCE[0]}")/_guard_common.py" plugin_scope
