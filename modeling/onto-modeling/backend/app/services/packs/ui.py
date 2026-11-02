"""上传包自带界面的静态资源（ADR-0055）：按项目、按锁定版本、凭短期票据从包仓库发 `ui/` 下的文件。

- 只发上传包的：官方包（入库来源 builtin）的界面代码随平台编译，不从这里走；
- 票据在路径里（`/api/v1/pack-ui/<票据>/<ui/ 下的路径>`），iframe 里的相对引用自动带上；票据只授权读这个包
  这个版本的 `ui/`，不是登录态，泄露给 iframe 里的代码也拿不到别的东西；
- 每次读都按项目锁文件重查：包停用、换了版本，旧票据立即打不开；
- 响应加隔离头：CSP `sandbox allow-scripts`（直接打开也是不透明源，读不到平台的 localStorage）、
  `connect-src 'none'`（发不了请求，只能经 postMessage 窄接口）、`frame-ancestors 'self'`、nosniff。

票据存在进程内（后端单进程）；重启后作废，前端重新申请即可。
"""

from __future__ import annotations

import mimetypes
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import manifest as mf
from . import registry
from .checks import UI_STATIC_SUFFIXES

TICKET_TTL_S = 2 * 3600
# 票据限额：每个用户同时有效的最多这么多张，全平台另有总上限；满了拒绝新申请，不淘汰别人已经在用的
TICKETS_PER_USER = 50
TICKETS_MAX = 5000
URL_PREFIX = "/api/v1/pack-ui"
CSP = "; ".join((
    "sandbox allow-scripts",
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'self'",
))
ISOLATION_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html",
          ".htm": "text/html", ".json": "application/json", ".map": "application/json", ".svg": "image/svg+xml",
          ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".webp": "image/webp", ".txt": "text/plain"}


class PackUiError(Exception):
    def __init__(self, code: str, message: str, status: int = 404, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


@dataclass(frozen=True)
class Ticket:
    project_id: str
    pack: str
    version: str
    editor: str
    user: str
    expires_at: float


_tickets: dict[str, Ticket] = {}
_lock = threading.Lock()


def _enabled_view(manager, project_id: str, pack: str) -> registry.PackView | None:
    root = manager.project_root(project_id)
    return next((view for view in registry.desired_packs(manager, root) if view.name == pack), None)


def open_session(manager, project_id: str, pack: str, editor_id: str, *, user: str) -> dict:
    """给本项目启用中的上传包的一个编辑器发票据，返回 iframe 地址。"""
    view = _enabled_view(manager, project_id, pack)
    if view is None:
        raise PackUiError("pack_not_enabled", "本项目没启用这个插件", 409, {"pack": pack})
    editor = next((item for item in view.manifest.editors() if item["id"] == editor_id), None)
    if editor is None:
        raise PackUiError("not_found", "插件没有这个编辑器", 404, {"pack": pack, "editor": editor_id})
    if view.official:
        raise PackUiError("invalid_argument", "官方插件的编辑器随平台加载，不用单独申请", 400, {"mode": "bundled"})
    if editor["ui_api"] and not mf.satisfies(mf.UI_API, editor["ui_api"]):
        raise PackUiError("page_api_unsupported", "平台的界面接口版本不够，这个编辑器打不开", 409,
                          {"required": editor["ui_api"], "platform": mf.UI_API})
    entry = f"{editor['id']}/{mf.EDITOR_ENTRY_IFRAME}"
    if not (view.directory / mf.UI_DIR / entry).is_file():
        raise PackUiError("not_found", "插件的编辑器缺少入口文件", 404, {"pack": pack, "editor": editor_id})
    expires = time.time() + TICKET_TTL_S
    with _lock:
        now = time.time()
        for key in [key for key, item in _tickets.items() if item.expires_at <= now]:
            _tickets.pop(key, None)
        # 同一个人重开同一个编辑器：沿用原来的票据、顺延有效期
        token = next((key for key, item in _tickets.items()
                      if (item.user, item.project_id, item.pack, item.version, item.editor)
                      == (user, project_id, pack, view.version, editor["id"])), None)
        if token is None:
            if sum(1 for item in _tickets.values() if item.user == user) >= TICKETS_PER_USER:
                raise PackUiError("rate_limited", "打开的插件编辑器太多了，关掉一些或稍后再试", 429,
                                  {"limit": TICKETS_PER_USER})
            if len(_tickets) >= TICKETS_MAX:
                raise PackUiError("rate_limited", "平台上打开的插件编辑器太多了，请稍后再试", 429,
                                  {"limit": TICKETS_MAX})
            token = secrets.token_urlsafe(24)
        _tickets[token] = Ticket(project_id, pack, view.version, editor["id"], user, expires)
    return {
        "url": f"{URL_PREFIX}/{token}/{entry}", "expires_at": expires, "ui_api": mf.UI_API,
        "pack": {"name": pack, "version": view.version, "label": view.manifest.label},
        "editor": {"id": editor["id"], "title": editor["title"]},
        # 窄接口 chat.command 只能发这几条（本包清单声明的命令）
        "commands": [f"/{name}" for name in view.manifest.items("commands")],
    }


# 来自包界面的命令（S-20 带 origin.pack）在服务端再按包校验一次：只认本包声明的命令，参数是短的单行值，限频
COMMAND_ARGS_MAX = 10
COMMAND_ARG_CHARS = 200
COMMAND_TEXT_CHARS = 500
COMMAND_INTERVAL_S = 3.0
_ARG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,39}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_last_command: dict[tuple[str, str], float] = {}


