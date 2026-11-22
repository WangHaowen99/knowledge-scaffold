"""项目工作区（详设 4.9；方案 2.1「workspace.py 拆分归属」）。

- core：项目目录与元数据、锁与原子写、建项目与进度、改名归档、克隆、删除清扫、回收站、启动维护；
- files：文件树、读写、资料与用途、知识包目录规范化；
- versions：主线版本、标签、整树回退、历史版本文件；
- transfer：迁移包与工作区压缩包的导入导出、已发布知识包的反向导入、示例注入；
- gitops / layout / errors：git 操作、布局常量与错误类型（叶子模块）。

项目所有权在 services/ownership.py，发布在 services/publish，求解契约在 services/solver。
"""

from ..toolkit import script_contract as _script_contract
from .core import (  # noqa: F401
    WorkspaceCore,
    WorkspaceManager,
    remove_tree,
)
from .errors import NameConflictError, WorkspaceError  # noqa: F401
from .versions import ROLLBACK_EXCLUDED_PREFIXES, ROLLBACK_EXCLUDED_TOP  # noqa: F401
from .gitops import (  # noqa: F401
    STALE_GIT_LOCK_SECONDS,
    _changed_paths,
    _commit_paths,
    _git,
    _head_commit,
    _remove_stale_lock,
    _run_git,
    remove_git_locks,
)
from .layout import (  # noqa: F401
    IMPORTED_PACKAGE_DIR,
    META_FILE,
    PURGE_MARKER,
    RESOURCE_DIRECTIVES,
    RESOURCE_SUBDIRS,
    UNTRACK_COMMIT_MESSAGE,
    UNTRACKED_STATE_PATHS,
    _content_revision,
    _strip_frontmatter,
)

# 脚本契约以工作区工具箱脚本为单一事实源（toolkit.py），规则检查与发布共用
ScriptContractError = _script_contract.ScriptContractError
_script_header_meta = _script_contract.parse_script_contract
