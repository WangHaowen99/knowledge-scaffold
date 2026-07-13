"""Standalone FastMCP server for MetaWise search/chat toolsets."""

from __future__ import annotations

import argparse
import inspect
import os
import warnings
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Any, Callable, Mapping

from pydantic import Field

from .chat_adapter import execute_chat_tool
from .config import MCPRuntimeConfig, load_mcp_runtime_config, validate_mcp_runtime_config
from .scope import MCPRequestScope, auth_error_payload, extract_bearer_token, resolve_mcp_scope_from_token
from .search_adapter import execute_search_tool
from .service_catalog import load_mcp_service_catalog

StringListInput = list[str] | str | None
DictInput = dict[str, Any] | str | None
DictListInput = list[dict[str, Any]] | str
PathPatternInput = list[Any] | str


def create_mcp_server(config: MCPRuntimeConfig | None = None, *, toolset: str = "search") -> Any:
    cfg = config or load_mcp_runtime_config()
    validate_mcp_runtime_config(cfg)
    FastMCP = _load_fastmcp()
    normalized_toolset = _normalize_toolset(toolset)
    mcp = FastMCP("meta-wise-{}".format(normalized_toolset))

    def _with_scope(required_scope: str, caller: Callable[[MCPRequestScope], dict[str, Any]]) -> dict[str, Any]:
        try:
            scope = _resolve_scope(cfg, required_scope=required_scope)
        except Exception as exc:
            from .token_service import MCPTokenAuthError

            if isinstance(exc, MCPTokenAuthError):
                return auth_error_payload(exc)
            return {
                "ok": False,
                "error": {
                    "type": "auth_error",
                    "error_code": "INVALID_MCP_TOKEN",
                    "detail": str(exc),
                    "retriable": False,
                },
            }
        return caller(scope)

    if normalized_toolset in {"search", "all"}:
        _register_search_tools(mcp, call_with_scope=_with_scope)
    if normalized_toolset in {"chat", "all"}:
        _register_chat_tool(mcp, call_with_scope=_with_scope)
    return mcp


