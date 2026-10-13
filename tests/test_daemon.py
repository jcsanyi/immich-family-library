"""The webhook receiver, against the fake Immich and a real aiohttp test server."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from fakes import ALICE, BOB, FAMILY, TEMP_DIR, FakeAsset, make_accounts, patch_world

from ifl.config import parse_config
from ifl.daemon import Daemon
from ifl.immich import TOKEN_HEADER

RAW = {
    "immich": {"url": "https://x.example"},
    "service": {"readonly": False, "temp_dir": str(TEMP_DIR)},
    "family": {"email": "f@x", "api_key": "k", "dropbox_album": "Drop"},
    "members": {
        "alice": {"email": "alice@x", "api_key": "k"},
        "bob": {"email": "bob@x", "api_key": "k"},
    },
    "webhook": {"url": "http://ifl:8080/hook", "token": "t0k"},
}


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.observer", "ifl.move", "ifl.daemon")
    w.partners[FAMILY] = {ALICE, BOB}
    return w


@pytest.fixture
async def daemon(world):
    return Daemon(make_accounts(), parse_config(RAW))


async def client_for(daemon) -> TestClient:
    app = web.Application()
    app.router.add_post(daemon.cfg.webhook.path, daemon.handle)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def event(asset_id, owner_id):
    return {
        "type": "AssetV1",
        "trigger": "AssetCreate",
        "data": {"asset": {"id": str(asset_id), "ownerId": str(owner_id)}},
    }


async def test_receiver_checks_the_token_and_the_shape(daemon):
    client = await client_for(daemon)
    try:
        r = await client.post("/hook", json=event(uuid4(), ALICE))
        assert r.status == 401
        r = await client.post("/hook", json=event(uuid4(), ALICE), headers={TOKEN_HEADER: "wrong"})
        assert r.status == 401
        r = await client.post("/hook", json={"data": {}}, headers={TOKEN_HEADER: "t0k"})
        assert r.status == 400
        aid = uuid4()
        r = await client.post("/hook", json=event(aid, ALICE), headers={TOKEN_HEADER: "t0k"})
        assert r.status == 202 and daemon.events.get_nowait() == (aid, ALICE)
    finally:
        await client.close()


async def test_a_batch_trashes_reuploads_and_ignores_the_rest(world, daemon):
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg")
    )
    world.metadata[copy.id] = {
        "ifl": {"origin_user_id": str(ALICE), "origin_asset_id": str(uuid4())}
    }
    # Fresh uploads, as the webhook sees them: no exif yet, seconds old.
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    re = world.add_asset(
        FakeAsset(
            uuid4(),
            ALICE,
            checksum="beach",
            original_file_name="beach.jpg",
            exif_info=None,
            updated_at=now,
        )
    )
    new = world.add_asset(
        FakeAsset(
            uuid4(),
            ALICE,
            checksum="fresh",
            original_file_name="fresh.jpg",
            exif_info=None,
            updated_at=now,
        )
    )
    stranger = uuid4()

    await daemon.handle_batch([(re.id, None), (new.id, ALICE), (uuid4(), stranger), (re.id, ALICE)])

    assert re.is_trashed and not new.is_trashed
    assert daemon.tally == {ALICE: 1}
    assert not daemon.in_flight


async def test_drain_coalesces_events(world, daemon, monkeypatch):
    monkeypatch.setattr("ifl.daemon.BATCH_WAIT", 0.05)
    seen = []

    async def fake_batch(batch):
        seen.append(batch)

    daemon.handle_batch = fake_batch
    task = asyncio.create_task(daemon.drain())
    ids = [(uuid4(), ALICE) for _ in range(3)]
    for item in ids:
        await daemon.events.put(item)
    await asyncio.sleep(0.2)
    task.cancel()
    assert seen == [ids]


async def test_a_server_that_fails_to_start_takes_the_pass_task_down_with_it(
    world, daemon, monkeypatch
):
    started = asyncio.Event()

    async def pass_forever(*a, **kw):
        started.set()
        await asyncio.sleep(3600)

    async def cannot_bind(self):
        await started.wait()  # the pass task is already running when the bind fails
        raise OSError("address already in use")

    monkeypatch.setattr("ifl.daemon.run_pass", pass_forever)
    monkeypatch.setattr(Daemon, "serve", cannot_bind)

    with pytest.raises(OSError, match="address already in use"):
        await daemon.run()

    left = [
        t.get_name() for t in asyncio.all_tasks() if t.get_name() in ("passes", "webhook-drain")
    ]
    assert left == []


def local_daemon(**service) -> Daemon:
    """A daemon whose receiver binds an ephemeral port on loopback."""
    cfg = parse_config({**RAW, "service": {**RAW["service"], **service}})
    # Port 0 asks the OS for a free one. The config refuses it, so it goes in here.
    cfg = replace(cfg, webhook=replace(cfg.webhook, listen="127.0.0.1:0"))
    return Daemon(make_accounts(), cfg)


async def test_run_does_a_pass_serves_and_stops_when_told(world, monkeypatch):
    daemon = local_daemon()
    passes = []

    async def one_pass(accounts, cfg, *, in_flight, mark):
        passes.append((in_flight, mark))
        daemon.stopping.set()
        return SimpleNamespace(mark="next", moved=None)

    monkeypatch.setattr("ifl.daemon.run_pass", one_pass)

    await asyncio.wait_for(daemon.run(), 5)

    # The pass shares the receiver's in-flight set. A daemon starts with no mark.
    assert passes == [(daemon.in_flight, None)]
    assert daemon.mark == "next"  # what the next pass is given
    left = [
        t.get_name() for t in asyncio.all_tasks() if t.get_name() in ("passes", "webhook-drain")
    ]
    assert left == []


async def test_a_failing_pass_is_logged_and_the_next_one_still_runs(world, monkeypatch, caplog):
    daemon = local_daemon(interval_seconds=1)
    calls = []

    async def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("immich went away")
        daemon.stopping.set()
        return SimpleNamespace(mark=None, moved=None)

    monkeypatch.setattr("ifl.daemon.run_pass", flaky)

    with caplog.at_level("ERROR", logger="ifl.daemon"):
        await asyncio.wait_for(daemon.passes(), 5)

    assert len(calls) == 2
    assert "pass failed; next one in 1s" in caplog.text and "immich went away" in caplog.text


async def test_serve_binds_the_listen_address_and_the_receiver_answers(world):
    daemon = local_daemon()
    runner = await daemon.serve()
    try:
        host, port = runner.addresses[0][:2]
        aid = uuid4()
        async with aiohttp.ClientSession() as http:
            ok = await http.post(
                f"http://{host}:{port}/hook", json=event(aid, ALICE), headers={TOKEN_HEADER: "t0k"}
            )
            bad = await http.post(f"http://{host}:{port}/hook", json=event(aid, ALICE))
        assert (ok.status, bad.status) == (202, 401)
    finally:
        await runner.cleanup()
    assert daemon.events.get_nowait() == (aid, ALICE)


async def test_a_failing_batch_is_logged_and_the_drain_keeps_going(
    world, daemon, monkeypatch, caplog
):
    monkeypatch.setattr("ifl.daemon.BATCH_WAIT", 0.05)
    seen = []

    async def flaky_batch(batch):
        seen.append(batch)
        if len(seen) == 1:
            raise ConnectionError("immich went away")

    daemon.handle_batch = flaky_batch
    task = asyncio.create_task(daemon.drain())
    first, second = (uuid4(), ALICE), (uuid4(), BOB)
    await daemon.events.put(first)
    await asyncio.sleep(0.15)
    await daemon.events.put(second)
    await asyncio.sleep(0.15)
    task.cancel()
    assert seen == [[first], [second]]
    assert "webhook batch failed" in caplog.text and "immich went away" in caplog.text


def test_the_daily_report_names_members_with_trashed_reuploads(world, caplog):
    from datetime import UTC, datetime, timedelta

    from ifl.move import MoveManyResult, MoveResult

    def trashed(owner):
        return MoveResult(
            asset_id=uuid4(),
            owner_id=owner,
            family_asset_id=uuid4(),
            file_name="a.jpg",
            uploaded=False,
            edits=0,
            duplicate=True,
            reupload=True,
            trashed=True,
        )

    cfg = parse_config({**RAW, "warnings": {"report_at": "22:00"}})
    started = datetime(2026, 10, 11, 21, 30, tzinfo=UTC)
    daemon = Daemon(make_accounts(), cfg, now=started)
    daemon.count(MoveManyResult(done=[trashed(ALICE), trashed(ALICE), trashed(BOB)]))
    daemon.count(MoveManyResult(done=[trashed(ALICE)]))  # the webhook and the pass both count

    with caplog.at_level("WARNING", logger="ifl.daemon"):
        # Not due yet: nothing said, the tally kept.
        daemon.report(now=started + timedelta(minutes=20))
        assert not caplog.records and daemon.tally == {ALICE: 3, BOB: 1}

        # The first report after a start covers only the time since the start, and says so.
        at = started + timedelta(minutes=35)
        daemon.report(now=at)
        assert sorted(r.getMessage() for r in caplog.records) == [
            "alice re-uploaded 3 photos the family already has since 2026-10-11T21:30+00:00; trashed",
            "bob re-uploaded 1 photo the family already has since 2026-10-11T21:30+00:00; trashed",
        ]
        assert daemon.tally == {} and daemon.reported_at == at

        # Once a day: nothing more until 22:00 comes round again, and no line for nobody.
        caplog.clear()
        daemon.count(MoveManyResult(done=[trashed(BOB)]))
        daemon.report(now=at + timedelta(hours=23))
        assert not caplog.records
        daemon.report(now=at + timedelta(hours=24))
        assert [r.getMessage() for r in caplog.records] == [
            "bob re-uploaded 1 photo the family already has since 2026-10-11T22:05+00:00; trashed"
        ]


async def test_each_pass_counts_its_reuploads_and_a_failing_pass_still_reports(world, monkeypatch):
    daemon = local_daemon(interval_seconds=1)
    reports = []
    monkeypatch.setattr(daemon, "report", lambda: reports.append(dict(daemon.tally)))
    moved = SimpleNamespace(reuploads_trashed=lambda: {ALICE: 2})

    async def one_pass(*a, **kw):
        if not reports:
            return SimpleNamespace(mark=None, moved=moved)
        daemon.stopping.set()
        raise RuntimeError("immich went away")

    monkeypatch.setattr("ifl.daemon.run_pass", one_pass)

    await asyncio.wait_for(daemon.passes(), 5)

    assert reports == [{ALICE: 2}, {ALICE: 2}]


async def test_a_wrong_owner_hint_only_changes_who_is_asked_first(world, daemon):
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg")
    )
    world.metadata[copy.id] = {
        "ifl": {"origin_user_id": str(ALICE), "origin_asset_id": str(uuid4())}
    }
    re = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="beach", original_file_name="beach.jpg")
    )

    await daemon.handle_batch([(re.id, BOB)])  # the payload names bob; the asset is alice's

    assert re.is_trashed and daemon.tally == {ALICE: 1}


async def test_one_asset_that_cannot_be_read_does_not_cost_the_batch(
    world, daemon, monkeypatch, caplog
):
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg")
    )
    world.metadata[copy.id] = {
        "ifl": {"origin_user_id": str(ALICE), "origin_asset_id": str(uuid4())}
    }
    re = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="beach", original_file_name="beach.jpg")
    )
    lost = uuid4()
    real = world.get_asset_if_readable

    async def flaky(client, asset_id):
        if asset_id == lost:
            raise aiohttp.ClientConnectionError("immich went away")
        return await real(client, asset_id)

    monkeypatch.setattr("ifl.observer.get_asset_if_readable", flaky)

    with caplog.at_level("WARNING", logger="ifl.daemon"):
        await daemon.handle_batch([(lost, ALICE), (re.id, ALICE)])

    # Not "no member has it": the failure is said, and the rest of the batch goes on.
    assert (
        f"webhook: asset {lost} not read, left for the next pass: "
        "ClientConnectionError: immich went away"
    ) in caplog.text
    assert "Traceback" not in caplog.text  # Immich failing is no fault of the service
    assert re.is_trashed and daemon.tally == {ALICE: 1}


async def test_a_clean_stop_reports_the_tally_it_would_otherwise_lose(world, monkeypatch, caplog):
    daemon = local_daemon()
    daemon.tally = {ALICE: 2}

    async def one_pass(*a, **kw):
        daemon.stopping.set()
        return SimpleNamespace(mark=None, moved=None)

    monkeypatch.setattr("ifl.daemon.run_pass", one_pass)

    with caplog.at_level("WARNING", logger="ifl.daemon"):
        await asyncio.wait_for(daemon.run(), 5)

    assert "alice re-uploaded 2 photos the family already has since" in caplog.text
    assert daemon.tally == {}
