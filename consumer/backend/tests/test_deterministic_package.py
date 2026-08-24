import json
import shutil
import threading
from types import SimpleNamespace

import pytest
from flask import Flask

import agent.tools.execute_script as execute_script_module

from api.routes.ontologies import (
    _copy_versioned_package_contents,
    _deterministic_package_immutable_response,
    _install_ontology_version_package,
)
from ontology.ontology_catalog import (
    _public_operations_catalog,
    load_script_catalog,
    public_script_catalog,
)
from ontology.operation_execution import (
    DETERMINISTIC_LOCK_NAME,
    DeterministicPackageError,
    load_deterministic_operation_specs,
    protected_operation_keys,
    validate_and_lock_deterministic_operations,
    verify_deterministic_package_lock,
)
from agent.tools.execute_script import handle_execute_script, handle_execute_script_path
from agent.tools.ontology_context import handle_ontology_context


def _write_package(root, *, execution=True, data_access="none", aliases=None):
    (root / "operations").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "schemas").mkdir()
    aliases_yaml = ""
    if aliases:
        aliases_yaml = "aliases:\n" + "".join("  - {}\n".format(item) for item in aliases)
    execution_yaml = ""
    if execution:
        execution_yaml = (
            "execution:\n"
            "  type: deterministic\n"
            "  input_schema: schemas/query.input.json\n"
            "  output_schema: schemas/query.output.json\n"
        )
    (root / "operations" / "query.md").write_text(
        "---\nresource: operation\nkey: query\n{}{}---\n".format(
            aliases_yaml,
            execution_yaml,
        ),
        encoding="utf-8",
    )
    (root / "scripts" / "query.md").write_text(
        "---\n"
        "resource: script\n"
        "key: query_handler\n"
        "bound_operation: query\n"
        "entrypoint: query.py\n"
        "data_access: {}\n"
        "---\n\n"
        "| 参数 key | CLI 参数 | 类型 | 必填 |\n"
        "| --- | --- | --- | --- |\n"
        "| code | --code | string | 是 |\n".format(data_access),
        encoding="utf-8",
    )
    (root / "scripts" / "query.py").write_text(
        "import json\nprint(json.dumps({'value': 'ok'}))\n",
        encoding="utf-8",
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {"code": {"type": "string"}},
    }
    (root / "schemas" / "query.input.json").write_text(json.dumps(schema), encoding="utf-8")
    output_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {"value": {"type": "string"}},
    }
    (root / "schemas" / "query.output.json").write_text(
        json.dumps(output_schema),
        encoding="utf-8",
    )


def test_package_without_execution_keeps_legacy_catalog_and_has_no_lock(tmp_path):
    _write_package(tmp_path, execution=False)

    assert validate_and_lock_deterministic_operations(tmp_path) == ()
    assert not (tmp_path / DETERMINISTIC_LOCK_NAME).exists()
    assert public_script_catalog(load_script_catalog(tmp_path)) == [
        {
            "operation_key": "query",
            "script_key": "query_handler",
            "title": "query_handler",
            "parameters": [
                {
                    "name": "code",
                    "required": True,
                    "type": "string",
                    "multiple": False,
                }
            ],
        }
    ]


def test_valid_deterministic_package_generates_stable_lock_and_hides_handler(tmp_path):
    _write_package(tmp_path, aliases=["Query Exact"])

    specs = validate_and_lock_deterministic_operations(tmp_path)
    reloaded = load_deterministic_operation_specs(tmp_path, verify_integrity=True)

    assert len(specs) == 1
    assert reloaded == specs
    assert specs[0].operation_key == "query"
    assert specs[0].aliases == ("Query Exact",)
    assert specs[0].handler.script_key == "query_handler"
    assert specs[0].handler.data_access == "none"
    assert specs[0].package_revision.startswith("sha256:")
    lock = json.loads((tmp_path / DETERMINISTIC_LOCK_NAME).read_text(encoding="utf-8"))
    assert lock["version"] == 2
    assert lock["operations"]["query"]["operation_path"] == "operations/query.md"
    assert lock["operations"]["query"]["routing_tokens"] == ["query", "query exact"]
    assert public_script_catalog(load_script_catalog(tmp_path)) == []


