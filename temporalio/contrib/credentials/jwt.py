"""A retain policy for credentials that are JWTs."""

from __future__ import annotations

import base64
import binascii
import json

from temporalio.contrib.credentials._api import _SEAL, RetainPolicy


def _subject(token: str) -> tuple[tuple[str, str] | None, str]:
    parts = token.split(".")
    if len(parts) != 3:
        return None, "isn't a JWT"
    body = parts[1].rstrip("=")
    try:
        claims = json.loads(
            base64.b64decode(
                body + "=" * (-len(body) % 4), altchars=b"-_", validate=True
            )
        )
    except (binascii.Error, ValueError):
        return None, "has a malformed payload"
    if not isinstance(claims, dict):
        return None, "has a malformed payload"
    iss, sub = claims.get("iss", ""), claims.get("sub", "")
    if not isinstance(iss, str) or not isinstance(sub, str):
        return None, "has a malformed payload"
    if not sub:
        return None, "has no subject"
    return (iss, sub), ""


def _keep_same_subject(held: str | None, offered: str) -> str | None:
    if held is None:
        return "the Workflow holds no token to compare with"
    held_subject, err = _subject(held)
    if held_subject is None:
        return f"the held token {err}"
    offered_subject, err = _subject(offered)
    if offered_subject is None:
        return f"the offered token {err}"
    if held_subject != offered_subject:
        return "the tokens name different subjects"
    return None


same_subject = RetainPolicy("jwt.same_subject", _keep_same_subject, _sealed=_SEAL)
"""Keeps a JWT in place of the one the Workflow holds when both name the same
subject from the same issuer (their ``iss`` and ``sub`` claims). It's meant for
refreshing a session's token before it expires.

It doesn't verify either token: it reads their claims as they are. The service
each token is forwarded to verifies it. It declines a malformed token, and it
declines when the Workflow holds nothing, so it can't install a first token.
"""
