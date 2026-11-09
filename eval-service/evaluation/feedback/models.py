"""FeedbackSignal v1 as sent by consumer; unknown fields are ignored so consumer may add fields without a new version.

Field limits are those consumer truncates to before sending. A signal that still breaks them, or misses a field
its type requires, is rejected on its own; the rest of the batch is kept.

The shared contract contracts/flywheel/feedback-signal.v1.schema.json is generated from these models; in a source
checkout, where contracts/ sits beside eval-service, this rewrites it:
    cd eval-service && python -m evaluation.feedback.models
"""
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SIGNAL_SCHEMA = 'consumer-feedback-signal.v1'
BATCH_SCHEMA = 'consumer-feedback-signal-batch.v1'
BATCH_LIMIT = 50
SIGNAL_BYTES = 256 * 1024
IDENTIFIER = r'^[A-Za-z0-9_.:-]{1,200}$'


class Part(BaseModel):
    model_config = ConfigDict(extra='ignore')


class Origin(Part):
    service: str = Field(default='consumer', max_length=64)
    workspace_id: str = Field(default='', max_length=200)
    session_id: str = Field(default='', max_length=200)
    run_id: str = Field(default='', max_length=200)
    turn_id: int | None = None


class Actor(Part):
    user_id: str = Field(min_length=1, max_length=256)


class Package(Part):
    ontology_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    version: str = Field(default='', max_length=64)
    content_digest: str = Field(default='', max_length=128)


class QA(Part):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(default='', max_length=8000)
    turn_status: str = Field(default='', max_length=64)
    error_code: str | None = Field(default=None, max_length=128)
    conversation_context: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def _context(self):
        if any(len(item) > 500 for item in self.conversation_context):
            raise ValueError('conversation context entries are limited to 500 characters')
        return self


class Vote(Part):
    vote: Literal['up', 'down', 'none']
    previous_vote: str | None = Field(default=None, max_length=8)
    reason_category: Literal['wrong_answer', 'missing_data', 'wrong_process', 'other'] | None = None
    comment: str = Field(default='', max_length=2000)
    expected_answer: str = Field(default='', max_length=4000)


class ScopeHint(Part):
    operation_key: str | None = Field(default=None, max_length=200)
    entity_key: str | None = Field(default=None, max_length=200)
    kind_hint: str | None = Field(default=None, max_length=32)


class Suggestion(Part):
    content: str = Field(min_length=1, max_length=4000)
    scope_hint: ScopeHint = Field(default_factory=ScopeHint)


class Detection(Part):
    detected_by: Literal['user_vote', 'user_correction', 'system_rule']
    rules: list[str] = Field(default_factory=list, max_length=50)


class Correction(Part):
    text: str = Field(min_length=1, max_length=2000)
    matched: str = Field(default='', max_length=200)
    next_answer: str = Field(default='', max_length=2000)


class ToolFailure(Part):
    tool: str = Field(default='', max_length=200)
    error_code: str = Field(default='', max_length=200)
    message: str = Field(default='', max_length=2000)


class ToolEvent(Part):
    index: int = 0
    tool: str = Field(default='', max_length=200)
    arguments: str = Field(default='', max_length=1000)
    result: str = Field(default='', max_length=2000)
    failed: bool = False


class Document(Part):
    path: str = Field(default='', max_length=500)
    version: str = Field(default='', max_length=64)


class Evidence(Part):
    evidence_level: str = Field(default='', max_length=32)
    partial: bool = False
    read_documents: list[Document] = Field(default_factory=list, max_length=50)
    tool_failures: list[ToolFailure] = Field(default_factory=list, max_length=50)
    iteration_count: int | None = None
    tool_event_total: int | None = None
    tool_events: list[ToolEvent] = Field(default_factory=list, max_length=40)


def _present(*names):
    return {'required': list(names), 'properties': {name: {'type': 'object'} for name in names}}


def _kind(value, **extra):
    return {'required': ['signal_type', *extra.pop('required', [])],
            'properties': {'signal_type': {'const': value}, **extra.pop('properties', {})}}


# The parts each signal type carries, the same rules as FeedbackSignalV1._required_parts, for JSON Schema users.
TYPE_RULES = [
    {'if': _kind('turn_feedback'), 'then': _present('qa', 'feedback')},
    {'if': _kind('error_qa'), 'then': _present('qa', 'error_detection')},
    {'if': _kind('error_qa', required=['error_detection'], properties={'error_detection': {
        'type': 'object', 'required': ['detected_by'], 'properties': {'detected_by': {'const': 'user_correction'}}}}),
     'then': _present('correction')},
    {'if': _kind('user_suggestion'), 'then': _present('suggestion', 'package')},
]


class FeedbackSignalV1(Part):
    model_config = ConfigDict(extra='ignore', json_schema_extra={'allOf': TYPE_RULES})
    schema_: Literal[SIGNAL_SCHEMA] = Field(alias='schema')
    signal_id: str = Field(pattern=r'^sig_[0-9a-f]{32}$')
    signal_type: Literal['turn_feedback', 'error_qa', 'user_suggestion']
    signal_seq: int = Field(default=0, ge=0, le=10 ** 9)
    occurred_at: str = Field(default='', max_length=64)
    origin: Origin = Field(default_factory=Origin)
    actor: Actor
    package: Package | None = None
    qa: QA | None = None
    feedback: Vote | None = None
    suggestion: Suggestion | None = None
    error_detection: Detection | None = None
    correction: Correction | None = None
    evidence: Evidence | None = None

    @model_validator(mode='after')
    def _required_parts(self):
        """The parts each signal type carries (FeedbackSignal v1, 8.2)."""
        if self.signal_type == 'turn_feedback' and not (self.qa and self.feedback):
            raise ValueError('turn feedback needs the question, the answer and the vote')
        if self.signal_type == 'error_qa' and not (self.qa and self.error_detection):
            raise ValueError('an erroneous answer needs the question, the answer and how it was detected')
        if self.signal_type == 'error_qa' and self.error_detection.detected_by == 'user_correction' and not self.correction:
            raise ValueError('a user correction needs the correction text')
        if self.signal_type == 'user_suggestion' and not (self.suggestion and self.package):
            raise ValueError('a suggestion needs its content and the package it concerns')
        return self


def contract():
    """The shared JSON Schema of FeedbackSignal v1, as the evaluation service accepts it."""
    schema = FeedbackSignalV1.model_json_schema(by_alias=True)
    schema.pop('title', None)
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'https://example.com/contracts/flywheel/feedback-signal.v1.schema.json',
            'title': SIGNAL_SCHEMA,
            '$comment': (f'Generated from eval-service/evaluation/feedback/models.py; do not edit by hand. '
                         f'Consumer posts batches {{"schema": "{BATCH_SCHEMA}", "signals": [...]}} of 1-{BATCH_LIMIT} signals '
                         f'from one user to POST /evaluation/api/internal/feedback-signals; each signal at most {SIGNAL_BYTES // 1024} KB. '
                         'Receivers ignore unknown fields; removing a field or changing its meaning needs v2.'),
            **schema}


CONTRACT = Path(__file__).resolve().parents[3] / 'contracts' / 'flywheel' / 'feedback-signal.v1.schema.json'


def write_contract(path=CONTRACT):
    Path(path).write_text(json.dumps(contract(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    write_contract()
