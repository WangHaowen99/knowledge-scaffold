"""Build the OpenAPI file DA-java mounts for its knowledge-service slot.

DA ships the knowledge-base tool contract split across two bundled specs:
kb_search / expand / doc_grep / wiki_browse in tool-platform.engine.json and
reprocess_document in knowledge-service.engine.json. kb-service implements
all five, so this merges them into one spec (taken verbatim from the DA
source being deployed, so schemas and header parameters stay DA's own).

usage: make_kb_spec.py <openapi dir from DA resources> <output.json>
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("make_kb_spec")

KB_TOOLS = ("kb_search", "expand", "doc_grep", "wiki_browse")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # NOSONAR(S4792)
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    platform = json.loads((src / "tool-platform.engine.json").read_text(encoding="utf-8"))
    knowledge = json.loads((src / "knowledge-service.engine.json").read_text(encoding="utf-8"))
    spec = dict(knowledge)
    paths = dict(knowledge.get("paths", {}))
    for tool in KB_TOOLS:
        paths[f"/v1/tools/{tool}"] = platform["paths"][f"/v1/tools/{tool}"]
    spec["paths"] = paths
    components = spec.setdefault("components", {})
    for section, items in platform.get("components", {}).items():
        target = components.setdefault(section, {})
        for name, value in items.items():
            target.setdefault(name, value)
    spec.pop("servers", None)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    tools = sorted(p.rsplit("/", 1)[-1] for p in paths)
    logger.info("wrote %s with tools: %s", out, ", ".join(tools))


if __name__ == "__main__":
    main()
