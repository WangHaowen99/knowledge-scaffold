"""证据采集共用格式（ADR-0082）：规范 JSON、锚点编码、哈希和任务说明头。向量文件 rea-sidecar 的 Node 测试也用。"""

import json
from pathlib import Path

from app.evidence import spec

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "evidence_vectors.json").read_text(encoding="utf-8"))


def test_vectors_match():
    for case in VECTORS["canonical"]:
        assert spec.canonical_json(case["value"]) == case["json"]
        assert spec.scope_hash(case["value"]) == case["sha256"]
    for case in VECTORS["segments"]:
        assert spec.encode_segment(case["text"]) == case["encoded"]
    for case in VECTORS["bin_anchor"]:
        assert spec.bin_anchor(*case["args"]) == case["anchor"]
    for case in VECTORS["relation"]:
        assert spec.relation_anchor(*case["args"]) == case["anchor"]
    for case in VECTORS["record"]:
        assert spec.record_hash(case["record"]) == case["hash"]
        assert spec.record_hash({**case["record"], "hash": "x"}) == case["hash"]
    for case in VECTORS["job_scope"]:
        assert spec.job_scope(case["job_id"]) == case["scope"]
    for case in VECTORS["job_header"]:
        assert spec.encode_job_header(case["spec"]) == case["header"]
        assert spec.decode_job_header(case["header"]) == case["spec"]


def test_anchor_escapes_reserved_characters():
    assert spec.encode_segment("a.b/c#d") == "a%2Eb%2Fc%23d"
    assert "#" not in spec.bin_anchor("src", "sym", "1#油泵")
