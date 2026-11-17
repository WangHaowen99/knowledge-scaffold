"""What the adapter offers, given which DA routes the configured backend serves (pure).

The same adapter fronts the DA TypeScript backend and the DeepAnalyze
platform's knowledge service. Both serve ``/api/knowledge/*``; the platform
has no ``/api/settings`` and answers 501 for the routes it has not taken over
(summaries, export, quality audit, preprocessing). Each optional route is
probed with a request that cannot change anything: a knowledge base and a
document that do not exist, which every DA route checks before doing work.
A route that exists answers with its own error ("Knowledge base not found");
a missing one gets the server's generic 404 or the platform's 501.
"""

from __future__ import annotations

import json

KNOWLEDGE, PREVIEW, REPORTS, SETTINGS = "/api/knowledge", "/api/preview", "/api/reports", "/api/settings"
_KB, _DOC = "kb-adapter-probe", "doc-adapter-probe"

# probe -> (method, DA API, path); the paths name a knowledge base and a document that do not exist
PROBES: dict[str, tuple[str, str, str]] = {
    "settings.providers": ("GET", SETTINGS, "/providers"),
    "settings.strategies": ("GET", SETTINGS, "/pipeline-strategies"),
    "settings.parsing_mode": ("GET", SETTINGS, "/parsing-priority-mode"),
    "settings.auto_process": ("GET", SETTINGS, "/key/auto_process"),
    "settings.docling": ("GET", SETTINGS, "/docling-config"),
    "settings.mineru": ("GET", SETTINGS, "/mineru-config"),
    "settings.vlm": ("GET", SETTINGS, "/vlm-config"),
    "process_all": ("POST", KNOWLEDGE, f"/kbs/{_KB}/process-all"),
    "reindex": ("POST", KNOWLEDGE, f"/kbs/{_KB}/reindex"),
    "rebuild_embeddings": ("POST", KNOWLEDGE, f"/kbs/{_KB}/rebuild-embeddings"),
    "regenerate_abstracts": ("POST", KNOWLEDGE, f"/kbs/{_KB}/regenerate-abstracts"),
    "regenerate_abstract": ("POST", KNOWLEDGE, f"/kbs/{_KB}/documents/{_DOC}/regenerate-abstract"),
    "quality_audit": ("POST", KNOWLEDGE, f"/kbs/{_KB}/documents/{_DOC}/quality-audit"),
    "quality_report": ("GET", KNOWLEDGE, f"/kbs/{_KB}/quality-report"),
    "preprocess": ("POST", KNOWLEDGE, f"/kbs/{_KB}/preprocess"),
    "preprocess.status": ("GET", KNOWLEDGE, f"/kbs/{_KB}/preprocess/status"),
    "media": ("GET", KNOWLEDGE, f"/kbs/{_KB}/documents/{_DOC}/media-metadata"),
    "original": ("GET", KNOWLEDGE, f"/kbs/{_KB}/documents/{_DOC}/original"),
    "export": ("GET", KNOWLEDGE, f"/kbs/{_KB}/documents/{_DOC}/export/markdown"),
    "folder_delete": ("DELETE", KNOWLEDGE, f"/kbs/{_KB}/folder?path=probe"),
    "wiki": ("GET", KNOWLEDGE, f"/{_KB}/wiki/probe"),
    "anchors": ("GET", PREVIEW, f"/anchors/{_DOC}:paragraph:0"),
    "preview_raw": ("GET", PREVIEW, f"/kbs/{_KB}/documents/{_DOC}/preview/raw"),
    "structure_map": ("GET", PREVIEW, f"/kbs/{_KB}/documents/{_DOC}/structure-map"),
    "timeline": ("GET", REPORTS, f"/timeline/{_KB}"),
    "graph": ("GET", REPORTS, f"/graph/{_KB}"),
    "reports": ("GET", REPORTS, f"/reports/kb/{_KB}"),
}

ACTIONS = [
    {"id": "process_pending", "scope": "kb", "label": "处理未完成的文档", "description": "包括处理失败的文档",
     "job": True},
    {"id": "regenerate_summaries", "scope": "kb", "label": "补全缺失摘要", "confirm": True, "job": True},
    {"id": "reindex", "scope": "kb", "label": "全部重新处理", "description": "重新解析并生成全部已完成文档的摘要和索引",
     "confirm": True, "job": True},
    {"id": "rebuild_embeddings", "scope": "kb", "label": "重建向量", "description": "换了向量模型后执行",
     "confirm": True, "job": True},
    {"id": "preprocess", "scope": "kb", "label": "深度预处理",
     "description": "由 DA 智能体生成全局概览、还原跨页表格、核查图片内容，完成后出报告", "confirm": True, "job": True},
    {"id": "regenerate_summary", "scope": "document", "label": "重新生成摘要", "job": False},
    {"id": "quality_audit", "scope": "document", "label": "质量审计", "job": False,
     "params_schema": {"type": "object", "properties": {
         "re_extract": {"type": "boolean", "title": "合格也重新提取", "default": False,
                        "description": "换用更强的模型重新提取内容，耗时并产生模型费用"}}}},
]
ACTION_NEEDS = {
    "process_pending": ("process_all",), "regenerate_summaries": ("regenerate_abstracts",), "reindex": ("reindex",),
    "rebuild_embeddings": ("rebuild_embeddings",), "preprocess": ("preprocess", "preprocess.status"),
    "regenerate_summary": ("regenerate_abstract",), "quality_audit": ("quality_audit",),
}
VIEWS = [
    {"id": "overview", "scope": "document", "label": "概览", "media_type": "text/markdown"},
    {"id": "structure_map", "scope": "document", "label": "结构", "media_type": "application/json", "renderer": "table"},
    {"id": "media", "scope": "document", "label": "媒体信息", "media_type": "application/json", "renderer": "kv"},
    {"id": "quality_report", "scope": "kb", "label": "质量报告", "media_type": "application/json", "renderer": "table"},
    {"id": "timeline", "scope": "kb", "label": "时间线", "media_type": "application/json", "renderer": "timeline"},
]
# the overview is read through /api/knowledge pages, which every backend serves
VIEW_NEEDS = {"overview": (), "structure_map": ("structure_map",), "media": ("media",),
              "quality_report": ("quality_report",), "timeline": ("timeline",)}