def test_generic_execution_rejects_deterministic_operation_before_loose_script_lookup(
    tmp_path,
    monkeypatch,
):
    _write_package(tmp_path)
    (tmp_path / "operations" / "legacy.md").write_text(
        "---\nresource: operation\nkey: legacy\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "a-legacy.md").write_text(
        "---\n"
        "resource: script\n"
        "key: query\n"
        "bound_operation: legacy\n"
        "entrypoint: legacy.py\n"
        "---\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "legacy.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    validate_and_lock_deterministic_operations(tmp_path)
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        lambda *_args, **_kwargs: (
            SimpleNamespace(id="demo", version="1.0.0", ontology_dir=str(tmp_path)),
            None,
        ),
    )
    calls = []
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or "bypassed",
    )

    result = handle_execute_script(
        ontology_id="demo",
        operation_key="query",
        arguments={},
        base_path=tmp_path,
    )

    assert result == "Error: deterministic_operation_protected"
    assert calls == []


def test_operation_and_handler_binding_share_normalized_key_semantics(tmp_path):
    _write_package(tmp_path)
    operation_path = tmp_path / "operations" / "query.md"
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace("key: query", "key: Query"),
        encoding="utf-8",
    )

    specs = validate_and_lock_deterministic_operations(tmp_path)
    catalog = load_script_catalog(tmp_path)
    blocked = handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="main",
        ontology_dir=tmp_path,
    )

    assert specs[0].operation_key == "Query"
    assert catalog[0]["_is_deterministic_handler"] is True
    assert public_script_catalog(catalog) == []
    assert blocked == "Error: deterministic_operation_protected"


def test_v1_lock_hashes_operation_declarations_before_routing(tmp_path):
    _write_package(tmp_path, aliases=["Query Exact"])
    validate_and_lock_deterministic_operations(tmp_path)
    lock_path = tmp_path / DETERMINISTIC_LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["version"] = 1
    for operation in lock["operations"].values():
        operation.pop("operation_path", None)
        operation.pop("routing_tokens", None)
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    assert load_deterministic_operation_specs(tmp_path)[0].aliases == ("Query Exact",)

    operation_path = tmp_path / "operations" / "query.md"
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace(
            "Query Exact",
            "Changed Alias",
        ),
        encoding="utf-8",
    )
    with pytest.raises(DeterministicPackageError) as exc:
        load_deterministic_operation_specs(tmp_path)

    assert exc.value.code == "package_integrity_mismatch"


@pytest.mark.parametrize(
    "handler_source",
    (
        "import json, random\nprint(json.dumps({'value': random.random()}))\n",
        "import json\nfrom random import *\nprint(json.dumps({'value': random()}))\n",
        "import json\nfrom datetime import datetime\n"
        "print(json.dumps({'value': datetime.now().isoformat()}))\n",
        "import json\nfrom datetime import datetime\n"
        "clock = datetime.now\nprint(json.dumps({'value': clock().isoformat()}))\n",
        "import json, uuid\nprint(json.dumps({'value': uuid.uuid4().hex}))\n",
        "import json, subprocess\n"
        "subprocess.Popen(['true'])\nprint(json.dumps({'value': 'ok'}))\n",
        "import json, os\nos.system('true')\nprint(json.dumps({'value': 'ok'}))\n",
        "import json, os\nprint(json.dumps({'value': os.getpid()}))\n",
        "import json, platform\nprint(json.dumps({'value': platform.node()}))\n",
        "import json, socket\nprint(json.dumps({'value': socket.gethostname()}))\n",
        "import json\nprint(json.dumps({'value': open('/proc/self/stat').read()}))\n",
        "import json\nloader = __builtins__['__import__']\n"
        "module = loader('os')\nprint(json.dumps({'value': module.getpid()}))\n",
    ),
)
def test_deterministic_package_rejects_obvious_nondeterministic_apis(
    tmp_path,
    handler_source,
):
    _write_package(tmp_path)
    (tmp_path / "scripts" / "query.py").write_text(handler_source, encoding="utf-8")

    with pytest.raises(DeterministicPackageError) as exc_info:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc_info.value.code == "determinism_contract_violation"


