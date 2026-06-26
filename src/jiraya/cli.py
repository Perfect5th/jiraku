"""Command-line entry point for jiraya."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .composition import JiraConfig, JirayaConfig, build_system
from .domain import (
    ActivityLevel,
    ActivityLogged,
    DomainEvent,
    PollCycleCompleted,
)

_LEVEL_COLOR = {
    ActivityLevel.INFO: "\033[90m",
    ActivityLevel.SUCCESS: "\033[32m",
    ActivityLevel.WARNING: "\033[33m",
    ActivityLevel.ERROR: "\033[31m",
}
_RESET = "\033[0m"

_DEFAULT_ENV_FILE = ".jira.env"


def load_env_file(path: str | os.PathLike[str], *, override: bool = False) -> bool:
    """Load ``KEY=value`` pairs from a dotenv-style file into ``os.environ``.

    Existing environment variables win unless ``override`` is set. Returns
    whether the file existed. Values are taken verbatim after the first ``=``
    (so a JQL containing ``=`` and quotes survives intact); fully-wrapping
    quotes are stripped.
    """
    p = Path(path)
    if not p.is_file():
        return False
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    return True


def _default_state_db() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "jiraya", "state.db")


def _resolve_state_db(args: argparse.Namespace) -> str | None:
    if getattr(args, "no_state", False):
        return None
    if getattr(args, "state_db", None):
        return args.state_db
    # Persist by default only where it matters across restarts (the dashboard).
    return _default_state_db() if getattr(args, "default_state", False) else None


def _config_from_args(args: argparse.Namespace) -> JirayaConfig:
    jira = JiraConfig.from_env()
    # Safety: writing to a real Jira board requires an explicit --apply. Without
    # it (and unless using the in-memory demo) we default to a no-write dry run
    # so a bare invocation never mutates a production board by surprise.
    probe = JirayaConfig(source=args.source, jira=jira)
    targets_real_jira = probe.resolve_source() == "jira"
    dry_run = args.dry_run or (targets_real_jira and not args.apply)
    return JirayaConfig(
        classifier=args.classifier,
        source=args.source,
        interval_seconds=args.interval,
        classifier_model=args.classifier_model,
        work_model=args.work_model,
        copilot_fallback_to_keyword=args.copilot_fallback,
        dry_run=dry_run,
        repo_registry_path=args.repo_registry,
        learned_rules_path=args.learned_rules,
        require_repo=not args.no_require_repo,
        provision=args.provision,
        work=args.work,
        state_db_path=_resolve_state_db(args),
        jira=jira,
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--classifier", choices=["keyword", "copilot"],
                        default="keyword", help="Intent classifier to use.")
    parser.add_argument("--source", choices=["auto", "memory", "jira"], default="auto",
                        help="Ticket source. 'auto' uses real Jira when "
                             "credentials are configured, else the in-memory demo.")
    parser.add_argument("--interval", type=float, default=1800.0,
                        help="Seconds between poll cycles.")
    parser.add_argument("--classifier-model", default=None,
                        help="Model for the Copilot CLI classifier.")
    parser.add_argument("--work-model", default=None,
                        help="Model for the work agent. If unset, each ticket uses "
                             "the model recommended by its classification.")
    parser.add_argument("--copilot-fallback", action="store_true",
                        help="Fall back to the keyword classifier if Copilot fails.")
    parser.add_argument("--repo-registry", default=None,
                        help="Path to a YAML repo registry (project->repo catalog).")
    parser.add_argument("--learned-rules", default=None,
                        help="Path to persist repo-resolution rules learned from "
                             "inbox corrections.")
    parser.add_argument("--state-db", default=None,
                        help="SQLite file for persisting actioned tickets + the "
                             "inbox across restarts.")
    parser.add_argument("--no-state", action="store_true",
                        help="Disable state persistence (in-memory only).")
    parser.add_argument("--no-require-repo", action="store_true",
                        help="Do not escalate tickets whose repository cannot be "
                             "resolved (skip the repo confidence gate).")
    parser.add_argument("--provision", action="store_true",
                        help="git clone resolved repos into local workspaces so the "
                             "worker agent can start (off by default; never in dry-run).")
    parser.add_argument("--work", action="store_true",
                        help="Run the work agent (Copilot CLI) in the cloned "
                             "workspace to implement the ticket and open a PR "
                             "(implies --provision; never in dry-run).")
    writes = parser.add_mutually_exclusive_group()
    writes.add_argument("--dry-run", action="store_true",
                        help="Read real Jira items but never write back. This is "
                             "the default for a real Jira source.")
    writes.add_argument("--apply", action="store_true",
                        help="Actually perform Jira status transitions "
                             "(required to write to a real Jira board).")
    parser.add_argument("--env-file", default=None,
                        help=f"Path to a dotenv file with Jira credentials "
                             f"(default: ./{_DEFAULT_ENV_FILE} if present).")


def _banner(system) -> str:
    mode = system.source_mode
    if mode == "jira":
        if system.dry_run:
            return "Source: real Jira (dry-run: no writes — pass --apply to transition tickets)"
        return "Source: real Jira (apply: status transitions WILL be written)"
    return "Source: in-memory demo (no Jira credentials configured)"


def _make_console_printer(color: bool):
    def printer(event: DomainEvent) -> None:
        if isinstance(event, ActivityLogged) and event.activity is not None:
            a = event.activity
            if color:
                c = _LEVEL_COLOR[a.level]
                print(f"{c}[{a.level}]{_RESET} {a.agent} · {a.ticket_key}: {a.message}")
            else:
                print(f"[{a.level}] {a.agent} · {a.ticket_key}: {a.message}")
        elif isinstance(event, PollCycleCompleted):
            print(f"--- poll cycle #{event.cycle} complete: "
                  f"{event.processed} ticket(s) processed ---")
    return printer


def _print_summary(system) -> None:
    m = system.service.metrics
    print()
    print("Triage summary")
    print(f"  source       : {_banner(system)}")
    print(f"  processed    : {m.processed}")
    print(f"  transitioned : {m.transitioned}")
    print(f"  escalated    : {m.escalated}")
    print(f"  automation   : {m.automation_rate * 100:.0f}%")
    by_cat = ", ".join(f"{k}={v}" for k, v in m.by_category.items()) or "none"
    print(f"  by category  : {by_cat}")
    open_inbox = system.inbox.open_entries()
    if open_inbox:
        print(f"\nInbox — {len(open_inbox)} item(s) need human review:")
        for e in open_inbox:
            print(f"  • {e.ticket_key} [{e.category}] {e.reason}")


def _bootstrap_env(args: argparse.Namespace) -> None:
    """Load the credentials dotenv before any config is read from the env."""
    if args.env_file:
        if not load_env_file(args.env_file):
            print(f"Warning: env file not found: {args.env_file}", file=sys.stderr)
    else:
        load_env_file(_DEFAULT_ENV_FILE)


def cmd_run(args: argparse.Namespace) -> int:
    _bootstrap_env(args)
    config = _config_from_args(args)
    system = build_system(config)
    color = sys.stdout.isatty() and not args.no_color
    print(_banner(system))
    system.bus.subscribe(_make_console_printer(color))

    async def _drive() -> None:
        if args.once:
            await system.poller.run_once()
        else:
            await system.poller.run_forever(max_cycles=args.cycles)

    try:
        asyncio.run(_drive())
    except KeyboardInterrupt:  # pragma: no cover
        print("\nInterrupted.")
        return 130
    _print_summary(system)
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    _bootstrap_env(args)
    from .tui import JirayaApp  # imported lazily so `run` works without textual TTY

    config = _config_from_args(args)
    JirayaApp(config=config, poll_interval=args.interval).run()
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    """Prompt the work agent to do further work in a ticket's provisioned repo."""
    _bootstrap_env(args)
    config = _config_from_args(args)
    system = build_system(config)
    color = sys.stdout.isatty() and not args.no_color
    print(_banner(system))
    if type(system.work_runner).__name__ == "NoopWorkAgentRunner":
        print("Note: no work agent configured (pass --work --apply to do real work).")
    system.bus.subscribe(_make_console_printer(color))

    outcome = system.service.run_followup(args.ticket, args.instruction)
    if outcome is None:
        print(f"Could not run follow-up work for {args.ticket}.", file=sys.stderr)
        return 1
    if outcome.work and outcome.work.opened_pr:
        print(f"\nOpened/updated PR: {outcome.work.pr_url}")
    elif outcome.work and outcome.work.needs_input:
        print(f"\nAgent needs input: {outcome.work.question}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """Forget an actioned ticket so it can be re-triaged on the next poll."""
    _bootstrap_env(args)
    config = _config_from_args(args)
    system = build_system(config)
    removed = system.service.forget_ticket(args.ticket)
    if removed:
        print(f"Forgot {args.ticket}: removed from the ledger/inbox; "
              "it will be re-triaged on the next poll if still untriaged in Jira.")
        return 0
    print(f"Nothing to forget for {args.ticket} (not in the ledger or inbox).",
          file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiraya",
        description="Agent-powered Jira triage agent with a TUI dashboard.",
    )
    sub = parser.add_subparsers(dest="command")

    p_tui = sub.add_parser("tui", help="Launch the real-time dashboard (default).")
    _add_common(p_tui)
    p_tui.set_defaults(func=cmd_tui, interval=20.0, default_state=True)

    p_run = sub.add_parser("run", help="Run the triage harness headlessly.")
    _add_common(p_run)
    p_run.add_argument("--once", action="store_true",
                       help="Run a single poll cycle and exit.")
    p_run.add_argument("--cycles", type=int, default=None,
                       help="Stop after N poll cycles (default: run forever).")
    p_run.add_argument("--no-color", action="store_true", help="Disable colored output.")
    p_run.set_defaults(func=cmd_run, default_state=False)

    p_work = sub.add_parser(
        "work",
        help="Prompt the work agent to do further work in a ticket's repo.")
    _add_common(p_work)
    p_work.add_argument("ticket", help="The Jira ticket key (e.g. PROJ-123).")
    p_work.add_argument("instruction", help="What the agent should do.")
    p_work.add_argument("--no-color", action="store_true", help="Disable colored output.")
    p_work.set_defaults(func=cmd_work, interval=20.0, default_state=False)

    p_forget = sub.add_parser(
        "forget",
        help="Forget an actioned ticket so it can be re-triaged on the next poll.")
    _add_common(p_forget)
    p_forget.add_argument("ticket", help="The Jira ticket key to forget (e.g. PROJ-123).")
    p_forget.set_defaults(func=cmd_forget, default_state=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        # Default to the dashboard.
        args = parser.parse_args(["tui", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
