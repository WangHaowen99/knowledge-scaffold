"""Hub wire contracts. Project and workspace identities are deliberately absent."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .storage import HubError

Identifier = Annotated[str, Field(pattern=r'^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,127}$')]
Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Kind = Literal['release', 'evaluation_snapshot']


class PackageError(HubError):
    def __init__(self, code, message, status=400, phase='validation'):
        super().__init__(message, status)
        self.code, self.phase = code, phase

    def payload(self):
        return dict(code=self.code, phase=self.phase, retryable=self.status == 503, message=str(self))


class PublishBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: Identifier
    ontology_id: Identifier
    change_type: Literal['modeling_content', 'external_import', 'mapping_update']
    kind: Kind = 'release'
    input_digest: Digest
    version: Annotated[str, Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9.+_-]{0,63}$')] | None = None
    base_release_id: Identifier | None = None
    base_digest: Digest | None = None

    @model_validator(mode='after')
    def check_reference(self):
        import re
        if self.version and re.fullmatch(r'0\.1\.[0-9]+', self.version) and len(self.version.split('.')[2]) > 12:
            raise ValueError('Version sequence exceeds supported range')
        if bool(self.base_release_id) != bool(self.base_digest):
            raise ValueError('Base version and digest must be supplied together')
        if self.change_type == 'mapping_update' and not self.base_release_id:
            raise ValueError('Mapping update requires a base version')
        if (self.change_type == 'external_import') != bool(self.version):
            raise ValueError('Only external import supplies a version')
        return self


def check_capability(service, payload):
    expected = {'modeling': 'modeling_content', 'consumer': 'external_import', 'evaluation': 'mapping_update'}
    if expected.get(service) != payload['change_type'] or (
            service == 'consumer' and payload['kind'] != 'release'):
        raise PackageError('service_forbidden', 'Service capability denied', 403, 'authorization')


class AuthorizedUser(BaseModel):
    model_config = ConfigDict(extra='forbid')
    issuer: Annotated[str, Field(min_length=1, max_length=2048)]
    sub: Annotated[str, Field(min_length=1, max_length=256)]


class GrantBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    users: Annotated[list[AuthorizedUser], Field(min_length=1, max_length=100)]