def test_deterministic_package_accepts_narrow_artifact_capability(tmp_path):
    _write_package(tmp_path)
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\n"
        "from shared_capabilities.ontology_script_sdk import write_artifact\n"
        "write_artifact('result.txt', 'synthetic result')\n"
        "print(json.dumps({'value': 'ok'}))\n",
        encoding="utf-8",
    )

    specs = validate_and_lock_deterministic_operations(tmp_path)

    assert specs[0].handler.entrypoint == "scripts/query.py"


def test_deterministic_package_qualifies_imported_local_dependencies(tmp_path):
    _write_package(tmp_path)
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\nfrom helper import value\nprint(json.dumps({'value': value()}))\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "helper.py").write_text(
        "import random\ndef value():\n    return random.random()\n",
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc_info:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc_info.value.code == "determinism_contract_violation"


def test_deterministic_package_qualifies_parent_package_initializers(tmp_path):
    _write_package(tmp_path)
    package_dir = tmp_path / "scripts" / "helper_pkg"
    package_dir.mkdir()
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\nfrom helper_pkg.value import value\n"
        "print(json.dumps({'value': value()}))\n",
        encoding="utf-8",
    )
    (package_dir / "__init__.py").write_text(
        "import random\nPACKAGE_VALUE = random.random()\n",
        encoding="utf-8",
    )
    (package_dir / "value.py").write_text(
        "def value():\n    return 'ok'\n",
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc_info:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc_info.value.code == "determinism_contract_violation"


def test_deterministic_package_accepts_relative_import_from_qualified_initializer(
    tmp_path,
):
    _write_package(tmp_path)
    package_dir = tmp_path / "scripts" / "helper_pkg"
    package_dir.mkdir()
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\nfrom helper_pkg.value import value\n"
        "print(json.dumps({'value': value()}))\n",
        encoding="utf-8",
    )
    (package_dir / "__init__.py").write_text(
        "PACKAGE_VALUE = 'ok'\n",
        encoding="utf-8",
    )
    (package_dir / "value.py").write_text(
        "from . import PACKAGE_VALUE\n"
        "def value():\n    return PACKAGE_VALUE\n",
        encoding="utf-8",
    )

    specs = validate_and_lock_deterministic_operations(tmp_path)

    assert specs[0].handler.entrypoint == "scripts/query.py"


def test_deterministic_package_rejects_dependency_outside_sandbox_contract(tmp_path):
    _write_package(tmp_path)
    (tmp_path / "scripts" / "query.py").write_text(
        "import dependency_that_does_not_exist_onto\n"
        "import json\n"
        "print(json.dumps({'value': 'ok'}))\n",
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc_info:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc_info.value.code == "deterministic_dependency_unavailable"
    assert "dependency_that_does_not_exist_onto" in str(exc_info.value)


def test_deterministic_package_accepts_declared_sandbox_dependency(tmp_path):
    _write_package(tmp_path)
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\n"
        "import requests\n"
        "print(json.dumps({'value': requests.__name__}))\n",
        encoding="utf-8",
    )

    specs = validate_and_lock_deterministic_operations(tmp_path)

    assert specs[0].handler.entrypoint == "scripts/query.py"


def test_deterministic_package_accepts_pinned_neo4j_driver(tmp_path):
    _write_package(tmp_path)
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\n"
        "from neo4j import GraphDatabase\n"
        "print(json.dumps({'value': GraphDatabase.__name__}))\n",
        encoding="utf-8",
    )

    specs = validate_and_lock_deterministic_operations(tmp_path)

    assert specs[0].handler.entrypoint == "scripts/query.py"


