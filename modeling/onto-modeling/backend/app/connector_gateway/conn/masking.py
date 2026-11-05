"""脱敏策略 `mask-v1`（ADR-0084；证据采集接口 §9 的脱敏表）。

先按列名（中英文）和列注释识别，再看取值；判断不了的文本列按敏感处理，宁可多遮。处置：口令类丢弃，标识类掩码，
金额分桶（数量级），跨表关联的标识加盐哈希（盐按项目，同一个值在不同表里哈希相同，样本之间还能对上）。
输出的取值只有字符串、整数、布尔和 null，不出浮点数。
"""

from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any, Iterable

POLICY = "mask-v1"

TREATMENT = {
    "secret": "drop",
    "phone": "mask", "id_number": "mask", "person_name": "mask", "birth_date": "mask", "email": "mask",
    "bank_card": "mask", "address": "mask", "binary": "mask", "text": "mask",
    "amount": "bucket",
    "join_key": "hash",
    "none": None,
}

# ---- 列名识别 ----

_SECRET_TOKENS = {"password", "passwd", "pwd", "secret", "token", "salt", "apikey", "credential", "credentials",
                  "privatekey", "otp"}
_SECRET_PHRASES = ("api_key", "private_key", "access_key", "secret_key", "pass_word")
_SECRET_ZH = ("密码", "口令", "密钥", "秘钥", "令牌")
_PHONE_TOKENS = {"phone", "mobile", "tel", "telephone", "cellphone", "cell", "fax"}
_PHONE_ZH = ("手机", "电话", "联系方式", "传真")
_ID_PHRASES = ("id_card", "idcard", "id_no", "idno", "id_number", "identity", "cert_no", "certificate", "passport",
               "ssn", "national_id", "citizen_id")
_ID_ZH = ("身份证", "证件")
_BIRTH_TOKENS = {"birthday", "birthdate", "dob", "birth"}
_BIRTH_ZH = ("出生", "生日")
_EMAIL_TOKENS = {"email", "mail", "mailbox"}
_EMAIL_ZH = ("邮箱", "电子邮件")
_CARD_PHRASES = ("bank_card", "bankcard", "card_no", "cardno", "card_number", "account_no", "account_number", "iban")
_CARD_ZH = ("银行卡", "卡号", "银行账号", "账号")
_ADDRESS_TOKENS = {"address", "addr", "street"}
_ADDRESS_ZH = ("地址", "住址")
_NAME_ZH = ("姓名", "联系人", "收货人", "收件人", "寄件人", "负责人", "经办人", "申请人")
_PERSON_WORDS = {"customer", "contact", "user", "employee", "emp", "receiver", "recipient", "sender", "buyer", "seller",
                 "owner", "member", "staff", "manager", "patient", "student", "consignee", "payer", "payee", "holder",
                 "applicant", "operator", "real", "full", "first", "last", "nick", "legal", "person", "people",
                 "client", "driver", "teacher", "doctor", "creator"}
_PERSON_NAME_TOKENS = {"realname", "fullname", "nickname", "firstname", "lastname", "username", "surname"}
_AMOUNT_TOKENS = {"amount", "amt", "price", "balance", "salary", "wage", "wages", "income", "fee", "cost"}
_AMOUNT_ZH = ("金额", "余额", "工资", "薪", "价格", "单价", "费用")
# 业务对象的名称、编号：不是个人信息（取值里搜到个人信息时仍按敏感处理）
_BUSINESS_PREFIX = {"product", "item", "goods", "category", "brand", "sku", "model", "unit", "warehouse", "carrier",
                    "channel", "dept", "department", "store", "shop", "order", "invoice", "tracking", "shipment",
                    "batch", "waybill", "project"}
_BUSINESS_SUFFIX = {"name", "no", "code", "sn", "number", "title"}

# ---- 取值识别 ----

