"""End-to-end tests for ``GET /object-index``.

Covers parameter validation (prefix, default/1..100 limit), ascending
path paging with no duplicates and no omissions, ten-minute snapshot
replay against inter-page PUTs / moves / GC (410 once expired), and
tampered/mismatched cursors (400).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg


def _path(name: str) -> str:
    return f"idx/{name}-{uuid.uuid4().hex[:10]}"


def _put(client, path: str, body: bytes, media_type: str = "application/octet-stream"):
    return client.put(
        f"/objects/{path}", content=body, headers={"Content-Type": media_type}
    )


def _get(client, **params):
    return client.get("/object-index", params=params)


def _walk(client, **params):
    """Follow next_cursor until the end; return all entries in order."""
    pages = []
    cursor = None
    seen_snapshots = set()
    while True:
        query = dict(params)
        if cursor is not None:
            query["cursor"] = cursor
        r = _get(client, **query)
        assert r.status_code == 200, r.text
        body = r.json()
        pages.append(body)
        seen_snapshots.add(body["snapshot_id"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen_snapshots) == 1  # the whole walk replays one snapshot
    entries = [e for p in pages for e in p["entries"]]
    return pages, entries


# ---- parameter validation -----------------------------------------------------


def test_default_limit_is_50(client):
    prefix = _path("default")
    for i in range(55):
        _put(client, f"{prefix}/obj-{i:03d}", f"body-{i}".encode())
    r = _get(client, prefix=prefix)
    assert r.status_code == 200
    body = r.json()
    assert len(body["entries"]) == 50
    assert body["limit"] == 50
    assert body["total"] == 55
    assert body["offset"] == 0
    assert body["next_cursor"] is not None

    r2 = _get(client, prefix=prefix, cursor=body["next_cursor"])
    assert r2.status_code == 200
    body2 = r2.json()
    assert len(body2["entries"]) == 5
    assert body2["offset"] == 50
    assert body2["next_cursor"] is None

    # Replaying the (null-cursor) final position returns the same empty
    # page: mint the end cursor explicitly from body2's offset.
    from app.main import app

    secret = app.state.index_secret
    from app.object_index import IndexCursor, encode_cursor

    end_cursor = encode_cursor(
        secret,
        IndexCursor(
            snapshot_id=body2["snapshot_id"],
            offset=55,
            prefix=prefix,
            limit=50,
        ),
    )
    r3 = _get(client, prefix=prefix, cursor=end_cursor)
    assert r3.status_code == 200
    assert r3.json()["entries"] == []
    assert r3.json()["offset"] == 55
    assert r3.json()["next_cursor"] is None


def test_limit_boundaries(client):
    prefix = _path("bounds")
    _put(client, f"{prefix}/a", b"a")
    for value in ("1", "100", "50"):
        assert _get(client, prefix=prefix, limit=value).status_code == 200
    for value in ("0", "101", "-1", "1.5", "01", "", "abc", "true", "1e2"):
        r = _get(client, prefix=prefix, limit=value)
        assert r.status_code == 400, (value, r.status_code, r.text)
        assert r.json()["detail"].startswith("limit must be an integer")


def test_repeated_parameters_are_400(client):
    assert client.get("/object-index?limit=2&limit=3").status_code == 400
    assert client.get("/object-index?prefix=a&prefix=b").status_code == 400
    assert client.get("/object-index?cursor=x&cursor=y").status_code == 400


def test_prefix_matches_literally_not_segments(client):
    base = _path("lit")
    _put(client, f"{base}/a", b"1")
    _put(client, f"{base}/ab", b"2")
    _put(client, f"{base}/abc", b"3")
    _put(client, f"{base}/z", b"4")
    _, entries = _walk(client, prefix=f"{base}/a", limit=2)
    assert [e["path"] for e in entries] == [f"{base}/a", f"{base}/ab", f"{base}/abc"]


def test_empty_prefix_is_the_whole_namespace(client):
    # An empty-store snapshot is itself a valid empty full walk.  (Use a
    # unique prefix so concurrent tests cannot add rows to the set.)
    empty = _path("empty-namespace")
    r = _get(client, prefix=empty, limit=10)
    assert r.status_code == 200
    assert r.json()["entries"] == []
    assert r.json()["total"] == 0
    assert r.json()["next_cursor"] is None

    # prefix="" / prefix omitted both mean the whole namespace: walk a
    # full-namespace snapshot (more than 100 objects may exist when the
    # whole suite shares one database) and find the new object in order.
    p = _path("full")
    _put(client, f"{p}/one", b"one")
    for kwargs in ({"prefix": ""}, {}):
        pages, entries = _walk(client, limit=100, **kwargs)
        paths = [e["path"] for e in entries]
        assert f"{p}/one" in paths
        assert paths == sorted(paths)
        assert pages[0]["prefix"] == ""


# ---- ordering, paging, entry shape -------------------------------------------


def test_ascending_order_and_entry_fields(client):
    prefix = _path("order")
    paths = [f"{prefix}/{n}" for n in ("c", "a", "b", "z", "m")]
    meta = {}
    for p in paths:
        r = _put(client, p, f"content-of-{p}".encode(), "text/csv")
        meta[p] = (int(r.headers["x-revision-no"]), r.headers["etag"])
    pages, entries = _walk(client, prefix=prefix, limit=2)
    assert [e["path"] for e in entries] == sorted(paths)
    for e in entries:
        rev_no, etag = meta[e["path"]]
        assert e["revision_no"] == rev_no
        assert e["etag"] == etag
        assert e["media_type"] == "text/csv"
        assert e["bytes"] == len(f"content-of-{e['path']}".encode())
        assert set(e) == {"path", "revision_no", "etag", "media_type", "bytes"}
    assert [len(p["entries"]) for p in pages] == [2, 2, 1]
    assert [p["offset"] for p in pages] == [0, 2, 4]
    assert pages[0]["total"] == 5
    assert pages[-1]["next_cursor"] is None


def test_paging_no_duplicates_no_omissions(client):
    prefix = _path("pages")
    want = [f"{prefix}/k-{i:04d}" for i in range(137)]
    for p in want:
        _put(client, p, p.encode())
    pages, entries = _walk(client, prefix=prefix, limit=10)
    got = [e["path"] for e in entries]
    assert got == want
    assert len(got) == len(set(got)) == 137
    assert [len(p["entries"]) for p in pages][:-1] == [10] * 13
    assert len(pages[-1]["entries"]) == 7


# ---- snapshot immutability ----------------------------------------------------


def test_put_between_pages_does_not_change_replay(client):
    prefix = _path("freeze-put")
    for i in range(5):
        _put(client, f"{prefix}/old-{i}", f"v1-{i}".encode())
    first = _get(client, prefix=prefix, limit=2).json()
    cursor = first["next_cursor"]
    # Writes after the snapshot: a new object, an overwrite, a revision.
    _put(client, f"{prefix}/new-after-snapshot", b"late")
    _put(client, f"{prefix}/old-0", b"v2-overwritten")

    # Continue the SAME snapshot via the cursor (not a fresh walk).
    got = list(first["entries"])
    while cursor is not None:
        page = _get(client, prefix=prefix, limit=2, cursor=cursor).json()
        assert page["snapshot_id"] == first["snapshot_id"]
        got.extend(page["entries"])
        cursor = page["next_cursor"]

    assert [e["path"] for e in got] == [f"{prefix}/old-{i}" for i in range(5)]
    assert f"{prefix}/new-after-snapshot" not in [e["path"] for e in got]
    old0 = next(e for e in got if e["path"] == f"{prefix}/old-0")
    assert old0["bytes"] == len(b"v1-0")  # frozen metadata, not the overwrite

    # Replaying the very first cursor returns the identical first page.
    replayed = _get(
        client, prefix=prefix, limit=2, cursor=_start_cursor(first)
    ).json()
    assert replayed == first

    # A fresh no-cursor request sees the post-snapshot state.
    fresh = _get(client, prefix=prefix, limit=50).json()
    fresh_paths = {e["path"] for e in fresh["entries"]}
    assert f"{prefix}/new-after-snapshot" in fresh_paths
    assert fresh["snapshot_id"] != first["snapshot_id"]


def _start_cursor(page):
    """Mint an offset-0 cursor for a snapshot (replays its first page)."""
    from app.main import app
    from app.object_index import IndexCursor, encode_cursor

    return encode_cursor(
        app.state.index_secret,
        IndexCursor(
            snapshot_id=page["snapshot_id"],
            offset=0,
            prefix=page["prefix"],
            limit=page["limit"],
        ),
    )


def test_move_between_pages_does_not_change_replay(client):
    prefix = _path("freeze-move")
    _put(client, f"{prefix}/keep", b"keep")
    moving = f"{prefix}/will-move"
    r = _put(client, moving, b"moving-bytes", "text/plain")
    etag = r.headers["etag"]
    rev_no = int(r.headers["x-revision-no"])

    first = _get(client, prefix=prefix, limit=1).json()
    cursor = first["next_cursor"]

    move = client.post(
        "/moves",
        json={
            "source_path": moving,
            "target_path": f"{_path('moved-target')}/elsewhere",
            "source_etag": etag,
            "source_revision_no": rev_no,
        },
    )
    assert move.status_code == 201, move.text

    r2 = _get(client, prefix=prefix, limit=1, cursor=cursor)
    assert r2.status_code == 200
    paths = [e["path"] for e in first["entries"]] + [
        e["path"] for e in r2.json()["entries"]
    ]
    assert moving in paths  # the detached source stays in the frozen index
    assert client.get(f"/objects/{moving}").status_code == 404


def test_gc_between_pages_does_not_change_replay(client):
    prefix = _path("freeze-gc")
    p = f"{prefix}/history"
    _put(client, p, b"rev-one", "text/plain")
    r2 = _put(client, p, b"rev-two-current")
    # A second object so limit=1 yields a real next_cursor.
    _put(client, f"{prefix}/second", b"other")

    first = _get(client, prefix=prefix, limit=1).json()
    cursor = first["next_cursor"]

    # Collect the historical revision; verify frozen entry metadata is
    # unaffected (the snapshot has no FK to revisions).
    gc = client.post(
        "/gc",
        json={
            "paths": [p],
            "before": "2099-01-01T00:00:00Z",
            "dry_run": False,
        },
    )
    assert gc.status_code == 200, gc.text
    assert gc.json()["deleted"][0]["revision_no"] == 1

    seen = list(first["entries"])
    while cursor is not None:
        page = _get(client, prefix=prefix, limit=1, cursor=cursor).json()
        assert page["snapshot_id"] == first["snapshot_id"]
        seen.extend(page["entries"])
        cursor = page["next_cursor"]

    entry = next(e for e in seen if e["path"] == p)
    assert entry["revision_no"] == 2
    assert entry["etag"] == r2.headers["etag"]
    assert entry["bytes"] == len(b"rev-two-current")


def test_snapshot_is_not_a_gc_retention_reason(client):
    """Pinning an index entry must NOT retain the revision for /gc: a
    snapshot is metadata-only and deliberately has no revision FK."""
    prefix = _path("no-pin")
    p = f"{prefix}/obj"
    _put(client, p, b"first-historical")
    _put(client, p, b"second-historical")
    third = _put(client, p, b"third-current")
    assert _get(client, prefix=prefix).status_code == 200

    # Revisions 1 and 2 are historical; the index snapshot pinned
    # neither: only revision 3 survives as current.
    gc = client.post(
        "/gc",
        json={
            "paths": [p],
            "before": "2099-01-01T00:00:00Z",
            "dry_run": False,
        },
    ).json()
    assert {d["revision_no"] for d in gc["deleted"]} == {1, 2}
    retained = [
        (d["path"], d["revision_no"], tuple(d["reasons"]))
        for d in gc["retained"]
    ]
    assert (p, 3, ("current",)) in retained
    assert third.headers["etag"]  # sanity: object still live


# ---- cursor validation --------------------------------------------------------


def test_tampered_cursor_is_400(client):
    prefix = _path("tamper")
    _put(client, f"{prefix}/a", b"a")
    _put(client, f"{prefix}/b", b"b")
    cursor = _get(client, prefix=prefix, limit=1).json()["next_cursor"]
    assert cursor  # a mid-walk cursor (not the end-of-walk null)
    payload, mac = cursor.rsplit(".", 1)
    flip = "A" if payload[3] != "A" else "B"
    tampered = payload[:3] + flip + payload[4:] + "." + mac
    assert _get(client, prefix=prefix, limit=1, cursor=tampered).status_code == 400
    assert _get(client, prefix=prefix, limit=1, cursor=mac + "." + payload) \
        .status_code == 400
    assert _get(client, prefix=prefix, limit=1, cursor="garbage").status_code == 400
    # The genuine cursor still works after the rejected forgeries.
    assert _get(client, prefix=prefix, limit=1, cursor=cursor).status_code == 200


def test_cursor_prefix_limit_mismatch_is_400(client):
    prefix = _path("mismatch")
    _put(client, f"{prefix}/a", b"a")
    body = _get(client, prefix=prefix, limit=1).json()
    cursor = body["next_cursor"] if body["next_cursor"] else None
    # Single-entry page: end cursor is null, so create more entries.
    _put(client, f"{prefix}/b", b"b")
    cursor = _get(client, prefix=prefix, limit=1).json()["next_cursor"]

    assert _get(client, prefix=prefix + "/x", limit=1, cursor=cursor).status_code == 400
    assert _get(client, prefix=prefix, limit=2, cursor=cursor).status_code == 400
    # Omitting prefix on the replay means prefix="" -> mismatch too.
    assert _get(client, limit=1, cursor=cursor).status_code == 400


def test_foreign_cursor_410_after_purge_and_unknown_snapshot(client):
    prefix = _path("unknown")
    _put(client, f"{prefix}/a", b"a")
    _put(client, f"{prefix}/b", b"b")
    cursor = _get(client, prefix=prefix, limit=1).json()["next_cursor"]
    assert cursor
    # Delete every snapshot row (simulating purge long after expiry).
    _purge_all()
    r = _get(client, prefix=prefix, limit=1, cursor=cursor)
    assert r.status_code == 410
    assert r.json()["detail"] == "snapshot_expired"
    assert r.json()["snapshot_id"]


def test_expired_snapshot_is_410(client):
    prefix = _path("expire")
    for i in range(3):
        _put(client, f"{prefix}/n-{i}", f"b{i}".encode())
    body = _get(client, prefix=prefix, limit=1).json()
    snapshot_id = body["snapshot_id"]
    cursor = body["next_cursor"]
    _force_expire(snapshot_id)
    r = _get(client, prefix=prefix, limit=1, cursor=cursor)
    assert r.status_code == 410
    assert r.json() == {
        "detail": "snapshot_expired",
        "snapshot_id": snapshot_id,
        "ttl_seconds": 600,
    }
    # The expired row is opportunistically purged; starting over works.
    fresh = _get(client, prefix=prefix, limit=1)
    assert fresh.status_code == 200
    assert fresh.json()["snapshot_id"] != snapshot_id


def test_snapshot_ttl_is_ten_minutes(client):
    body = _get(client, prefix=_path("ttl"), limit=1).json()
    from datetime import datetime

    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert (expires - created).total_seconds() == 600


# ---- concurrency: snapshot creation does not block writes --------------------


def test_index_walk_takes_no_object_locks(base_url):
    """DB-level check: a snapshot transaction must not FOR-lock objects,
    so a concurrent UPDATE of an object row is not blocked while the
    walk runs.  Deterministic, no timing-based assertion."""
    import app.db as db

    async def scenario():
        dsn = os.environ["DATABASE_URL"]
        setup = await asyncpg.connect(dsn)
        path = f"idx/lockcheck-{uuid.uuid4().hex}"
        await setup.execute(
            "INSERT INTO objects (path) VALUES ($1)"
            " ON CONFLICT (path) DO NOTHING",
            path,
        )
        await setup.close()

        walker = await asyncpg.connect(dsn)
        writer = await asyncpg.connect(dsn)
        # Fail fast (instead of hanging) if the walk ever holds a lock
        # that blocks a writer UPDATE.
        await writer.execute("SET lock_timeout = '2s'")
        try:
            # The index transaction: repeatable-read snapshot walk,
            # exactly like the endpoint, left deliberately uncommitted.
            tx = walker.transaction(isolation="repeatable_read")
            await tx.start()
            await db.create_index_snapshot(walker, uuid.uuid4().hex, "", 50, 600)
            # The writer UPDATEs an objects row the walker just scanned.
            # A FOR SHARE/FOR UPDATE lock would block this; instead the
            # update must commit immediately (verified without sleeps).
            updated = await writer.fetchval(
                "UPDATE objects SET revision_seq = revision_seq + 1"
                " WHERE path = $1 RETURNING revision_seq",
                path,
            )
            assert updated == 1
            await tx.rollback()
        finally:
            await walker.close()
            await writer.close()

    asyncio.run(scenario())


def test_concurrent_walk_and_writes(client):
    """HTTP-level smoke: walks while PUTs stream in always return
    internally consistent pages (each walk pins one snapshot id)."""
    import threading

    prefix = _path("concurrent")
    errors = []

    def writer():
        try:
            for i in range(40):
                _put(client, f"{prefix}/w-{i:03d}", f"w{i}".encode())
        except Exception as exc:  # pragma: no cover - test diagnostics
            errors.append(exc)

    def walker():
        try:
            pages, entries = _walk(client, prefix=prefix, limit=7)
            paths = [e["path"] for e in entries]
            assert paths == sorted(paths)
            assert len(paths) == len(set(paths))
        except Exception as exc:  # pragma: no cover - test diagnostics
            errors.append(exc)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=walker)
    t2.start()
    t1.start()
    t1.join()
    t2.join()
    assert not errors


# ---- concurrency: opportunistic purge race -----------------------------------


def test_concurrent_requests_purging_same_expired_snapshot(client):
    """The stale-snapshot cleanup race: many index requests reap the SAME
    expired rows at the same time.  Every request must answer its page
    (never 500 on a 40001 from the repeatable-read transaction) and the
    valid first/continuation pages must stay bound to their snapshots."""
    import threading

    live_prefix = _path("race-live")
    for i in range(3):
        _put(client, f"{live_prefix}/obj-{i}", f"b{i}".encode())
    first = _get(client, prefix=live_prefix, limit=2).json()
    live_cursor = first["next_cursor"]
    assert live_cursor

    rounds = 8
    for _ in range(rounds):
        # Several expired snapshots (headers + entries) that every request
        # below tries to delete simultaneously.
        _seed_expired_snapshots(5, with_entries=True)

        barrier = threading.Barrier(6)
        responses = []

        def request(params):
            barrier.wait()
            responses.append(_get(client, **params))

        threads = [
            threading.Thread(target=request, kwargs={"params": {"prefix": _path("race-fresh")}})
            for _ in range(5)
        ]
        threads.append(
            threading.Thread(
                target=request,
                kwargs={
                    "params": {"prefix": live_prefix, "limit": 2, "cursor": live_cursor}
                },
            )
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        statuses = [r.status_code for r in responses]
        assert statuses.count(200) == 6, [(s, r.text) for s, r in zip(statuses, responses)]
        # The replay of the still-valid snapshot returns its exact next
        # page (same snapshot id, offset 2, the one remaining entry).
        replays = [r for r in responses if "cursor=" in str(r.request.url)]
        assert len(replays) == 1
        body = replays[0].json()
        assert body["snapshot_id"] == first["snapshot_id"]
        assert body["offset"] == 2
        assert [e["path"] for e in body["entries"]] == [f"{live_prefix}/obj-2"]

    # The expired rows (and their cascaded entries) really are reaped.
    assert _count_expired_rows() == (0, 0)


def test_concurrent_replays_of_an_expired_snapshot_are_410(client):
    """The race with an expired CURSOR: all reapers collide on the same
    row, and every request must still answer 410 -- never 500."""
    import threading

    prefix = _path("race-expired-cursor")
    for i in range(2):
        _put(client, f"{prefix}/n-{i}", f"b{i}".encode())
    body = _get(client, prefix=prefix, limit=1).json()
    snapshot_id = body["snapshot_id"]
    cursor = body["next_cursor"]
    _force_expire(snapshot_id)

    barrier = threading.Barrier(8)
    responses = []

    def replay():
        barrier.wait()
        responses.append(_get(client, prefix=prefix, limit=1, cursor=cursor))

    threads = [threading.Thread(target=replay) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [r.status_code for r in responses] == [410] * 8, [
        (r.status_code, r.text) for r in responses
    ]
    assert all(r.json()["snapshot_id"] == snapshot_id for r in responses)
    # After the racing reaps, starting over yields a fresh snapshot.
    fresh = _get(client, prefix=prefix, limit=1)
    assert fresh.status_code == 200
    assert fresh.json()["snapshot_id"] != snapshot_id


def test_concurrent_purge_is_a_read_committed_unit(base_url):
    """DB-level check of the chosen transaction boundary: the purge is a
    single autocommit (read-committed) DELETE, so two connections reaping
    the same expired row serialise on its row lock -- the loser re-reads
    an empty set and deletes zero rows instead of raising 40001."""
    import app.db as db

    async def scenario():
        dsn = os.environ["DATABASE_URL"]
        sid = f"purge-race-{uuid.uuid4().hex}"
        seed = await asyncpg.connect(dsn)
        try:
            await seed.execute(
                "INSERT INTO object_index_snapshots"
                " (id, prefix, page_limit, total, expires_at)"
                " VALUES ($1, '', 50, 0, now() - interval '1 minute')",
                sid,
            )
            await seed.execute(
                "INSERT INTO object_index_entries"
                " (snapshot_id, ordinal, path, revision_no, etag, media_type, bytes)"
                " VALUES ($1, 0, 'p', 1, '\"e\"', 'text/plain', 1)",
                sid,
            )
        finally:
            await seed.close()

        a = await asyncpg.connect(dsn)
        b = await asyncpg.connect(dsn)
        try:
            # No surrounding repeatable-read transaction: each call is its
            # own read-committed statement, exactly like the endpoint.
            await asyncio.gather(
                db.purge_expired_index_snapshots(a),
                db.purge_expired_index_snapshots(b),
            )
        except asyncpg.SerializationError as exc:  # pragma: no cover - the bug
            raise AssertionError(
                "concurrent purge raised 40001"
            ) from exc
        finally:
            await a.close()
            await b.close()

        check = await asyncpg.connect(dsn)
        try:
            assert await check.fetchval(
                "SELECT count(*) FROM object_index_snapshots WHERE id = $1", sid
            ) == 0
            assert await check.fetchval(
                "SELECT count(*) FROM object_index_entries WHERE snapshot_id = $1", sid
            ) == 0
        finally:
            await check.close()

    asyncio.run(scenario())


async def _seed_expired_snapshots_async(n: int, with_entries: bool) -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        for i in range(n):
            sid = f"expired-{uuid.uuid4().hex}"
            await conn.execute(
                "INSERT INTO object_index_snapshots"
                " (id, prefix, page_limit, total, created_at, expires_at)"
                " VALUES ($1, 'race-expired', 50, 0,"
                " now() - interval '20 minutes', now() - interval '10 minutes')",
                sid,
            )
            if with_entries:
                await conn.execute(
                    "INSERT INTO object_index_entries"
                    " (snapshot_id, ordinal, path, revision_no, etag,"
                    " media_type, bytes)"
                    " VALUES ($1, 0, 'race-expired/gone', 1, '\"e\"',"
                    " 'text/plain', 1)",
                    sid,
                )
    finally:
        await conn.close()


def _seed_expired_snapshots(n: int, with_entries: bool = False) -> None:
    asyncio.run(_seed_expired_snapshots_async(n, with_entries))


async def _count_expired_rows_async() -> tuple[int, int]:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        snaps = await conn.fetchval(
            "SELECT count(*) FROM object_index_snapshots WHERE expires_at <= now()"
        )
        entries = await conn.fetchval(
            "SELECT count(*) FROM object_index_entries e"
            " JOIN object_index_snapshots s ON s.id = e.snapshot_id"
            " WHERE s.expires_at <= now()"
        )
        return snaps, entries
    finally:
        await conn.close()


def _count_expired_rows() -> tuple[int, int]:
    return asyncio.run(_count_expired_rows_async())


# ---- direct-DB helpers --------------------------------------------------------


async def _force_expire_async(snapshot_id: str) -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute(
            "UPDATE object_index_snapshots"
            " SET expires_at = now() - interval '1 second' WHERE id = $1",
            snapshot_id,
        )
    finally:
        await conn.close()


def _force_expire(snapshot_id: str) -> None:
    asyncio.run(_force_expire_async(snapshot_id))


async def _purge_all_async() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute("DELETE FROM object_index_snapshots")
    finally:
        await conn.close()


def _purge_all() -> None:
    asyncio.run(_purge_all_async())
