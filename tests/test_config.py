from datetime import time
from pathlib import Path

import pytest

from ifl.config import ConfigError, WebhookConfig, parse_config

GOOD = {
    "immich": {"url": "https://x.example/"},
    "service": {},
    "family": {"email": "f@x", "api_key": "k", "dropbox_album": "Drop"},
    "members": {"a": {"email": "a@x", "api_key": "k"}},
}


def test_parse_good():
    cfg = parse_config(GOOD, config_dir=Path("/etc/ifl"))
    assert cfg.immich.url == "https://x.example"
    assert cfg.family_account.email == "f@x"
    assert cfg.family.dropbox_album == "Drop"
    assert cfg.member_accounts["a"].name == "a"
    assert cfg.service.lock_path == Path("/etc/ifl/run.lock")
    assert cfg.service.temp_dir == Path("/tmp/immich-family-library")
    assert cfg.warnings.report_time == time(22, 0)
    assert cfg.service.readonly is True
    assert cfg.limits.albums_per_pass == 5


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r.pop("immich"), "immich: Field required"),
        (lambda r: r["family"].pop("api_key"), "family.api_key: Field required"),
        (lambda r: r["family"].update({"dropbox_album": ""}), "dropbox_album"),
        (lambda r: r.pop("members"), "at least one"),
        (lambda r: r["members"].update({"b": {"email": "a@x", "api_key": "k"}}), "more than one"),
        (lambda r: r.update({"limits": {"albums_per_pass": -1}}), "albums_per_pass"),
        (lambda r: r.update({"limits": {"albums_per_pass": "5"}}), "albums_per_pass"),
        (lambda r: r.update({"limits": {"assets_per_pass": -2}}), "assets_per_pass"),
        # Unknown names are errors, not silent defaults.
        (
            lambda r: r["service"].update({"poll_interval_seconds": 60}),
            "service.poll_interval_seconds: Extra inputs are not permitted",
        ),
        (
            lambda r: r["members"]["a"].update({"name": "Alice"}),
            "members.a.name: Extra inputs are not permitted",
        ),
        (
            lambda r: r.update({"serivce": {"readonly": False}}),
            "serivce: Extra inputs are not permitted",
        ),
        (lambda r: r.update({"service": 5}), "service: Input should be a valid dictionary"),
        # A value of the wrong type is an error, never a guess.
        (lambda r: r["immich"].update({"url": 5}), "immich.url: Input should be a valid string"),
        (
            lambda r: r["service"].update({"readonly": "false"}),
            "service.readonly: Input should be a valid boolean",
        ),
        (
            lambda r: r["service"].update({"readonly": 0}),
            "readonly: Input should be a valid boolean",
        ),
        (lambda r: r["service"].update({"lock_path": 5}), "lock_path: must be a non-empty path"),
        (lambda r: r["service"].update({"lock_path": ""}), "lock_path: must be a non-empty path"),
        (lambda r: r["service"].update({"temp_dir": 5}), "temp_dir: must be a non-empty path"),
        (lambda r: r["service"].update({"temp_dir": ""}), "temp_dir: must be a non-empty path"),
        (
            lambda r: r["family"].update({"email": 5}),
            "family.email: Input should be a valid string",
        ),
        (
            lambda r: r["family"].update({"dropbox_album": 5}),
            "dropbox_album: Input should be a valid string",
        ),
        (lambda r: r["members"]["a"].update({"api_key": 5}), "members.a.api_key: Input should be"),
        (
            lambda r: r.update({"limits": {"assets_per_pass": True}}),
            "assets_per_pass: Input should be a valid integer",
        ),
        (
            lambda r: r["service"].update({"settle_seconds": False}),
            "settle_seconds: Input should be a valid integer",
        ),
        (lambda r: r.update({"tags": {"moved": 5}}), "tags.moved: Input should be a valid string"),
    ],
)
def test_parse_errors(mutate, msg):
    import copy

    raw = copy.deepcopy(GOOD)
    mutate(raw)
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw)


def test_the_example_file_shows_every_default_but_the_lock_path():
    import re
    import tomllib

    from ifl.config import to_raw

    text = (Path(__file__).parents[1] / "config.example.toml").read_text()
    # The [webhook] section is commented out in the example; read it as written.
    text = re.sub(r"(?m)^# (\[webhook\]|(?:url|token|listen) = )", r"\1", text)
    example = tomllib.loads(text)
    # The one value that is not the default: the example is the container's
    # config, and the default lock is beside the config, which is mounted read-only.
    assert example["service"].pop("lock_path") == "/data/run.lock"
    required = {
        "immich": {"url": "https://x.example"},
        "webhook": {"url": "http://x", "token": "t"},
        "family": {"email": "f@x", "api_key": "k"},
        "members": {"a": {"email": "a@x", "api_key": "k"}},
    }
    defaults = to_raw(parse_config(required))
    defaults["service"].pop("lock_path")
    for section, settings in defaults.items():
        if section == "members":
            continue
        for key, default in settings.items():
            if key not in required.get(section, {}):
                assert example[section][key] == default, f"[{section}] {key}"


def test_dropbox_album_defaults():
    import copy

    raw = copy.deepcopy(GOOD)
    raw["family"].pop("dropbox_album")
    assert parse_config(raw).family.dropbox_album == "Family Dropbox"