def check_command(manager, project_id: str, pack: str, command: str, args: dict, text: str) -> None:
    """S-20 收到来自包界面的命令：包要启用中，命令要是本包清单里的，参数与说明要短、不带换行；同一项目同一包限频。"""
    view = _enabled_view(manager, project_id, pack)
    if view is None:
        raise PackUiError("pack_not_enabled", "本项目没启用这个插件", 409, {"pack": pack})
    allowed = {f"/{name}" for name in view.manifest.items("commands")}
    if command.strip() not in allowed:
        raise PackUiError("forbidden", "插件界面只能发它自己的命令", 403, {"pack": pack, "reason": "not_pack_command"})
    fields = {}
    if len(args) > COMMAND_ARGS_MAX:
        fields["args"] = "too_many"
    for key, value in args.items():
        if not isinstance(key, str) or not _ARG_KEY_RE.match(key):
            fields["args"] = "key"
        elif isinstance(value, bool) or (isinstance(value, (int, float)) and value == value and abs(value) < 1e15):
            continue
        elif not isinstance(value, str) or len(value) > COMMAND_ARG_CHARS or _CONTROL_RE.search(value):
            fields[f"args.{key}"] = "short_single_line"
    if len(text or "") > COMMAND_TEXT_CHARS or _CONTROL_RE.search((text or "").replace("\n", "")):
        fields["text"] = "too_long"
    if fields:
        raise PackUiError("invalid_argument", "插件界面发的命令参数要是短的单行值", 400, {"fields": fields})
    now = time.time()
    with _lock:
        last = _last_command.get((project_id, pack), 0.0)
        if now - last < COMMAND_INTERVAL_S:
            raise PackUiError("rate_limited", "插件发命令太频繁，稍后再试", 429,
                              {"retry_after_s": round(COMMAND_INTERVAL_S - (now - last), 1)})
        _last_command[(project_id, pack)] = now


def _clean(rel: str) -> str | None:
    if not rel or "\\" in rel or "\x00" in rel:
        return None
    parts = PurePosixPath(rel).parts
    if rel.startswith("/") or any(part in ("..", ".") or part.startswith(".") for part in parts):
        return None
    return "/".join(parts)


def resolve(manager, token: str, rel: str) -> tuple[Path, str]:
    """票据 + ui/ 下的相对路径 → (文件, 媒体类型)。票据过期、包已停用或换了版本、路径越界都当不存在。"""
    with _lock:
        ticket = _tickets.get(token)
    if ticket is None or ticket.expires_at <= time.time():
        raise PackUiError("not_found", "地址已失效，请重新打开编辑器", 404)
    clean = _clean(rel)
    if clean is None:
        raise PackUiError("not_found", "文件不存在", 404)
    try:
        view = _enabled_view(manager, ticket.project_id, ticket.pack)
    except Exception:  # noqa: BLE001 - 项目已删除
        view = None
    if view is None or view.version != ticket.version or view.official:
        with _lock:
            _tickets.pop(token, None)
        raise PackUiError("pack_not_enabled", "插件已停用或换了版本，请重新打开", 404, {"pack": ticket.pack})
    base = (view.directory / mf.UI_DIR).resolve()
    path = view.directory / mf.UI_DIR / clean
    if path.is_symlink() or not path.is_file() or base not in path.resolve().parents:
        raise PackUiError("not_found", "文件不存在", 404)
    suffix = path.suffix.lower()
    media = _TYPES.get(suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if suffix not in UI_STATIC_SUFFIXES:
        raise PackUiError("not_found", "文件不存在", 404)
    return path, media


def revoke_all() -> None:
    """测试与停机用：清掉全部票据与命令限频记录。"""
    with _lock:
        _tickets.clear()
        _last_command.clear()
