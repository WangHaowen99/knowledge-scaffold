"""URL imports refuse internal addresses at the entry and never echo them back (reserved example addresses only)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest

from kb_service import documents, url_guard
from kb_service.parsing import ParseError, docreader_client, parse_url

DOCUMENTATION_NET = ipaddress.ip_network("203.0.113.0/24")  # stands in for a public address below


def _teredo(server: str, client: str) -> str:
    value = (0x20010000 << 96) | (int(ipaddress.IPv4Address(server)) << 64)
    return str(ipaddress.IPv6Address(value | (0xFFFFFFFF ^ int(ipaddress.IPv4Address(client)))))


def _resolver(monkeypatch, addresses):
    async def resolve(host, port):
        if isinstance(addresses, Exception):
            raise addresses
        return list(addresses)

    monkeypatch.setattr(url_guard, "_resolve", resolve)


def _treat_documentation_net_as_public(monkeypatch):
    real = url_guard._reachable
    monkeypatch.setattr(url_guard, "_reachable", lambda ip: ip in DOCUMENTATION_NET or real(ip))


def _reject(url: str) -> url_guard.UrlRejected:
    with pytest.raises(url_guard.UrlRejected) as info:
        asyncio.run(url_guard.check_public_url(url))
    return info.value


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:5198/healthz",
        "http://localhost:8080/healthz",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped
        "http://[::127.0.0.1]/",  # IPv4-compatible
        "http://[64:ff9b::7f00:1]/",  # NAT64
        "http://[64:ff9b:1::c000:20a]/",  # local-use NAT64
        "http://[2002:7f00:1::]/",  # 6to4
        f"http://[{_teredo('192.0.2.1', '127.0.0.1')}]/",  # Teredo
        "http://2130706433/",  # 127.0.0.1 written as one number
        "http://0x7f.1/",
        "http://192.0.2.10/",
    ],
)
def test_internal_address_literals_are_refused(url, monkeypatch):
    _resolver(monkeypatch, AssertionError("literal addresses need no lookup"))
    error = _reject(url)
    assert error.code == "url_not_allowed" and error.message == url_guard.BLOCKED_MESSAGE


def test_embedded_ipv4_decides_even_when_the_ipv6_prefix_looks_public(monkeypatch):
    _treat_documentation_net_as_public(monkeypatch)
    assert url_guard.is_public_address("64:ff9b::cb00:7114")  # NAT64 to 203.0.113.20
    assert not url_guard.is_public_address("64:ff9b::c633:6407")  # NAT64 to 198.51.100.7


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1\\@public.example.com/",  # host 127.0.0.1 to some fetchers
        "http://example.com\\..\\healthz",
        "http://user@example.com/",
        "http://user:secret@example.com/",
        "http://example.com/\x00",
        "http://exam\tple.com/",
        "http://example.com/a b",
    ],
)
def test_ambiguous_urls_are_refused_before_any_lookup(url, monkeypatch):
    _resolver(monkeypatch, AssertionError("an ambiguous URL must not be resolved"))
    assert _reject(url).code == "bad_url"


def test_a_name_resolving_inside_is_refused_without_naming_the_address(monkeypatch):
    _resolver(monkeypatch, ["203.0.113.20", "198.51.100.7"])  # one internal answer is enough
    _treat_documentation_net_as_public(monkeypatch)
    error = _reject("http://service.example.net:8080/healthz")
    assert error.code == "url_not_allowed" and "198.51.100.7" not in error.message


def test_public_names_pass_and_unresolvable_names_are_refused(monkeypatch):
    _treat_documentation_net_as_public(monkeypatch)
    _resolver(monkeypatch, ["203.0.113.20"])
    asyncio.run(url_guard.check_public_url("https://example.com/?dup=1"))
    for failure in (socket.gaierror("no such host"), asyncio.TimeoutError()):
        _resolver(monkeypatch, failure)
        error = _reject("https://example.org/")
        assert error.code == "url_unresolvable" and "域名" in error.message


def test_only_http_urls_with_a_host_are_accepted():
    assert _reject("ftp://example.com/file").code == "bad_url"
    assert _reject("http:///path").code == "bad_url"
    assert _reject("http://example.com:99999/").code == "bad_url"


def test_url_import_is_refused_before_a_document_is_created(monkeypatch):
    from kb_service import db

    def no_database():
        raise AssertionError("a refused URL must not create a document")

    monkeypatch.setattr(db, "pool", no_database)
    with pytest.raises(documents.DocumentError) as info:
        asyncio.run(documents.create_url_document("tenant_demo", "kb_demo", "http://127.0.0.1:5198/healthz"))
    assert (info.value.status, info.value.code) == (400, "url_not_allowed")
    assert "127.0.0.1" not in info.value.message


def test_import_endpoint_answers_400(api_client):
    r = api_client.post("/v1/kbs/kb_demo/documents/url", json={"url": "http://localhost:8080/healthz"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "url_not_allowed"


def test_fetch_errors_lose_internal_addresses():
    assert url_guard.safe_fetch_error(
        "service.example.net resolves to restricted IP 192.0.2.5: private IP address"
    ) == url_guard.FETCH_BLOCKED_MESSAGE
    cleaned = url_guard.safe_fetch_error("connect to 198.51.100.9:8080 failed; 2001:db8::5 unreachable at 12:30:45")
    assert "198.51.100.9" not in cleaned and "2001:db8::5" not in cleaned and "12:30:45" in cleaned


@pytest.mark.parametrize(
    "message",
    [
        "HTTPConnectionPool(host='db-internal', port=5432): Max retries exceeded with url: /healthz",
        "Cannot connect to host db-internal:5432 ssl:default [Connection refused]",
        "Failed to resolve 'db-internal' ([Errno -3] Temporary failure in name resolution)",
        "dial tcp: lookup db-internal on 127.0.0.11:53: no such host",
        'could not translate host name "db-internal" to address',
    ],
)
def test_fetch_errors_lose_internal_host_names(message):
    cleaned = url_guard.safe_fetch_error(message, "https://example.com/report")
    assert "db-internal" not in cleaned and "127.0.0.11" not in cleaned and url_guard.HIDDEN_HOST in cleaned


def test_the_users_own_host_and_ordinary_words_stay():
    message = "502 Bad Gateway from example.com:443 at 12:30:45 (Internal Server Error, ssl:default)"
    assert url_guard.safe_fetch_error(message, "https://example.com/report") == message


def test_parse_url_checks_again_and_cleans_docreader_errors(monkeypatch):
    async def fetch_refused(url):
        raise ParseError("parse_failed", "service.example.net resolves to restricted IP 198.51.100.3: private IP address")

    async def fetch_failed(url):
        raise ParseError("parse_failed", "HTTPConnectionPool(host='db-internal', port=80): Max retries exceeded")

    _treat_documentation_net_as_public(monkeypatch)
    _resolver(monkeypatch, ["203.0.113.30"])
    monkeypatch.setattr(docreader_client, "read_url", fetch_refused)
    with pytest.raises(ParseError) as info:
        asyncio.run(parse_url("http://service.example.net/"))
    assert info.value.message == url_guard.FETCH_BLOCKED_MESSAGE

    monkeypatch.setattr(docreader_client, "read_url", fetch_failed)
    with pytest.raises(ParseError) as info:
        asyncio.run(parse_url("http://service.example.net/"))
    assert "db-internal" not in info.value.message and info.value.code == "parse_failed"

    _resolver(monkeypatch, ["198.51.100.3"])
    with pytest.raises(ParseError) as info:
        asyncio.run(parse_url("http://service.example.net/"))
    assert info.value.code == "url_not_allowed" and "198.51.100.3" not in info.value.message