def test_deterministic_package_accepts_public_runtime_sdk_export(tmp_path):
    _write_package(tmp_path)
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\n"
        "from shared_capabilities.ontology_script_sdk import load_effective_connection\n"
        "print(json.dumps({'value': load_effective_connection.__name__}))\n",
        encoding="utf-8",
    )

    specs = validate_and_lock_deterministic_operations(tmp_path)

    assert specs[0].handler.entrypoint == "scripts/query.py"


@pytest.mark.parametrize("private_name", ("bounded_runner", "connection_env"))
def test_deterministic_package_rejects_private_runtime_sdk_module(
    tmp_path,
    private_name,
):
    _write_package(tmp_path)
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\n"
        "from shared_capabilities.ontology_script_sdk import {}\n"
        "print(json.dumps({{'value': 'ok'}}))\n".format(private_name),
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc_info:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc_info.value.code == "deterministic_dependency_unavailable"
    assert private_name in str(exc_info.value)


def test_deterministic_package_accepts_local_namespace_package(tmp_path):
    _write_package(tmp_path)
    helpers = tmp_path / "scripts" / "helpers"
    helpers.mkdir()
    (helpers / "normalize.py").write_text(
        "def value():\n    return 'ok'\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\n"
        "from helpers import normalize\n"
        "print(json.dumps({'value': normalize.value()}))\n",
        encoding="utf-8",
    )

    specs = validate_and_lock_deterministic_operations(tmp_path)

    assert specs[0].handler.entrypoint == "scripts/query.py"


