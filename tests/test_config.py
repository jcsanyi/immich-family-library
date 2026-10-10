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
    assert cfg.observe_only is True


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r.pop("immich"), "url is required"),
        (lambda r: r["family"].pop("api_key"), "missing 'api_key'"),
        (lambda r: r["family"].pop("dropbox_album"), "dropbox_album"),
        (lambda r: r.pop("members"), "at least one"),
        (lambda r: r["members"].update({"b": {"email": "a@x", "api_key": "k"}}), "more than one"),
    ],
)
def test_parse_errors(mutate, msg):
    import copy

    raw = copy.deepcopy(GOOD)
    mutate(raw)
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw)
