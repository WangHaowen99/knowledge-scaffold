"""Synthetic field contracts, independent of any running database or model."""
from copy import deepcopy
from datetime import date, datetime, time
import io

from openpyxl import Workbook
import pytest

from evaluation.core import Failure, digest
from evaluation.data_sources import read_tables
from evaluation.field_types import TYPE_RULE_VERSION, convert_value, field_kind
from evaluation.graph_import import GraphImports, import_identity, scope_target


SCOPE = {'workspace_property': 'test_scope', 'workspace_value': 'synthetic_test',
         'ontology_property': 'test_ontology', 'ontology_value': 'synthetic_package'}


def mapping(declared='字符串', *, fields=None, keys=None, resource='Items'):
    declarations = {'id': declared, **(fields or {})}
    return {'objects': {'Item': {
        'primary_key': keys or ['id'], 'fields': {field: {'type': kind} for field, kind in declarations.items()},
        'sources': [{'resource': resource, 'fields': {field: {'source_column': field} for field in declarations}}],
    }}}


def assert_failure(code, function, *args):
    with pytest.raises(Failure) as caught:
        function(*args)
    assert caught.value.code == code
    return caught.value


def plan(values, declared='字符串'):
    return GraphImports.plan(SCOPE, {'Items': {'columns': ['id'], 'rows': [[value] for value in values]}},
                             mapping(declared), {})[0][0][1]


@pytest.mark.parametrize('value,declared,expected', [
    (123, '字符串', '123'), (123.0, 'text', '123'), ('00123', 'string', '00123'),
    (False, '字符串', 'false'), ('123', '整数', 123), (123.0, 'int', 123),
    ('1.25', '小数', 1.25), ('123', '数字', 123.0), ('是', '布尔', True),
    ('0', 'boolean', False), ('false', 'bool', False), ('Y', '布尔', True),
    ('', '字符串', ''), ('', '整数', None), (None, '整数', None),
    ('2026-01-02', '日期', '2026-01-02'),
    ('2026-01-02 12:30:00', 'datetime', '2026-01-02T12:30:00'),
    ('2026-01-02T00:00:00', '日期', '2026-01-02'), ('active', '枚举', 'active'),
])
def test_declared_scalar_rules(value, declared, expected):
    actual = convert_value(value, field_kind(declared, 'synthetic field'), 'synthetic row')
    assert actual == expected and type(actual) is type(expected)


@pytest.mark.parametrize('value,declared', [
    ('not_an_integer', '整数'), ('12.5', 'integer'), (1.5, 'integer'), (True, 'integer'),
    (2 ** 63, 'integer'), ('NaN', '小数'), (float('inf'), 'string'),
    ('unknown', 'boolean'), (2, 'boolean'), ('2026-02-30', '日期'),
    ('2026-01-02T12:00:00', '日期'), ('20260102', 'date'), ({'id': 1}, 'string'),
])
def test_invalid_values_are_locatable_without_echoing_values(value, declared):
    with pytest.raises(Failure) as caught:
        convert_value(value, field_kind(declared, 'synthetic field'), 'Item / Items / row 2 / id')
    assert caught.value.code == 'graph_field_value_invalid'
    assert 'row 2' in caught.value.message
    assert 'not_an_integer' not in caught.value.message


def test_excel_and_csv_produce_identical_typed_graph_plans():
    book = Workbook()
    book.active.title = 'Items'
    book.active.append(['id'])
    book.active.append([123])
    book.active.append(['00123'])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    excel = read_tables(stream.getvalue(), 'Items.xlsx')
    csv = read_tables(b'id\n123\n00123\n', 'Items.csv')
    assert isinstance(excel['Items']['rows'][0][0], int)
    assert not isinstance(excel['Items']['rows'][0][0], bool)
    assert isinstance(csv['Items']['rows'][0][0], str)
    assert GraphImports.plan(SCOPE, excel, mapping(), {}) == GraphImports.plan(SCOPE, csv, mapping(), {})


def test_legacy_undeclared_fields_keep_values_and_unknown_types_fail():
    assert plan([123], '') == [{'id': 123}]
    assert_failure('graph_field_type_unsupported', plan, [123], 'unsupported')


