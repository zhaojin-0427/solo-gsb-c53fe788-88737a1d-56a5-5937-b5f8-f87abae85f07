"""Deterministic, tamper-evident cursors for ``GET /object-index``.

A first (no-cursor) index request materialises a ten-minute *snapshot*
of the paths under a prefix, ordered ascending.  Every later request
replays that snapshot instead of re-reading the live tables; the
*cursor* is the signed continuation token that binds a request to one
snapshot and to a position inside it.

A cursor payload contains:

* ``sid``   -- the snapshot row it belongs to (unknown/expired -> 410);
* ``off``   -- ordinal offset of the FIRST entry of the page to return
  (i.e. one past the last entry already delivered);
* ``prefix``/``lim`` -- the snapshot's selection; the request must echo
  them, so a cursor can never be replayed under a different query
  (that mismatch, like any forgery, answers 400).

Integrity is HMAC-SHA-256 keyed with a server-side secret stored in
``service_meta`` (a separate key from the copy-cursor secret), so
tokens survive process restarts and a flipped byte always fails
verification.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

CURSOR_VERSION = 1
_MAC_LEN = 18  # bytes of HMAC-SHA-256 kept in the token


@dataclass(frozen=True, slots=True)
class IndexCursor:
    """Decoded object-index cursor payload."""

    snapshot_id: str
    offset: int
    prefix: str
    limit: int


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(token: str) -> bytes | None:
    try:
        padded = token + "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:
        return None


def encode_cursor(secret: str, cursor: IndexCursor) -> str:
    """Serialise and sign a cursor; deterministic for the same input."""
    payload = json.dumps(
        {
            "v": CURSOR_VERSION,
            "sid": cursor.snapshot_id,
            "off": cursor.offset,
            "pfx": cursor.prefix,
            "lim": cursor.limit,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    mac = hmac.new(secret.encode("ascii"), payload, hashlib.sha256).digest()
    return _b64encode(payload) + "." + _b64encode(mac[:_MAC_LEN])


def decode_cursor(secret: str, token: str) -> IndexCursor | None:
    """Verify and decode a cursor token; ``None`` when invalid/tampered."""
    if not token or "." not in token:
        return None
    payload_b64, mac_b64 = token.rsplit(".", 1)
    payload = _b64decode(payload_b64)
    mac = _b64decode(mac_b64)
    if payload is None or mac is None:
        return None
    expected = hmac.new(
        secret.encode("ascii"), payload, hashlib.sha256
    ).digest()[:_MAC_LEN]
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != CURSOR_VERSION:
        return None
    try:
        offset = data["off"]
        limit = data["lim"]
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return None
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
        ):
            return None
        return IndexCursor(
            snapshot_id=_checked_str(data["sid"]),
            offset=offset,
            prefix=_checked_str(data["pfx"]),
            limit=limit,
        )
    except (KeyError, TypeError):
        return None


def _checked_str(value) -> str:
    if not isinstance(value, str):
        raise TypeError("cursor field must be a string")
    return value