def test_nested_absolute_import_uses_entrypoint_search_path(tmp_path):
    _write_package(tmp_path)
    package = tmp_path / "scripts" / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub.py").write_text(
        "import sibling\nVALUE = sibling.VALUE\n",
        encoding="utf-8",
    )
    # This is the file the old dependency-parent resolver inspected.
    (package / "sibling.py").write_text("VALUE = 'qualified-local'\n", encoding="utf-8")
    # Python keeps scripts/ as sys.path[0], so this is the file runtime imports.
    (tmp_path / "scripts" / "sibling.py").write_text(
        "import random\nVALUE = random.random()\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\nimport pkg.sub\nprint(json.dumps({'value': pkg.sub.VALUE}))\n",
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc_info:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc_info.value.code == "determinism_contract_violation"
    assert "scripts/sibling.py" in str(exc_info.value)


def test_lock_remains_valid_after_shared_manifest_is_split_from_version(tmp_path):
    source = tmp_path / "source"
    installed = tmp_path / "ontology" / "versions" / "1.0.0"
    source.mkdir()
    _write_package(source)
    (source / "manifest.yaml").write_text(
        "kind: ontology-package\nschema_version: 0.1\nid: demo\n",
        encoding="utf-8",
    )
    validate_and_lock_deterministic_operations(source)
    shutil.copytree(source, installed, ignore=shutil.ignore_patterns("manifest.yaml"))

    specs = load_deterministic_operation_specs(installed, verify_integrity=True)

    assert specs[0].operation_key == "query"


def test_lock_integrity_detects_changed_handler(tmp_path):
    _write_package(tmp_path)
    validate_and_lock_deterministic_operations(tmp_path)
    (tmp_path / "scripts" / "query.py").write_text("print('{}')\n", encoding="utf-8")

    with pytest.raises(DeterministicPackageError) as exc:
        verify_deterministic_package_lock(tmp_path)

    assert exc.value.code == "package_integrity_mismatch"


def test_lock_integrity_detects_added_importable_file(tmp_path):
    _write_package(tmp_path)
    validate_and_lock_deterministic_operations(tmp_path)
    (tmp_path / "scripts" / "shadow.py").write_text("VALUE = 'changed'\n", encoding="utf-8")

    with pytest.raises(DeterministicPackageError) as exc:
        verify_deterministic_package_lock(tmp_path)

    assert exc.value.code == "package_integrity_mismatch"


def test_lock_integrity_includes_top_level_versions_python_dependencies(tmp_path):
    _write_package(tmp_path)
    dependency_dir = tmp_path / "versions"
    dependency_dir.mkdir()
    (dependency_dir / "helper.py").write_text(
        "VALUE = 'before'\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "query.py").write_text(
        "import json\n"
        "from versions.helper import VALUE\n"
        "print(json.dumps({'value': VALUE}))\n",
        encoding="utf-8",
    )

    validate_and_lock_deterministic_operations(tmp_path)
    lock = json.loads(
        (tmp_path / DETERMINISTIC_LOCK_NAME).read_text(encoding="utf-8")
    )

    assert "versions/helper.py" in {entry["path"] for entry in lock["files"]}
    (dependency_dir / "helper.py").write_text(
        "VALUE = 'after'\n",
        encoding="utf-8",
    )
    with pytest.raises(DeterministicPackageError) as exc:
        verify_deterministic_package_lock(tmp_path)

    assert exc.value.code == "package_integrity_mismatch"


def test_complete_lock_verification_observes_cancellation(tmp_path):
    _write_package(tmp_path)
    validate_and_lock_deterministic_operations(tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(DeterministicPackageError) as exc:
        verify_deterministic_package_lock(tmp_path, cancel_event=cancel_event)

    assert exc.value.code == "execution_cancelled"


def test_execution_unknown_field_is_rejected(tmp_path):
    _write_package(tmp_path)
    operation_path = tmp_path / "operations" / "query.md"
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace(
            "  type: deterministic\n",
            "  type: deterministic\n  mode: strict\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_declaration_invalid"


def test_deterministic_parser_outside_scripts_is_rejected_at_publish_time(tmp_path):
    _write_package(tmp_path)
    (tmp_path / "parsers").mkdir()
    (tmp_path / "parsers" / "query.py").write_text(
        "import json\nprint(json.dumps({'matched': True, 'arguments': {}, 'issues': []}))\n",
        encoding="utf-8",
    )
    operation_path = tmp_path / "operations" / "query.md"
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace(
            "  type: deterministic\n",
            "  type: deterministic\n  parser: parsers/query.py\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_path_invalid"
    assert "within scripts/" in str(exc.value)
    assert not (tmp_path / DETERMINISTIC_LOCK_NAME).exists()


def test_deterministic_parser_cannot_share_handler_path(tmp_path):
    _write_package(tmp_path)
    operation_path = tmp_path / "operations" / "query.md"
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace(
            "  type: deterministic\n",
            "  type: deterministic\n  parser: scripts/query.py\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_path_invalid"
    assert str(exc.value) == "Deterministic parser and handler must use different files."
    assert not (tmp_path / DETERMINISTIC_LOCK_NAME).exists()


def test_remote_schema_ref_is_rejected(tmp_path):
    _write_package(tmp_path)
    schema_path = tmp_path / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["code"] = {"$ref": "https://example.com/code.json"}
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_schema_invalid"


def test_remote_dynamic_schema_ref_is_rejected(tmp_path):
    _write_package(tmp_path)
    schema_path = tmp_path / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["code"] = {
        "$dynamicRef": "https://example.com/code.json"
    }
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_schema_invalid"
    assert "non-local schema reference" in str(exc.value)


def test_nested_schema_id_is_rejected(tmp_path):
    _write_package(tmp_path)
    schema_path = tmp_path / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["code"]["$id"] = "nested.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_schema_invalid"
    assert "nested $id" in str(exc.value)


def test_unresolved_local_schema_ref_is_rejected_at_publish_time(tmp_path):
    _write_package(tmp_path)
    schema_path = tmp_path / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["code"] = {"$ref": "#/$defs/missing"}
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_schema_invalid"
    assert "unresolved local $ref" in str(exc.value)


def test_duplicate_json_schema_keys_are_rejected_at_publish_time(tmp_path):
    _write_package(tmp_path)
    schema_path = tmp_path / "schemas" / "query.input.json"
    schema_path.write_text(
        '{"type":"object","properties":{},"properties":{"code":{"type":"string"}}}',
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_schema_invalid"


def test_duplicate_deterministic_operation_key_is_rejected(tmp_path):
    _write_package(tmp_path)
    duplicate = tmp_path / "operations" / "query-copy.md"
    duplicate.write_text(
        (tmp_path / "operations" / "query.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "deterministic_alias_conflict"
    assert "declared more than once" in str(exc.value)


def test_deterministic_alias_cannot_conflict_with_legacy_operation(tmp_path):
    _write_package(tmp_path, aliases=["legacy"])
    (tmp_path / "operations" / "legacy.md").write_text(
        "---\nresource: operation\nkey: legacy\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "deterministic_alias_conflict"


def test_deterministic_alias_conflict_uses_unicode_nfkc_normalization(tmp_path):
    _write_package(tmp_path, aliases=["Ｑｕｅｒｙ　Ｅｘａｃｔ"])
    (tmp_path / "operations" / "legacy.md").write_text(
        "---\nresource: operation\nkey: query exact\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "deterministic_alias_conflict"


def test_invalid_legacy_frontmatter_does_not_enable_new_strict_validation(tmp_path):
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / "legacy.md").write_text(
        "---\ninvalid: [\n---\nlegacy body\n",
        encoding="utf-8",
    )

    assert validate_and_lock_deterministic_operations(tmp_path) == ()


def test_indented_execution_in_invalid_frontmatter_fails_closed(tmp_path):
    _write_package(tmp_path)
    operation_path = tmp_path / "operations" / "query.md"
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace(
            "execution:\n",
            " execution:\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_declaration_invalid"
    assert public_script_catalog(load_script_catalog(tmp_path)) == []
    assert handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="main",
        ontology_dir=tmp_path,
    ) == "Error: deterministic_operation_protected"


@pytest.mark.parametrize(
    "execution_yaml",
    (
        "execution: deterministic\n",
        "execution:\n  - deterministic\n",
        (
            "execution:\n"
            "  type: scripted\n"
            "  input_schema: schemas/query.input.json\n"
            "  output_schema: schemas/query.output.json\n"
        ),
    ),
)
def test_invalid_execution_declaration_without_lock_still_protects_routes_and_handler(
    tmp_path,
    monkeypatch,
    execution_yaml,
):
    _write_package(tmp_path, aliases=["Query Exact"])
    operation_path = tmp_path / "operations" / "query.md"
    valid_execution_yaml = (
        "execution:\n"
        "  type: deterministic\n"
        "  input_schema: schemas/query.input.json\n"
        "  output_schema: schemas/query.output.json\n"
    )
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace(
            valid_execution_yaml,
            execution_yaml,
        ),
        encoding="utf-8",
    )
    assert not (tmp_path / DETERMINISTIC_LOCK_NAME).exists()

    assert protected_operation_keys(tmp_path) == frozenset({"query", "Query Exact"})
    assert public_script_catalog(load_script_catalog(tmp_path)) == []
    assert handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="main",
        ontology_dir=tmp_path,
    ) == "Error: deterministic_operation_protected"

    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        lambda *_args, **_kwargs: (
            SimpleNamespace(id="demo", version="1.0.0", ontology_dir=str(tmp_path)),
            None,
        ),
    )
    bypass_calls = []
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        lambda **kwargs: bypass_calls.append(kwargs) or "BYPASSED",
    )
    for operation_key in ("query", "Query Exact"):
        assert handle_execute_script(
            ontology_id="demo",
            operation_key=operation_key,
            arguments={},
            base_path=tmp_path,
        ) == "Error: deterministic_operation_protected"
    assert bypass_calls == []

    with pytest.raises(DeterministicPackageError) as exc:
        validate_and_lock_deterministic_operations(tmp_path)

    assert exc.value.code == "execution_declaration_invalid"
    assert not (tmp_path / DETERMINISTIC_LOCK_NAME).exists()


def test_deterministic_handler_cannot_be_executed_by_agent_or_legacy_direct(tmp_path):
    _write_package(tmp_path)
    validate_and_lock_deterministic_operations(tmp_path)

    blocked_agent = handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="main",
        ontology_dir=tmp_path,
    )
    blocked_direct = handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="direct",
        ontology_dir=tmp_path,
    )
    allowed = handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="deterministic_service",
        ontology_dir=tmp_path,
    )

    assert blocked_agent == "Error: deterministic_operation_protected"
    assert blocked_direct == "Error: deterministic_operation_protected"
    assert json.loads(allowed) == {"value": "ok"}


def test_handler_protection_classification_failure_fails_closed(
    tmp_path,
    monkeypatch,
):
    _write_package(tmp_path)
    validate_and_lock_deterministic_operations(tmp_path)
    monkeypatch.setattr(
        "ontology.operation_execution.protected_handler_paths",
        lambda _root: (_ for _ in ()).throw(RuntimeError("classification failed")),
    )

    blocked = handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="main",
        ontology_dir=tmp_path,
    )

    assert blocked == "Error: deterministic_operation_protection_unavailable"


def test_installed_lock_prevents_tampered_declaration_from_downgrading_to_legacy(tmp_path):
    _write_package(tmp_path)
    validate_and_lock_deterministic_operations(tmp_path)
    operation_path = tmp_path / "operations" / "query.md"
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace(
            "execution:\n"
            "  type: deterministic\n"
            "  input_schema: schemas/query.input.json\n"
            "  output_schema: schemas/query.output.json\n",
            "",
        ),
        encoding="utf-8",
    )

    assert public_script_catalog(load_script_catalog(tmp_path)) == []
    blocked = handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="main",
        ontology_dir=tmp_path,
    )
    with pytest.raises(DeterministicPackageError) as exc:
        load_deterministic_operation_specs(tmp_path, verify_integrity=True)

    assert blocked == "Error: deterministic_operation_protected"
    assert exc.value.code == "package_integrity_mismatch"


