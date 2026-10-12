from datetime import time
from pathlib import Path

import pytest

from ifl.config import ConfigError, parse_config

GOOD = {
    "immich": {"url": "https://x.example/"},
    "service": {},
    "family": {"email": "f@x", "api_key": "k", "dropbox_album": "Drop"},
    "members": {"a": {"email": "a@x", "api_key": "k"}},
}


def test_parse_good():
    cfg = parse_config(GOOD, config_dir=Path("/etc/ifl"))
    assert cfg.immich_url == "https://x.example"
    assert cfg.family.email == "f@x"
    assert cfg.dropbox_album == "Drop"
    assert cfg.members["a"].name == "a"
    assert cfg.state_path == Path("/etc/ifl/data/state.json")
    assert cfg.report_at == time(22, 0)
    assert cfg.readonly is True
    assert cfg.albums_per_pass == 5


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r.pop("immich"), "url is required"),
        (lambda r: r["family"].pop("api_key"), "missing 'api_key'"),
        (lambda r: r["family"].update({"dropbox_album": ""}), "dropbox_album"),
        (lambda r: r.pop("members"), "at least one"),
        (lambda r: r["members"].update({"b": {"email": "a@x", "api_key": "k"}}), "more than one"),
        (lambda r: r.update({"limits": {"albums_per_pass": -1}}), "albums_per_pass"),
        (lambda r: r.update({"limits": {"albums_per_pass": "5"}}), "albums_per_pass"),
        (lambda r: r.update({"limits": {"assets_per_pass": -2}}), "assets_per_pass"),
        # Unknown names are errors, not silent defaults.
        (
            lambda r: r["service"].update({"poll_interval_seconds": 60}),
            r"\[service\] has an unknown key 'poll_interval_seconds'",
        ),
        (
            lambda r: r["members"]["a"].update({"name": "Alice"}),
            r"\[members.a\] has an unknown key 'name'",
        ),
        (lambda r: r.update({"serivce": {"readonly": False}}), r"unknown section \[serivce\]"),
        (lambda r: r.update({"service": 5}), r"\[service\] must be a table"),
    ],
)
def test_parse_errors(mutate, msg):
    import copy

    raw = copy.deepcopy(GOOD)
    mutate(raw)
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw)


def test_dropbox_album_defaults():
    import copy

    raw = copy.deepcopy(GOOD)
    raw["family"].pop("dropbox_album")
    assert parse_config(raw).dropbox_album == "Family Dropbox"


def test_limits_section():
    raw = {**GOOD, "limits": {"albums_per_pass": 0}}
    assert parse_config(raw).albums_per_pass == 0


def test_to_raw_redacts_and_resolves():
    import copy

    from ifl.config import to_raw

    good = copy.deepcopy(GOOD)
    good["family"]["api_key"] = "sekrit-family"
    good["members"]["a"]["api_key"] = "sekrit-member"
    raw = to_raw(parse_config(good, config_dir=Path("/etc/ifl")))
    assert raw["family"]["api_key"] == "*********mily"
    assert raw["members"]["a"]["api_key"] == "*********mber"
    assert raw["service"] == {
        "readonly": True,
        "state_path": "/etc/ifl/data/state.json",
        "settle_seconds": 60,
        "interval_seconds": 600,
    }
    assert raw["limits"] == {"albums_per_pass": 5, "assets_per_pass": 20}
    assert "sekrit" not in str(raw)


def test_to_raw_minimal_keeps_only_non_defaults():
    from ifl.config import to_raw

    cfg = parse_config(GOOD, config_dir=Path("/etc/ifl"))
    raw = to_raw(cfg, minimal=True)
    assert set(raw) == {"immich", "family", "members"}
    assert "dropbox_album" in raw["family"]  # "Drop" isn't the default

    cfg = parse_config({**GOOD, "family": {**GOOD["family"], "dropbox_album": "Family Dropbox"}})
    assert "dropbox_album" not in to_raw(cfg, minimal=True)["family"]

    changed = {**GOOD, "service": {"readonly": False}, "limits": {"albums_per_pass": 5}}
    raw = to_raw(parse_config(changed, config_dir=Path("/etc/ifl")), minimal=True)
    assert raw["service"] == {"readonly": False}
    assert "limits" not in raw
    changed["limits"] = {"assets_per_pass": 0}
    assert to_raw(parse_config(changed), minimal=True)["limits"] == {"assets_per_pass": 0}


