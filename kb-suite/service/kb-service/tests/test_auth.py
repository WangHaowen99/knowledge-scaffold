import pytest

from kb_service.auth import AuthError, Principal, extract_token, narrow_to_scope, parse_scope_header, resolve_tenant


def _p(**kw):
    base = dict(
        key_id="k", name="n", tenant_id="t1", capabilities=frozenset({"retrieve"}),
        kb_allowlist=(), can_assert_tenant=False,
    )
    base.update(kw)
    return Principal(**base)


def test_extract_token():
    assert extract_token({"authorization": "Bearer abc"}) == "abc"
    assert extract_token({"x-api-key": "k2"}) == "k2"
    assert extract_token({}) is None


def test_scope_header():
    assert parse_scope_header("a, b,,c") == ["a", "b", "c"]
    assert parse_scope_header(None) == []


def test_tenant_bound_key_rejects_other_tenant():
    with pytest.raises(AuthError):
        resolve_tenant(_p(), "t2")
    assert resolve_tenant(_p(), None) == "t1"
    assert resolve_tenant(_p(), "t1") == "t1"


def test_asserting_key_needs_header():
    p = _p(tenant_id=None, can_assert_tenant=True)
    with pytest.raises(AuthError):
        resolve_tenant(p, None)
    assert resolve_tenant(p, "tx") == "tx"


def test_capability_check():
    with pytest.raises(AuthError):
        _p().require("manage")


def test_scope_header_narrows_the_key():
    assert narrow_to_scope(_p(), []).kb_allowlist == ()
    assert narrow_to_scope(_p(), ["a", "b", "a"]).kb_allowlist == ("a", "b")
    assert narrow_to_scope(_p(kb_allowlist=("a", "c")), ["a", "b"]).kb_allowlist == ("a",)
    # an empty allowlist means "everything": narrowing to nothing must not fall back to it
    with pytest.raises(AuthError):
        narrow_to_scope(_p(kb_allowlist=("c",)), ["a"])
