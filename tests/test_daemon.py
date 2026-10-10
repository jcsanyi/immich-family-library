"""The webhook receiver, against the fake Immich and a real aiohttp test server."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from fakes import ALICE, BOB, FAMILY, FakeAsset, make_accounts, patch_world

from ifl.config import parse_config
from ifl.daemon import Daemon
from ifl.immich import TOKEN_HEADER
from ifl.ledger import Ledger

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
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    yield led
    led.close()


@pytest.fixture
async def daemon(world, ledger):
    return Daemon(make_accounts(), parse_config(RAW), ledger)


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


async def test_a_batch_trashes_reuploads_and_ignores_the_rest(world, ledger, daemon):
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
    assert ledger.counts()["reuploads"] == 1 and ledger.get("beach")["reupload_count"] == 1
    assert not daemon.in_flight


async def test_drain_coalesces_events(world, ledger, daemon, monkeypatch):
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