def _register_search_tools(mcp: Any, *, call_with_scope: Callable[[str, Callable[[MCPRequestScope], dict[str, Any]]], dict[str, Any]]) -> None:
    @mcp.tool()
    def get_object_types(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
    ) -> dict[str, Any]:
        """获取当前本体绑定图数据中的全部实体/节点类型列表。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("get_object_types", {"ontology_id": ontology_id}, scope=scope))

    @mcp.tool()
    def get_relation_types(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
    ) -> dict[str, Any]:
        """获取当前本体绑定图数据中的全部语义关系类型列表。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("get_relation_types", {"ontology_id": ontology_id}, scope=scope))

    @mcp.tool()
    def get_nodes_schema(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        element_types: Annotated[StringListInput, Field(description='可选节点类型数组，示例 ["Company", "Person"]；也兼容合法 JSON 字符串。')] = None,
    ) -> dict[str, Any]:
        """获取节点类型及其属性字段 schema；只返回结构，不返回实例数据。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("get_nodes_schema", {"ontology_id": ontology_id, "element_types": element_types}, scope=scope))

    @mcp.tool()
    def get_edges_schema(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        relation_types: Annotated[StringListInput, Field(description='可选关系类型数组，示例 ["包含", "影响"]；也兼容合法 JSON 字符串。')] = None,
    ) -> dict[str, Any]:
        """获取关系 schema 和 relation_triples；路径查询前应优先查看关系三元组。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("get_edges_schema", {"ontology_id": ontology_id, "relation_types": relation_types}, scope=scope))

    @mcp.tool()
    def property_filter(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        queries: Annotated[DictListInput, Field(description="属性过滤子查询数组；也兼容合法 JSON 字符串。")],
        max_total_rows: Annotated[int, Field(ge=1, le=5000, description="最大返回行数。")] = 500,
        entity: Annotated[str, Field(description="可选本体对象名，用于解析映射。")] = "",
    ) -> dict[str, Any]:
        """按属性过滤节点/关系。单个 filter_dict 内多个字段表示 AND；queries 表示多个独立子查询。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("property_filter", {"ontology_id": ontology_id, "entity": entity, "queries": queries, "max_total_rows": max_total_rows}, scope=scope))

    @mcp.tool()
    def pattern_search(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        path_pattern: Annotated[PathPatternInput, Field(description="节点-关系-节点路径模板；必须是一条路径的数组，也兼容合法 JSON 字符串。")],
        return_vars: Annotated[StringListInput, Field(description='返回变量列表，例如 ["a", "r1", "b"]；也兼容合法 JSON 字符串。')] = None,
        return_properties: Annotated[Any | None, Field(description="返回属性投影。")] = None,
        limit: Annotated[int, Field(ge=1, le=2000, description="返回条数上限。")] = 100,
        entity: Annotated[str, Field(description="可选本体对象名，用于解析映射。")] = "",
    ) -> dict[str, Any]:
        """按图结构路径匹配子图数据。

        适用场景：用户描述了明确实体关系路径，例如 A 类型节点经过 R 关系连接到 B 类型节点。
        建议先调用 get_edges_schema，参考 relation_triples 构造 path_pattern。

        path_pattern 固定骨架：
        [[节点选择器, 节点属性?], [关系选择器, 方向], [节点选择器, 节点属性?], ...]

        规则：
        - 每次只描述一条路径，不支持 [[path1], [path2]] 这种批量路径数组。
        - 一跳路径必须正好包含起点节点段、关系段、终点节点段 3 个顶层元素。
        - 每个节点段和关系段都必须是数组，不能把对象或字符串直接放在 path_pattern 顶层。
        - 方向只支持 "->"、"<-"、"--"；不关心方向时使用 "--"。
        - relation_triples 中 source_label=S, relation_type=R, target_label=T 表示 S -[R]-> T。
          从 S 查 T 用 [R, "->"]；从 T 查回 S 用 [R, "<-"]。
        - return_vars 按当前路径自动编号，一跳路径只有 a、r1、b。
        """
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("pattern_search", {"ontology_id": ontology_id, "entity": entity, "path_pattern": path_pattern, "return_vars": return_vars, "return_properties": return_properties, "limit": limit}, scope=scope))

    @mcp.tool()
    def count_search(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        queries: Annotated[DictListInput, Field(description="计数子查询数组；也兼容合法 JSON 字符串。")],
    ) -> dict[str, Any]:
        """统计满足条件的节点或关系数量，支持批量子查询。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("count_search", {"ontology_id": ontology_id, "queries": queries}, scope=scope))

    @mcp.tool()
    def aggregate_search(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        element_class: str,
        element_type: str,
        target_property: str,
        agg: str,
        filter_dict: DictInput = None,
    ) -> dict[str, Any]:
        """对节点或关系字段做 SUM/AVG/MIN/MAX/COUNT 聚合，字段名应来自 schema。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("aggregate_search", {"ontology_id": ontology_id, "element_class": element_class, "element_type": element_type, "target_property": target_property, "agg": agg, "filter_dict": filter_dict or {}}, scope=scope))

    @mcp.tool()
    def sorted_search(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        element_class: str,
        element_type: str,
        filter_dict: DictInput = None,
        return_properties: StringListInput = None,
        sort_by: StringListInput = None,
        ascending: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """查询节点或关系并按字段排序，适合最大/最小/最新/排名类问题。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("sorted_search", {"ontology_id": ontology_id, "element_class": element_class, "element_type": element_type, "filter_dict": filter_dict or {}, "return_properties": return_properties, "sort_by": sort_by, "ascending": ascending, "limit": limit, "offset": offset}, scope=scope))

    @mcp.tool()
    def get_element_info(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        element_instance_id: str,
        element_class: str = "",
        element_type: str = "",
    ) -> dict[str, Any]:
        """按节点或关系实例 ID 查询单个元素的完整详情。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("get_element_info", {"ontology_id": ontology_id, "element_instance_id": element_instance_id, "element_class": element_class, "element_type": element_type}, scope=scope))

    @mcp.tool()
    def hop_search(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        entity_instance_id: str,
        hop_num: int = 1,
        accurate_flag: bool = False,
    ) -> dict[str, Any]:
        """从指定节点实例出发查询 N 度邻域；entity_instance_id 应来自其他检索工具返回。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("hop_search", {"ontology_id": ontology_id, "entity_instance_id": entity_instance_id, "hop_num": hop_num, "accurate_flag": accurate_flag}, scope=scope))

    @mcp.tool()
    def shortest_path(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        start_label: str,
        start_props: DictInput,
        end_label: str,
        end_props: DictInput = None,
        max_depth: int = 15,
        directed: bool = False,
    ) -> dict[str, Any]:
        """查找两个节点之间的最短关联路径，start_props/end_props 用于定位起止节点。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("shortest_path", {"ontology_id": ontology_id, "start_label": start_label, "start_props": start_props, "end_label": end_label, "end_props": end_props or {}, "max_depth": max_depth, "directed": directed}, scope=scope))

    @mcp.tool()
    def list_logic_titles(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        entity_type: Annotated[str, Field(description="可选业务实体类型过滤。")] = "",
    ) -> dict[str, Any]:
        """列出当前本体绑定图数据中的业务逻辑标题目录。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("list_logic_titles", {"ontology_id": ontology_id, "entity_type": entity_type}, scope=scope))

    @mcp.tool()
    def get_logic_content(
        ontology_id: Annotated[str, Field(description="本体 ID，必填。")],
        entity_instance_ids: Annotated[list[str] | str, Field(description="业务实体实例 ID 数组；也兼容合法 JSON 字符串。")],
        logic_titles: Annotated[StringListInput, Field(description="可选业务逻辑标题数组；也兼容合法 JSON 字符串。")] = None,
    ) -> dict[str, Any]:
        """按实体实例 ID 和可选标题读取业务逻辑内容。"""
        return call_with_scope("mcp:search", lambda scope: execute_search_tool("get_logic_content", {"ontology_id": ontology_id, "entity_instance_ids": entity_instance_ids, "logic_titles": logic_titles or []}, scope=scope))