_PHONE_VALUE = re.compile(r"(?:\+?86)?1[3-9]\d{9}")
_ID_VALUE = re.compile(r"\d{17}[\dXx]")
_EMAIL_VALUE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_CARD_VALUE = re.compile(r"\d{16,19}")
_ADDRESS_MARKS = ("省", "市", "区", "县", "镇", "乡", "村", "路", "街", "道", "巷", "弄", "号", "室", "栋", "单元", "楼")
_PII_SEARCH = re.compile(
    r"(?<!\d)1[3-9]\d{9}(?!\d)"                      # 手机号
    r"|(?<![\dA-Za-z])\d{17}[\dXx](?![\dA-Za-z])"     # 18 位证件号
    r"|[\w.+-]+@[\w-]+\.[\w.-]+"                      # 邮箱
    r"|(?<!\d)\d{16,19}(?!\d)"                        # 银行卡
)

CODE_MAX_DISTINCT = 20
_CODE_MAX_LENGTH = 32


def tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    return [part for part in re.split(r"[^0-9a-z一-鿿]+", spaced.lower()) if part]


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def _name_category(column: str, table: str, comment: str) -> str | None:
    parts = tokens(column)
    joined = "_" + "_".join(parts) + "_"
    text = f"{column} {comment}"
    words = set(parts)

    if words & _SECRET_TOKENS or any(f"_{p}_" in joined for p in _SECRET_PHRASES) or any(z in text for z in _SECRET_ZH):
        return "secret"
    if words & _PHONE_TOKENS or any(z in text for z in _PHONE_ZH):
        return "phone"
    if any(f"_{p}_" in joined for p in _ID_PHRASES) or any(z in text for z in _ID_ZH):
        return "id_number"
    if words & _BIRTH_TOKENS or any(z in text for z in _BIRTH_ZH):
        return "birth_date"
    if words & _EMAIL_TOKENS or "e_mail" in joined or any(z in text for z in _EMAIL_ZH):
        return "email"
    if any(f"_{p}_" in joined for p in _CARD_PHRASES) or any(z in text for z in _CARD_ZH):
        return "bank_card"
    if words & _ADDRESS_TOKENS or any(z in text for z in _ADDRESS_ZH):
        return "address"
    if words & _PERSON_NAME_TOKENS or any(z in text for z in _NAME_ZH):
        return "person_name"
    if "name" in words:
        index = parts.index("name")
        before = {_singular(word) for word in parts[:index]}
        if before & _PERSON_WORDS:
            return "person_name"
        table_words = {_singular(word) for word in tokens(table)}
        if not before and table_words & _PERSON_WORDS:
            return "person_name"
    if words & _AMOUNT_TOKENS or any(z in text for z in _AMOUNT_ZH):
        return "amount"
    return None


def _is_business_label(column: str) -> bool:
    parts = tokens(column)
    if parts == ["sku"]:
        return True
    return len(parts) >= 2 and parts[-1] in _BUSINESS_SUFFIX and _singular(parts[0]) in _BUSINESS_PREFIX


def _looks_like_address(text: str) -> bool:
    return any(char.isdigit() for char in text) and sum(mark in text for mark in _ADDRESS_MARKS) >= 3


def _value_category(values: list[str]) -> str | None:
    """过半取值完整匹配某类个人信息就归那一类。"""
    if not values:
        return None
    half = (len(values) + 1) // 2
    compact = [re.sub(r"[\s-]", "", value) for value in values]
    checks = (
        ("phone", lambda i: _PHONE_VALUE.fullmatch(compact[i])),
        ("id_number", lambda i: _ID_VALUE.fullmatch(compact[i])),
        ("email", lambda i: _EMAIL_VALUE.fullmatch(values[i].strip())),
        ("bank_card", lambda i: _CARD_VALUE.fullmatch(compact[i])),
        ("address", lambda i: _looks_like_address(values[i])),
    )
    for category, matches in checks:
        if sum(1 for index in range(len(values)) if matches(index)) >= half:
            return category
    return None


