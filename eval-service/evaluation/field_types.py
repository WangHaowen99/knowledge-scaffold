"""Declared scalar types at the graph import boundary; absent declarations remain legacy values."""
from datetime import date, datetime, time
from decimal import Decimal
import math
import re

from .core import Failure
from .declared_types import declaration_tokens, parse_declared_type


TYPE_RULE_VERSION = 'declared-scalars-v4'
KINDS = {
    '字符串': 'string', '枚举': 'string', '对象引用': 'string', '整数': 'integer',
    '小数': 'number', '数字': 'number', '布尔': 'boolean', '日期': 'date',
}


def field_kind(declared, location):
    if declared is None or declared == '':
        return ''
    canonical, problem = parse_declared_type(declared)
    if problem:
        raise Failure('graph_field_type_unsupported', f'{location}：不支持的声明类型', 422, 'data_import')
    if canonical == '日期':
        tokens = {token.split()[0].lower() for token in declaration_tokens(declared)}
        if tokens & {'datetime', 'timestamp', '日期时间', '时间戳'}:
            return 'datetime'
        if tokens & {'time', '时间'}:
            return 'time'
    return KINDS[canonical]


def convert_value(value, kind, location):
    if not kind or value is None:
        return value
    try:
        if kind == 'string':
            if isinstance(value, str):
                return value
            if isinstance(value, bool):
                return 'true' if value else 'false'
            if isinstance(value, (int, float, Decimal)):
                if not isinstance(value, int) and not math.isfinite(value):
                    raise ValueError()
                return str(int(value)) if value == int(value) else str(value)
            if isinstance(value, (date, datetime, time)):
                return value.isoformat()
            raise ValueError()
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        if kind == 'boolean':
            if isinstance(value, bool):
                return value
            token = str(value).lower()
            if token in ('true', '是', 'yes', 'y', 't', '1'):
                return True
            if token in ('false', '否', 'no', 'n', 'f', '0'):
                return False
            raise ValueError()
        if isinstance(value, bool):
            raise ValueError()
        if kind == 'integer':
            if isinstance(value, str) and not re.fullmatch(r'[+-]?[0-9]+', value):
                raise ValueError()
            if not isinstance(value, (str, int, float, Decimal)):
                raise ValueError()
            integer = int(value)
            if (not isinstance(value, str) and integer != value) or not -(2 ** 63) <= integer < 2 ** 63:
                raise ValueError()
            return integer
        if kind == 'number':
            if not isinstance(value, (str, int, float, Decimal)):
                raise ValueError()
            number = float(value)
            if not math.isfinite(number):
                raise ValueError()
            return number
        if kind == 'time':
            if isinstance(value, (time, datetime)):
                return value.isoformat()
            if not isinstance(value, str):
                raise ValueError()
            value = re.sub(r'\.([0-9]{1,6})(?=[+-]|Z|$)',
                           lambda match: '.' + match.group(1).ljust(6, '0'), value)
            if re.match(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ]', value):
                return datetime.fromisoformat(value.replace('Z', '+00:00')).isoformat()
            if not re.match(r'^[0-9]{2}:[0-9]{2}', value):
                raise ValueError()
            return time.fromisoformat(value.replace('Z', '+00:00')).isoformat()
        if kind in ('date', 'datetime'):
            if isinstance(value, (date, datetime)):
                value = value.isoformat()
            if not isinstance(value, str):
                raise ValueError()
            if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
                parsed = date.fromisoformat(value)
                return parsed.isoformat() if kind == 'date' else datetime.combine(parsed, datetime.min.time()).isoformat()
            if not re.match(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ]', value):
                raise ValueError()
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if kind == 'date' and any((parsed.hour, parsed.minute, parsed.second, parsed.microsecond, parsed.tzinfo)):
                raise ValueError()
            return parsed.date().isoformat() if kind == 'date' else parsed.isoformat()
        raise ValueError()
    except (ValueError, TypeError, OverflowError) as exc:
        raise Failure('graph_field_value_invalid', f'{location}：无法按声明类型 {kind} 转换', 422, 'data_import') from exc
