"""WB-1426: ``/clear`` is a gateway command now — the CLI/TUI command stays as it was."""

from hermes_cli.commands import (
    GATEWAY_KNOWN_COMMANDS, _is_gateway_available, gateway_help_lines, resolve_command,
)


def test_clear_is_offered_in_the_gateway():
    cmd = resolve_command("clear")
    assert cmd is not None
    assert "clear" in GATEWAY_KNOWN_COMMANDS
    assert _is_gateway_available(cmd)
    line = [l for l in gateway_help_lines() if l.startswith("`/clear")]
    assert line and "Clear screen" not in line[0]          # the gateway text, not the terminal one
    assert "undo" in line[0]


def test_clear_in_the_cli_keeps_its_description_and_surface():
    cmd = resolve_command("clear")
    assert cmd is not None
    assert cmd.description == "Clear screen and start a new session"
    assert "screen" in cmd.describe().lower() or cmd.describe() != cmd.describe(gateway=True)
    assert cmd.desktop == "terminal"
    assert not cmd.gateway_only


def test_clear_is_in_the_telegram_menu_with_the_gateway_text():
    from hermes_cli.commands_platforms import telegram_bot_commands

    names = dict(telegram_bot_commands())
    assert "clear" in names
    assert "screen" not in names["clear"].lower()


def test_the_werkbank_command_menu_offers_clear_with_the_gateway_text():
    """WB-1343's menu reads the plugin catalog: /clear shows up there without a board change."""
    from plugins.platforms.werkbank.commands import catalog

    entry = [c for c in catalog() if c["name"] == "clear"]
    assert entry and "context" in entry[0]["description"].lower()
    assert "screen" not in entry[0]["description"].lower()


def test_cli_dispatch_table_still_clears_the_screen():
    import cli

    assert cli.HermesCLI._SLASH_DISPATCH["clear"] == ("_cmd_clear", True)