def test_corrupt_lock_conservatively_protects_all_bound_handlers(tmp_path):
    _write_package(tmp_path)
    validate_and_lock_deterministic_operations(tmp_path)
    operation_path = tmp_path / "operations" / "query.md"
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace(
            "execution:\n"
            "  type: deterministic\n"
            "  input_schema: schemas/query.input.json\n"
            "  output_schema: schemas/query.output.json\n",
            "",
        ),
        encoding="utf-8",
    )
    (tmp_path / DETERMINISTIC_LOCK_NAME).write_text("not-json", encoding="utf-8")

    assert public_script_catalog(load_script_catalog(tmp_path)) == []
    blocked = handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="main",
        ontology_dir=tmp_path,
    )

    assert blocked == "Error: deterministic_operation_protected"


def test_malformed_live_package_without_lock_still_protects_bound_handler(tmp_path):
    _write_package(tmp_path)
    validate_and_lock_deterministic_operations(tmp_path)
    (tmp_path / DETERMINISTIC_LOCK_NAME).unlink()
    (tmp_path / "schemas" / "query.input.json").write_text(
        '{"type":"object","properties":{"code":{"$ref":"#/missing"}}}',
        encoding="utf-8",
    )

    blocked = handle_execute_script_path(
        script_path="scripts/query.py",
        base_path=tmp_path,
        caller="main",
        ontology_dir=tmp_path,
    )

    assert blocked == "Error: deterministic_operation_protected"


