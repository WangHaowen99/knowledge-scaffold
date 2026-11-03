"""数据接入：文档转换器与默认（契约 PL-20、PL-21；ADR-0073），以及连接器网关取票据公钥的接口。

- PL-20 `GET /api/v1/converters`：平台装了哪些转换器、每种文件类型的默认、网关状态；
- PL-21 `PUT /api/v1/admin/converters/defaults`：管理员给一种文件类型指定默认转换器，只能在已有的里选；换了默认
  不重转旧资料（资料上有「重新转换」）；
- `GET /api/v1/connector/issuer-key`：内核签票据用的公钥（Ed25519 JWK），连接器网关验签用，免鉴权（公钥本来就公开）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..services import audit, material_conversion
from ..services.material_conversion import gateway, store

router = APIRouter(tags=["converters"])

TYPE_LABELS = {".pdf": "PDF", ".docx": "Word", ".xlsx": "Excel", ".xlsm": "Excel（启用宏）"}


def converters_view() -> dict:
    known = material_conversion.known_converters()
    loaded_items = gateway.providers()
    loaded = {item.get("id") for item in loaded_items} if loaded_items is not None else None
    defaults = store.defaults()
    types = sorted({file_type for item in known for file_type in item["types"]})
    rows = []
    for file_type in types:
        chosen = material_conversion.pick(file_type)
        rows.append({
            "type": file_type, "label": TYPE_LABELS.get(file_type, file_type.lstrip(".").upper()),
            "default": chosen["id"] if chosen else None, "chosen_by_admin": file_type in defaults,
            "converters": [item["id"] for item in known if file_type in item["types"]],
        })
    health = gateway.health()
    return {
        "gateway": {"status": health["status"], "detail": health.get("detail")},
        "items": [{**item, "loaded": None if loaded is None else item["id"] in loaded,
                   "default_for": [row["type"] for row in rows if row["default"] == item["id"]]} for item in known],
        "types": rows,
    }


@router.get("/api/v1/converters")
def list_converters(request: Request):
    deps.actor(request)
    return converters_view()


@router.put("/api/v1/admin/converters/defaults")
async def set_default(request: Request):
    deps.require_role(request, "admin")
    body = await deps.json_body(request)
    file_type = str(body.get("type") or "").lower()
    converter = str(body.get("converter") or "")
    if not any(item["id"] == converter for item in material_conversion.candidates(file_type)):
        raise ApiError("invalid_argument", "这种文件没有这个转换器", 400, {"fields": {"converter": "enum"}})
    store.set_default(file_type, converter, by=deps.username(request))
    audit.log(deps.actor(request), request.method, request.url.path, {"converter.default": file_type, "converter": converter})
    return converters_view()


@router.get("/api/v1/connector/issuer-key")
def issuer_key():
    return gateway.issuer().public_jwk()
