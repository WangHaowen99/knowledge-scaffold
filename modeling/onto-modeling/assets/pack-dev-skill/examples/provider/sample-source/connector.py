"""样例数据源连接器：从「主机:端口」上的 HTTP 服务路径 /schema 读一份 JSON 表结构（只用标准库）。

服务返回：
{"tables": [{"name": "orders", "comment": "订单", "columns": [{"name": "id", "type": "bigint", "key": "pk"}, ...],
             "rows": [[1, ...], ...]}],
 "foreign_keys": [{"table": "orders", "columns": ["customer_id"], "ref_table": "customer", "ref_columns": ["id"]}]}

插件只经 ctx 往外发表和事实；遮蔽、截断、渲染成文本版都由网关做。
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from app.connector_gateway.source.sdk import SourceError

# 只连登记的主机，不走代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch(ctx) -> dict:
    ctx.throttle()
    url = urllib.parse.urlunsplit(("http", f"{ctx.config['host']}:{ctx.config['port']}", "/schema", "", ""))
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {ctx.secret['token']}"})
    try:
        with _OPENER.open(request, timeout=ctx.query_timeout_s()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SourceError("auth_failed" if exc.code in (401, 403) else "connect_failed", f"服务返回 {exc.code}") from None
    except (OSError, ValueError):
        # 消息里不写地址和令牌
        raise SourceError("connect_failed", "连不上服务") from None


def test(ctx):
    data = _fetch(ctx)
    ctx.check("连接", True)
    ctx.check("表结构", isinstance(data.get("tables"), list), "返回里没有 tables" if "tables" not in data else "")


def collect(ctx):
    data = _fetch(ctx)
    tables = [item for item in data.get("tables") or [] if ctx.in_scope(str(item.get("name") or ""))]
    for done, item in enumerate(tables, 1):
        ctx.table(item["name"], item.get("columns") or [], item.get("rows") or [], comment=item.get("comment") or "")
        ctx.progress(done, len(tables))
    names = {item["name"] for item in tables}
    for key in data.get("foreign_keys") or []:
        if key.get("table") in names and key.get("ref_table") in names:
            ctx.fact("foreign_key", table=key["table"], columns=key["columns"], ref_table=key["ref_table"],
                     ref_columns=key["ref_columns"], name=key.get("name") or "")
    if not tables:
        ctx.limitation("范围里没有表")
