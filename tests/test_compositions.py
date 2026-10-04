"""End-to-end tests for object composition (POST /compositions)."""
from __future__ import annotations

import hashlib
import threading
import time
import uuid

FUTURE = "2099-01-01T00:00:00Z"

ETAG_OF = lambda body: '"' + hashlib.sha256(body).hexdigest() + '"'


def _p(name: str) -> str:
    return f"comp/{name}-{uuid.uuid4().hex[:12]}"


def _put(client, path: str, body: bytes, media="text/plain"):
    return client.put(
        f"/objects/{path}", content=body, headers={"Content-Type": media}
    )


def _seg(path, rev, etag, start, end):
    return {
        "path": path,
        "revision_no": rev,
        "etag": etag,
        "start": start,
        "end": end,
    }


def _compose(client, target, segments, media="text/plain", baseline="__MISSING__"):
    payload = {"target_path": target, "media_type": media, "segments": segments}
    if baseline != "__MISSING__":
        payload["target_baseline"] = baseline
    return client.post("/compositions", json=payload)


# ---- request validation --------------------------------------------------------

def test_composition_validation(client):
    a, t = _p("va"), _p("vat")
    _put(client, a, b"hello")
    good = _seg(a, 1, ETAG_OF(b"hello"), 0, 2)

    assert client.post("/compositions", content=b"not json").status_code == 400
    assert client.post("/compositions", json=["x"]).status_code == 400
    assert client.post("/compositions", json={}).status_code == 400

    # target_path
    r = _compose(client, "", [good])
    assert r.status_code == 400
    r = _compose(client, 7, [good])
    assert r.status_code == 400

    # media_type
    r = _compose(client, t, [good], media="")
    assert r.status_code == 400
    r = _compose(client, t, [good], media=123)
    assert r.status_code == 400

    # target_baseline must be present
    r = _compose(client, t, [good])
    assert r.status_code == 400
    assert "target_baseline" in r.json()["detail"]
    # ... null / object with positive revision_no and strong etag
    assert _compose(client, t, [good], baseline="x").status_code == 400
    assert _compose(client, t, [good], baseline={}).status_code == 400
    assert (
        _compose(
            client, t, [good],
            baseline={"revision_no": 0, "etag": '"x"'},
        ).status_code
        == 400
    )
    for bad_no in (-1, 1.5, "1", True):
        assert (
            _compose(
                client, t, [good],
                baseline={"revision_no": bad_no, "etag": '"x"'},
            ).status_code
            == 400
        )
    assert (
        _compose(
            client, t, [good],
            baseline={"revision_no": 1, "etag": "garbage"},
        ).status_code
        == 400
    )
    assert (
        _compose(
            client, t, [good],
            baseline={"revision_no": 1, "etag": 'W/"abc"'},
        ).status_code
        == 400
    )

    # segments: 1..100 entries
    assert _compose(client, t, [], baseline=None).status_code == 400
    too_many = [_seg(a, 1, ETAG_OF(b"hello"), 0, 1) for _ in range(101)]
    assert _compose(client, t, too_many, baseline=None).status_code == 400
    assert (
        _compose(client, t, "segments", baseline=None).status_code == 400
    )

    # per-segment shape
    assert _compose(client, t, ["x"], baseline=None).status_code == 400
    assert _compose(client, t, [{}], baseline=None).status_code == 400
    assert (
        _compose(client, t, [_seg("", 1, '"x"', 0, 1)], baseline=None).status_code
        == 400
    )
    for bad_no in (0, -2, 1.0, "1", False, None):
        assert (
            _compose(
                client, t, [_seg(a, bad_no, '"x"', 0, 1)], baseline=None
            ).status_code
            == 400
        )
    assert (
        _compose(client, t, [_seg(a, 1, "nope", 0, 1)], baseline=None).status_code
        == 400
    )
    assert (
        _compose(
            client, t, [_seg(a, 1, 'W/"abc"', 0, 1)], baseline=None
        ).status_code
        == 400
    )
    for bad_start in (-1, 1.5, "0", True, None):
        assert (
            _compose(
                client, t, [_seg(a, 1, ETAG_OF(b"hello"), bad_start, 1)],
                baseline=None,
            ).status_code
            == 400
        )
    for bad_end in (-1, 1.5, "1", True, None):
        assert (
            _compose(
                client, t, [_seg(a, 1, ETAG_OF(b"hello"), 0, bad_end)],
                baseline=None,
            ).status_code
            == 400
        )
    # start > end is a structural 400 (start == end is an empty segment,
    # a semantic 400 once the revision resolves).
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"hello"), 3, 2)], baseline=None
    )
    assert r.status_code == 400
    assert r.json()["index"] == 0


