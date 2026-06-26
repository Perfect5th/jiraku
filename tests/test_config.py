from __future__ import annotations

from jiraya.adapters import ReadOnlyTicketSource
from jiraya.cli import load_env_file
from jiraya.composition import JiraConfig, JirayaConfig, build_system


def test_jira_config_accepts_jira_base_alias():
    cfg = JiraConfig.from_env({"JIRA_BASE": "https://x.atlassian.net",
                               "JIRA_EMAIL": "e", "JIRA_API_TOKEN": "t"})
    assert cfg.base_url == "https://x.atlassian.net"
    assert cfg.is_configured


def test_jira_base_url_takes_precedence_over_jira_base():
    cfg = JiraConfig.from_env({"JIRA_BASE_URL": "https://primary",
                               "JIRA_BASE": "https://secondary",
                               "JIRA_EMAIL": "e", "JIRA_API_TOKEN": "t"})
    assert cfg.base_url == "https://primary"


def test_is_configured_requires_all_three():
    assert not JiraConfig.from_env({"JIRA_BASE": "https://x"}).is_configured


def test_resolve_source_auto_detects():
    configured = JirayaConfig(jira=JiraConfig(base_url="https://x", email="e",
                                              api_token="t"))
    assert configured.resolve_source() == "jira"
    assert JirayaConfig().resolve_source() == "memory"
    assert JirayaConfig(source="memory",
                        jira=JiraConfig(base_url="https://x", email="e",
                                        api_token="t")).resolve_source() == "memory"


def test_build_system_wraps_jira_source_in_dry_run():
    cfg = JirayaConfig(
        source="jira",
        dry_run=True,
        jira=JiraConfig(base_url="https://x.atlassian.net", email="e", api_token="t"),
    )
    system = build_system(cfg)
    assert system.source_mode == "jira"
    assert system.dry_run is True
    assert isinstance(system.source, ReadOnlyTicketSource)


def test_build_system_memory_is_not_wrapped():
    system = build_system(JirayaConfig())
    assert system.source_mode == "memory"
    assert system.dry_run is False
    assert not isinstance(system.source, ReadOnlyTicketSource)


def test_load_env_file_parses_quoted_and_jql_values(tmp_path, monkeypatch):
    env = tmp_path / ".jira.env"
    env.write_text(
        "# a comment\n"
        "export JIRA_BASE=https://warthogs.atlassian.net\n"
        'JIRA_EMAIL="user@example.com"\n'
        "JIRA_API_TOKEN=abc123\n"
        'JIRA_JQL=assignee = currentUser() AND status in ("To Do", "Untriaged")\n'
    )
    for key in ("JIRA_BASE", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_JQL"):
        monkeypatch.delenv(key, raising=False)

    assert load_env_file(env) is True
    import os
    assert os.environ["JIRA_BASE"] == "https://warthogs.atlassian.net"
    assert os.environ["JIRA_EMAIL"] == "user@example.com"  # surrounding quotes stripped
    assert os.environ["JIRA_API_TOKEN"] == "abc123"
    # The JQL keeps its '=' signs and inner quotes intact.
    assert os.environ["JIRA_JQL"] == 'assignee = currentUser() AND status in ("To Do", "Untriaged")'


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch):
    env = tmp_path / ".jira.env"
    env.write_text("JIRA_EMAIL=fromfile\n")
    monkeypatch.setenv("JIRA_EMAIL", "fromenv")
    load_env_file(env)
    import os
    assert os.environ["JIRA_EMAIL"] == "fromenv"


def test_load_env_file_missing_returns_false(tmp_path):
    assert load_env_file(tmp_path / "nope.env") is False


def _args(**overrides):
    import argparse
    ns = argparse.Namespace(
        classifier="keyword", source="auto", interval=10.0,
        classifier_model=None, work_model=None, copilot_fallback=False,
        dry_run=False, apply=False,
        repo_registry=None, learned_rules=None, no_require_repo=False, provision=False,
        work=False, state_db=None, no_state=False, default_state=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def test_cli_defaults_to_dry_run_against_real_jira(monkeypatch):
    from jiraya.cli import _config_from_args
    monkeypatch.setenv("JIRA_BASE", "https://x.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "e")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")

    cfg = _config_from_args(_args())
    assert cfg.resolve_source() == "jira"
    assert cfg.dry_run is True  # no --apply → never writes by default


def test_cli_apply_enables_writes(monkeypatch):
    from jiraya.cli import _config_from_args
    monkeypatch.setenv("JIRA_BASE", "https://x.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "e")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")

    cfg = _config_from_args(_args(apply=True))
    assert cfg.dry_run is False


def test_cli_memory_source_is_never_dry_run(monkeypatch):
    from jiraya.cli import _config_from_args
    for key in ("JIRA_BASE", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    cfg = _config_from_args(_args())
    assert cfg.resolve_source() == "memory"
    assert cfg.dry_run is False
