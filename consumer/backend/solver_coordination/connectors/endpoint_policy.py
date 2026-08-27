"""Administrator-controlled endpoint policy for the Connector boundary.

The platform never accepts a Connector URL from a knowledge package or a user
request.  This policy is applied again immediately before every HTTP call so a
mutable binding mapping cannot turn the generic client into an SSRF primitive.
Deployments should construct it from an explicit host allowlist. HTTPS remains
the default; a first-release private-network deployment may explicitly enable
HTTP without making plaintext endpoints generally acceptable. The default
additionally permits only reserved example names and loopback names used by
synthetic tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


class EndpointPolicyError(ValueError):
    """Raised when an endpoint is outside the administrator policy."""


_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$", re.IGNORECASE)
_DEFAULT_RESERVED_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".test",
)


def _canonical_host(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EndpointPolicyError("endpoint host is invalid")
    host = value.strip().rstrip(".").lower()
    if ":" in host:
        # urlsplit strips brackets for IPv6; IP literals are deliberately not
        # accepted by the default policy, even when they are loopback values.
        raise EndpointPolicyError("endpoint IP literals are not allowed")
    try:
        parsed_address = ipaddress.ip_address(host)
    except ValueError:
        parsed_address = None
    if parsed_address is not None:
        raise EndpointPolicyError("endpoint IP literals are not allowed")
    if not _HOST_RE.fullmatch(host):
        raise EndpointPolicyError("endpoint host is invalid")
    return host


@dataclass(frozen=True)
class EndpointPolicy:
    """Allow only administrator-approved Connector endpoints.

    ``allowed_hosts`` is exact-match.  If omitted, only reserved example
    domains and localhost are accepted, which keeps synthetic fixtures useful
    without silently allowing arbitrary production destinations.
    """

    allowed_hosts: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = (443,)
    # Plain HTTP is an explicit deployment choice for a private network. It is
    # fail-closed by default and never inferred from the selected port.
    allow_http: bool = False
    # ``None`` means synthetic-default behavior: localhost is allowed only
    # when no production host allowlist is present.  An explicit bool can be
    # used by local integration tests or deployment policy.
    allow_localhost: bool | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "EndpointPolicy":
        """Build the administrator endpoint policy from process configuration.

        The host allowlist is intentionally exact-match.  With no allowlist
        configured, only reserved test names (and localhost) are accepted;
        this keeps synthetic fixtures usable while failing closed for arbitrary
        production destinations.  A private production hostname therefore
        requires an explicit ``SOLVER_SERVICE_ALLOWED_HOSTS`` entry.

        ``environ`` is injectable for deterministic tests; production callers
        use :mod:`os.environ` by default.  Malformed configuration raises
        ``EndpointPolicyError`` instead of silently widening the policy.
        """

        values: Mapping[str, str] = os.environ if environ is None else environ
        raw_hosts = values.get("SOLVER_SERVICE_ALLOWED_HOSTS")
        raw_ports = values.get("SOLVER_SERVICE_ALLOWED_PORTS")
        raw_localhost = values.get("SOLVER_SERVICE_ALLOW_LOCALHOST")
        raw_http = values.get("SOLVER_SERVICE_ALLOW_HTTP")

        allowed_hosts = cls._csv_values(raw_hosts, "SOLVER_SERVICE_ALLOWED_HOSTS")
        allowed_ports = cls._ports(raw_ports)
        # A production host allowlist is an explicit deployment posture; do
        # not silently retain the synthetic localhost exception alongside it.
        # Operators can still opt in explicitly for local integration tests.
        allow_localhost = cls._boolean(
            raw_localhost,
            default=(raw_hosts is None or raw_hosts == ""),
            name="SOLVER_SERVICE_ALLOW_LOCALHOST",
        )
        allow_http = cls._boolean(
            raw_http,
            default=False,
            name="SOLVER_SERVICE_ALLOW_HTTP",
        )
        return cls(
            allowed_hosts=allowed_hosts,
            allowed_ports=allowed_ports,
            allow_http=allow_http,
            allow_localhost=allow_localhost,
        )

    @staticmethod
    def _csv_values(raw: Any, name: str) -> tuple[str, ...]:
        if raw is None or raw == "":
            return ()
        if not isinstance(raw, str):
            raise EndpointPolicyError("{} must be a comma-separated string".format(name))
        values = tuple(item.strip() for item in raw.split(","))
        if not values or any(not item for item in values):
            raise EndpointPolicyError("{} contains an empty entry".format(name))
        return values

    @classmethod
    def _ports(cls, raw: Any) -> tuple[int, ...]:
        if raw is None or raw == "":
            return (443,)
        if not isinstance(raw, str):
            raise EndpointPolicyError(
                "SOLVER_SERVICE_ALLOWED_PORTS must be comma-separated"
            )
        items = tuple(item.strip() for item in raw.split(","))
        if not items or any(not item for item in items):
            raise EndpointPolicyError(
                "SOLVER_SERVICE_ALLOWED_PORTS contains an empty entry"
            )
        try:
            ports = tuple(int(item, 10) for item in items)
        except (TypeError, ValueError) as exc:
            raise EndpointPolicyError(
                "SOLVER_SERVICE_ALLOWED_PORTS contains an invalid port"
            ) from exc
        return ports

    @staticmethod
    def _boolean(
        raw: Any,
        *,
        default: bool = True,
        name: str = "SOLVER_SERVICE_ALLOW_LOCALHOST",
    ) -> bool:
        if raw is None or raw == "":
            return default
        if not isinstance(raw, str):
            raise EndpointPolicyError("{} must be true or false".format(name))
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise EndpointPolicyError("{} must be true or false".format(name))

    def __post_init__(self) -> None:
        hosts = tuple(_canonical_host(host) for host in self.allowed_hosts)
        if len(set(hosts)) != len(hosts):
            raise EndpointPolicyError("endpoint host allowlist contains duplicates")
        ports = tuple(self.allowed_ports)
        if not ports or any(
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            for port in ports
        ):
            raise EndpointPolicyError("endpoint port allowlist is invalid")
        if len(set(ports)) != len(ports):
            raise EndpointPolicyError("endpoint port allowlist contains duplicates")
        if not isinstance(self.allow_http, bool):
            raise EndpointPolicyError("allow_http must be boolean")
        allow_localhost = self.allow_localhost
        if allow_localhost is None:
            allow_localhost = not hosts
        if not isinstance(allow_localhost, bool):
            raise EndpointPolicyError("allow_localhost must be boolean")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_ports", ports)
        object.__setattr__(self, "allow_localhost", allow_localhost)

    def validate(self, endpoint: str) -> str:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise EndpointPolicyError("endpoint is required")
        if endpoint != endpoint.strip():
            raise EndpointPolicyError("endpoint must not contain surrounding whitespace")
        if any(
            ord(char) < 0x20
            or ord(char) == 0x7F
            or char in {"\\", "\uFF0F", "\uFF03", "\uFF1F"}
            for char in endpoint
        ):
            raise EndpointPolicyError("endpoint contains control characters")
        try:
            parsed = urlsplit(endpoint)
        except ValueError as exc:
            # ``urlsplit`` raises for malformed bracketed IPv6/user-info
            # forms.  Convert it to the boundary's stable policy error.
            raise EndpointPolicyError("endpoint URL is invalid") from exc
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise EndpointPolicyError("endpoint must use HTTP or HTTPS")
        if scheme == "http" and not self.allow_http:
            raise EndpointPolicyError("endpoint HTTP is not enabled")
        if parsed.username or parsed.password or parsed.query:
            raise EndpointPolicyError("endpoint contains unsafe URL components")
        if parsed.fragment or "?" in endpoint or "#" in endpoint:
            raise EndpointPolicyError("endpoint contains unsafe URL components")
        if any(char in parsed.netloc for char in {"\\", "%"}):
            raise EndpointPolicyError("endpoint contains unsafe URL components")
        if parsed.path not in {"", "/"}:
            raise EndpointPolicyError("endpoint contains unsafe URL components")
        try:
            host = _canonical_host(parsed.hostname)
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise EndpointPolicyError("endpoint port is invalid") from exc
        if port not in self.allowed_ports:
            raise EndpointPolicyError("endpoint port is not allowlisted")
        allowed = host in self.allowed_hosts
        if self.allow_localhost and host == "localhost":
            allowed = True
        if not allowed and not self.allowed_hosts and any(
            host == suffix.lstrip(".") or host.endswith(suffix)
            for suffix in _DEFAULT_RESERVED_SUFFIXES
        ):
            allowed = True
        if not allowed:
            raise EndpointPolicyError("endpoint host is not allowlisted")
        # Return one canonical API base.  Callers append only the fixed
        # ``/api/v1/...`` protocol paths; administrator input cannot smuggle a
        # prefix, proxy route, or encoded path into those requests.
        normalized = "{}://{}".format(scheme, host)
        if parsed.port is not None:
            normalized += ":{}".format(port)
        return normalized


__all__ = ["EndpointPolicy", "EndpointPolicyError"]
