"""Data models for standalone skill parser."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FileMapItem:
    source: str
    target: str


@dataclass(frozen=True)
class OperationSummary:
    name: str
    tag: str
    business_desc: str