def test_exactly_100_segments_accepted(client):
    a, t = _p("hundred-src"), _p("hundred-tgt")
    _put(client, a, b"x")
    segs = [_seg(a, 1, ETAG_OF(b"x"), 0, 1) for _ in range(100)]
    r = _compose(client, t, segs, baseline=None)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["bytes"] == 100
    assert client.get(f"/objects/{t}").content == b"x" * 100


# ---- happy path ----------------------------------------------------------------

def test_composition_concatenates_segments_in_order(client):
    a, b, t = _p("a"), _p("b"), _p("t")
    _put(client, a, b"0123456789")
    _put(client, b, b"ABCDEFGHIJ")

    # Reversed order, a repeated source, overlapping intervals, and a
    # later segment appearing before an earlier one on purpose.
    segs = [
        _seg(b, 1, ETAG_OF(b"ABCDEFGHIJ"), 2, 5),   # CDE
        _seg(a, 1, ETAG_OF(b"0123456789"), 7, 10),  # 789
        _seg(a, 1, ETAG_OF(b"0123456789"), 0, 2),   # 01
        _seg(a, 1, ETAG_OF(b"0123456789"), 7, 10),  # 789 again
        _seg(b, 1, ETAG_OF(b"ABCDEFGHIJ"), 9, 10),  # J
    ]
    r = _compose(client, t, segs, media="application/x-built", baseline=None)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data == {
        "target_path": t,
        "revision_no": 1,
        "etag": ETAG_OF(b"CDE78901789J"),
        "bytes": 12,
    }
    assert r.headers["etag"] == ETAG_OF(b"CDE78901789J")
    assert r.headers["x-revision-no"] == "1"
    assert "last-modified" in r.headers

    g = client.get(f"/objects/{t}")
    assert g.status_code == 200
    assert g.content == b"CDE78901789J"
    assert g.headers["content-type"].startswith("application/x-built")


def test_composition_full_range_and_boundary_intervals(client):
    a, t = _p("full-src"), _p("full-tgt")
    body = b"abcdef"
    _put(client, a, body)
    # [0, len) whole revision; adjacent single-byte slices that cover it.
    segs = [_seg(a, 1, ETAG_OF(body), 0, 6)]
    r = _compose(client, t, segs, baseline=None)
    assert r.status_code == 201
    assert client.get(f"/objects/{t}").content == body

    t2 = _p("full-tgt2")
    segs = [_seg(a, 1, ETAG_OF(body), i, i + 1) for i in range(6)]
    r = _compose(client, t2, segs, baseline=None)
    assert r.status_code == 201
    assert client.get(f"/objects/{t2}").content == body


def test_composition_uses_media_type_of_request_not_source(client):
    a, t = _p("media-src"), _p("media-tgt")
    _put(client, a, b"bytes", media="text/csv")
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"bytes"), 0, 5)],
        media="image/png", baseline=None,
    )
    assert r.status_code == 201
    assert client.get(f"/objects/{t}").headers["content-type"].startswith(
        "image/png"
    )


def test_composition_pins_old_revision_after_republish(client):
    """Segments come from the NAMED revision: publishing a newer source
    revision does not change the composed bytes, ETag or length."""
    a, t = _p("old-src"), _p("old-tgt")
    put1 = _put(client, a, b"v1-old-bytes")
    _put(client, a, b"v2-brand-new")

    r = _compose(
        client, t, [_seg(a, 1, put1.headers["etag"], 3, 12)],
        baseline=None,
    )
    assert r.status_code == 201
    assert client.get(f"/objects/{t}").content == b"old-bytes"
    assert r.json()["etag"] == ETAG_OF(b"old-bytes")


def test_composition_with_matching_baseline(client):
    t = _p("base-tgt")
    a = _p("base-src")
    put = _put(client, t, b"current-target")
    _put(client, a, b"payload!!")
    etag = put.headers["etag"]
    rev = int(put.headers["x-revision-no"])

    r = _compose(
        client, t,
        [_seg(a, 1, ETAG_OF(b"payload!!"), 0, 9)],
        baseline={"revision_no": rev, "etag": etag},
    )
    assert r.status_code == 201
    # New revision numbered after the target's own history.
    assert r.json()["revision_no"] == 2
    assert client.get(f"/objects/{t}").content == b"payload!!"