# contract format -> (DA export format, file name suffix); "md" falls back to the L1 text, so it is always there
EXPORTS = {
    "md": ("markdown", ".md"), "json": ("raw-json", ".json"), "doctags": ("doctags", ".doctags.txt"),
    "manifest": ("structure-bundle", ".manifest.json"),
}
# the settings groups (backend_settings) a settings route feeds
SETTINGS_ROUTES = {
    "settings.providers": "providers", "settings.strategies": "strategies", "settings.parsing_mode": "parsing_mode",
    "settings.auto_process": "auto_process", "settings.docling": "docling", "settings.mineru": "mineru",
    "settings.vlm": "vlm",
}
# served by every DA backend through the core /api/knowledge routes
ALWAYS = ["search.options", "evidence", "documents.export", "folders"]


def served(status: int, content_type: str, text: str) -> bool | None:
    """Whether a probe found its route: ``None`` when the answer says nothing (server error, outage)."""

    if status == 501:
        return False
    if status == 405 or 200 <= status < 300 and "json" in content_type:
        return True
    if 200 <= status < 300:
        # an HTML page is a web frontend's fallback, not the route
        return False
    if status in (400, 409, 422):
        return True
    if status == 404:
        if "json" not in content_type:
            return False
        try:
            error = json.loads(text).get("error")
        except (ValueError, AttributeError):
            return False
        # the route's own "Knowledge base not found" and the like, not the server's bare "Not found"
        return isinstance(error, str) and error.strip().lower() not in ("", "not found")
    return None


def offer(found: dict[str, bool]) -> dict:
    """The contract declarations for the routes ``found``."""

    actions = [a for a in ACTIONS if all(found.get(n) for n in ACTION_NEEDS[a["id"]])]
    views = [v for v in VIEWS if all(found.get(n) for n in VIEW_NEEDS[v["id"]])]
    settings = sorted(group for probe, group in SETTINGS_ROUTES.items() if found.get(probe))
    features = list(ALWAYS)
    for feature, on in (
        ("wiki.read", found.get("wiki")), ("actions", actions), ("jobs", any(a["job"] for a in actions)),
        ("views", views), ("settings", settings), ("documents.file.range", found.get("original")),
        ("folders.delete", found.get("folder_delete")),
    ):
        if on:
            features.append(feature)
    return {
        "features": features, "actions": actions, "views": views, "settings": settings,
        "export_formats": list(EXPORTS) if found.get("export") else ["md"],
    }


# ---- file types: what the configured DA can parse ---------------------------------------------------

# DA's own TypeScript processors: always there
BASE_FILE_TYPES = ["txt", "md", "markdown", "html", "htm", "xlsx", "xls", "xlsm", "csv", "json", "xml", "yaml", "yml"]
# parser signal -> the file types it opens up. Docling and LibreOffice give no signal DA reports
# reliably (its module states say "not installed" while the bundled docling works), so pdf, docx,
# pptx through docling and doc, ppt through LibreOffice need DA_KB_EXTRA_FILE_TYPES.
PARSER_FILE_TYPES = {
    "mineru": ["pdf", "docx", "pptx"],
    "vlm": ["png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp"],
    "asr": ["mp3", "wav", "m4a", "flac", "aac", "ogg", "mp4", "mov", "avi", "mkv", "webm"],
}
ROLE_PARSERS = {"vlm": "vlm", "audio_transcribe": "asr"}


def role_parsers(provider_settings: object) -> set[str]:
    """Parsers a configured model role provides: images need a vision model, audio and video a transcriber."""

    if not isinstance(provider_settings, dict):
        return set()
    enabled = {p.get("id") for p in provider_settings.get("providers") or [] if isinstance(p, dict) and p.get("enabled")}
    defaults = provider_settings.get("defaults") or {}
    return {parser for role, parser in ROLE_PARSERS.items() if defaults.get(role) and defaults.get(role) in enabled}


def file_types(parsers: set[str], extra: list[str]) -> list[str]:
    found = list(BASE_FILE_TYPES)
    for parser in ("mineru", "vlm", "asr"):
        if parser in parsers:
            found += PARSER_FILE_TYPES[parser]
    return list(dict.fromkeys(found + [t.lower().lstrip(".") for t in extra if t.strip()]))