def test_limits_section():
    raw = {**GOOD, "limits": {"albums_per_pass": 0}}
    assert parse_config(raw).limits.albums_per_pass == 0


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
        "lock_path": "/etc/ifl/run.lock",
        "temp_dir": "/tmp/immich-family-library",
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
    assert (cfg.service.settle_seconds, cfg.tags.moved, cfg.tags.from_prefix) == (
        60,
        "family",
        "from",
    )
    assert cfg.immich.admin_api_key is None
    assert "tags" not in to_raw(cfg, minimal=True)

    raw["service"] = {"settle_seconds": 5}
    raw["tags"] = {"moved": "ifl/moved", "from_prefix": "given-by"}
    raw["immich"]["admin_api_key"] = "sekrit-admin"
    cfg = parse_config(raw)
    assert (cfg.service.settle_seconds, cfg.tags.moved, cfg.tags.from_prefix) == (
        5,
        "ifl/moved",
        "given-by",
    )
    assert cfg.immich.admin_api_key == "sekrit-admin"

    minimal = to_raw(cfg, minimal=True)
    assert minimal["service"] == {"settle_seconds": 5}
    assert minimal["tags"] == {"moved": "ifl/moved", "from_prefix": "given-by"}
    assert minimal["immich"]["admin_api_key"] == "********dmin"
    assert "sekrit" not in str(minimal)


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r.update({"service": {"settle_seconds": -1}}), "settle_seconds"),
        (lambda r: r.update({"tags": {"moved": "/family"}}), "tags.moved: must be a tag name"),
        (lambda r: r.update({"tags": {"from_prefix": ""}}), "from_prefix"),
        (lambda r: r["immich"].update({"admin_api_key": 5}), "admin_api_key"),
        (lambda r: r.update({"warnings": {"report_at": "10pm"}}), "report_at"),
        (lambda r: r.update({"warnings": {"report_at": "22:00:00"}}), "report_at"),
        (lambda r: r.update({"warnings": {"report_at": 22}}), "report_at"),
        (lambda r: r.update({"warnings": {"report_at": "10+01"}}), "report_at"),
        (lambda r: r.update({"warnings": {"report_at": "T1200"}}), "report_at"),
        (lambda r: r.update({"warnings": {"report_at": "24:00"}}), "report_at"),
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
    assert cfg.warnings.report_time == time(6, 30)
    assert to_raw(cfg, minimal=True)["warnings"] == {"report_at": "06:30"}
    assert "warnings" not in to_raw(parse_config(GOOD), minimal=True)


def test_webhook_and_interval_settings():
    from ifl.config import to_raw

    cfg = parse_config(GOOD)
    assert cfg.webhook is None and cfg.service.interval_seconds == 600
    raw = {
        **GOOD,
        "service": {"interval_seconds": 30},
        "webhook": {"url": "http://ifl:8080/hook", "token": "sekrit"},
    }
    cfg = parse_config(raw)
    assert cfg.service.interval_seconds == 30
    assert cfg.webhook.url == "http://ifl:8080/hook" and cfg.webhook.listen == "0.0.0.0:8080"
    # The receiver serves the path Immich is told to call, and binds what `listen` says.
    assert cfg.webhook.path == "/hook" and cfg.webhook.bind == ("0.0.0.0", 8080)
    minimal = to_raw(cfg, minimal=True)
    assert minimal["service"] == {"interval_seconds": 30}
    assert minimal["webhook"] == {"url": "http://ifl:8080/hook", "token": "**krit"}
    assert "listen" in to_raw(cfg)["webhook"]
    for bad in (
        {"url": "http://x"},
        {"token": "t"},
        {"url": "http://x", "token": "t", "path": "/hook"},  # no longer a setting
        {"url": "http://x", "token": 5},
        {"url": "ifl:8080/hook", "token": "t"},
        {"url": "ftp://ifl/hook", "token": "t"},
        {"url": "http:///hook", "token": "t"},
        {"url": "http://[ifl/hook", "token": "t"},
    ):
        with pytest.raises(ConfigError, match="webhook"):
            parse_config({**GOOD, "webhook": bad})
    for listen in (
        "ifl",
        "0.0.0.0:",
        "0.0.0.0:http",
        "0.0.0.0:0",
        "0.0.0.0:65536",
        "::1:8080",
        "[:8080",
        "::]:8080",
        "[]:8080",
        "9000",
        8080,
    ):
        with pytest.raises(ConfigError, match="webhook.listen: (must be|Input should be)"):
            parse_config({**GOOD, "webhook": {"url": "http://x", "token": "t", "listen": listen}})
    for listen, bind in (
        ("127.0.0.1:9000", ("127.0.0.1", 9000)),
        (":9000", ("0.0.0.0", 9000)),
        ("[::]:9000", ("::", 9000)),
    ):
        raw = {**GOOD, "webhook": {"url": "http://x", "token": "t", "listen": listen}}
        cfg = parse_config(raw)
        assert cfg.webhook.bind == bind and cfg.webhook.path == "/"
    with pytest.raises(ConfigError, match="interval_seconds"):
        parse_config({**GOOD, "service": {"interval_seconds": 0}})
    # The config checks `listen`, and `bind` also refuses what it can't split.
    with pytest.raises(ValueError, match="not host:port"):
        _ = WebhookConfig.model_construct(url="http://x", token="t", listen="ifl").bind


def test_a_config_file_that_cannot_be_read_is_a_config_error(tmp_path):
    from ifl.config import load_config

    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "missing.toml")
    with pytest.raises(ConfigError, match="cannot read the config file"):
        load_config(tmp_path)  # a directory
    binary = tmp_path / "config.toml"
    binary.write_bytes(b"\xff\xfe not text")
    with pytest.raises(ConfigError, match="cannot read the config file"):
        load_config(binary)
