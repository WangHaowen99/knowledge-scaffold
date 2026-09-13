"""Safe ontology package inputs for shared parsing and evidence indexing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ontology.package_browser import PackageBrowserError, list_package_files, read_package_text
from ontology.resource_types import ResourceSource

SEMANTIC_DIRECTORIES = {
    "objects": "object",
    "operations": "operation",
    "rules": "rule",
    "metrics": "metric",
    "scripts": "script",
}
SEMANTIC_EXTENSIONS = {".md", ".txt", ".csv", ".tsv"}
MAX_SOURCE_BYTES = 512_000
_SECRET_RE = re.compile(r"(?im)^\s*(?:password|passwd|secret|api[_-]?key|access[_-]?token|private[_-]?key)\s*[:=]")


@dataclass(frozen=True)
class SourceReadWarning:
    code: str
    source_path: str


@dataclass(frozen=True)
class PackageSources:
    sources: tuple[ResourceSource, ...]
    file_digests: dict[str, str]
    file_sizes: dict[str, int]
    warnings: tuple[SourceReadWarning, ...]


class PackageSourceReader:
    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def read(self, *, ontology_dir: Path, ontology_id: str, ontology_version: str) -> PackageSources:
        root = ontology_dir.resolve()
        listed = list_package_files(
            root,
            recursive=True,
            allowed_extensions=SEMANTIC_EXTENSIONS,
            trusted_root=root,
        )
        sources: list[ResourceSource] = []
        digests: dict[str, str] = {}
        sizes: dict[str, int] = {}
        warnings: list[SourceReadWarning] = []
        for item in listed:
            path = str(item["name"])
            resource = _resource_for_path(path)
            if resource is None:
                continue
            try:
                payload = read_package_text(
                    root,
                    path,
                    allowed_extensions=SEMANTIC_EXTENSIONS,
                    max_bytes=MAX_SOURCE_BYTES,
                    trusted_root=root,
                    include_sha256=True,
                )
            except PackageBrowserError as exc:
                warnings.append(SourceReadWarning("SOURCE_{}".format(exc.code.upper()), path))
                continue
            content = str(payload["content"])
            if path.startswith("assets/") and _SECRET_RE.search(content):
                warnings.append(SourceReadWarning("SOURCE_SECRET_REJECTED", path))
                continue
            digest = str(payload["sha256"])
            digests[path] = digest
            sizes[path] = int(payload["size"])
            sources.append(
                ResourceSource(
                    ontology_id=ontology_id,
                    ontology_version=ontology_version,
                    resource=resource,
                    source_path=path,
                    media_type=_media_type(path),
                    source_text=content,
                    source_content_sha256=digest,
                )
            )
        sources.sort(key=lambda source: source.source_path.encode("utf-8"))
        return PackageSources(tuple(sources), digests, sizes, tuple(warnings))


def _resource_for_path(path: str) -> str | None:
    parts = Path(path).parts
    if path == "index.md":
        return "root"
    if len(parts) == 2 and parts[0] in SEMANTIC_DIRECTORIES and parts[1] != "index.md":
        return SEMANTIC_DIRECTORIES[parts[0]]
    if parts and parts[0] == "assets":
        return "asset"
    return None


def _media_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".tsv": "text/tab-separated-values",
        ".txt": "text/plain",
    }.get(suffix, "text/plain")