def test_installed_deterministic_version_is_immutable_in_file_editor(tmp_path):
    version_dir = tmp_path / "versions" / "1.0.0"
    operations_dir = version_dir / "operations"
    operations_dir.mkdir(parents=True)
    (version_dir / DETERMINISTIC_LOCK_NAME).write_text("{}", encoding="utf-8")

    app = Flask(__name__)
    with app.app_context():
        response, status_code = _deterministic_package_immutable_response(operations_dir)

    assert status_code == 409
    assert response.get_json()["error_code"] == "deterministic_operation_immutable"


def test_symlinked_deterministic_lock_remains_immutable_in_file_editor(tmp_path):
    version_dir = tmp_path / "versions" / "1.0.0"
    operations_dir = version_dir / "operations"
    operations_dir.mkdir(parents=True)
    (version_dir / DETERMINISTIC_LOCK_NAME).symlink_to(version_dir / "missing-lock")

    app = Flask(__name__)
    with app.app_context():
        response, status_code = _deterministic_package_immutable_response(operations_dir)

    assert status_code == 409
    assert response.get_json()["error_code"] == "deterministic_operation_immutable"


def test_installed_deterministic_version_cannot_be_overwritten(tmp_path):
    source = tmp_path / "replacement"
    installed = tmp_path / "ontology" / "versions" / "1.0.0"
    source.mkdir()
    installed.mkdir(parents=True)
    (source / "replacement.txt").write_text("replacement\n", encoding="utf-8")
    (installed / "original.txt").write_text("original\n", encoding="utf-8")
    (installed / DETERMINISTIC_LOCK_NAME).write_text("{}", encoding="utf-8")

    with pytest.raises(DeterministicPackageError) as exc_info:
        _copy_versioned_package_contents(source, installed)

    assert exc_info.value.code == "deterministic_operation_immutable"
    assert (installed / "original.txt").read_text(encoding="utf-8") == "original\n"
    assert not (installed / "replacement.txt").exists()


