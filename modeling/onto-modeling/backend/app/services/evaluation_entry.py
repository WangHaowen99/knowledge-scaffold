"""评测服务在浏览器中的入口地址，只由部署配置 ONTO_WEB_EVAL_PUBLIC_URL 决定。

该地址会收到用户登录（经跨源窗口消息交接），因此不提供页面修改入口。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .. import config

EVALUATION_PATH = "/evaluation/"


def normalize(value: str) -> str:
    """空串由浏览器按建模页面同主机的 5180 端口推导；其余须为不含凭据的 http(s) 地址。"""
    value = (value or "").strip()
    if not value:
        return ""
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("评测服务地址必须是完整的独立 http(s) 地址")
    if any((url.username, url.password, url.query, url.fragment)):
        raise ValueError("评测服务地址不能包含账号、查询参数或片段")
    if url.path.rstrip("/") not in ("", "/evaluation"):
        raise ValueError("评测服务地址的路径必须是 /evaluation/")
    return f"{url.scheme}://{url.netloc}{EVALUATION_PATH}"


def public_url() -> str:
    return normalize(config.EVAL_PUBLIC_URL)