def test_join_endpoints_convert_consistently_and_conflicts_fail():
    contract, tables = reference_contract({'Item': 'string', 'Target': 'string'})
    tables['Item']['rows'] = [[123]]
    relations = {'Item': [{'target': 'Target', 'relation': 'links'}]}
    objects, edges = GraphImports.plan(SCOPE, tables, contract, relations)
    assert objects[0][1] == objects[1][1] == [{'id': '123'}] and len(edges) == 1
    contract['objects']['Target']['fields']['id']['type'] = 'integer'
    assert_failure('graph_join_type_conflict', GraphImports.plan, SCOPE, tables, contract, relations)


@pytest.mark.parametrize('source_type,target_type', [
    ('string', 'string'), ('integer', 'integer'), ('integer', 'number'), ('number', 'integer'),
    ('', 'string'), ('string', ''), ('', ''),
])
def test_join_type_compatibility_preserves_numeric_and_undeclared_fields(source_type, target_type):
    contract, tables = reference_contract({'Item': source_type, 'Target': target_type})
    for table in tables.values():
        table['rows'] = [[123]]
    relations = {'Item': [{'target': 'Target', 'relation': 'links'}]}
    objects, edges = GraphImports.plan(SCOPE, tables, contract, relations)
    assert len(objects) == 2 and len(edges) == 1


@pytest.mark.parametrize('previous_version', [None, 'declared-scalars-v1', 'declared-scalars-v2', 'declared-scalars-v3'])
def test_import_identity_invalidates_previous_receipts(previous_version):
    connection = {'uri': 'bolt://graph.example.net:7687', **SCOPE}
    tables = {'Items': {'columns': ['id'], 'rows': [[123]]}}
    identity = [scope_target(connection), tables, mapping(), {}]
    previous = identity if previous_version is None else [previous_version, *identity]
    assert import_identity(connection, tables, mapping(), {}) != digest(previous)
    assert TYPE_RULE_VERSION


@pytest.mark.parametrize('value,declared,expected', [
    ('12.5', '小数（合成单位）', 12.5), ('7', '整数型', 7), ('7', 'bigint', 7),
    ('7', '整数:合成计数', 7), ('00123', 'char(32)', '00123'),
    ('00123', '引用', '00123'), ('是', '布尔值', True), ('2.5', '双精度', 2.5),
    ('2026-01-02 12:30:00', '日期时间（合成说明）', '2026-01-02T12:30:00'),
    (datetime(2026, 1, 2, 12, 30), 'timestamp', '2026-01-02T12:30:00'),
    (date(2026, 1, 2), 'date', '2026-01-02'),
    ('12:30', '时间', '12:30:00'), ('12:30:01.25', 'time', '12:30:01.250000'),
    ('12:30:00Z', 'time', '12:30:00+00:00'), (time(12, 30), 'time', '12:30:00'),
    ('2026-01-02 12:30:00', '时间', '2026-01-02T12:30:00'),
    (datetime(2026, 1, 2, 12, 30), 'time', '2026-01-02T12:30:00'),
    ('2026-01-02T12:30:01.25Z', '时间（合成说明）', '2026-01-02T12:30:01.250000+00:00'),
    ('2026-01-02T12:30:01.25+08:00', 'TIME', '2026-01-02T12:30:01.250000+08:00'),
    (time(12, 30), 'string', '12:30:00'),
])
def test_modeling_aliases_and_temporal_values(value, declared, expected):
    actual = convert_value(value, field_kind(declared, 'synthetic field'), 'synthetic row')
    assert actual == expected and type(actual) is type(expected)


@pytest.mark.parametrize('value,declared', [
    ('25:00:00', 'time'), ('2026-02-30T12:30:00', 'time'), (45658, 'time'),
    ('2026-01-02', 'time'), ('20260102T123000', 'time'), (date(2026, 1, 2), 'time'),
    ('1,000', 'integer'), ('3.0', 'integer'), (45658, 'date'),
])
def test_strict_conversion_does_not_guess_formats(value, declared):
    with pytest.raises(Failure) as caught:
        convert_value(value, field_kind(declared, 'synthetic field'), 'synthetic row')
    assert caught.value.code == 'graph_field_value_invalid'


def test_object_declaration_wins_over_source_fallback():
    contract = mapping('整数型')
    contract['objects']['Item']['sources'][0]['fields']['id']['type'] = 'string'
    tables = {'Items': {'columns': ['id'], 'rows': [['7']]}}
    assert GraphImports.plan(SCOPE, tables, contract, {})[0][0][1] == [{'id': 7}]
    contract['objects']['Item']['fields']['id']['type'] = ''
    assert GraphImports.plan(SCOPE, tables, contract, {})[0][0][1] == [{'id': '7'}]