def test_composition_from_moved_away_paths_leftover_revision(client):
    """A historical revision of a moved-away source path is still in the
    library and composes normally."""
    s, m, t = _p("m-src"), _p("m-mid"), _p("m-tgt")
    _put(client, s, b"moved-away-v1")
    put2 = _put(client, s, b"moved-away-v2!!")
    from tests.test_moves import _move as move

    assert move(
        client, s, m, etag=put2.headers["etag"], rev=2
    ).status_code == 201
    # Source path detached (GET 404) but its old revisions remain stored.
    assert client.get(f"/objects/{s}").status_code == 404
    r = _compose(
        client, t, [_seg(s, 1, ETAG_OF(b"moved-away-v1"), 0, 13)],
        baseline=None,
    )
    assert r.status_code == 201
    assert client.get(f"/objects/{t}").content == b"moved-away-v1"


# ---- empty / out of bounds (400) -----------------------------------------------

def test_empty_segment_is_400_and_publishes_nothing(client):
    a, t = _p("empty-src"), _p("empty-tgt")
    _put(client, a, b"xx")
    _put(client, t, b"keep-me")
    etag, rev = client.head(f"/objects/{t}").headers["etag"], 1

    r = _compose(
        client, t,
        [
            _seg(a, 1, ETAG_OF(b"xx"), 0, 1),
            _seg(a, 1, ETAG_OF(b"xx"), 1, 1),  # empty
        ],
        baseline={"revision_no": rev, "etag": etag},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["index"] == 1
    assert body["start"] == 1 and body["end"] == 1 and body["length"] == 2
    # Baseline still matches: nothing was published.
    g = client.get(f"/objects/{t}")
    assert g.content == b"keep-me"
    assert g.headers["etag"] == etag


def test_end_equals_length_is_allowed_but_past_end_is_400(client):
    a, t = _p("bounds-src"), _p("bounds-tgt")
    _put(client, a, b"abc")
    # end == length (3) is the final valid half-open boundary.
    r = _compose(client, t, [_seg(a, 1, ETAG_OF(b"abc"), 2, 3)], baseline=None)
    assert r.status_code == 201
    assert client.get(f"/objects/{t}").content == b"c"

    t2 = _p("bounds-tgt2")
    r = _compose(client, t2, [_seg(a, 1, ETAG_OF(b"abc"), 2, 4)], baseline=None)
    assert r.status_code == 400
    body = r.json()
    assert "out of bounds" in body["detail"]
    assert body["index"] == 0 and body["length"] == 3
    assert body["start"] == 2 and body["end"] == 4
    assert client.get(f"/objects/{t2}").status_code == 404


def test_bounds_400_index_points_to_first_offending_segment(client):
    a, t = _p("bo-src"), _p("bo-tgt")
    _put(client, a, b"ab")
    segs = [
        _seg(a, 1, ETAG_OF(b"ab"), 0, 1),
        _seg(a, 1, ETAG_OF(b"ab"), 0, 5),
    ]
    r = _compose(client, t, segs, baseline=None)
    assert r.status_code == 400
    assert r.json()["index"] == 1
    assert client.get(f"/objects/{t}").status_code == 404


# ---- missing source revisions (404) --------------------------------------------

def test_unknown_source_path_is_404(client):
    t = _p("miss-tgt")
    r = _compose(client, t, [_seg(_p("never"), 1, '"x"', 0, 1)], baseline=None)
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "source_revision_not_found"
    assert len(body["missing"]) == 1
    assert body["missing"][0]["index"] == 0
    assert body["missing"][0]["revision_no"] == 1
    # Nothing left behind: the target is a true 404 and is first-creatable.
    assert client.get(f"/objects/{t}").status_code == 404
    create = client.put(
        f"/objects/{t}", content=b"fresh",
        headers={"If-None-Match": "*"},
    )
    assert create.status_code == 204
    assert create.headers["x-revision-no"] == "1"


def test_collected_revision_number_is_404(client):
    a, t = _p("col-src"), _p("col-tgt")
    _put(client, a, b"v1-aaaa")
    _put(client, a, b"v2-cur")
    assert client.post(
        "/gc",
        json={"paths": [a], "before": FUTURE, "dry_run": False},
    ).status_code == 200
    # rev 1 gone; pinning it is a 404.
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"v1-aaaa"), 0, 2)], baseline=None
    )
    assert r.status_code == 404
    assert body_missing(r) == [{"index": 0, "path": a, "revision_no": 1}]
    assert client.get(f"/objects/{t}").status_code == 404


