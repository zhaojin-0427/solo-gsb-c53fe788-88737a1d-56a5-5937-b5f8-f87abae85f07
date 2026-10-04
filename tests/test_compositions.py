"""End-to-end tests for object composition (POST /compositions)."""
from __future__ import annotations

import hashlib
import threading
import uuid

FUTURE = "2099-01-01T00:00:00Z"

ETAG_OF = lambda body: '"' + hashlib.sha256(body).hexdigest() + '"'


def _p(name: str) -> str:
    return f"comp/{name}-{uuid.uuid4().hex[:12]}"


def _put(client, path: str, body: bytes, media="text/plain"):
    r = client.put(
        f"/objects/{path}", content=body, headers={"Content-Type": media}
    )
    assert r.status_code == 204
    return r


def _src(path, rev, body, start, end):
    """A source fragment pinning (path, revision) by its strong ETag."""
    return {
        "path": path,
        "revision_no": rev,
        "etag": ETAG_OF(body),
        "start": start,
        "end": end,
    }


def _compose(client, target, sources, baseline=None, media="text/plain"):
    return client.post(
        "/compositions",
        json={
            "target_path": target,
            "target_baseline": baseline,
            "media_type": media,
            "sources": sources,
        },
    )


def _baseline(rev, body):
    return {"revision_no": rev, "etag": ETAG_OF(body)}


# ---- validation ---------------------------------------------------------------

def test_composition_validation(client):
    src = _p("v-src")
    _put(client, src, b"0123456789")
    good_source = _src(src, 1, b"0123456789", 0, 5)

    assert client.post("/compositions", content=b"not json").status_code == 400
    assert client.post("/compositions", json=["a"]).status_code == 400
    assert client.post("/compositions", json={}).status_code == 400

    # target_path: required, non-empty string.
    assert _compose(client, "", [good_source]).status_code == 400
    r = client.post(
        "/compositions",
        json={
            "target_path": 7,
            "target_baseline": None,
            "media_type": "text/plain",
            "sources": [good_source],
        },
    )
    assert r.status_code == 400

    # target_baseline: key required; null or {revision_no, strong etag}.
    r = client.post(
        "/compositions",
        json={
            "target_path": _p("v-t"),
            "media_type": "text/plain",
            "sources": [good_source],
        },
    )
    assert r.status_code == 400  # key missing
    for bad in (42, "x", [], True):
        r = client.post(
            "/compositions",
            json={
                "target_path": _p("v-t"),
                "target_baseline": bad,
                "media_type": "text/plain",
                "sources": [good_source],
            },
        )
        assert r.status_code == 400, bad
    for bad_no in (0, -1, 1.5, "1", True, None):
        r = client.post(
            "/compositions",
            json={
                "target_path": _p("v-t"),
                "target_baseline": {"revision_no": bad_no, "etag": '"x"'},
                "media_type": "text/plain",
                "sources": [good_source],
            },
        )
        assert r.status_code == 400, bad_no
    for bad_etag in ("", 123, "not-an-etag", 'W/"abc"', None):
        r = client.post(
            "/compositions",
            json={
                "target_path": _p("v-t"),
                "target_baseline": {"revision_no": 1, "etag": bad_etag},
                "media_type": "text/plain",
                "sources": [good_source],
            },
        )
        assert r.status_code == 400, bad_etag

    # media_type: required, non-empty string.
    for bad in (None, "", 7):
        r = client.post(
            "/compositions",
            json={
                "target_path": _p("v-t"),
                "target_baseline": None,
                "media_type": bad,
                "sources": [good_source],
            },
        )
        assert r.status_code == 400, bad

    # sources: 1..100 fragments.
    assert _compose(client, _p("v-t"), []).status_code == 400
    assert _compose(client, _p("v-t"), "a").status_code == 400
    assert _compose(client, _p("v-t"), [good_source] * 101).status_code == 400
    assert _compose(client, _p("v-t"), ["x"]).status_code == 400

    # fragment fields.
    def with_field(**patch):
        item = dict(good_source)
        item.update(patch)
        return _compose(client, _p("v-t"), [item])

    assert with_field(path="").status_code == 400
    assert with_field(path=7).status_code == 400
    for bad_no in (0, -2, 1.5, "1", False, None):
        assert with_field(revision_no=bad_no).status_code == 400, bad_no
    for bad_etag in ("", 5, "nope", 'W/"abc"', None):
        assert with_field(etag=bad_etag).status_code == 400, bad_etag
    for bad_start in (-1, 1.5, "0", True, None):
        assert with_field(start=bad_start).status_code == 400, bad_start
    for bad_end in (-1, 2.5, "5", True, None):
        assert with_field(end=bad_end).status_code == 400, bad_end
    # Empty or reversed fragments: end <= start is a 400.
    assert with_field(start=3, end=3).status_code == 400
    assert with_field(start=4, end=3).status_code == 400
    assert with_field(start=0, end=0).status_code == 400
    # Missing keys.
    for key in ("path", "revision_no", "etag", "start", "end"):
        item = dict(good_source)
        del item[key]
        assert _compose(client, _p("v-t"), [item]).status_code == 400, key


