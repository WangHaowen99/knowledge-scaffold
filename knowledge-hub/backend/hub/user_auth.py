"""Online identity validation against the trusted account service, never Onto."""
from dataclasses import dataclass
import base64
import binascii
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .package_contract import PackageError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Identity:
    subject: str
    service: str
    issuer: str = ""
    sub: str = ""
    delegation_id: str = ""
    delegation_ontology_id: str = ""
    delegation_nonce: str = ""
    delegation_expires_at: float = 0


def authenticate(request):
    supplied = request.headers.get('x-hub-service-token', '')
    # The existing Hub credential identifies the modeling service for all APIs.
    configured = {
        'modeling': os.environ.get('HUB_SERVICE_TOKEN', ''),
        'evaluation': os.environ.get('HUB_EVALUATION_SERVICE_TOKEN', ''),
        'consumer': os.environ.get('HUB_CONSUMER_SERVICE_TOKEN', ''),
    }
    enabled = [value for value in configured.values() if value]
    if len(enabled) != len(set(enabled)):
        raise PackageError('auth_configuration', 'Service credentials must be distinct', 503, 'authorization')
    service = next((name for name, value in configured.items()
                    if value and hmac.compare_digest(value.encode(), supplied.encode())), None)
    header = request.headers.get('authorization', '')
    delegated = request.headers.get('x-hub-delegation', '')
    if not service:
        raise PackageError('unauthenticated', 'User and service authentication required', 401, 'authorization')
    if delegated:
        return authenticate_delegation(request, delegated, service, configured[service])
    if not header.lower().startswith('bearer ') or not header[7:].strip():
        raise PackageError('unauthenticated', 'User and service authentication required', 401, 'authorization')
    base = os.environ.get('HUB_AUTH_URL', '').rstrip('/')
    url = urllib.parse.urlsplit(base)
    has_extras = any((url.username, url.password, url.query, url.fragment))
    if url.scheme not in ('http', 'https') or not url.hostname or has_extras:
        raise PackageError('auth_configuration', 'Account service is not configured', 503, 'authorization')
    # /userinfo verifies signature, issuer, audience, access type and current token
    # version in the existing auth-server. No positive authorization cache is used.
    try:
        req = urllib.request.Request(f'{base}/userinfo', headers={'Authorization': f'Bearer {header[7:].strip()}'})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=8) as response:
            raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError('Oversized identity response')
            data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('sub'), str) or not 0 < len(data['sub']) <= 256:
            raise ValueError('Invalid identity response')
        if data.get('password_change_required'):
            raise PackageError('password_change_required', 'Account action required', 403, 'authorization')
    except urllib.error.HTTPError as exc:
        status = 401 if exc.code in (401, 403) else 503
        raise PackageError('identity_unavailable', 'Identity verification failed', status, 'authorization') from exc
    # URLError is an OSError.
    except (OSError, ValueError) as exc:
        raise PackageError('identity_unavailable', 'Identity verification unavailable', 503, 'authorization') from exc
    issuer = os.environ.get('HUB_AUTH_ISSUER', '').strip() or base
    return Identity(subject_id(issuer, data['sub']), service, issuer, data['sub'])


def authenticate_delegation(request, supplied, service, secret):
    import time
    if service != 'modeling':
        raise PackageError('delegation_forbidden', 'Delegated access is not allowed for this service', 403, 'authorization')
    try:
        encoded, signature = supplied.split('.', 1)
        expected = hmac.new(secret.encode(), b'hub-delegation-v1.' + encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError
        raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        payload = json.loads(raw)
        now = time.time()
        if (not isinstance(payload, dict) or payload.get('v') != 1
                or payload.get('method') != request.method
                or payload.get('path') != request.url.path
                or not isinstance(payload.get('iat'), (int, float))
                or not isinstance(payload.get('exp'), (int, float))
                or payload['exp'] - payload['iat'] > 300
                or payload['iat'] > now + 60 or payload['exp'] < now - 60
                or not isinstance(payload.get('delegation_id'), str)
                or not isinstance(payload.get('ontology_id'), str)
                or not isinstance(payload.get('issuer'), str)
                or not isinstance(payload.get('sub'), str)
                or not isinstance(payload.get('nonce'), str)):
            raise ValueError
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error):
        raise PackageError('invalid_delegation', 'Delegated authorization is invalid or expired', 401, 'authorization') from None
    return Identity(subject_id(payload['issuer'], payload['sub']), service, payload['issuer'], payload['sub'],
                    payload['delegation_id'], payload['ontology_id'], payload['nonce'], payload['exp'])


def require_direct(identity):
    if identity.delegation_id:
        raise PackageError('delegation_forbidden', 'This operation requires direct user authorization', 403, 'authorization')


def subject_id(issuer, sub):
    return hashlib.sha256(json.dumps([issuer, sub], ensure_ascii=False).encode()).hexdigest()


def validate_targets(users):
    """Validate identity syntax and issuer; callers resolve target accounts."""
    from .package_contract import AuthorizedUser
    users = [AuthorizedUser.model_validate(user).model_dump() for user in users]
    issuer = os.environ.get('HUB_AUTH_ISSUER', '').strip() or os.environ.get('HUB_AUTH_URL', '').rstrip('/')
    if not issuer:
        raise PackageError('auth_configuration', 'Trusted issuer is not configured', 503, 'authorization')
    if any(user['issuer'] != issuer for user in users):
        raise PackageError('invalid_target', 'Unknown account issuer', 422, 'authorization')
    return users
