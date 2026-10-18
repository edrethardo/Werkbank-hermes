"""WB-1212: the classic CLI replaces secrets before it stages the turn (session row, display) and asks
"Im Tresor speichern / Verwerfen" through its own choice modal — answered without the model.

All secrets are FAKE and assembled at runtime."""

from types import SimpleNamespace

import pytest

from agent import secret_intake as si
from agent.vault_store import VaultStore
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin

FAKE_GH = "ghp" + "_" + "FAKE0fake1FAKE2fake3FAKE4fake5FAKE6f"


@pytest.fixture(autouse=True)
def _intake_on(monkeypatch):
    si.store().clear()
    monkeypatch.setattr(si, "enabled", lambda: True)
    yield
    si.store().clear()


def _cli(answer):
    asked = []

    def modal(*, title, detail, choices, timeout=120):
        asked.append({"title": title, "detail": detail, "choices": choices})
        return answer

    cli = SimpleNamespace(_app=object(), session_id="cli-sess", agent=None, asked=asked,
                          _prompt_text_input_modal=modal,
                          _normalize_slash_confirm_choice=lambda raw, choices: raw)
    return cli


def test_cli_redacts_and_saves_on_choice(capsys):
    from hermes_constants import get_hermes_home
    cli = _cli("save")
    out = CLIChatTurnMixin._chat_secret_intake(cli, f"richte {FAKE_GH} ein")
    assert out == "richte [GEHEIMNIS:github-token-1] ein"
    assert len(cli.asked) == 1 and FAKE_GH not in str(cli.asked)
    items = VaultStore(get_hermes_home() / "vault").list_items()
    assert [i.kind for i in items] == ["secret"]
    printed = capsys.readouterr().out
    assert "gespeichert" in printed and FAKE_GH not in printed


def test_cli_discard(capsys):
    cli = _cli("drop")
    CLIChatTurnMixin._chat_secret_intake(cli, f"x {FAKE_GH}")
    assert "verworfen" in capsys.readouterr().out
    assert all(e.value is None for e in si.store()._by_id.values())


def test_cli_without_app_does_not_prompt(capsys):
    cli = _cli("save")
    cli._app = None
    assert CLIChatTurnMixin._chat_secret_intake(cli, f"x {FAKE_GH}") == "x [GEHEIMNIS:github-token-1]"
    assert cli.asked == []
    assert FAKE_GH not in capsys.readouterr().out


def test_history_file_never_stores_the_secret(tmp_path):
    from hermes_cli.cli_tui_mixin import _SecretMaskingFileHistory
    path = tmp_path / "history"
    _SecretMaskingFileHistory(str(path)).store_string(f"nimm {FAKE_GH} bitte")
    stored = path.read_text()
    assert FAKE_GH not in stored and "nimm [GEHEIMNIS:github-token] bitte" in stored
    assert si.store()._by_id == {}   # masking keeps nothing, offers nothing


def test_chat_redacts_before_it_stages_the_turn():
    """The staged row (session DB / display) is built from the message chat() passes on: the intake
    must run first. Structural, because chat() needs a live agent and terminal."""
    import inspect
    src = inspect.getsource(CLIChatTurnMixin.chat)
    assert "self._chat_secret_intake(message)" in src
    assert src.index("self._chat_secret_intake(message)") < src.index("self._chat_stage_user_message(agent, message)")


def test_cli_no_secret_no_prompt():
    cli = _cli("save")
    text = "nur Text"
    assert CLIChatTurnMixin._chat_secret_intake(cli, text) is text and cli.asked == []