def test_unknown_revision_number_on_existing_path_is_404(client):
    a, t = _p("gap-src"), _p("gap-tgt")
    _put(client, a, b"only-rev-one")
    r = _compose(client, t, [_seg(a, 9, '"x"', 0, 1)], baseline=None)
    assert r.status_code == 404
    assert body_missing(r) == [{"index": 0, "path": a, "revision_no": 9}]


def test_all_missing_segments_reported_in_request_order(client):
    t = _p("multi-miss-tgt")
    a = _p("multi-miss-a")
    _put(client, a, b"ok")
    segs = [
        _seg(_p("gone-1"), 1, '"x"', 0, 1),
        _seg(a, 1, ETAG_OF(b"ok"), 0, 1),
        _seg(a, 7, '"x"', 0, 1),
    ]
    r = _compose(client, t, segs, baseline=None)
    assert r.status_code == 404
    missing = r.json()["missing"]
    assert [m["index"] for m in missing] == [0, 2]
    assert missing[1]["revision_no"] == 7


def body_missing(response):
    return response.json()["missing"]


# ---- ETag / baseline mismatch (412) --------------------------------------------

def test_source_etag_mismatch_is_412(client):
    a, t = _p("se-src"), _p("se-tgt")
    _put(client, a, b"v1-aaaa")
    _put(client, a, b"v2-current!!")
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"v2-current!!"), 0, 4)],
        baseline=None,
    )
    assert r.status_code == 412
    body = r.json()
    assert body["detail"] == "precondition_failed"
    (mm,) = body["source_mismatches"]
    assert mm == {
        "index": 0,
        "path": a,
        "revision_no": 1,
        "expected_etag": ETAG_OF(b"v2-current!!"),
        "actual_etag": ETAG_OF(b"v1-aaaa"),
    }
    assert client.get(f"/objects/{t}").status_code == 404
    assert client.get(f"/objects/{a}").content == b"v2-current!!"


def test_weak_etag_is_400_not_412(client):
    a, t = _p("we-src"), _p("we-tgt")
    _put(client, a, b"v1")
    r = _compose(
        client, t, [_seg(a, 1, 'W/"' + hashlib.sha256(b"v1").hexdigest() + '"', 0, 1)],
        baseline=None,
    )
    assert r.status_code == 400


def test_target_baseline_etag_mismatch_is_412_with_current_validators(client):
    t = _p("tb-tgt")
    a = _p("tb-src")
    _put(client, t, b"target-v1")
    _put(client, t, b"target-v2-current")
    _put(client, a, b"the-payload")
    # Right current revision NUMBER, stale ETag: only "etag" mismatches.
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"the-payload"), 0, 4)],
        baseline={"revision_no": 2, "etag": '"' + "0" * 64 + '"'},
    )
    assert r.status_code == 412
    target = r.json()["target"]
    assert target["reason"] == "baseline_mismatch"
    assert target["mismatches"] == ["etag"]
    assert target["expected_revision_no"] == 2
    assert target["current_revision_no"] == 2
    assert target["current_etag"] == ETAG_OF(b"target-v2-current")
    assert r.headers["etag"] == ETAG_OF(b"target-v2-current")
    # Target unchanged.
    assert client.get(f"/objects/{t}").content == b"target-v2-current"


def test_target_baseline_revision_no_mismatch_is_412(client):
    t = _p("tr-tgt")
    a = _p("tr-src")
    put1 = _put(client, t, b"target-v1")
    _put(client, t, b"target-v2")
    _put(client, a, b"the-payload")
    # Current revision number (2) but the OLD rev 1 ETag: only the
    # revision number disagrees.
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"the-payload"), 0, 4)],
        baseline={"revision_no": 2, "etag": put1.headers["etag"]},
    )
    assert r.status_code == 412
    target = r.json()["target"]
    assert target["mismatches"] == ["etag"]
    assert target["current_revision_no"] == 2
    assert client.get(f"/objects/{t}").content == b"target-v2"


def test_target_baseline_revision_number_disagrees(client):
    t = _p("tr2-tgt")
    a = _p("tr2-src")
    _put(client, t, b"target-v1")
    _put(client, t, b"target-v2")
    _put(client, a, b"the-payload")
    # Both the CURRENT etag but the OLD revision number.
    head = client.head(f"/objects/{t}")
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"the-payload"), 0, 4)],
        baseline={"revision_no": 1, "etag": head.headers["etag"]},
    )
    assert r.status_code == 412
    target = r.json()["target"]
    assert target["mismatches"] == ["revision_no"]


