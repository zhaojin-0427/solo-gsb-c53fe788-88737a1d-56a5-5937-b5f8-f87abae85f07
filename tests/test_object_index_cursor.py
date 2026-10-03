"""Unit tests for GET /object-index cursors."""
from dataclasses import replace

from app.object_index import IndexCursor, decode_cursor, encode_cursor

SECRET = "00" * 32
CURSOR = IndexCursor(
    snapshot_id="s" * 32,
    offset=120,
    prefix="logs/2026/",
    limit=25,
)


def test_cursor_roundtrip():
    token = encode_cursor(SECRET, CURSOR)
    assert decode_cursor(SECRET, token) == CURSOR


def test_cursor_roundtrip_empty_prefix_and_zero_offset():
    first = replace(CURSOR, prefix="", offset=0)
    token = encode_cursor(SECRET, first)
    assert decode_cursor(SECRET, token) == first


def test_cursor_is_deterministic():
    assert encode_cursor(SECRET, CURSOR) == encode_cursor(SECRET, CURSOR)


def test_cursor_offsets_differ():
    other = encode_cursor(SECRET, replace(CURSOR, offset=145))
    assert other != encode_cursor(SECRET, CURSOR)


def test_cursor_rejects_wrong_secret():
    token = encode_cursor(SECRET, CURSOR)
    assert decode_cursor("ff" * 32, token) is None


def test_copy_secret_does_not_sign_index_cursors():
    # The two features use independent keys in service_meta; a token
    # minted with one key must not validate under the other.
    token = encode_cursor(SECRET, CURSOR)
    assert decode_cursor("11" * 32, token) is None


def test_cursor_rejects_tampering():
    token = encode_cursor(SECRET, CURSOR)
    payload, mac = token.rsplit(".", 1)
    flip = "A" if payload[5] != "A" else "B"
    assert decode_cursor(SECRET, payload[:5] + flip + payload[6:] + "." + mac) is None
    flip = "A" if mac[2] != "A" else "B"
    assert decode_cursor(SECRET, payload + "." + mac[:2] + flip + mac[3:]) is None


def test_cursor_rejects_garbage():
    assert decode_cursor(SECRET, "") is None
    assert decode_cursor(SECRET, "not-a-cursor") is None
    assert decode_cursor(SECRET, "!!!.@@@") is None
    assert decode_cursor(SECRET, "aGVsbG8.d29ybGQ") is None  # valid b64, bad MAC
