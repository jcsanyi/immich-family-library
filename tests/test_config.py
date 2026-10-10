from pathlib import Path

import pytest

from ifl.config import ConfigError, parse_config

GOOD = {
    "immich": {"url": "https://x.example/"},
    "family": {"email": "f@x", "api_key": "k", "dropbox_album": "Drop"},
    "members": {"a": {"email": "a@x", "api_key": "k"}},
}


def test_parse_good():
    cfg = parse_config(GOOD, config_dir=Path("/etc/ifl"))
    assert cfg.immich_url == "https://x.example"
    assert cfg.family.email == "f@x"
    assert cfg.dropbox_album == "Drop"
    assert cfg.members["a"].name == "a"
    assert cfg.ledger_path == Path("/etc/ifl/data/ledger.sqlite")
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
    assert raw["service"] == {"readonly": True, "ledger_path": "/etc/ifl/data/ledger.sqlite"}
    assert raw["limits"] == {"albums_per_pass": 5}
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
