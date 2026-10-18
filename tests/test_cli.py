"""The radar console script."""

import pytest

from radar import registry
from radar.cli import main

COMMANDS = ("migrate", "seed", "sim", "bootstrap", "train", "eval", "dictionary", "web",
            "worker", "tick", "prune", "push")


def test_radar_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["--help"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    assert all(c in out for c in COMMANDS)


def test_dictionary_writes_the_data_dictionary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["dictionary"])
    assert (tmp_path / "docs" / "data-dictionary.md").read_text() == (
        registry.render_dictionary_md())
