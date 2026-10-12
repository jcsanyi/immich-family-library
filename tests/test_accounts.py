"""The startup checks on each API key, against a stub client. No network."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fakes import T0
from immichpy.client.generated.models.api_key_response_dto import ApiKeyResponseDto
from immichpy.client.generated.models.server_version_response_dto import (
    ServerVersionResponseDto,
)
from immichpy.client.generated.models.user_admin_response_dto import UserAdminResponseDto
from immichpy.client.generated.models.user_avatar_color import UserAvatarColor
from immichpy.client.generated.models.user_status import UserStatus

from ifl import accounts as acc
from ifl.accounts import (
    ADMIN_PERMISSIONS,
    FAMILY_PERMISSIONS,
    MEMBER_PERMISSIONS,
    AccountError,
    check_server,
    open_account,
    open_accounts,
)
from ifl.config import AccountConfig, Config, parse_config

URL = "https://x.example"


def user(email: str, *, is_admin: bool = False) -> UserAdminResponseDto:
    return UserAdminResponseDto(
        avatar_color=UserAvatarColor.PRIMARY,
        cluster_group_id=uuid4(),
        created_at=T0,
        deleted_at=None,
        email=email,
        id=uuid4(),
        is_admin=is_admin,
        license=None,
        name=email.split("@")[0],
        oauth_id="",
        profile_changed_at=T0,
        profile_image_path="",
        quota_size_in_bytes=None,
        quota_usage_in_bytes=0,
        should_change_password=False,
        status=UserStatus.ACTIVE,
        storage_label=None,
        updated_at=T0,
    )


def key(permissions) -> ApiKeyResponseDto:
    return ApiKeyResponseDto(
        created_at=T0, id=uuid4(), name="ifl", permissions=sorted(permissions), updated_at=T0
    )


class StubClient:
    """What `AsyncClient(base_url=..., api_key=...)` hands back: the three calls
    the startup check makes, answered from `KEYS` by api key, plus `close`."""

    KEYS: dict[str, dict] = {}  # api_key -> {"user": ..., "key": ..., "version": ..., "raise": ...}
    made: list[StubClient] = []

    def __init__(self, base_url: str, api_key: str):
        self.base_url, self.api_key, self.closed = base_url, api_key, False
        spec = self.KEYS[api_key]
        self.users = SimpleNamespace(get_my_user=self._answer(spec, "user"))
        self.api_keys = SimpleNamespace(get_my_api_key=self._answer(spec, "key"))
        self.server = SimpleNamespace(
            get_server_version=self._answer(
                spec,
                "version",
                ServerVersionResponseDto(major=3, minor=3, patch=1, prerelease=None),
            )
        )
        StubClient.made.append(self)

    @staticmethod
    def _answer(spec, name, default=None):
        async def call():
            if spec.get("raise"):
                raise spec["raise"]
            return spec.get(name, default)

        return call

    async def close(self):
        self.closed = True


@pytest.fixture
def stub(monkeypatch):
    StubClient.KEYS = {}
    StubClient.made = []
    monkeypatch.setattr(acc, "AsyncClient", StubClient)
    return StubClient


async def test_a_good_key_opens_an_account(stub):
    stub.KEYS["k1"] = {"user": user("Alice@x"), "key": key(MEMBER_PERMISSIONS)}
    a = await open_account(
        URL, AccountConfig("alice", "k1", "alice@x"), required=MEMBER_PERMISSIONS
    )
    assert a.name == "alice" and a.email == "Alice@x" and a.user_id == stub.KEYS["k1"]["user"].id
    assert a.client.base_url == URL and a.client.api_key == "k1" and not a.client.closed
    assert str(a) == "alice <Alice@x>"


async def test_a_key_for_the_wrong_user_is_refused_and_closed(stub):
    stub.KEYS["k1"] = {"user": user("bob@x"), "key": key(MEMBER_PERMISSIONS)}
    with pytest.raises(AccountError, match="key belongs to bob@x, config says alice@x"):
        await open_account(
            URL, AccountConfig("alice", "k1", "alice@x"), required=MEMBER_PERMISSIONS
        )
    assert stub.made[0].closed
    # No email in the config means no check, which is how the admin key is opened.
    a = await open_account(URL, AccountConfig("admin", "k1"), required=set())
    assert a.email == "bob@x"


async def test_an_admin_key_must_belong_to_an_admin(stub):
    stub.KEYS["k1"] = {"user": user("root@x", is_admin=False), "key": key(ADMIN_PERMISSIONS)}
    with pytest.raises(AccountError, match="not an admin"):
        await open_account(
            URL, AccountConfig("admin", "k1"), required=ADMIN_PERMISSIONS, must_be_admin=True
        )
    stub.KEYS["k1"]["user"] = user("root@x", is_admin=True)
    a = await open_account(
        URL, AccountConfig("admin", "k1"), required=ADMIN_PERMISSIONS, must_be_admin=True
    )
    assert a.email == "root@x"


async def test_missing_permissions_are_named_and_all_covers_everything(stub):
    stub.KEYS["k1"] = {
        "user": user("alice@x"),
        "key": key(MEMBER_PERMISSIONS - {"asset.delete", "album.delete"}),
    }
    with pytest.raises(
        AccountError, match=r"missing permissions: \['album.delete', 'asset.delete'\]"
    ):
        await open_account(
            URL, AccountConfig("alice", "k1", "alice@x"), required=MEMBER_PERMISSIONS
        )
    assert stub.made[-1].closed
    stub.KEYS["k1"]["key"] = key({"all"})
    a = await open_account(
        URL, AccountConfig("alice", "k1", "alice@x"), required=FAMILY_PERMISSIONS
    )
    assert not a.client.closed


async def test_a_failing_call_becomes_an_account_error_and_closes_the_client(stub):
    stub.KEYS["k1"] = {"raise": ConnectionError("no route to host")}
    with pytest.raises(AccountError, match=r"\[alice\] key check failed: no route to host"):
        await open_account(URL, AccountConfig("alice", "k1", "alice@x"), required=set())
    assert stub.made[0].closed


async def test_the_server_must_be_at_least_the_minimum_version(stub):
    stub.KEYS["k1"] = {
        "version": ServerVersionResponseDto(major=3, minor=2, patch=9, prerelease=None)
    }
    with pytest.raises(AccountError, match=r"server is 3.2.9, need 3.3.0\+"):
        await check_server(stub(URL, "k1"))
    stub.KEYS["k1"] = {
        "version": ServerVersionResponseDto(major=3, minor=3, patch=0, prerelease=None)
    }
    assert await check_server(stub(URL, "k1")) == "3.3.0"


async def test_open_accounts_opens_every_key_and_closes_them_all_if_one_fails(stub):
    cfg = parse_config(
        {
            "immich": {"url": URL, "admin_api_key": "ka"},
            "family": {"email": "f@x", "api_key": "kf"},
            "members": {
                "alice": {"email": "a@x", "api_key": "k1"},
                "bob": {"email": "b@x", "api_key": "k2"},
            },
        }
    )
    stub.KEYS = {
        "kf": {"user": user("f@x"), "key": key(FAMILY_PERMISSIONS)},
        "k1": {"user": user("a@x"), "key": key(MEMBER_PERMISSIONS)},
        "k2": {"user": user("b@x"), "key": key(MEMBER_PERMISSIONS)},
        "ka": {"user": user("root@x", is_admin=True), "key": key(ADMIN_PERMISSIONS)},
    }
    opened = await open_accounts(cfg)
    assert set(opened.members) == {"alice", "bob"} and opened.admin.email == "root@x"
    assert opened.family.email == "f@x"
    assert set(opened.by_user_id()) == {
        opened.family.user_id,
        *(m.user_id for m in opened.members.values()),
    }
    await opened.close()
    assert all(c.closed for c in stub.made)

    # bob's key is for the wrong user: everything already opened is closed again.
    stub.made = []
    stub.KEYS["k2"]["user"] = user("carol@x")
    with pytest.raises(AccountError, match=r"\[bob\]"):
        await open_accounts(cfg)
    assert [c.api_key for c in stub.made] == ["kf", "k1", "k2"] and all(c.closed for c in stub.made)


def two_member_config(bob_email: str, admin_key: str | None = None) -> Config:
    return parse_config(
        {
            "immich": {"url": URL, **({"admin_api_key": admin_key} if admin_key else {})},
            "family": {"email": "f@x", "api_key": "kf"},
            "members": {
                "alice": {"email": "a@x", "api_key": "k1"},
                "bob": {"email": bob_email, "api_key": "k2"},
            },
        }
    )


@pytest.mark.parametrize(
    ("bob_email", "same_as", "named"), [("A@x", "k1", "alice"), ("F@x", "kf", "family")]
)
async def test_one_immich_user_cannot_be_two_participants(stub, bob_email, same_as, named):
    # The emails differ only in case, so the config's own check lets them through.
    stub.KEYS = {
        "kf": {"user": user("f@x"), "key": key(FAMILY_PERMISSIONS | MEMBER_PERMISSIONS)},
        "k1": {"user": user("a@x"), "key": key(MEMBER_PERMISSIONS)},
    }
    stub.KEYS["k2"] = stub.KEYS[same_as]
    with pytest.raises(AccountError, match=rf"\[{named}\] and \[bob\] are the same Immich user"):
        await open_accounts(two_member_config(bob_email))
    assert all(c.closed for c in stub.made)


@pytest.mark.parametrize("shared_with", ["kf", "k1"])
async def test_the_admin_key_may_belong_to_the_family_or_a_member(stub, shared_with):
    everything = FAMILY_PERMISSIONS | MEMBER_PERMISSIONS | ADMIN_PERMISSIONS
    stub.KEYS = {
        "kf": {"user": user("f@x", is_admin=True), "key": key(everything)},
        "k1": {"user": user("a@x", is_admin=True), "key": key(everything)},
        "k2": {"user": user("b@x"), "key": key(MEMBER_PERMISSIONS)},
    }
    stub.KEYS["ka"] = stub.KEYS[shared_with]  # a second key for the same user
    opened = await open_accounts(two_member_config("b@x", admin_key="ka"))
    owner = opened.family if shared_with == "kf" else opened.members["alice"]
    assert opened.admin.user_id == owner.user_id and len(opened.by_user_id()) == 3
    await opened.close()
