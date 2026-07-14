# Ontology package v0.1-only runtime

## Context

The merged consumer backend contained two package layouts. Runtime readers preferred v0.1 directory indexes in some places while other paths still read or generated `ONTOLOGY.md` and root `*-list.md` files. Importing a valid v0.1 package therefore created duplicate catalogs, and Agent, graph, script, evolution, and read-ledger paths could observe different sources.

## Decision

- The installed and runtime package contract is `kind: ontology-package` with `schema_version: 0.1`.
- Runtime catalog readers only use `index.md`, `objects/index.md`, `operations/index.md`, `rules/index.md`, `metrics/index.md`, and `scripts/index.md`.
- Script execution metadata only comes from `scripts/*.md` descriptors. `script_catalog` and `execute_script` use the same descriptor loader.
- Source-template imports generate v0.1 directly and never create `ONTOLOGY.md` or `*-list.md` files.
- Legacy package recognition and conversion are isolated in `ontology/legacy_package_converter.py`. The upload boundary invokes it before installation, removes the legacy artifacts, and installs only v0.1 files.
- The legacy converter is a one-release migration component. After the legacy intake window closes, delete its module and remove the legacy manifest/layout branches from `ontology/package_import.py`; runtime modules require no follow-up cleanup.

## Alternatives considered

- Keep reading both layouts indefinitely. This preserves ambiguity and allows catalogs and executable mappings to diverge.
- Generate legacy files from v0.1 during import. This duplicates package state and caused the behavior being removed.
- Reject every legacy upload immediately. This would leave no migration path for existing exported packages.

## Consequences

- Existing runtime directories must be converted or re-imported before the strict loader will register them.
- Uploaded legacy packages remain usable during the migration release, but their installed form contains no legacy list files.
- API fields now report `index_path`, `objects_index_path`, and `operations_index_path`.
- Tests and package producers must build the v0.1 directory indexes and script descriptors explicitly.