def has_pii(values: Iterable[str]) -> bool:
    return any(_PII_SEARCH.search(value) for value in values)


def is_code_column(values: list[str]) -> bool:
    """低基数代码列（状态、类型）：取值少、重复多、都短。没有取值时判断不了。"""
    if not values:
        return False
    distinct = set(values)
    return (len(distinct) <= CODE_MAX_DISTINCT and len(distinct) * 2 <= len(values)
            and all(len(value) <= _CODE_MAX_LENGTH for value in distinct))


def classify(column: str, *, table: str = "", comment: str = "", type_class: str = "text",
             values: Iterable[Any] = (), join_key: bool = False) -> str:
    """列的敏感类别（`sensitivity`）。`type_class` 取 numeric / text / temporal / bool / binary。"""
    by_name = _name_category(column, table, comment or "")
    if by_name and by_name != "amount":
        return by_name
    texts = [str(plain(value)) for value in values if value is not None]
    by_value = _value_category(texts) if type_class in ("text", "numeric") else None
    if by_value:
        return by_value
    if join_key:
        return "join_key"
    if by_name == "amount":
        return "amount"
    if type_class == "binary":
        return "binary"
    if type_class != "text":
        return "none"
    if has_pii(texts):
        return "text"
    if _is_business_label(column) or is_code_column(texts):
        return "none"
    return "text"


def is_sensitive(category: str) -> bool:
    return category != "none"


# ---- 处置 ----


def plain(value: Any) -> Any:
    """驱动返回的值转成记录里能放的：字符串、整数、布尔、null。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    return str(value)


def _keep(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return "*" * len(text)
    return text[:head] + "*" * (len(text) - head - tail) + (text[-tail:] if tail else "")


def bucket(value: Any) -> str:
    """金额按数量级分桶：`[1000,10000)`；负数前面加 `-`，绝对值小于 1 是 `(0,1)`。"""
    try:
        number = decimal.Decimal(str(plain(value)).strip())
    except (decimal.InvalidOperation, ValueError):
        return "***"
    if not number.is_finite():
        return "***"
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    if magnitude < 1:
        return f"{sign}(0,1)"
    exponent = magnitude.adjusted()
    return f"{sign}[{10 ** exponent},{10 ** (exponent + 1)})"


@dataclass
class Masker:
    """按类别处置一个取值。盐为空时用本次随机盐（样本之间仍能关联，跨快照不行），`ephemeral_salt` 标出来。"""

    salt: str = ""

    def __post_init__(self):
        self.ephemeral_salt = not self.salt
        if self.ephemeral_salt:
            self.salt = secrets.token_hex(16)

    def digest(self, value: Any) -> str:
        return hashlib.sha256(f"{self.salt}:{plain(value)}".encode("utf-8")).hexdigest()[:12]

    def apply(self, category: str, value: Any) -> Any:
        if value is None:
            return None
        treatment = TREATMENT.get(category, "mask")
        if treatment is None:
            return plain(value)
        if treatment == "hash":
            return self.digest(value)
        if treatment == "bucket":
            return bucket(value)
        if treatment == "drop":
            return None
        return mask(category, plain(value))


def mask(category: str, value: Any) -> str:
    text = str(value)
    if category == "phone":
        return _keep(text, 3, 4)
    if category == "id_number":
        return _keep(text, 2, 2)
    if category == "person_name":
        return _keep(text, 1, 0)
    if category == "bank_card":
        return _keep(text, 0, 4)
    if category == "birth_date":
        return text[:4] + "-**-**" if re.match(r"\d{4}", text) else "***"
    if category == "email":
        local, _, domain = text.partition("@")
        tld = domain.rsplit(".", 1)[-1] if "." in domain else "***"
        return f"{local[:1]}***@***.{tld}"
    if category == "address":
        return text[:3] + "***" if text[:1] and "一" <= text[0] <= "鿿" else "***"
    if category == "binary":
        return "<binary>"
    return "***"
