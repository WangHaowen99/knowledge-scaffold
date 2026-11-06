"""Receive consumer feedback: check who sends it and for whom, then store each signal once, encrypted.

Every request carries the submitting user's login, which says for whom, and consumer's service token, which says
who sends it; the service token never replaces the user. Nothing here calls a model or Hub: signals are stored and
the background processing is woken, so consumer's deliverer gets its answer at once.
"""
import hashlib
import secrets
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from ..core import Failure, canonical, digest
from .models import BATCH_LIMIT, BATCH_SCHEMA, SIGNAL_BYTES, SIGNAL_SCHEMA, FeedbackSignalV1
from .store import FeedbackStore

DAY = 86400
RETENTION_DAYS = 90
REASONS = {'schema_invalid': '信号格式不符合 FeedbackSignal v1', 'actor_mismatch': '信号提交者与登录用户不一致',
           'signal_too_large': '单条信号超过 256 KB', 'payload_mismatch': '同一信号标识对应不同内容'}


def run_key(workspace_id, run_id):
    """One consumer run, without keeping its workspace or run identifiers in plain text."""
    return hashlib.sha256(f'{workspace_id}|{run_id}'.encode()).hexdigest() if run_id else None


class Intake:
    def __init__(self, config, store, identity, wake=None, clock=time.time):
        self.config, self.store, self.identity, self.wake, self.clock = config, store, identity, wake, clock
        self.signals = FeedbackStore(store)

    def authorized(self, supplied):
        """The consumer service token: set, at least 32 characters, unlike the other service tokens, and equal."""
        expected = (self.config.flywheel_token or '').strip()
        others = {self.config.hub_token, self.config.consumer_token, self.config.modeling_service_token} - {''}
        return (len(expected) >= 32 and expected not in others and bool(supplied)
                and secrets.compare_digest(supplied.encode(), expected.encode()))

    def caller(self, token):
        try:
            return self.identity.profile(token)
        except Failure as exc:
            if exc.status == 401 or exc.upstream_status == 401:
                raise Failure('invalid_identity', '用户登录无效或已过期', 401, 'authorization') from exc
            if exc.status == 403:
                raise
            raise Failure('dependency_unavailable', '暂时无法校验用户登录，请稍后重试', 503, 'authorization') from exc

    def health(self, token, service_token):
        """Consumer's connectivity check: its address, its service token and a user's login, before it pushes feedback.

        The service token is checked first, so a caller without it cannot use this to reach the account service.
        It answers while receiving is off too, with feedback_enabled false, so consumer can say so."""
        if not self.authorized(service_token):
            raise Failure('service_forbidden', '服务凭据无效', 403, 'authorization')
        self.caller(token)
        return {'status': 'ok', 'feedback_enabled': self.config.flag('feedback_enabled'),
                'accepted_schemas': [SIGNAL_SCHEMA, BATCH_SCHEMA],
                'server_time': datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}

    def receive(self, body, token, service_token):
        if not self.config.flag('feedback_enabled'):
            raise Failure('feedback_disabled', '评测服务未开启消费反馈接收', 503, 'feedback')
        if not self.authorized(service_token):
            raise Failure('service_forbidden', '服务凭据无效', 403, 'authorization')
        profile = self.caller(token)
        signals = body.get('signals') if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get('schema') != BATCH_SCHEMA or not isinstance(signals, list) \
                or not 0 < len(signals) <= BATCH_LIMIT:
            raise Failure('invalid_input', f'需要 {BATCH_SCHEMA}，每批 1–{BATCH_LIMIT} 条信号', 422)
        now = self.clock()
        purge_after = now + self.config.number('feedback_retention_days', RETENTION_DAYS) * DAY
        results = [self.one(item, profile, now, purge_after) for item in signals]
        if self.wake and any(result['status'] == 'accepted' for result in results):
            self.wake()
        return {'results': results, 'received_at': datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}

    def one(self, item, profile, now, purge_after):
        signal_id = item.get('signal_id') if isinstance(item, dict) and isinstance(item.get('signal_id'), str) else ''
        if len(canonical(item).encode()) > SIGNAL_BYTES:
            return self.rejected(signal_id, 'signal_too_large')
        try:
            signal = FeedbackSignalV1.model_validate(item)
        except ValidationError:
            return self.rejected(signal_id[:64], 'schema_invalid')
        if signal.actor.user_id != profile['username']:
            return self.rejected(signal.signal_id, 'actor_mismatch')
        columns = dict(signal_type=signal.signal_type, signal_seq=signal.signal_seq,
                       detected_by=signal.error_detection.detected_by if signal.error_detection else None,
                       vote=signal.feedback.vote if signal.feedback else None,
                       run_key=run_key(signal.origin.workspace_id, signal.origin.run_id),
                       ontology_id=signal.package.ontology_id if signal.package else None,
                       package_version=signal.package.version if signal.package else None)
        # The user name stays out of the store: the subject digest is enough to count distinct users.
        kept = {key: value for key, value in item.items() if key != 'actor'}
        status, intake_id = self.signals.insert(signal.signal_id, profile['subject'], columns, digest(item), kept, now, purge_after)
        if status == 'conflict':
            return {'signal_id': signal.signal_id, 'status': 'conflict', 'code': 'payload_mismatch', 'message': REASONS['payload_mismatch']}
        return {'signal_id': signal.signal_id, 'status': status, 'intake_id': intake_id}

    @staticmethod
    def rejected(signal_id, code):
        return {'signal_id': signal_id, 'status': 'rejected', 'code': code, 'message': REASONS[code]}