def test_missing_mapping_table_is_a_locatable_import_failure():
    error = assert_failure('mapping_table_missing', GraphImports.converted_rows, {}, mapping(), {}, SCOPE)
    assert error.status == 422 and 'Item' in error.message and 'Items' in error.message


def test_duplicate_keys_allow_identical_payloads_but_reject_conflicting_payloads():
    assert plan([123, '123']) == [{'id': '123'}, {'id': '123'}]
    assert len(plan([123, '00123'])) == 2
    contract = mapping(fields={'value': 'string'})
    tables = {'Items': {'columns': ['id', 'value'], 'rows': [[123, 'same'], ['123', 'same']]}}
    assert len(GraphImports.converted_rows(tables, contract, {})['Item']) == 2
    tables['Items']['rows'][1][1] = 'different'
    error = assert_failure('graph_key_collision', GraphImports.plan, SCOPE, tables, contract, {})
    assert '2' in error.message and '3' in error.message


def reference_contract(declarations):
    contract, tables = {'objects': {}}, {}
    for label, declared in declarations.items():
        contract['objects'][label] = mapping(declared, resource=label)['objects']['Item']
        tables[label] = {'columns': ['id'], 'rows': [['123']]}
    return contract, tables


@pytest.mark.parametrize('reference', ['对象引用', '引用', 'reference', 'ref（合成说明）'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('declared,value,expected', [
    ('integer', '123', 123), ('string', '00123', '00123'), ('number', '1.25', 1.25),
    ('boolean', 'yes', True), ('date', '2026-01-02', '2026-01-02'),
    ('datetime', '2026-01-02 12:30:00', '2026-01-02T12:30:00'),
])
def test_reference_uses_declared_join_endpoint_type(reference, reverse, declared, value, expected):
    contract, tables = reference_contract({'Source': reference, 'Target': declared})
    for table in tables.values():
        table['rows'] = [[value]]
    source, target = ('Target', 'Source') if reverse else ('Source', 'Target')
    relations = {source: [{'target': target, 'relation': 'links'}]}
    original = deepcopy((contract, tables, relations))
    converted = GraphImports.converted_rows(tables, contract, relations)
    for rows in converted.values():
        assert rows == [{'id': expected}]
        assert type(rows[0]['id']) is type(expected)
    assert (contract, tables, relations) == original


@pytest.mark.parametrize('reverse_order', [False, True])
def test_reference_chains_and_cycles_resolve_independently_of_order(reverse_order):
    declarations = [('Source', '对象引用'), ('Middle', 'ref'), ('Target', 'integer')]
    contract, tables = reference_contract(dict(reversed(declarations) if reverse_order else declarations))
    relations = {'Source': [{'target': 'Middle', 'relation': 'links'}],
                 'Middle': [{'target': 'Target', 'relation': 'links'},
                            {'target': 'Source', 'relation': 'links'}]}
    converted = GraphImports.converted_rows(tables, contract, relations)
    assert all(rows == [{'id': 123}] for rows in converted.values())


@pytest.mark.parametrize('target_type', ['ref', ''])
def test_references_without_concrete_endpoint_keep_string_fallback(target_type):
    contract, tables = reference_contract({'Source': '对象引用', 'Target': target_type})
    relations = {'Source': [{'target': 'Target', 'relation': 'links'}]}
    assert GraphImports.converted_rows(tables, contract, relations)['Source'] == [{'id': '123'}]
    assert GraphImports.converted_rows(tables, contract, {})['Source'] == [{'id': '123'}]


def test_reference_with_incompatible_targets_is_rejected():
    contract, tables = reference_contract({'Source': '对象引用', 'Integer': 'integer', 'Text': 'string'})
    relations = {'Source': [{'target': label, 'relation': 'links'} for label in ('Integer', 'Text')]}
    assert_failure('graph_join_type_conflict', GraphImports.converted_rows, tables, contract, relations)


def test_reference_conversion_rejects_invalid_value_before_writing():
    contract, tables = reference_contract({'Source': '对象引用', 'Target': 'integer'})
    tables['Source']['rows'] = [['invalid_synthetic_integer']]
    relations = {'Source': [{'target': 'Target', 'relation': 'links'}]}
    error = assert_failure('graph_field_value_invalid', GraphImports.plan, SCOPE, tables, contract, relations)
    assert 'Source' in error.message and 'id' in error.message
    assert 'invalid_synthetic_integer' not in error.message


def test_legacy_source_declaration_resolves_reference_but_does_not_override_object_type():
    contract, tables = reference_contract({'Source': '', 'Target': 'integer'})
    source = contract['objects']['Source']
    source['sources'][0]['fields']['id']['type'] = 'ref'
    relations = {'Source': [{'target': 'Target', 'relation': 'links'}]}
    assert GraphImports.converted_rows(tables, contract, relations)['Source'] == [{'id': 123}]
    source['fields']['id']['type'] = 'string'
    assert_failure('graph_join_type_conflict', GraphImports.converted_rows, tables, contract, relations)


@pytest.mark.parametrize('reverse', [False, True])
def test_reference_non_primary_join_field_preserves_own_primary_key(reverse):
    contract, tables = reference_contract({'Source': 'string', 'Target': 'integer'})
    contract['objects']['Source'] = mapping('string', fields={'target_id': '对象引用'}, resource='Source')['objects']['Item']
    tables['Source'] = {'columns': ['id', 'target_id'], 'rows': [['source_1', '123']]}
    relations = ({'Target': [{'target': 'Source', 'relation': 'links', 'targetJoinProperty': 'target_id'}]}
                 if reverse else {'Source': [{'target': 'Target', 'relation': 'links',
                                              'sourceJoinProperty': 'target_id'}]})
    converted = GraphImports.converted_rows(tables, contract, relations)
    assert converted['Source'] == [{'id': 'source_1', 'target_id': 123}]
    assert converted['Target'] == [{'id': 123}]


@pytest.mark.parametrize('value', ['123', '1.5'])
def test_reference_numeric_constraints_do_not_truncate_fractional_values(value):
    contract, tables = reference_contract({'Source': 'ref', 'Integer': 'integer', 'Number': 'number'})
    tables['Source']['rows'] = [[value]]
    relations = {'Source': [{'target': label, 'relation': 'links'} for label in ('Integer', 'Number')]}
    if value == '123':
        assert GraphImports.converted_rows(tables, contract, relations)['Source'] == [{'id': 123}]
    else:
        assert_failure('graph_field_value_invalid', GraphImports.converted_rows, tables, contract, relations)


def test_unanchored_reference_cycle_and_self_reference_keep_string_values():
    contract, tables = reference_contract({'Source': '对象引用', 'Target': 'reference'})
    relations = {'Source': [{'target': 'Target', 'relation': 'links'},
                            {'target': 'Source', 'relation': 'links'}],
                 'Target': [{'target': 'Source', 'relation': 'links'}]}
    assert all(rows == [{'id': '123'}] for rows in GraphImports.converted_rows(tables, contract, relations).values())


def test_reference_resolution_preserves_null_and_reports_primary_key_collision():
    contract, tables = reference_contract({'Source': 'ref', 'Target': 'integer'})
    relations = {'Source': [{'target': 'Target', 'relation': 'links'}]}
    tables['Source']['rows'] = [[None]]
    assert_failure('graph_key_missing', GraphImports.converted_rows, tables, contract, relations)
    contract['objects']['Source'] = mapping('ref', fields={'value': 'string'}, resource='Source')['objects']['Item']
    tables['Source'] = {'columns': ['id', 'value'], 'rows': [['123', 'first'], [123, 'second']]}
    assert_failure('graph_key_collision', GraphImports.converted_rows, tables, contract, relations)


@pytest.mark.parametrize('id_type,part_type,values,expected', [
    ('integer', 'string', [[1, 2, True], ['1', '2', 'yes']], {'id': 1, 'part': '2', 'value': True}),
    ('string', 'integer', [[123, '7', True], ['123', 7, 'yes']], {'id': '123', 'part': 7, 'value': True}),
])
def test_composite_keys_and_payloads_compare_after_normalization(id_type, part_type, values, expected):
    contract = mapping(id_type, fields={'part': part_type, 'value': 'boolean'}, keys=['id', 'part'])
    tables = {'Items': {'columns': ['id', 'part', 'value'], 'rows': values}}
    rows = GraphImports.converted_rows(tables, contract, {})['Item']
    assert rows == [expected] * 2
    tables['Items']['rows'][1][2] = False
    assert_failure('graph_key_collision', GraphImports.converted_rows, tables, contract, {})


@pytest.mark.parametrize('value', [True, 1.0])
def test_undeclared_payload_types_are_not_silently_merged(value):
    contract = mapping('string', fields={'value': ''})
    tables = {'Items': {'columns': ['id', 'value'], 'rows': [[1, 1], ['1', value]]}}
    assert_failure('graph_key_collision', GraphImports.converted_rows, tables, contract, {})
