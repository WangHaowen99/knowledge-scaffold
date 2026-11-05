"""连接型提供方的清单（证据采集接口 §4 的 ProviderInfo + §8）：网关进程列 `/v1/providers` 用，不导入驱动。"""

from __future__ import annotations

_DB_FIELDS = (
    {"name": "host", "label": "主机", "secret": False},
    {"name": "port", "label": "端口", "secret": False},
    {"name": "database", "label": "库名", "secret": False},
    {"name": "username", "label": "用户名", "secret": False},
    {"name": "password", "label": "密码", "secret": True},
)

_COMMON = {
    "version": "0.1.0", "runtime": "subprocess", "output_format": "evidence-snapshot/1", "input": "connection",
    "self_service": False, "max_artifact_bytes": None,
}

PROVIDERS: dict[str, dict] = {
    "db-mysql": {
        **_COMMON, "id": "db-mysql", "label": "MySQL", "kind": "db", "scheme": "db", "module": "db_mysql",
        "drivers": [{"module": "pymysql", "dist": "PyMySQL"}], "executables": [],
        "evidence_types": ["entity", "field", "relation", "constraint", "operation", "rule", "profile", "sample"],
        "capabilities": ["discover", "snapshot", "sample"], "cred_fields": list(_DB_FIELDS),
        "readonly_method": "只读账号 · 只读会话 · 语句白名单", "default_port": 3306,
        "anchor_rule": "db:<源>/<库>.<表>[.<列>]，按库、表、列名生成，名字不变锚点不变",
        "license_note": "驱动 PyMySQL（MIT）",
    },
    "db-postgres": {
        **_COMMON, "id": "db-postgres", "label": "PostgreSQL", "kind": "db", "scheme": "db", "module": "db_postgres",
        "drivers": [{"module": "pg8000", "dist": "pg8000"}], "executables": [],
        "evidence_types": ["entity", "field", "relation", "constraint", "operation", "rule", "profile", "sample"],
        "capabilities": ["discover", "snapshot", "sample"], "cred_fields": list(_DB_FIELDS),
        "readonly_method": "只读账号 · 只读会话 · 语句白名单", "default_port": 5432,
        "anchor_rule": "db:<源>/<schema>.<表>[.<列>]，按 schema、表、列名生成，名字不变锚点不变",
        "license_note": "驱动 pg8000（BSD-3-Clause）",
    },
    "code-repo": {
        **_COMMON, "id": "code-repo", "label": "代码仓库", "kind": "code", "scheme": "code", "module": "code_repo",
        "drivers": [{"module": "javalang", "dist": "javalang"}], "executables": ["git"],
        "evidence_types": ["entity", "field", "relation", "operation", "rule"],
        "capabilities": ["snapshot"],
        "cred_fields": [
            {"name": "repo_url", "label": "仓库地址", "secret": False},
            {"name": "ref", "label": "分支或提交", "secret": False},
            {"name": "token", "label": "访问令牌", "secret": True},
        ],
        "readonly_method": "只读令牌 · 按提交浅克隆 · 不执行代码", "default_port": 443,
        "anchor_rule": "code:<源>/sym:<限定名>，按符号生成；没有符号时 code:<源>/file:<路径>#L<行>",
        "license_note": "Java 解析用 javalang（MIT）",
    },
}

# 不出现在 /v1/providers 里的内部字段。drivers：网关只用 find_spec 看装没装、读包元数据取版本，不导入；
# executables：子进程审计钩子只放行这些可执行文件（数据库类一个都不放）
INTERNAL = ("scheme", "module", "default_port", "drivers", "executables")


def describe(provider_id: str, *, installed: bool = True) -> dict:
    info = PROVIDERS[provider_id]
    return {**{key: value for key, value in info.items() if key not in INTERNAL}, "installed": installed}


def scheme_of(provider_id: str) -> str:
    return PROVIDERS[provider_id]["scheme"]


def module_of(provider_id: str) -> str:
    """`module` 写完整模块路径（带点）时原样用，测试的假提供方就这样指过去。"""
    module = PROVIDERS[provider_id]["module"]
    return module if "." in module else f"app.connector_gateway.conn.{module}"
