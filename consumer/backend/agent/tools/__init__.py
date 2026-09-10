"""Tools for LLM-based planning and execution."""

import importlib as _importlib
import sys as _sys

from agent.tools.business_data_search import (
    BUSINESS_DATA_SEARCH_TOOL_SCHEMA,
    handle_business_data_search,
)
from agent.tools.execute_script import EXECUTE_SCRIPT_TOOL_SCHEMA, handle_execute_script
from agent.tools.memory import MEMORY_TOOL_SCHEMA, handle_memory
from agent.tools.ontology_context import ONTOLOGY_CONTEXT_TOOL_SCHEMA, handle_ontology_context
from agent.tools.report_business_checkpoint import (
    REPORT_BUSINESS_CHECKPOINT_TOOL_SCHEMA,
    ReportBusinessCheckpointTool,
)
from agent.tools.skill import SKILL_TOOL_SCHEMA, handle_skill
from agent.tools.spawn_sub_agent import (
    SPAWN_SUB_AGENT_SCHEMA,
    handle_spawn_sub_agent_async,
)
from agent.tools.web_search_tool import WEB_SEARCH_TOOL_SCHEMA, handle_web_search

__all__ = [
    "handle_business_data_search",
    "BUSINESS_DATA_SEARCH_TOOL_SCHEMA",
    "handle_execute_script",
    "EXECUTE_SCRIPT_TOOL_SCHEMA",
    "handle_ontology_context",
    "ONTOLOGY_CONTEXT_TOOL_SCHEMA",
    "handle_skill",
    "SKILL_TOOL_SCHEMA",
    "handle_memory",
    "MEMORY_TOOL_SCHEMA",
    "handle_spawn_sub_agent_async",
    "SPAWN_SUB_AGENT_SCHEMA",
    "handle_web_search",
    "WEB_SEARCH_TOOL_SCHEMA",
    "REPORT_BUSINESS_CHECKPOINT_TOOL_SCHEMA",
    "ReportBusinessCheckpointTool",
]

# A cyclical import through the native-async runtime (agent.runtime.service
# imports agent.tools.events) can leave these submodules cached in
# sys.modules while this package is still initializing, so importlib skips
# binding the package attribute (e.g. agent.tools.execute_script). Rebind the
# affected submodules explicitly so dotted monkeypatch targets and attribute
# access stay resolvable regardless of import order.
for _submodule_name in ("business_data_search", "execute_script"):
    _submodule = _importlib.import_module("{}.{}".format(__name__, _submodule_name))
    _sys.modules[__name__].__dict__[_submodule_name] = _submodule
del _submodule, _submodule_name, _importlib, _sys
