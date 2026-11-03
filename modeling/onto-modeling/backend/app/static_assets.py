"""前端构建产物的静态服务：按 Accept-Encoding 发构建时预压缩好的 .br / .gz，并带缓存头。

- 前端构建（frontend/build/precompress.js）给 1 KB 以上的 js、css 等各写一份 .br、.gz，这里只挑文件、不现压；
- Vite 产物里 assets/ 下的文件名带内容哈希，内容变了名字就变，按一年不变缓存；
- index.html 等不带哈希的文件每次向服务端确认（no-cache，没变时 304），新部署后马上换到新的代码块。
"""

from __future__ import annotations

import os
from mimetypes import guess_type

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, PathLike, StaticFiles
from starlette.types import Scope

HASHED_DIR = "assets"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
# 服务端的偏好顺序：同样都接受时先发 br（比 gzip 小约 15%～20%）
ENCODINGS = (("br", ".br"), ("gzip", ".gz"))


def accepted_encodings(header: str) -> set[str]:
    """Accept-Encoding 里接受的编码（q=0 的不算）。"""
    accepted = set()
    for part in header.split(","):
        name, _, params = part.strip().partition(";")
        q = params.strip()
        if q.startswith("q="):
            try:
                if float(q[2:]) <= 0:
                    continue
            except ValueError:
                continue
        if name.strip():
            accepted.add(name.strip().lower())
    return accepted


class PrecompressedStaticFiles(StaticFiles):
    def file_response(self, full_path: PathLike, stat_result: os.stat_result, scope: Scope,
                      status_code: int = 200) -> Response:
        request_headers = Headers(scope=scope)
        rel = self.get_path(scope).replace(os.sep, "/")
        headers = {"Cache-Control": IMMUTABLE if rel.startswith(f"{HASHED_DIR}/") else REVALIDATE}
        path, stat, encoding = full_path, stat_result, None
        variants = [(name, f"{full_path}{suffix}") for name, suffix in ENCODINGS if os.path.isfile(f"{full_path}{suffix}")]
        if variants:
            headers["Vary"] = "Accept-Encoding"
            accepted = accepted_encodings(request_headers.get("accept-encoding", ""))
            chosen = next(((name, variant) for name, variant in variants if name in accepted), None)
            if chosen:
                encoding, path = chosen
                stat = os.stat(path)
                headers["Content-Encoding"] = encoding
        media_type = guess_type(str(full_path))[0] or "text/plain"
        response = FileResponse(path, status_code=status_code, headers=headers, media_type=media_type, stat_result=stat)
        if self.is_not_modified(response.headers, request_headers):
            return NotModifiedResponse(response.headers)
        return response
