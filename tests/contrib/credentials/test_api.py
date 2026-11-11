"""Unit tests for the credentials API, link format, and retain policies."""

import base64
import copy
import json
import pickle

import pytest

from temporalio.api.common.v1 import Payload
from temporalio.contrib import credentials
from temporalio.contrib.credentials import _api, _link


def jwt_for(iss: str, sub: str, nonce: str = "") -> str:
    def b64(value: object) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        )

    return f"{b64({'alg': 'none'})}.{b64({'iss': iss, 'sub': sub, 'n': nonce})}.sig"


def test_credential_redacts() -> None:
    cred = credentials.Credential("user_token", "secret")
    for shown in (
        str(cred),
        repr(cred),
        f"{cred}",
        f"{cred!s}",
        f"{cred:>20}",
        "%s %r" % (cred, cred),
    ):
        assert "secret" not in shown
        assert "[REDACTED]" in shown
    assert cred.reveal() == "secret"
    assert cred.name == "user_token"
    assert copy.deepcopy(cred) is cred
    with pytest.raises(TypeError):
        pickle.dumps(cred)
    with pytest.raises(TypeError):
        json.dumps({"token": cred})


def test_if_absent() -> None:
    assert credentials.if_absent._keep(None, "new") is None
    assert credentials.if_absent._keep("old", "new") is not None


def test_same_subject() -> None:
    keep = credentials.jwt.same_subject._keep
    alice1, alice2 = jwt_for("idp", "alice", "1"), jwt_for("idp", "alice", "2")
    assert keep(alice1, alice2) is None
    assert keep(None, alice2) == "the Workflow holds no token to compare with"
    assert keep(alice1, jwt_for("idp", "bob")) == "the tokens name different subjects"
    assert (
        keep(alice1, jwt_for("other", "alice")) == "the tokens name different subjects"
    )
    assert keep(alice1, "opaque") == "the offered token isn't a JWT"
    assert keep("a.b.c", alice2) == "the held token has a malformed payload"
    assert keep(alice1, jwt_for("idp", "")) == "the offered token has no subject"


def test_policies_are_sealed() -> None:
    with pytest.raises(TypeError, match="sealed"):
        credentials.RetainPolicy("always", lambda held, offered: None)
    with pytest.raises(TypeError, match="retain must be"):
        credentials.requires("user_token", retain=True)  # type: ignore[arg-type]


def test_declarations() -> None:
    with pytest.raises(TypeError, match="at least one"):
        credentials.requires()
    with pytest.raises(TypeError, match="non-empty"):
        credentials.accepts("")

    @credentials.requires("a", retain=credentials.if_absent)
    @credentials.accepts("b")
    @credentials.revokes("c")
    def handler() -> None: ...

    spec = _api.spec_of(handler)
    assert spec is not None
    assert spec.required == ["a"]
    assert spec.accepted == ["b"]
    assert spec.revokes == ["c"]
    assert spec.retain == {"a": credentials.if_absent}


def test_attach_innermost_wins() -> None:
    with credentials.attach(a="1", b="2"):
        with credentials.attach_with_key("payments", {"c": "3"}):
            att = _api._attached.get()
            assert (
                att is not None and att.values == {"c": "3"} and att.key == "payments"
            )
        att = _api._attached.get()
        assert (
            att is not None and att.values == {"a": "1", "b": "2"} and att.key is None
        )
    assert _api._attached.get() is None
    with pytest.raises(TypeError, match="must be a str"):
        with credentials.attach(a=1):  # type: ignore[arg-type]
            pass


def test_link_format() -> None:
    # The Go binding layer's encodings, byte for byte.
    pending = _link.pending_payloads({"user_token": "t"}, "payments")
    assert pending["credentials"].metadata["encoding"] == b"json/credentials-pending"
    assert json.loads(pending["credentials"].data) == {"user_token": "t"}
    assert pending["credentials-key"].metadata["encoding"] == b"json/credentials-key"
    assert json.loads(pending["credentials-key"].data) == "payments"
    assert _link.pending_nexus({"user_token": "t"}, None) == {
        "credentials": '{"user_token":"t"}'
    }
    assert all(credentials.is_credentials_payload(p) for p in pending.values())
    assert not credentials.is_credentials_payload(
        Payload(metadata={"encoding": b"json/plain"})
    )

    sealed = Payload(
        metadata={"encoding": b"binary/credentials-sealed"}, data=b"v1.start.x"
    )
    assert _link.opened_from_header({"credentials": sealed}) is None
    sealed.metadata["credentials-opened"] = (
        b'{"ok":true,"credentials":{"user_token":"t"}}'
    )
    assert _link.opened_from_header({"credentials": sealed}) == _link.Opened(
        ok=True, credentials={"user_token": "t"}
    )
    for raw, reason in [
        (b'{"ok":false,"reason":"does-not-open"}', "does-not-open"),
        (b'{"ok":false}', "malformed"),
        (b"not json", "malformed"),
        (b'{"ok":true,"credentials":{"user_token":1}}', "malformed"),
    ]:
        assert _link.parse_opened(raw) == _link.Opened(ok=False, reason=reason)
    assert _link.parse_opened(b'{"ok":true}') == _link.Opened(ok=True)

    assert _link.opened_from_nexus(
        {"Credentials-Opened": '{"ok":true}'}
    ) == _link.Opened(ok=True)
    assert _link.strip_nexus(
        {"Credentials": "x", "CREDENTIALS-KEY": "y", "other": "z"}
    ) == {"other": "z"}


def test_plugin_needs_proxy_mode() -> None:
    with pytest.raises(ValueError, match="proxy=True"):
        credentials.Plugin()
