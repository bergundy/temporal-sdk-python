"""The link format between an application and its binding layer.

It's plaintext, and matches ``go.temporal.io/sdk/contrib/credentials/binding``.
In a Temporal header (``map<string, Payload>``):

- Pending, app to binding: ``credentials`` is ``{encoding: json/credentials-pending}``
  with a JSON object of strings. ``credentials-key`` optionally names a key:
  ``{encoding: json/credentials-key}`` with a JSON string.
- Sealed, on the wire: ``credentials`` is ``{encoding: binary/credentials-sealed}``.
- Opened, binding to app: the sealed payload unchanged, plus metadata
  ``credentials-opened`` holding ``{"ok": true, "credentials": {...}}`` or
  ``{"ok": false, "reason": "..."}``.
- Local, Local Activities only, never on the wire: ``credentials`` is
  ``{encoding: json/credentials-local}`` with a JSON object of strings.

In a Nexus header (``map<string, string>``), the same values are the header
strings themselves, and the opened result is a ``credentials-opened`` header.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from temporalio.api.common.v1 import Payload

HEADER = "credentials"
HEADER_KEY = "credentials-key"
HEADER_OPENED = "credentials-opened"
RESERVED = frozenset((HEADER, HEADER_KEY, HEADER_OPENED))

ENCODING_SEALED = b"binary/credentials-sealed"
ENCODING_PENDING = b"json/credentials-pending"
ENCODING_KEY = b"json/credentials-key"
ENCODING_LOCAL = b"json/credentials-local"
_ENCODINGS = frozenset(
    (ENCODING_SEALED, ENCODING_PENDING, ENCODING_KEY, ENCODING_LOCAL)
)

REASON_MALFORMED = "malformed"


def is_credentials_payload(payload: Payload) -> bool:
    """Report whether a payload is a credentials payload in any form.

    Payload codecs that visit header payloads must pass these through
    unchanged.
    """
    return payload.metadata.get("encoding") in _ENCODINGS


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _payload(encoding: bytes, data: bytes) -> Payload:
    return Payload(metadata={"encoding": encoding}, data=data)


def pending_payloads(values: Mapping[str, str], key: str | None) -> dict[str, Payload]:
    """Temporal header entries carrying values for the binding layer to seal."""
    headers = {HEADER: _payload(ENCODING_PENDING, _json(dict(values)))}
    if key is not None:
        headers[HEADER_KEY] = _payload(ENCODING_KEY, _json(key))
    return headers


def pending_nexus(values: Mapping[str, str], key: str | None) -> dict[str, str]:
    """Nexus header entries carrying values for the binding layer to seal."""
    headers = {HEADER: _json(dict(values)).decode()}
    if key is not None:
        headers[HEADER_KEY] = _json(key).decode()
    return headers


def local_payloads(values: Mapping[str, str]) -> dict[str, Payload]:
    """Temporal header entries carrying values to a Local Activity."""
    return {HEADER: _payload(ENCODING_LOCAL, _json(dict(values)))}


@dataclass(frozen=True)
class Opened:
    """What the binding layer attached to a header it tried to open."""

    ok: bool
    reason: str = ""
    credentials: Mapping[str, str] = field(default_factory=dict)

    def marshal(self) -> bytes:
        """Encode the result as the binding layer attaches it."""
        if self.ok:
            return _json({"ok": True, "credentials": dict(self.credentials)})
        return _json({"ok": False, "reason": self.reason})


_MALFORMED = Opened(ok=False, reason=REASON_MALFORMED)


def _string_map(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return None
    return value


def parse_opened(raw: bytes | str) -> Opened:
    """Decode an opened result. Anything unreadable is a malformed failure."""
    try:
        result = json.loads(raw)
    except ValueError:
        return _MALFORMED
    if not isinstance(result, dict):
        return _MALFORMED
    if result.get("ok") is not True:
        reason = result.get("reason")
        return Opened(
            ok=False,
            reason=reason if isinstance(reason, str) and reason else REASON_MALFORMED,
        )
    # An empty map is omitted.
    creds = result.get("credentials", {})
    values = _string_map({} if creds is None else creds)
    return _MALFORMED if values is None else Opened(ok=True, credentials=values)


def opened_from_header(headers: Mapping[str, Payload]) -> Opened | None:
    """The opened result on a Temporal header, or None if there's none."""
    payload = headers.get(HEADER)
    if payload is None or HEADER_OPENED not in payload.metadata:
        return None
    return parse_opened(payload.metadata[HEADER_OPENED])


def local_from_header(headers: Mapping[str, Payload]) -> dict[str, str] | None:
    """Local Activity credentials on a Temporal header, or None."""
    payload = headers.get(HEADER)
    if payload is None or payload.metadata.get("encoding") != ENCODING_LOCAL:
        return None
    try:
        return _string_map(json.loads(payload.data))
    except ValueError:
        return None


def opened_from_nexus(headers: Mapping[str, str]) -> Opened | None:
    """The opened result on a Nexus header, matching keys case-insensitively."""
    for k, v in headers.items():
        if k.lower() == HEADER_OPENED:
            return parse_opened(v)
    return None


def strip_payloads(headers: Mapping[str, Payload]) -> dict[str, Payload]:
    """A copy of headers without the reserved credentials entries."""
    return {k: v for k, v in headers.items() if k not in RESERVED}


def strip_nexus(headers: Mapping[str, str] | None) -> dict[str, str]:
    """A copy of Nexus headers without the reserved credentials entries."""
    return {k: v for k, v in (headers or {}).items() if k.lower() not in RESERVED}
