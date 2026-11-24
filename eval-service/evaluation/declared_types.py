"""Modeling-compatible declaration parsing without modeling runtime dependencies."""
import re


TYPE_ALIASES = {
    '字符串': ('字符串', '文本', '字符', '字符型', 'string', 'str', 'text', 'varchar', 'char', 'nvarchar'),
    '整数': ('整数', '整型', '整数型', '长整型', 'int', 'integer', 'bigint', 'smallint', 'tinyint', 'long', 'int64'),
    '小数': ('小数', '浮点', '浮点型', '浮点数', '双精度', 'float', 'double', 'decimal', 'real', 'numeric'),
    '数字': ('数字', '数值', '数值型', 'number'),
    '布尔': ('布尔', '布尔型', '布尔值', 'bool', 'boolean'),
    '日期': ('日期', '日期时间', '时间', '时间戳', 'date', 'datetime', 'timestamp', 'time'),
    '枚举': ('枚举', 'enum'),
    '对象引用': ('对象引用', '引用', 'reference', 'ref'),
}
_ALIAS_INDEX = {alias.lower(): canonical for canonical, aliases in TYPE_ALIASES.items() for alias in aliases}
_TYPE_PLACEHOLDERS = {'', '无', '-', '—', '待填写', '待定', '待确认', '未知', 'todo', 'tbd', 'n/a'}


def declaration_tokens(cell):
    text = '' if cell is None else str(cell).strip()
    if re.search(r'\{[^{}]*\}', text):
        return []
    text = re.sub(r'\[\[[^\]]*\]\]|（[^）]*）|\([^)]*\)|<[^>]*>|`', ' ', text)
    text = re.split(r'[:：]', text, maxsplit=1)[0]
    text = re.sub(r'→|->', ' ', text).strip()
    if text.lower() in _TYPE_PLACEHOLDERS:
        return []
    return [token.strip() for token in re.split(r'[/|、,，;；]|或|\bor\b', text, flags=re.I) if token.strip()]


def parse_declared_type(cell):
    tokens = declaration_tokens(cell)
    if not tokens:
        return '', 'empty'
    found = [_ALIAS_INDEX.get(token.lower()) for token in tokens]
    if len(tokens) > 1:
        if None in found or len(set(found)) > 1:
            return '', 'multiple'
        return found[0], ''
    if found[0]:
        return found[0], ''
    canonical = _ALIAS_INDEX.get(tokens[0].split()[0].lower())
    return (canonical, '') if canonical else ('', 'unknown')