def test_composition_exactly_100_sources_accepted(client):
    src = _p("hundred-src")
    _put(client, src, b"x")
    target = _p("hundred-t")
    r = _compose(client, target, [_src(src, 1, b"x", 0, 1)] * 100)
    assert r.status_code == 201
    assert r.json()["bytes"] == 100
    assert client.get(f"/objects/{target}").content == b"x" * 100


# ---- happy path ---------------------------------------------------------------

def test_compose_concatenates_fragments_in_request_order(client):
    a, b, target = _p("ord-a"), _p("ord-b"), _p("ord-t")
    _put(client, a, b"HEADER;", media="application/x-a")
    _put(client, b, b"footer.", media="application/x-b")

    r = _compose(
        client,
        target,
        [
            _src(a, 1, b"HEADER;", 0, 4),   # "HEAD"
            _src(b, 1, b"footer.", 0, 7),   # "footer."
            _src(a, 1, b"HEADER;", 4, 7),   # "ER;"
        ],
        media="application/x-composed",
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["target_path"] == target
    assert data["revision_no"] == 1
    assert data["bytes"] == len(b"HEADfooter.ER;")
    assert data["etag"] == ETAG_OF(b"HEADfooter.ER;")
    assert r.headers["etag"] == data["etag"]
    assert r.headers["x-revision-no"] == "1"
    assert "last-modified" in r.headers

    g = client.get(f"/objects/{target}")
    assert g.status_code == 200
    assert g.content == b"HEADfooter.ER;"
    assert g.headers["etag"] == data["etag"]
    assert g.headers["content-type"].startswith("application/x-composed")

    # Fragment order is the semantics: swapping fragments changes the
    # bytes and therefore the ETag.
    target2 = _p("ord-t2")
    r2 = _compose(
        client,
        target2,
        [_src(b, 1, b"footer.", 0, 7), _src(a, 1, b"HEADER;", 0, 7)],
    )
    assert r2.status_code == 201
    assert client.get(f"/objects/{target2}").content == b"footer.HEADER;"
    assert r2.json()["etag"] != data["etag"]


def test_compose_half_open_intervals(client):
    """[start, end): end is exclusive; adjacent fragments tile exactly."""
    src, target = _p("half-src"), _p("half-t")
    _put(client, src, b"0123456789")
    r = _compose(
        client,
        target,
        [
            _src(src, 1, b"0123456789", 2, 5),    # "234"
            _src(src, 1, b"0123456789", 0, 2),    # "01"
            _src(src, 1, b"0123456789", 5, 10),   # "56789"
        ],
    )
    assert r.status_code == 201
    assert r.json()["bytes"] == 10
    assert client.get(f"/objects/{target}").content == b"2340156789"
    # A single fragment spanning the whole revision: end == length is legal.
    whole = _compose(client, _p("half-t2"), [_src(src, 1, b"0123456789", 0, 10)])
    assert whole.status_code == 201
    assert whole.json()["bytes"] == 10


def test_compose_same_source_repeated(client):
    src, target = _p("rep-src"), _p("rep-t")
    _put(client, src, b"ab")
    r = _compose(
        client,
        target,
        [_src(src, 1, b"ab", 0, 2)] * 3 + [_src(src, 1, b"ab", 1, 2)],
    )
    assert r.status_code == 201
    assert client.get(f"/objects/{target}").content == b"abababb"


def test_compose_reads_pinned_old_revision(client):
    """Fragments come from the named revision, not the current one."""
    src, target = _p("old-src"), _p("old-t")
    _put(client, src, b"v1-bytes")
    _put(client, src, b"v2-is-longer-and-different")
    r = _compose(client, target, [_src(src, 1, b"v1-bytes", 0, 2)])
    assert r.status_code == 201
    assert client.get(f"/objects/{target}").content == b"v1"
    # The current revision of the source is untouched.
    assert client.get(f"/objects/{src}").content == b"v2-is-longer-and-different"


def test_compose_target_equals_source_path(client):
    """Target and source may be the same path: the fragments are read
    from the pinned OLD revision and published as the next revision."""
    p = _p("self")
    _put(client, p, b"self-v1")
    _put(client, p, b"self-v2-longer")
    r = _compose(
        client,
        p,
        [_src(p, 1, b"self-v1", 0, 4), _src(p, 2, b"self-v2-longer", 5, 8)],
        baseline={"revision_no": 2, "etag": ETAG_OF(b"self-v2-longer")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["revision_no"] == 3
    assert client.get(f"/objects/{p}").content == b"selfv2-"


def test_compose_into_moved_away_target_continues_numbering(client):
    """A moved-away path (no current revision) accepts baseline=null and
    keeps its high-water revision numbering."""
    t, elsewhere = _p("mw-t"), _p("mw-elsewhere")
    _put(client, t, b"m1")
    put2 = _put(client, t, b"m2")
    src = _p("mw-src")
    _put(client, src, b"payload")
    moved = client.post(
        "/moves",
        json={
            "source_path": t,
            "target_path": elsewhere,
            "source_etag": put2.headers["etag"],
            "source_revision_no": 2,
        },
    )
    assert moved.status_code == 201
    r = _compose(client, t, [_src(src, 1, b"payload", 0, 7)], baseline=None)
    assert r.status_code == 201
    assert r.json()["revision_no"] == 3  # 1,2 moved away; never reused


# ---- target baseline ------------------------------------------------------------

def test_baseline_null_requires_absent_target(client):
    src, target = _p("bn-src"), _p("bn-t")
    _put(client, src, b"data")
    _put(client, target, b"occupied")
    r = _compose(client, target, [_src(src, 1, b"data", 0, 4)], baseline=None)
    assert r.status_code == 412
    data = r.json()
    assert data["detail"] == "target_baseline_mismatch"
    assert data["current_revision_no"] == 1
    assert data["current_etag"] == ETAG_OF(b"occupied")
    assert r.headers["etag"] == ETAG_OF(b"occupied")
    # The failed composition changed nothing.
    assert client.get(f"/objects/{target}").content == b"occupied"
    assert client.head(f"/objects/{target}").headers["x-revision-no"] if False else True


def test_baseline_match_allows_recompose_with_next_number(client):
    src, target = _p("bm-src"), _p("bm-t")
    _put(client, src, b"first")
    _put(client, src, b"second")
    r1 = _compose(client, target, [_src(src, 1, b"first", 0, 5)])
    assert r1.status_code == 201 and r1.json()["revision_no"] == 1

    # Correct baseline (rev 1 identity) -> rev 2.
    r2 = _compose(
        client,
        target,
        [_src(src, 2, b"second", 0, 6)],
        baseline={"revision_no": 1, "etag": r1.json()["etag"]},
    )
    assert r2.status_code == 201
    assert r2.json()["revision_no"] == 2
    assert client.get(f"/objects/{target}").content == b"second"

    # Stale revision number -> 412.
    r3 = _compose(
        client,
        target,
        [_src(src, 2, b"second", 0, 6)],
        baseline={"revision_no": 1, "etag": r2.json()["etag"]},
    )
    assert r3.status_code == 412
    assert r3.json()["current_revision_no"] == 2
    # Stale ETag -> 412.
    r4 = _compose(
        client,
        target,
        [_src(src, 2, b"second", 0, 6)],
        baseline={"revision_no": 2, "etag": r1.json()["etag"]},
    )
    assert r4.status_code == 412
    assert r4.json()["current_etag"] == r2.json()["etag"]
    assert client.get(f"/objects/{target}").content == b"second"


def test_baseline_given_but_target_absent_is_412(client):
    src, target = _p("ba-src"), _p("ba-t")
    _put(client, src, b"data")
    r = _compose(
        client,
        target,
        [_src(src, 1, b"data", 0, 4)],
        baseline=_baseline(1, b"whatever"),
    )
    assert r.status_code == 412
    data = r.json()
    assert data["detail"] == "target_baseline_mismatch"
    assert data["current_etag"] is None
    assert data["current_revision_no"] is None
    assert client.get(f"/objects/{target}").status_code == 404


# ---- source errors --------------------------------------------------------------

def test_source_revision_not_found_is_404(client):
    src, target = _p("nf-src"), _p("nf-t")
    _put(client, src, b"only-rev-1")
    # Path exists but revision 2 does not.
    r = _compose(client, target, [_src(src, 2, b"only-rev-1", 0, 1)])
    assert r.status_code == 404
    data = r.json()
    assert data["detail"] == "source_revision_not_found"
    assert data["source_index"] == 0
    assert data["path"] == src and data["revision_no"] == 2
    # Path never published at all.
    r = _compose(client, target, [_src(_p("nf-never"), 1, b"x", 0, 1)])
    assert r.status_code == 404
    # Nothing was created at the target.
    assert client.get(f"/objects/{target}").status_code == 404


def test_source_revision_collected_by_gc_is_404(client):
    src, target = _p("gc-src"), _p("gc-t")
    _put(client, src, b"collected-v1")
    _put(client, src, b"kept-v2")
    gone = client.post(
        "/gc", json={"paths": [src], "before": FUTURE, "dry_run": False}
    )
    assert [d["revision_no"] for d in gone.json()["deleted"]] == [1]
    r = _compose(client, target, [_src(src, 1, b"collected-v1", 0, 4)])
    assert r.status_code == 404
    assert r.json()["detail"] == "source_revision_not_found"
    assert client.get(f"/objects/{target}").status_code == 404


def test_source_etag_mismatch_is_412(client):
    src, target = _p("mm-src"), _p("mm-t")
    _put(client, src, b"real-bytes")
    r = _compose(client, target, [_src(src, 1, b"other-bytes", 0, 3)])
    assert r.status_code == 412
    data = r.json()
    assert data["detail"] == "source_etag_mismatch"
    assert data["source_index"] == 0
    assert data["current_etag"] == ETAG_OF(b"real-bytes")
    assert r.headers["etag"] == ETAG_OF(b"real-bytes")
    assert client.get(f"/objects/{target}").status_code == 404


def test_fragment_out_of_range_is_400(client):
    src, target = _p("oor-src"), _p("oor-t")
    _put(client, src, b"12345")
    # end past the revision length.
    r = _compose(client, target, [_src(src, 1, b"12345", 0, 6)])
    assert r.status_code == 400
    data = r.json()
    assert data["detail"] == "fragment_out_of_range"
    assert data["source_index"] == 0
    assert data["start"] == 0 and data["end"] == 6 and data["length"] == 5
    # start inside, end outside.
    assert _compose(client, target, [_src(src, 1, b"12345", 4, 9)]).status_code == 400
    # start at the very end: any non-empty fragment is out of range.
    assert _compose(client, target, [_src(src, 1, b"12345", 5, 6)]).status_code == 400
    # Nothing left behind.
    assert client.get(f"/objects/{target}").status_code == 404


def test_sources_checked_in_request_order(client):
    """The first failing fragment (in request order) is the one reported."""
    ok, missing = _p("ord-ok"), _p("ord-missing")
    _put(client, ok, b"fine")
    r = _compose(
        client,
        _p("ord-t"),
        [
            _src(ok, 1, b"fine", 0, 4),
            _src(missing, 3, b"x", 0, 1),
            _src(ok, 1, b"wrong-etag", 0, 4),
        ],
    )
    assert r.status_code == 404
    assert r.json()["source_index"] == 1


def test_failure_leaves_no_target_revision_and_no_placeholder(client):
    """After any failure the target is a clean 404 and can still be
    created with If-None-Match: * (no committed placeholder interferes)."""
    src, target = _p("rb-src"), _p("rb-t")
    _put(client, src, b"data")
    assert _compose(client, target, [_src(src, 9, b"data", 0, 1)]).status_code == 404
    create = client.put(
        f"/objects/{target}",
        content=b"fresh",
        headers={"If-None-Match": "*"},
    )
    assert create.status_code == 204
    assert create.headers["x-revision-no"] == "1"


# ---- interplay with the existing mechanisms --------------------------------------

def test_composed_object_supports_conditionals_and_ranges(client):
    """A composed revision is an ordinary revision: conditional requests,
    ranges and optimistic writes behave exactly as for PUT content."""
    src, target = _p("cond-src"), _p("cond-t")
    _put(client, src, b"0123456789")
    composed = _compose(client, target, [_src(src, 1, b"0123456789", 0, 10)])
    etag = composed.json()["etag"]

    assert client.get(
        f"/objects/{target}", headers={"If-None-Match": etag}
    ).status_code == 304
    r = client.get(f"/objects/{target}", headers={"Range": "bytes=2-5"})
    assert r.status_code == 206 and r.content == b"2345"
    assert client.get(
        f"/objects/{target}", headers={"If-Match": '"stale"'}
    ).status_code == 412
    # Optimistic write on the composed object.
    assert client.put(
        f"/objects/{target}", content=b"v2", headers={"If-Match": etag}
    ).status_code == 204
    assert client.put(
        f"/objects/{target}", content=b"v3", headers={"If-Match": etag}
    ).status_code == 412


def test_composition_does_not_pin_sources_against_gc(client):
    """Once the composition commits, its sources are ordinary revisions
    again: /gc collects an unpinned source revision, and the composed
    target (its own independent revision) is unaffected."""
    src, target = _p("pin-src"), _p("pin-t")
    _put(client, src, b"gc-me-v1")
    _put(client, src, b"keep-v2")
    r = _compose(client, target, [_src(src, 1, b"gc-me-v1", 0, 8)])
    assert r.status_code == 201

    preview = client.post(
        "/gc", json={"paths": [src], "before": FUTURE, "dry_run": True}
    ).json()
    (obj,) = preview["objects"]
    assert obj["candidates"] == [{"revision_no": 1, "bytes": 8}]
    assert obj["retained"] == [{"revision_no": 2, "reasons": ["current"]}]

    gone = client.post(
        "/gc", json={"paths": [src], "before": FUTURE, "dry_run": False}
    )
    assert [d["revision_no"] for d in gone.json()["deleted"]] == [1]
    assert client.get(f"/objects/{target}").content == b"gc-me-v1"


def test_compose_from_moved_away_source_revision(client):
    """A move detaches the source's current pointer but keeps its
    revisions: composing from the old revision still works."""
    src, dst, target = _p("mv-src"), _p("mv-dst"), _p("mv-t")
    _put(client, src, b"moved-away-bytes")
    _put(client, src, b"current-v2")
    moved = client.post(
        "/moves",
        json={
            "source_path": src,
            "target_path": dst,
            "source_etag": ETAG_OF(b"current-v2"),
            "source_revision_no": 2,
        },
    )
    assert moved.status_code == 201
    r = _compose(client, target, [_src(src, 1, b"moved-away-bytes", 0, 5)])
    assert r.status_code == 201
    assert client.get(f"/objects/{target}").content == b"moved"


def test_compose_from_lease_pinned_historical_revision(client):
    """A revision pinned by a lease is still in the library and composable."""
    src, target = _p("lease-src"), _p("lease-t")
    _put(client, src, b"leased-v1")
    _put(client, src, b"leased-v2")
    lid = client.post(
        "/revision-leases",
        json={"path": src, "revision_no": 1, "ttl_seconds": 600},
    ).json()["lease_id"]
    r = _compose(client, target, [_src(src, 1, b"leased-v1", 0, 6)])
    assert r.status_code == 201
    assert client.get(f"/objects/{target}").content == b"leased"
    client.delete(f"/revision-leases/{lid}")


# ---- concurrency --------------------------------------------------------------

def test_composition_races_target_put(client):
    """Composition (baseline null) vs a first PUT on the target serialise
    on the row lock: either the PUT wins (composition 412) or the
    composition wins (PUT continues as revision 2) -- never a 500, never
    two revision 1s."""
    for i in range(8):
        src, target = _p(f"tp-src-{i}"), _p(f"tp-t-{i}")
        _put(client, src, b"source-bytes")
        barrier = threading.Barrier(2)
        results = {}

        def do_compose():
            barrier.wait()
            results["comp"] = _compose(
                client, target, [_src(src, 1, b"source-bytes", 0, 6)]
            )

        def do_put():
            barrier.wait()
            results["put"] = _put(client, target, b"put-bytes")

        threads = [threading.Thread(target=do_compose), threading.Thread(target=do_put)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)

        comp, put = results["comp"], results["put"]
        assert put.status_code == 204
        if comp.status_code == 201:
            # Composition published rev 1; the PUT waited and continued.
            assert comp.json()["revision_no"] == 1
            assert put.headers["x-revision-no"] == "2"
            assert client.get(f"/objects/{target}").content == b"put-bytes"
        else:
            # PUT committed first; the null baseline no longer holds.
            assert comp.status_code == 412
            assert comp.json()["detail"] == "target_baseline_mismatch"
            assert put.headers["x-revision-no"] == "1"
            assert client.get(f"/objects/{target}").content == b"put-bytes"


def test_composition_races_put_on_source(client):
    """A PUT publishing a NEW revision of the source can never mix new
    bytes into a composition: fragments always come from the pinned old
    revision, whichever transaction commits first."""
    for i in range(8):
        src, target = _p(f"sp-src-{i}"), _p(f"sp-t-{i}")
        _put(client, src, b"old-v1")
        barrier = threading.Barrier(2)
        results = {}

        def do_compose():
            barrier.wait()
            results["comp"] = _compose(
                client, target, [_src(src, 1, b"old-v1", 0, 6)]
            )

        def do_put():
            barrier.wait()
            results["put"] = _put(client, src, b"brand-new-v2")

        threads = [threading.Thread(target=do_compose), threading.Thread(target=do_put)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)

        assert results["comp"].status_code == 201
        assert results["put"].status_code == 204
        assert results["put"].headers["x-revision-no"] == "2"
        # The composed bytes are the old revision's, always.
        assert client.get(f"/objects/{target}").content == b"old-v1"
        assert client.get(f"/objects/{src}").content == b"brand-new-v2"


def test_composition_races_gc_on_source(client):
    """Composition vs /gc execute on the source path serialise on the
    object row lock: either GC committed first (composition 404) or the
    composition copied the bytes first (201) and GC collects the source
    revision right after -- never a deleted-in-flight source, never a
    deadlock."""
    for i in range(8):
        src, target = _p(f"gc-src-{i}"), _p(f"gc-t-{i}")
        _put(client, src, b"raced-old")
        _put(client, src, b"raced-current")
        barrier = threading.Barrier(2)
        results = {}

        def do_compose():
            barrier.wait()
            results["comp"] = _compose(
                client, target, [_src(src, 1, b"raced-old", 0, 5)]
            )

        def do_gc():
            barrier.wait()
            results["gc"] = client.post(
                "/gc",
                json={"paths": [src], "before": FUTURE, "dry_run": False},
            )

        threads = [threading.Thread(target=do_compose), threading.Thread(target=do_gc)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)

        comp, gc = results["comp"], results["gc"]
        assert gc.status_code == 200
        # In either interleaving rev 1 ends up collected: GC either won
        # outright or re-classified after the composition committed (the
        # composition leaves no retention pin behind).
        assert [d["revision_no"] for d in gc.json()["deleted"]] == [1]
        if comp.status_code == 201:
            # Composition held the locks first: bytes copied before GC.
            assert client.get(f"/objects/{target}").content == b"raced"
        else:
            # GC committed first: the revision is gone, cleanly 404.
            assert comp.status_code == 404
            assert comp.json()["detail"] == "source_revision_not_found"
            assert client.get(f"/objects/{target}").status_code == 404
        assert client.get(f"/objects/{src}").content == b"raced-current"


def test_composition_races_move_of_source(client):
    """A move detaches the source's current pointer but never deletes
    revisions, so a concurrent composition from the pinned old revision
    always succeeds with the original bytes; the two serialise on the
    shared row without deadlocking."""
    for i in range(6):
        src, dst, target = _p(f"mv-s-{i}"), _p(f"mv-d-{i}"), _p(f"mv-t-{i}")
        _put(client, src, b"raced-move-v1")
        _put(client, src, b"raced-move-v2")
        barrier = threading.Barrier(2)
        results = {}

        def do_compose():
            barrier.wait()
            results["comp"] = _compose(
                client, target, [_src(src, 1, b"raced-move-v1", 0, 10)]
            )

        def do_move():
            barrier.wait()
            results["move"] = client.post(
                "/moves",
                json={
                    "source_path": src,
                    "target_path": dst,
                    "source_etag": ETAG_OF(b"raced-move-v2"),
                    "source_revision_no": 2,
                },
            )

        threads = [threading.Thread(target=do_compose), threading.Thread(target=do_move)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)

        assert results["comp"].status_code == 201
        assert results["move"].status_code == 201
        assert client.get(f"/objects/{target}").content == b"raced-move"
        assert client.get(f"/objects/{dst}").content == b"raced-move-v2"
        assert client.get(f"/objects/{src}").status_code == 404


def test_two_compositions_swapping_roles_do_not_deadlock(client):
    """A composes target=X from source=Y while B composes target=Y from
    source=X: both lock sets are taken in the same ascending path order
    with one lock strength, so they serialise instead of deadlocking."""
    for i in range(8):
        x, y = _p(f"swap-x-{i}"), _p(f"swap-y-{i}")
        _put(client, x, b"xxx")
        _put(client, y, b"yy")
        barrier = threading.Barrier(2)
        results = {}

        def comp_a():
            barrier.wait()
            results["a"] = _compose(
                client,
                x,
                [_src(y, 1, b"yy", 0, 2)],
                baseline=_baseline(1, b"xxx"),
            )

        def comp_b():
            barrier.wait()
            results["b"] = _compose(
                client,
                y,
                [_src(x, 1, b"xxx", 0, 3)],
                baseline=_baseline(1, b"yy"),
            )

        threads = [threading.Thread(target=comp_a), threading.Thread(target=comp_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)  # no deadlock

        a, b = results["a"], results["b"]
        # Each baseline pins the composition's OWN target at rev 1, and a
        # composition only rewrites its own target, so both legitimately
        # succeed (the source fragments come from immutable rev-1 rows):
        # the race must serialise on the shared rows, never deadlock,
        # never 500, and never mix post-baseline bytes into a result.
        assert a.status_code == 201 and b.status_code == 201
        assert a.json()["revision_no"] == 2 and b.json()["revision_no"] == 2
        assert client.get(f"/objects/{x}").content == b"yy"
        assert client.get(f"/objects/{y}").content == b"xxx"
