"""Business-name metadata for safe ontology document filenames."""

import json


def report_name(text: str) -> str | None:
    """Read explicit JSON-string metadata; never infer names by URL-decoding stems."""
    if not text.startswith("---\n"):
        return None
    header, separator, _ = text[4:].partition("\n---\n")
    fields = [line.partition(":")[2].strip() for line in header.splitlines()
              if line.startswith("report_name:")]
    if not fields:
        return None
    if not separator or len(fields) != 1:
        raise ValueError("report_name 必须在完整的文档头中且只能声明一次")
    value = json.loads(fields[0])
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("report_name 必须是非空 JSON 字符串，且不能包含首尾空白")
    return value


def name_frontmatter(name: str | None) -> str:
    """Serialize only the business-name field, without package contract metadata."""
    return f"---\nreport_name: {json.dumps(name, ensure_ascii=False)}\n---\n" if name is not None else ""