def test_redact_short_secrets_entirely():
    from ifl.config import redact

    assert redact("abcd") == "****"
    assert redact("ab") == "**"
    assert redact("") == ""
    assert redact("abcdefgh") == "****efgh"


def test_show_config_round_trips(tmp_path, capsys):
    import tomllib

    from ifl.cli import main

    src = tmp_path / "config.toml"
    src.write_text(
        '[immich]\nurl = "https://x.example"\n[service]\nreadonly = false\n'
        '[family]\nemail = "f@x"\napi_key = "secret"\ndropbox_album = "Drop"\n'
        '[members.a]\nemail = "a@x"\napi_key = "secret2"\n'
    )
    assert main(["-c", str(src), "show-config", "--minimal"]) == 0
    out = capsys.readouterr().out
    assert "secret" not in out
    again = tomllib.loads(out)
    assert again["service"] == {"readonly": False}
    assert again["members"]["a"]["email"] == "a@x"


def test_stage4_settings_and_admin_key():
    import copy

    from ifl.config import to_raw

    raw = copy.deepcopy(GOOD)
    cfg = parse_config(raw)
    assert (cfg.settle_seconds, cfg.moved_tag, cfg.from_prefix) == (60, "family", "from")
    assert cfg.admin_api_key is None
    assert "tags" not in to_raw(cfg, minimal=True)

    raw["service"] = {"settle_seconds": 5}
    raw["tags"] = {"moved": "ifl/moved", "from_prefix": "given-by"}
    raw["immich"]["admin_api_key"] = "sekrit-admin"
    cfg = parse_config(raw)
    assert (cfg.settle_seconds, cfg.moved_tag, cfg.from_prefix) == (5, "ifl/moved", "given-by")
    assert cfg.admin_api_key == "sekrit-admin"

    minimal = to_raw(cfg, minimal=True)
    assert minimal["service"] == {"settle_seconds": 5}
    assert minimal["tags"] == {"moved": "ifl/moved", "from_prefix": "given-by"}
    assert minimal["immich"]["admin_api_key"] == "********dmin"
    assert "sekrit" not in str(minimal)


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r.update({"service": {"settle_seconds": -1}}), "settle_seconds"),
        (lambda r: r.update({"tags": {"moved": "/family"}}), "tags. moved"),
        (lambda r: r.update({"tags": {"from_prefix": ""}}), "from_prefix"),
        (lambda r: r["immich"].update({"admin_api_key": 5}), "admin_api_key"),
        (lambda r: r.update({"warnings": {"report_at": "10pm"}}), "report_at"),
        (lambda r: r.update({"warnings": {"report_at": "22:00:00"}}), "report_at"),
        (lambda r: r.update({"warnings": {"report_at": 22}}), "report_at"),
    ],
)
def test_stage4_setting_errors(mutate, msg):
    import copy

    raw = copy.deepcopy(GOOD)
    mutate(raw)
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw)


def test_report_at_is_a_local_time_of_day():
    from ifl.config import to_raw

    cfg = parse_config({**GOOD, "warnings": {"report_at": "06:30"}})
    assert cfg.report_at == time(6, 30)
    assert to_raw(cfg, minimal=True)["warnings"] == {"report_at": "06:30"}
    assert "warnings" not in to_raw(parse_config(GOOD), minimal=True)


def test_webhook_and_interval_settings():
    from ifl.config import to_raw

    cfg = parse_config(GOOD)
    assert cfg.webhook is None and cfg.interval_seconds == 600
    raw = {
        **GOOD,
        "service": {"interval_seconds": 30},
        "webhook": {"url": "http://ifl:8080/hook", "token": "sekrit"},
    }
    cfg = parse_config(raw)
    assert cfg.interval_seconds == 30
    assert cfg.webhook.url == "http://ifl:8080/hook" and cfg.webhook.listen == "0.0.0.0:8080"
    assert cfg.webhook.path == "/webhook/asset-created"
    minimal = to_raw(cfg, minimal=True)
    assert minimal["service"] == {"interval_seconds": 30}
    assert minimal["webhook"] == {"url": "http://ifl:8080/hook", "token": "**krit"}
    assert "listen" in to_raw(cfg)["webhook"]
    for bad in (
        {"url": "http://x"},
        {"token": "t"},
        {"url": "http://x", "token": "t", "path": "hook"},
    ):
        with pytest.raises(ConfigError, match="webhook"):
            parse_config({**GOOD, "webhook": bad})
    with pytest.raises(ConfigError, match="interval_seconds"):
        parse_config({**GOOD, "service": {"interval_seconds": 0}})