def _register_chat_tool(mcp: Any, *, call_with_scope: Callable[[str, Callable[[MCPRequestScope], dict[str, Any]]], dict[str, Any]]) -> None:
    @mcp.tool()
    def chat(
        query: Annotated[str, Field(description="用户问题。")],
        session_id: Annotated[str, Field(description="可选会话 ID。")] = "",
        model_profile_id: Annotated[str, Field(description="可选模型配置 ID。")] = "",
        arguments: Annotated[dict[str, Any] | None, Field(description="可选结构化参数。")] = None,
    ) -> dict[str, Any]:
        """与 MetaWise 主 Agent 进行单轮知识问答；需要确定性图数据时优先使用知识检索工具。"""
        args = {
            "query": query,
            "session_id": session_id,
            "model_profile_id": model_profile_id,
            "arguments": arguments or {},
        }
        return call_with_scope("mcp:chat", lambda scope: execute_chat_tool(args, scope=scope))


def _resolve_scope(cfg: MCPRuntimeConfig, *, required_scope: str) -> MCPRequestScope:
    headers = _get_headers_best_effort()
    token = extract_bearer_token(headers) or os.getenv("MCP_BEARER_TOKEN", "").strip()
    return resolve_mcp_scope_from_token(token, required_scope=required_scope)


def _get_headers_best_effort() -> Mapping[str, str]:
    get_http_headers = _load_get_http_headers()
    if get_http_headers is None:
        return {}
    for kwargs in ({"include_all": True}, {"include": {"authorization"}}, {}):
        try:
            headers = get_http_headers(**kwargs)
            if isinstance(headers, Mapping):
                return {str(k).lower(): str(v) for k, v in headers.items()}
        except TypeError:
            continue
        except Exception:
            return {}
    return {}