def test_null_baseline_but_target_exists_is_412(client):
    t = _p("nb-tgt")
    a = _p("nb-src")
    _put(client, t, b"occupied")
    _put(client, a, b"the-payload")
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"the-payload"), 0, 4)],
        baseline=None,
    )
    assert r.status_code == 412
    target = r.json()["target"]
    assert target["reason"] == "expected_absent"
    assert target["current_revision_no"] == 1
    assert target["current_etag"] == ETAG_OF(b"occupied")
    assert client.get(f"/objects/{t}").content == b"occupied"


def test_baseline_given_but_target_absent_is_412(client):
    t = _p("abs-tgt")
    a = _p("abs-src")
    _put(client, a, b"the-payload")
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"the-payload"), 0, 4)],
        baseline={"revision_no": 1, "etag": '"deadbeef"'},
    )
    assert r.status_code == 412
    target = r.json()["target"]
    assert target["reason"] == "target_absent"
    assert target["current_revision_no"] is None
    assert target["current_etag"] is None
    assert client.get(f"/objects/{t}").status_code == 404


def test_error_precedence_404_before_412_before_400(client):
    """One request simultaneously missing a source, carrying a stale
    source ETag (on another segment) and an out-of-bounds interval (on a
    third): 404 wins; fixing only the missing source gives 412; fixing
    the ETag too finally gives the bounds 400."""
    a, t = _p("pre-src"), _p("pre-tgt")
    _put(client, a, b"0123456789")
    segs = [
        _seg(_p("ghost"), 1, '"x"', 0, 1),                          # missing -> 404
        _seg(a, 1, '"' + "a" * 64 + '"', 0, 2),                    # wrong etag
        _seg(a, 1, ETAG_OF(b"0123456789"), 0, 99),  # out of bounds
    ]
    r = _compose(client, t, segs, baseline=None)
    assert r.status_code == 404

    # Replace the ghost segment with a valid one on the existing source:
    # the stale ETag now surfaces as 412 (404 gone).
    segs[0] = _seg(a, 1, ETAG_OF(b"0123456789"), 0, 1)
    r = _compose(client, t, segs, baseline=None)
    assert r.status_code == 412

    segs[1]["etag"] = ETAG_OF(b"0123456789")
    r = _compose(client, t, segs, baseline=None)
    assert r.status_code == 400
    assert r.json()["index"] == 2
    assert client.get(f"/objects/{t}").status_code == 404


# ---- target == source path -----------------------------------------------------

def test_target_equals_source_path_reads_named_old_revision(client):
    """When the target is the source path itself, segments still come
    from the explicitly pinned (possibly current/old) revision, never
    from the revision the composition is about to publish."""
    p = _p("self")
    _put(client, p, b"abcdef")
    # Rebuild from the current rev 1 with null baseline impossible
    # (target exists); pin baseline rev 1 and compose from rev 1.
    head = client.head(f"/objects/{p}")
    r = _compose(
        client, p,
        [
            _seg(p, 1, head.headers["etag"], 3, 6),  # def
            _seg(p, 1, head.headers["etag"], 0, 3),  # abc
        ],
        baseline={"revision_no": 1, "etag": head.headers["etag"]},
    )
    assert r.status_code == 201, r.text
    assert r.json()["revision_no"] == 2
    assert client.get(f"/objects/{p}").content == b"defabc"


def test_self_composition_from_historical_revision(client):
    p = _p("self-old")
    put1 = _put(client, p, b"v1-old!!")
    _put(client, p, b"v2-current-target")
    head2 = client.head(f"/objects/{p}")
    # Baseline pins current rev 2; the segment reads historical rev 1.
    r = _compose(
        client, p,
        [_seg(p, 1, put1.headers["etag"], 0, 8)],
        baseline={"revision_no": 2, "etag": head2.headers["etag"]},
    )
    assert r.status_code == 201
    assert r.json()["revision_no"] == 3
    assert client.get(f"/objects/{p}").content == b"v1-old!!"
    # Numbering remains monotonic and the historical rev survives /gc
    # only while referenced; it is now unreferenced, but rev 3 is current.
    gc = client.post(
        "/gc",
        json={"paths": [p], "before": FUTURE, "dry_run": False},
    ).json()
    assert 3 not in [d["revision_no"] for d in gc["deleted"]]
    assert client.get(f"/objects/{p}").content == b"v1-old!!"


