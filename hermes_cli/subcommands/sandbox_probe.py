"""``hermes sandbox-probe`` subcommand parser (WB-1090)."""

from __future__ import annotations

from typing import Callable

from hermes_cli.subcommands._shared import add_json_flag


def build_sandbox_probe_parser(subparsers, *, cmd_sandbox_probe: Callable) -> None:
    """Attach the ``sandbox-probe`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "sandbox-probe", help="Report this profile's effective strict-Docker tool policy",
        description="Report the terminal policy a run of this profile would ACTUALLY use "
            "(strict Docker on/off, sha256 of the governing config.yaml, network, credential "
            "exposure). Read-only: starts nothing, changes nothing.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", metavar="FILE",
        help="The run's config file to compare against the effective config")
    source.add_argument("--sandbox-config", dest="sandbox_config", metavar="FILE",
        help="Report the policy `hermes chat --sandbox-config FILE` would run with (WB-1176); "
             "config_sha256 is FILE's hash only when exactly that policy governs the terminal")
    add_json_flag(parser, "Print one JSON object")
    parser.set_defaults(func=cmd_sandbox_probe)
