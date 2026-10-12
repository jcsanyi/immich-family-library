"""The webhook receiver, against the fake Immich and a real aiohttp test server."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from fakes import ALICE, BOB, FAMILY, FakeAsset, make_accounts, patch_world

from ifl.config import parse_config
from ifl.daemon import Daemon
from ifl.immich import TOKEN_HEADER
from ifl.state import State

RAW = {
    "immich": {"url": "https://x.example"},
    "service": {"readonly": False},
    "family": {"email": "f@x", "api_key": "k", "dropbox_album": "Drop"},
    "members": {
        "alice": {"email": "alice@x", "api_key": "k"},
        "bob": {"email": "bob@x", "api_key": "k"},
    },
    "webhook": {"url": "http://ifl:8080/hook", "token": "t0k", "path": "/hook"},
}


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.observer", "ifl.move", "ifl.daemon")
    w.partners[FAMILY] = {ALICE, BOB}
    return w


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state.json")


@pytest.fixture
async def daemon(world, state):
    return Daemon(make_accounts(), parse_config(RAW), state)


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


async def test_a_batch_trashes_reuploads_and_ignores_the_rest(world, state, daemon):
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg")
    )
    world.metadata[copy.id] = {"ifl": {"origin_asset_id": str(uuid4())}}
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
    # Tallied and saved at once, so a restart keeps it.
    assert state.members[ALICE].reuploads == 1
    assert State.load(state.path).members[ALICE].reuploads == 1
    assert not daemon.in_flight


async def test_drain_coalesces_events(world, state, daemon, monkeypatch):
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
    world, state, daemon, monkeypatch
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


def local_daemon(state, **service) -> Daemon:
    """A daemon whose receiver binds an ephemeral port on loopback."""
    raw = {
        **RAW,
        "service": {**RAW["service"], **service},
        "webhook": {**RAW["webhook"], "listen": "127.0.0.1:0"},
    }
    return Daemon(make_accounts(), parse_config(raw), state)


async def test_run_does_a_pass_serves_and_stops_when_told(world, state, monkeypatch):
    daemon = local_daemon(state)
    passes = []

    async def one_pass(accounts, cfg, led, *, in_flight):
        passes.append(in_flight)
        daemon.stopping.set()
        return SimpleNamespace()

    monkeypatch.setattr("ifl.daemon.run_pass", one_pass)

    await asyncio.wait_for(daemon.run(), 5)

    assert passes == [daemon.in_flight]  # the pass shares the receiver's in-flight set
    left = [
        t.get_name() for t in asyncio.all_tasks() if t.get_name() in ("passes", "webhook-drain")
    ]
    assert left == []


async def test_each_pass_rereads_the_state_file(world, state, monkeypatch):
    from datetime import UTC, datetime

    daemon = local_daemon(state)
    mark = datetime(2026, 10, 1, tzinfo=UTC)
    # Edited on disk while the daemon runs: the next pass sees it, no restart needed.
    edited = State(state.path)
    edited.set_mark(ALICE, mark)
    seen = []

    async def one_pass(accounts, cfg, st, *, in_flight):
        seen.append(st.get_mark(ALICE))
        daemon.stopping.set()
        return SimpleNamespace()

    monkeypatch.setattr("ifl.daemon.run_pass", one_pass)
    await asyncio.wait_for(daemon.passes(), 5)
    assert seen == [mark]


async def test_a_failing_pass_is_logged_and_the_next_one_still_runs(
    world, state, monkeypatch, caplog
):
    daemon = local_daemon(state, interval_seconds=1)
    calls = []

    async def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("immich went away")
        daemon.stopping.set()
        return SimpleNamespace()

    monkeypatch.setattr("ifl.daemon.run_pass", flaky)

    with caplog.at_level("ERROR", logger="ifl.daemon"):
        await asyncio.wait_for(daemon.passes(), 5)

    assert len(calls) == 2
    assert "pass failed; next one in 1s" in caplog.text and "immich went away" in caplog.text


async def test_serve_binds_the_listen_address_and_the_receiver_answers(world, state):
    daemon = local_daemon(state)
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
    world, state, daemon, monkeypatch, caplog
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