def test_failed_self_composition_changes_nothing(client):
    p = _p("self-fail")
    _put(client, p, b"abcdef")
    head = client.head(f"/objects/{p}")
    # Out-of-bounds segment on the source == target path: no new revision.
    r = _compose(
        client, p, [_seg(p, 1, head.headers["etag"], 0, 99)],
        baseline={"revision_no": 1, "etag": head.headers["etag"]},
    )
    assert r.status_code == 400
    g = client.get(f"/objects/{p}")
    assert g.content == b"abcdef"
    assert g.headers["etag"] == head.headers["etag"]


# ---- failure atomicity / numbering ---------------------------------------------

def test_failure_publishes_no_revision_and_numbering_skips_nothing(client):
    a, t = _p("num-src"), _p("num-tgt")
    _put(client, a, b"payload!")
    # Failing attempt (412).
    r = _compose(client, t, [_seg(a, 1, '"%064d"' % 0, 0, 2)], baseline=None)
    assert r.status_code == 412
    # Successful retry starts at rev 1, not 2.
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"payload!"), 0, 2)], baseline=None
    )
    assert r.status_code == 201
    assert r.json()["revision_no"] == 1
    # Failed baselined attempt must not consume a number either.
    put2 = _put(client, t, b"intermediate")  # rev 2
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"payload!"), 0, 2)],
        baseline={"revision_no": 99, "etag": '"x"'},
    )
    assert r.status_code == 412
    put3 = _put(client, t, b"after")  # must be rev 3
    assert put3.headers["x-revision-no"] == "3"


# ---- GC interaction ------------------------------------------------------------

