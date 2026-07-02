"""Business data provider implementations.

Strategy A keeps the import path stable while graph providers live under
runtime/graph_search.
"""

from __future__ import annotations

from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "platform").is_dir() and (candidate / "runtime").is_dir():
            return candidate
    return start.parents[5]


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
_GRAPH_PROVIDER_DIR = (
    _REPO_ROOT / "runtime" / "graph_search" / "agentic" / "business_data" / "providers"
)
if _GRAPH_PROVIDER_DIR.is_dir():
    _path_str = str(_GRAPH_PROVIDER_DIR)
    if _path_str not in __path__:
        __path__.append(_path_str)

__all__: list[str] = []
