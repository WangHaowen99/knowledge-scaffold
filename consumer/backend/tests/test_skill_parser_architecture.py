# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "consumer" / "backend"
AUTHORITATIVE_PACKAGE = BACKEND_ROOT / "skill_parser"
DUPLICATE_MODELING_PACKAGE = REPO_ROOT / "modeling" / "skills" / "skill_parser"


def test_skill_parser_has_single_authoritative_package():
    assert AUTHORITATIVE_PACKAGE.is_dir()
    assert not DUPLICATE_MODELING_PACKAGE.exists()


def test_skill_parser_import_resolves_to_backend_package():
    from skill_parser import pipeline

    package_path = Path(pipeline.__file__).resolve().parent
    assert package_path == AUTHORITATIVE_PACKAGE
    assert callable(pipeline.run)