def create_mcp_http_root_app(config: MCPRuntimeConfig | None = None, *, transport: str | None = None):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    cfg = config or load_mcp_runtime_config()
    normalized_transport = _normalize_http_transport(transport or cfg.transport)
    endpoint_path = "/sse" if normalized_transport == "sse" else "/mcp"
    mounted_apps = []
    routes = []
    for item in load_mcp_service_catalog():
        server = create_mcp_server(cfg, toolset=item.toolset)
        subapp = server.http_app(path="/", transport=normalized_transport)
        mounted_apps.append(subapp)
        routes.append(Mount(
            "{}{}".format(item.endpoint_prefix, endpoint_path),
            app=subapp,
            name="{}-{}".format(item.service_id, normalized_transport),
        ))

    async def health(_request):
        return JSONResponse({"status": "ok"})

    routes.append(Route("/health", health, methods=["GET"]))

    @asynccontextmanager
    async def lifespan(_: Any):
        async with AsyncExitStack() as stack:
            for app in mounted_apps:
                await stack.enter_async_context(app.router.lifespan_context(app))
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def _normalize_http_transport(value: str | None) -> str:
    transport = str(value or "streamable-http").strip().lower()
    if transport == "http":
        return "streamable-http"
    if transport in {"streamable-http", "sse"}:
        return transport
    raise ValueError("HTTP 模式不支持 transport={!r}".format(value))


def run_server(config: MCPRuntimeConfig | None = None) -> None:
    import uvicorn

    cfg = config or load_mcp_runtime_config()
    app = create_mcp_http_root_app(cfg, transport=cfg.transport)
    uvicorn.run(app, host=cfg.server_host, port=cfg.server_port)


def _normalize_toolset(toolset: str | None) -> str:
    value = str(toolset or "search").strip().lower() or "search"
    if value not in {"search", "chat", "all"}:
        raise ValueError("未知 toolset: {}".format(toolset))
    return value


def _load_fastmcp():
    _suppress_fastmcp_authlib_deprecation_warning()
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("未安装 fastmcp，请先安装后启动 MCP Server") from exc
    return FastMCP


def _load_get_http_headers():
    _suppress_fastmcp_authlib_deprecation_warning()
    try:
        from fastmcp.server.dependencies import get_http_headers
    except Exception:  # pragma: no cover - depends on fastmcp version
        return None
    return get_http_headers


def _suppress_fastmcp_authlib_deprecation_warning() -> None:
    """Suppress a FastMCP dependency warning until FastMCP migrates off authlib.jose."""
    try:
        from authlib.deprecate import AuthlibDeprecationWarning
    except Exception:  # pragma: no cover - depends on optional dependency internals
        warning_category = Warning
    else:
        warning_category = AuthlibDeprecationWarning

    warnings.filterwarnings(
        "ignore",
        message=r"authlib\.jose module is deprecated.*",
        category=warning_category,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MetaWise MCP Server")
    parser.add_argument("--transport", default="", choices=["", "streamable-http", "sse", "stdio"])
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--toolset", default="all", choices=["search", "chat", "all"])
    args = parser.parse_args()
    cfg = load_mcp_runtime_config()
    if args.host or args.port or args.transport:
        cfg = MCPRuntimeConfig(
            **{
                **cfg.__dict__,
                "server_host": args.host or cfg.server_host,
                "server_port": args.port or cfg.server_port,
                "transport": args.transport or cfg.transport,
            }
        )
    if cfg.transport == "stdio":
        server = create_mcp_server(cfg, toolset=args.toolset)
        run_fn = server.run
        params = inspect.signature(run_fn).parameters
        kwargs = {"transport": "stdio"} if "transport" in params else {}
        run_fn(**kwargs)
        return
    run_server(cfg)


if __name__ == "__main__":
    main()
