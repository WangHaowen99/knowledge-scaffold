"""对外暴露 MCP 服务：把本工作台的本体产物以只读工具集提供给外部 MCP 客户端。

移植自 knowledge scaffold consumer 的 MCP 接入模式（bearer token + streamable-http），
v1 为单端点只读工具集；token 仅存哈希与掩码，明文只在创建响应返回一次。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path

from .. import config

TOKEN_PREFIX = "omcp_"
MAX_DOC_CHARS = 20000
MAX_SEARCH_HITS = 50

TOOL_CATALOG = [
    {"name": "list_projects", "description": "列出全部建模任务（id 与名称）"},
    {"name": "list_elements", "description": "列出某任务的业务对象与业务逻辑名称"},
    {"name": "read_document", "description": "读取某任务的一份本体文档（markdown，上限 2 万字符）"},
    {"name": "search_documents", "description": "在某任务的产物中全文检索关键词（返回命中行）"},
]


def _store_path() -> Path:
    return Path(config.DATA_DIR) / "mcp-service-tokens.json"


def _load_tokens() -> list[dict]:
    path = _store_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save_tokens(entries: list[dict]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def list_tokens() -> list[dict]:
    return [
        {k: v for k, v in entry.items() if k != "hash"} for entry in _load_tokens()
    ]


def create_token(name: str, created_by: str = "") -> tuple[str, dict]:
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    entry = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or "未命名").strip()[:64],
        "hash": hashlib.sha256(plaintext.encode()).hexdigest(),
        "masked": plaintext[:10] + "…" + plaintext[-4:],
        "created_at": int(time.time()),
        "created_by": created_by,
    }
    entries = _load_tokens()
    entries.append(entry)
    _save_tokens(entries)
    return plaintext, {k: v for k, v in entry.items() if k != "hash"}


def revoke_token(token_id: str) -> bool:
    entries = _load_tokens()
    remaining = [e for e in entries if e["id"] != token_id]
    if len(remaining) == len(entries):
        return False
    _save_tokens(remaining)
    return True


def verify_token(plaintext: str) -> bool:
    if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
        return False
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    return any(secrets.compare_digest(e["hash"], digest) for e in _load_tokens())


def build_asgi_app():
    """构建 streamable-http 的 MCP ASGI 应用（挂载到 /mcp-service）。"""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    from .workspace import WorkspaceManager

    workspaces = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)
    server = MCPServer("onto-web")

    @server.tool(description=TOOL_CATALOG[0]["description"])
    def list_projects() -> list[dict]:
        return [
            {"id": p["id"], "name": p["name"]} for p in workspaces.list_projects()[:100]
        ]

    @server.tool(description=TOOL_CATALOG[1]["description"])
    def list_elements(project_id: str) -> dict:
        root = workspaces.project_root(project_id)
        result = {}
        for kind, rel in (("objects", "ontology/object"), ("logics", "ontology/logic")):
            directory = root / rel
            names = (
                sorted(f.stem for f in directory.glob("*.md"))
                if directory.is_dir()
                else []
            )
            result[kind] = names[:500]
        return result

    @server.tool(description=TOOL_CATALOG[2]["description"])
    def read_document(project_id: str, path: str) -> dict:
        data = workspaces.read_file(project_id, path, MAX_DOC_CHARS)
        return {"path": path, "content": data["content"], "truncated": data["truncated"]}

    @server.tool(description=TOOL_CATALOG[3]["description"])
    def search_documents(project_id: str, query: str) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        root = workspaces.project_root(project_id)
        hits = []
        for rel_dir in ("ontology", "research", "reports", "knowledge"):
            directory = root / rel_dir
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.md")):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for line_no, line in enumerate(lines, 1):
                    if query in line:
                        hits.append(
                            {
                                "path": str(path.relative_to(root)),
                                "line": line_no,
                                "text": line.strip()[:200],
                            }
                        )
                        if len(hits) >= MAX_SEARCH_HITS:
                            return hits
        return hits

    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