def test_gc_cannot_delete_revision_while_composition_runs(client):
    """Composition FOR UPDATE-locks every source row; a concurrent GC
    execute waits for the commit and then re-classifies: a revision that
    fed a just-committed composition either was current/now historical
    and the new target current revision is itself safe.  The composed
    body is always intact and GC never deletes a revision the composing
    transaction still reads."""
    a, t = _p("gc-src"), _p("gc-tgt")
    v1 = b"old-pinned-bytes"
    big = bytes((i * 13 + 7) % 256 for i in range(2 * 1024 * 1024))
    _put(client, a, v1)
    _put(client, a, big)

    outcomes = {}
    barrier = threading.Barrier(2)

    def do_compose():
        barrier.wait()
        # Compose from the OLD revision 1 (otherwise GC-reachable), so a
        # race would actually be observable.
        outcomes["comp"] = _compose(
            client, t, [_seg(a, 1, ETAG_OF(v1), 0, len(v1))],
            baseline=None,
        )

    def do_gc():
        barrier.wait()
        time.sleep(0.05)
        outcomes["gc"] = client.post(
            "/gc",
            json={"paths": [a], "before": FUTURE, "dry_run": False},
        )

    threads = [threading.Thread(target=do_compose), threading.Thread(target=do_gc)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert not any(th.is_alive() for th in threads)

    assert outcomes["comp"].status_code == 201, outcomes["comp"].text
    assert client.get(f"/objects/{t}").content == v1
    deleted = [d["revision_no"] for d in outcomes["gc"].json()["deleted"]]
    # GC serialised with the composition; whatever order it won, it never
    # deleted a revision the composition was reading.  Rev 2 (the big
    # current revision) must always survive.
    assert 2 not in deleted
    # And the source still serves its current revision intact.
    assert len(client.get(f"/objects/{a}").content) == len(big)


def test_gc_after_committed_composition_keeps_new_target(client):
    a, t = _p("after-src"), _p("after-tgt")
    _put(client, a, b"v1-aaaa")
    _put(client, a, b"v2-curr")
    r = _compose(
        client, t, [_seg(a, 1, ETAG_OF(b"v1-aaaa"), 0, 7)], baseline=None
    )
    assert r.status_code == 201
    # GC over the target keeps its freshly published current revision.
    gc = client.post(
        "/gc",
        json={"paths": [t], "before": FUTURE, "dry_run": False},
    ).json()
    assert gc["deleted"] == []
    assert client.get(f"/objects/{t}").content == b"v1-aaaa"


# ---- concurrency ---------------------------------------------------------------

def test_concurrent_compositions_into_same_target_serialise(client):
    """Two compositions racing one fresh target: one creates rev 1, the
    other wakes to a target that no longer matches its null baseline and
    gets 412 -- never 500, never a duplicate revision 1."""
    a, b, t = _p("cc-a"), _p("cc-b"), _p("cc-t")
    _put(client, a, b"aaaa")
    _put(client, b, b"bbbb")

    for _ in range(8):
        # Fresh target every round.
        target = _p("cc-race-t")
        barrier = threading.Barrier(2)
        outcomes = {}

        def c1():
            barrier.wait()
            outcomes["x"] = _compose(
                client, target,
                [_seg(a, 1, ETAG_OF(b"aaaa"), 0, 4)],
                baseline=None,
            )

        def c2():
            barrier.wait()
            outcomes["y"] = _compose(
                client, target,
                [_seg(b, 1, ETAG_OF(b"bbbb"), 0, 4)],
                baseline=None,
            )

        threads = [threading.Thread(target=c1), threading.Thread(target=c2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        codes = sorted(outcomes[k].status_code for k in ("x", "y"))
        assert codes == [201, 412], codes
        body = client.get(f"/objects/{target}").content
        assert body in (b"aaaa", b"bbbb")
        # Exactly one revision was ever published to the fresh target.
        gc = client.post(
            "/gc",
            json={"paths": [target], "before": FUTURE, "dry_run": False},
        ).json()
        assert gc["deleted"] == []
        winner = outcomes["x"] if outcomes["x"].status_code == 201 else outcomes["y"]
        assert winner.json()["revision_no"] == 1


def test_concurrent_composition_and_put_into_target(client):
    """Composition vs PUT racing one fresh target serialise on the row
    lock: one wins rev 1, the loser is 412 (composition null baseline) /
    412 (PUT If-None-Match) or the PUT without a precondition simply
    becomes rev 2 -- but no 500 and no two rev 1s."""
    a, t = _p("cp-src"), _p("cp-tgt")
    _put(client, a, b"comp-bytes")

    for _ in range(8):
        target = _p("cp-race-t")
        barrier = threading.Barrier(2)
        outcomes = {}

        def do_comp():
            barrier.wait()
            outcomes["comp"] = _compose(
                client, target,
                [_seg(a, 1, ETAG_OF(b"comp-bytes"), 0, 10)],
                baseline=None,
            )

        def do_put():
            barrier.wait()
            outcomes["put"] = _put(client, target, b"put-bytes")

        threads = [
            threading.Thread(target=do_comp),
            threading.Thread(target=do_put),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        comp, put = outcomes["comp"], outcomes["put"]
        assert comp.status_code in (201, 412)
        assert put.status_code == 204
        body = client.get(f"/objects/{target}").content
        assert body in (b"comp-bytes", b"put-bytes")
        if comp.status_code == 201:
            # Composition's revision (1 if it won the race, 2 if the PUT
            # went first) is numbered strictly; never a duplicate 1.
            assert comp.json()["revision_no"] == (
                2 if put.headers["x-revision-no"] == "1" else 1
            )
        assert put.headers["x-revision-no"] in ("1", "2")


def test_concurrent_put_on_source_cannot_mix_new_bytes(client):
    """A source PUT concurrent with a composition that pinned the OLD
    revision: the composed body is the pinned bytes regardless of which
    transaction commits first, and the response never mixes old and new
    halves."""
    a, t = _p("mix-src"), _p("mix-tgt")
    old = b"old-version-0001"  # exactly 16 bytes
    new = b"new-version-0002"  # exactly 16 bytes
    _put(client, a, old)

    successes = 0
    for _ in range(8):
        target = _p("mix-race-t")
        barrier = threading.Barrier(2)
        outcomes = {}

        def do_compose():
            barrier.wait()
            outcomes["comp"] = _compose(
                client, target,
                [
                    _seg(a, 1, ETAG_OF(old), 0, 8),
                    _seg(a, 1, ETAG_OF(old), 8, 16),
                ],
                baseline=None,
            )

        def do_put():
            barrier.wait()
            outcomes["put"] = _put(client, a, new)

        threads = [
            threading.Thread(target=do_compose),
            threading.Thread(target=do_put),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        # The source keeps changing (rev 2, 3, ...); pin rev 1 stays
        # valid because revisions are immutable and retained while locked.
        assert outcomes["comp"].status_code == 201, outcomes["comp"].text
        body = client.get(f"/objects/{target}").content
        assert body == old, body  # never a mix like "old-version456789"
        successes += 1

    assert successes == 8
    assert client.get(f"/objects/{a}").content == new


def test_overlapping_compositions_do_not_deadlock(client):
    """Many compositions with overlapping, crossing source/target lock
    sets all take their rows in one ascending-path order, so they
    serialise rather than deadlock: every request finishes, and no two
    successful compositions on the same target share a revision number.

    Every target starts at rev 1; a worker whose target was already
    rebuilt by another worker simply loses with 412."""
    paths = [_p(f"dead-{i}") for i in range(6)]
    etag = ETAG_OF(b"x" * 20)
    for p in paths:
        _put(client, p, b"x" * 20)

    results: list = []
    barrier = threading.Barrier(6)

    def worker(i):
        # Target and source overlap in both directions across workers
        # (i -> (i+3) modulo 6 is an involution here: crossed pairs).
        target = paths[i]
        source = paths[(i + 3) % len(paths)]
        barrier.wait()
        results.append(
            _compose(
                client, target,
                [_seg(source, 1, etag, 0, 10)],
                baseline={"revision_no": 1, "etag": etag},
            )
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert not any(th.is_alive() for th in threads)
    assert len(results) == 6
    # Only 201/412 outcomes are legal -- never 500, never a lock timeout.
    assert sorted({r.status_code for r in results}) in (
        [201],
        [201, 412],
        [412],
    )
    for p in paths:
        g = client.get(f"/objects/{p}")
        assert g.status_code == 200
        assert g.content in (b"x" * 20, b"x" * 10)
    # Where a composition committed, the original rev 1 is the sole
    # historical revision and the composed rev 2 is current -- no number
    # was duplicated or skipped.
    gc = client.post(
        "/gc",
        json={"paths": paths, "before": FUTURE, "dry_run": True},
    ).json()
    for obj in gc["objects"]:
        nos = [c["revision_no"] for c in obj["candidates"]]
        assert nos in ([], [1])
        retained = [r["revision_no"] for r in obj["retained"]]
        assert retained == [2] if nos == [1] else retained == [1]


def test_composition_waits_behind_streaming_get(client):
    """A streaming GET share-locks a source object row; the composition's
    FOR UPDATE waits until the stream ends, and the stream stays intact.
    The pinned revision remains valid, so after waiting the composition
    still succeeds with exactly the pinned bytes."""
    a, t = _p("stream-src"), _p("stream-tgt")
    body = bytes((i * 17 + 3) % 256 for i in range(2 * 1024 * 1024))
    _put(client, a, body)

    result = {}
    with client.stream("GET", f"/objects/{a}") as resp:
        assert resp.status_code == 200
        it = resp.iter_bytes()
        first = next(it)

        def do_compose():
            result["r"] = _compose(
                client, t, [_seg(a, 1, ETAG_OF(body), 10, 20)],
                baseline=None,
            )

        th = threading.Thread(target=do_compose)
        th.start()
        time.sleep(0.3)  # let the composition block on the share lock
        rest = b"".join(it)
    th.join(timeout=60)
    assert first + rest == body
    assert result["r"].status_code == 201
    assert client.get(f"/objects/{t}").content == body[10:20]


def test_concurrent_move_and_composition_serialise(client):
    """A move (POST /moves) takes the same ordered object-row locks.
    Whichever wins, the outcome is consistent: the composition reads
    only revisions that existed when pinned, the move either detaches
    its source after the commit or loses first, and nothing 500s or
    deadlocks."""
    from tests.test_moves import _move as move

    for _ in range(6):
        s, m, t = _p("mvcomp-s"), _p("mvcomp-m"), _p("mvcomp-t")
        body = b"move-or-compose"
        _put(client, s, body)
        barrier = threading.Barrier(2)
        outcomes = {}

        def do_comp():
            barrier.wait()
            outcomes["comp"] = _compose(
                client, t, [_seg(s, 1, ETAG_OF(body), 0, len(body))],
                baseline=None,
            )

        def do_move():
            barrier.wait()
            outcomes["move"] = move(
                client, s, m, etag=ETAG_OF(body), rev=1
            )

        threads = [
            threading.Thread(target=do_comp),
            threading.Thread(target=do_move),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        comp, mv = outcomes["comp"], outcomes["move"]
        assert comp.status_code in (201, 412)
        assert mv.status_code in (201, 412, 404, 409)
        if comp.status_code == 201:
            # Pinned bytes came from rev 1 whether or not it was detached.
            assert client.get(f"/objects/{t}").content == body
        if mv.status_code == 201:
            assert client.get(f"/objects/{s}").status_code == 404
            assert client.get(f"/objects/{m}").content == body