def test_new_version_install_regenerates_lock_for_evolved_snapshot(tmp_path):
    source = tmp_path / "evolved"
    source.mkdir()
    _write_package(source)
    original_specs = validate_and_lock_deterministic_operations(source)
    original_revision = original_specs[0].package_revision
    (source / "scripts" / "query.py").write_text(
        "import json\nprint(json.dumps({'value': 'evolved'}))\n",
        encoding="utf-8",
    )

    ontology_dir = tmp_path / "ontologies" / "demo"
    installed = _install_ontology_version_package(
        source_root=source,
        ontology_dir=ontology_dir,
        ontology_id="demo",
        ontology_name="Demo",
        ontology_description="",
        target_version="2.0.0",
        existed=False,
    )

    installed_specs = load_deterministic_operation_specs(
        installed,
        verify_integrity=True,
    )
    assert installed_specs[0].package_revision != original_revision
    assert verify_deterministic_package_lock(installed) == (
        installed_specs[0].package_revision
    )


@pytest.mark.parametrize(
    ("resource", "key"),
    (
        ("operation", "query"),
        ("operation", "Query Exact"),
        ("script", "query_handler"),
        ("script", "query.md"),
    ),
)
def test_deterministic_operation_context_is_protected(
    tmp_path,
    monkeypatch,
    resource,
    key,
):
    _write_package(tmp_path, aliases=["Query Exact"])
    validate_and_lock_deterministic_operations(tmp_path)
    monkeypatch.setattr(
        "agent.tools.ontology_context.get_ontology_manifest",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                id="demo",
                ontology_dir=str(tmp_path),
                manifest_path=str(tmp_path / "manifest.yaml"),
            ),
            None,
        ),
    )

    result = handle_ontology_context(
        ontology_id="demo",
        resource=resource,
        key=key,
    )

    assert result == "Error: deterministic_operation_protected"


@pytest.mark.parametrize(
    "filename_key",
    ("operation-description", "operation-description.md"),
)
def test_deterministic_operation_context_is_protected_by_resolved_filename(
    tmp_path,
    monkeypatch,
    filename_key,
):
    _write_package(tmp_path)
    (tmp_path / "operations" / "query.md").rename(
        tmp_path / "operations" / "operation-description.md"
    )
    validate_and_lock_deterministic_operations(tmp_path)
    monkeypatch.setattr(
        "agent.tools.ontology_context.get_ontology_manifest",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                id="demo",
                ontology_dir=str(tmp_path),
                manifest_path=str(tmp_path / "manifest.yaml"),
            ),
            None,
        ),
    )

    result = handle_ontology_context(
        ontology_id="demo",
        resource="operation",
        key=filename_key,
    )

    assert result == "Error: deterministic_operation_protected"


def test_public_operation_catalog_filters_only_operation_key_column():
    catalog = [
        {"名称": "query", "业务描述": "authoritative query"},
        {"名称": "legacy", "业务描述": "query"},
    ]

    assert _public_operations_catalog(
        catalog,
        deterministic_keys=frozenset({"query"}),
    ) == [{"名称": "legacy", "业务描述": "query"}]
