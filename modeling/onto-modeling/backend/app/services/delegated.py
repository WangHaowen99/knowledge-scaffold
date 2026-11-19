"""A registered delegation standing in for a user's login on calls to the evaluation service."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Delegated:
    """Works only together with this service's own credential; kept out of logs and reprs all the same."""
    handle: str = field(repr=False)
