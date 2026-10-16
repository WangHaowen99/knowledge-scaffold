"""Transfer a workspace snapshot to the linked MCP package with optimistic locking."""
import base64
import hashlib
from pathlib import Path
import tempfile
import zipfile

from . import modeling_mcp
from .workspace import WorkspaceError


def sync_workspace_package(meta, request, workspaces):
    if not modeling_mcp._modeling_project_id(meta):
        return {"synced": False, "detail": "项目尚未关联 Modeling"}
    # Status and token requests can arrive together. Serialize snapshot + receipt.
    with workspaces._project_lock(meta["id"] + ":mcp-package-sync"):
        meta = workspaces.get_project(meta["id"])
        root = workspaces.project_root(meta["id"])
        if not any(p.name != "index.md" for p in (root / "ontology/object").glob("*.md")):
            return {"synced": False, "detail": "工作区尚无对象知识文件"}
        with tempfile.TemporaryDirectory(prefix="onto-mcp-package-") as tmp:
            archive = Path(tmp) / "package.zip"
            try:
                workspaces.export_package_zip(meta["id"], archive)
            except WorkspaceError as exc:
                raise modeling_mcp.ModelingMcpError(str(exc), 409) from exc
            # Sync makes content queryable; it does not certify a release.
            with zipfile.ZipFile(archive) as zf:
                files = {name: zf.read(name) for name in zf.namelist() if not name.endswith("/")}
            for name in files:
                if name.endswith("/manifest.yaml"):
                    files[name] = files[name].replace(b"status: released\n", b"status: draft\n")
            digest = hashlib.sha256()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, content in sorted(files.items()):
                    digest.update(name.encode())
                    digest.update(b"\0")
                    digest.update(str(len(content)).encode() + b"\0" + content)
                    zf.writestr(name, content)
            fingerprint = digest.hexdigest()
            if meta.get("modeling_package_digest") == fingerprint and meta.get("modeling_package_revision"):
                return {"synced": True, "changed": False, "revision": meta["modeling_package_revision"]}
            if archive.stat().st_size > 50 * 1024 * 1024:
                raise modeling_mcp.ModelingMcpError("知识包超过同步大小上限（50 MB）", 413)
            body = {"archive": base64.b64encode(archive.read_bytes()).decode("ascii")}
            if meta.get("modeling_package_revision"):
                body["expected_revision"] = meta["modeling_package_revision"]
            result = modeling_mcp._management_request(
                "POST", f"/api/projects/{meta['modeling_project_id']}/mcp/package", request, body, meta=meta,
            )
            if not result.get("revision"):
                raise modeling_mcp.ModelingMcpError("知识包同步响应缺少 revision")
            workspaces._update_meta(meta["id"], modeling_package_digest=fingerprint,
                                    modeling_package_revision=result["revision"])
            return result