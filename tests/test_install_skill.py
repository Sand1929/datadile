from types import SimpleNamespace

import pytest

from datadile import core
from datadile.core import install_skill_command


def test_install_skill_confirms_resolved_destination(monkeypatch, tmp_path):
    """Skill install asks with the exact resolved target path before writing."""
    prompts = []
    destination = tmp_path / core.DEFAULT_SKILL_INSTALL_PATH

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(core.Confirm, "ask", lambda prompt, default: prompts.append((prompt, default)) or True)

    install_skill_command(SimpleNamespace(destination=None, agent=core.DEFAULT_SKILL_AGENT, global_install=False, yes=False))

    assert prompts == [(f"Install Datadile skill to {destination}?", False)]
    assert destination.exists()


def test_install_skill_cancel_does_not_create_destination(monkeypatch, tmp_path):
    """Declining the confirmation leaves the destination untouched."""
    destination = tmp_path / "SKILL.md"

    monkeypatch.setattr(core.Confirm, "ask", lambda prompt, default: False)

    install_skill_command(
        SimpleNamespace(destination=destination, agent=core.DEFAULT_SKILL_AGENT, global_install=False, yes=False)
    )

    assert not destination.exists()


def test_install_skill_yes_skips_confirmation(monkeypatch, tmp_path):
    """Non-interactive installs can bypass the confirmation prompt."""
    destination = tmp_path / "SKILL.md"

    def fail_if_prompted(prompt, default):
        raise AssertionError("confirmation prompt should be skipped")

    monkeypatch.setattr(core.Confirm, "ask", fail_if_prompted)

    install_skill_command(SimpleNamespace(destination=destination, agent=core.DEFAULT_SKILL_AGENT, global_install=False, yes=True))

    assert destination.exists()


def test_install_skill_agent_selects_default_destination(monkeypatch, tmp_path):
    """Agent selection controls the project-local default install path."""
    destination = tmp_path / ".claude" / "skills" / "datadile" / "SKILL.md"

    monkeypatch.chdir(tmp_path)

    install_skill_command(SimpleNamespace(destination=None, agent="claude", global_install=False, yes=True))

    assert destination.exists()


def test_install_skill_explicit_destination_overrides_agent(monkeypatch, tmp_path):
    """A destination argument wins over the selected agent default."""
    destination = tmp_path / "custom" / "SKILL.md"

    monkeypatch.chdir(tmp_path)

    install_skill_command(SimpleNamespace(destination=destination, agent="cursor", global_install=False, yes=True))

    assert destination.exists()
    assert not (tmp_path / ".agents" / "skills" / "datadile" / "SKILL.md").exists()


def test_install_skill_global_selects_user_level_destination(monkeypatch, tmp_path):
    """Global installs use the selected agent's user-level skills directory."""
    destination = tmp_path / ".claude" / "skills" / "datadile" / "SKILL.md"

    monkeypatch.setenv("HOME", str(tmp_path))

    install_skill_command(SimpleNamespace(destination=None, agent="claude", global_install=True, yes=True))

    assert destination.exists()


def test_install_skill_explicit_destination_overrides_global(monkeypatch, tmp_path):
    """A destination argument wins over the global default."""
    destination = tmp_path / "custom" / "SKILL.md"

    monkeypatch.setenv("HOME", str(tmp_path))

    install_skill_command(SimpleNamespace(destination=destination, agent="opencode", global_install=True, yes=True))

    assert destination.exists()
    assert not (tmp_path / ".agents" / "skills" / "datadile" / "SKILL.md").exists()


@pytest.mark.parametrize(
    "agent, relative_destination",
    [
        ("github-copilot", ".agents/skills/datadile/SKILL.md"),
        ("copilot", ".agents/skills/datadile/SKILL.md"),
        ("openai-codex", ".agents/skills/datadile/SKILL.md"),
        ("codex", ".agents/skills/datadile/SKILL.md"),
        ("vscode", ".agents/skills/datadile/SKILL.md"),
        ("vs-code", ".agents/skills/datadile/SKILL.md"),
        ("visual-studio-code", ".agents/skills/datadile/SKILL.md"),
        ("snowflake-cortex", ".cortex/skills/datadile/SKILL.md"),
        ("cortex", ".cortex/skills/datadile/SKILL.md"),
    ],
)
def test_install_skill_additional_agent_defaults(monkeypatch, tmp_path, agent, relative_destination):
    """Additional agent names and aliases resolve to their project-local defaults."""
    destination = tmp_path / relative_destination

    monkeypatch.chdir(tmp_path)

    install_skill_command(SimpleNamespace(destination=None, agent=agent, global_install=False, yes=True))

    assert destination.exists()


def test_install_skill_global_uses_agents_directory_for_non_claude_snowflake(monkeypatch, tmp_path):
    """Global installs use ~/.agents for agents that support the shared location."""
    destination = tmp_path / ".agents" / "skills" / "datadile" / "SKILL.md"

    monkeypatch.setenv("HOME", str(tmp_path))

    install_skill_command(SimpleNamespace(destination=None, agent="vscode", global_install=True, yes=True))

    assert destination.exists()
