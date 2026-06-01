from types import SimpleNamespace

import pytest
import yaml

from datadile import core
from datadile.core import init_command


def test_init_command_writes_default_config(monkeypatch, tmp_path):
    """The init command writes datadile.yaml in the current directory."""
    destination = tmp_path / "datadile.yaml"

    monkeypatch.chdir(tmp_path)

    init_command(SimpleNamespace(destination=None, force=False))

    assert destination.exists()
    assert yaml.safe_load(destination.read_text()) == yaml.safe_load(core.CONFIG_TEMPLATE)


def test_init_command_writes_custom_destination(tmp_path):
    """A destination argument controls where the config is written."""
    destination = tmp_path / "config" / "datadile.yaml"

    init_command(SimpleNamespace(destination=destination, force=False))

    assert destination.exists()


def test_init_command_refuses_to_overwrite_without_force(tmp_path):
    """Existing config files are left untouched unless --force is passed."""
    destination = tmp_path / "datadile.yaml"
    destination.write_text("existing: true\n")

    with pytest.raises(SystemExit) as exc_info:
        init_command(SimpleNamespace(destination=destination, force=False))

    assert exc_info.value.code == 1
    assert destination.read_text() == "existing: true\n"


def test_init_command_force_overwrites_existing_config(tmp_path):
    """--force replaces an existing config file with the starter template."""
    destination = tmp_path / "datadile.yaml"
    destination.write_text("existing: true\n")

    init_command(SimpleNamespace(destination=destination, force=True))

    assert yaml.safe_load(destination.read_text()) == yaml.safe_load(core.CONFIG_TEMPLATE)
