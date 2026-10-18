"""WB-1212: secret intake for user prompts — detection, byte-exact replacement, in-memory store,
vault save / discard, switch, audit without cleartext, false-alarm corpus.

Every secret below is FAKE and assembled at runtime (no scanner-shaped literal in the source)."""

import logging

import pytest

from agent import secret_intake as si
from agent.vault_store import VaultStore

# -- fake secrets (runtime-assembled, obviously fake) ---------------------------------------------
FAKE_GH = "ghp" + "_" + "FAKE0fake1FAKE2fake3FAKE4fake5FAKE6f"
FAKE_ANT = "sk-" + "ant-" + "api03-FAKEfake0123456789FAKEfake0123456789FAKE"
FAKE_AWS = "AKIA" + "FAKE0123456789XY"
FAKE_BOT = "1234567890" + ":" + "AAFakeFakeFakeFakeFakeFakeFake_fake12"
FAKE_JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiJmYWtlIn0" + "." + "FAKEsigFAKEsig0123"
FAKE_PEM = ("-----BEGIN " + "PRIVATE KEY-----\nMIIFAKEFAKEFAKE\nFAKEFAKE==\n-----END " + "PRIVATE KEY-----")
FAKE_PW = "Fake" + "Passw0rd!"


@pytest.fixture(autouse=True)
def _clean_store(monkeypatch):
    si.store().clear()
    monkeypatch.setattr(si, "enabled", lambda: True)
    yield
    si.store().clear()


def _vault(tmp_path):
    return lambda home: VaultStore(tmp_path / "vault")


# -- detection ------------------------------------------------------------------------------------

@pytest.mark.parametrize("secret,art", [
    (FAKE_GH, "github-token"), (FAKE_ANT, "anthropic-key"), (FAKE_AWS, "aws-key"),
    (FAKE_BOT, "telegram-bot-token"), (FAKE_JWT, "jwt"), (FAKE_PEM, "private-key"),
])
def test_detects_known_shapes(secret, art):
    text = f"bitte nimm {secret} für das Setup"
    found = si.find_secrets(text)
    assert [(text[f.start:f.end], f.art) for f in found] == [(secret, art)]


@pytest.mark.parametrize("text,value", [
    ("password=" + FAKE_PW, FAKE_PW),
    ("Mein Passwort: " + FAKE_PW + " danke", FAKE_PW),
    ('{"api_key": "Zq8' + 'FAKEfake0123xyzQ"}', "Zq8FAKEfake0123xyzQ"),
    ("MY_SERVICE_TOKEN=Zq8" + "FAKEfake0123xyzQ", "Zq8FAKEfake0123xyzQ"),
    ("Authorization: Bearer Zq8" + "FAKEfake0123xyzQabc", "Zq8FAKEfake0123xyzQabc"),
    ("postgres://app:" + FAKE_PW + "@db.example.invalid/app", FAKE_PW),
])
def test_detects_keyword_assignments(text, value):
    found = si.find_secrets(text)
    assert [text[f.start:f.end] for f in found] == [value]


FALSE_ALARMS = [
    "commit 4f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39 ist auf main",
    "Branch fix/sk-learn-upgrade-for-python bitte rebasen",
    "UUID 550e8400-e29b-41d4-a716-446655440000 aus dem Log",
    "sha256: 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "md5 d41d8cd98f00b204e9800998ecf8427e und sha1 da39a3ee5e6b4b0d3255bfef95601890afd80709",
    "max_tokens: 4096, tokenizer: cl100k_base",
    "password: required, token: optional",
    "export OPENAI_API_KEY=$OPENAI_API_KEY",
    "api_key = os.getenv('OPENAI_API_KEY')",
    "Secretary: J. Smith, author=Smith",
    "Die Datei liegt unter /home/user/.ssh/id_ed25519.pub",
    "session_id=20261018_231532_0c0b12 und request-id 3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "Schau dir https://github.com/org/repo/pull/123 an",
    "Platzhalter [GEHEIMNIS:github-token-1] bleibt",
    "Telefon +49 151 23456789, PLZ 10115",
]


@pytest.mark.parametrize("text", FALSE_ALARMS)
def test_false_alarm_corpus(text):
    assert si.find_secrets(text) == []


# -- replacement ----------------------------------------------------------------------------------

def test_rest_of_text_is_byte_identical():
    before, after = "Hallo 👋\n  bitte richte ein: ", "  \n\tund dann `git push`. Ende."
    res = si.intake(before + FAKE_GH + after, "s1", surface="test")
    assert res.text == before + "[GEHEIMNIS:github-token-1]" + after
    assert FAKE_GH not in res.text
    assert len(res.new) == 1


def test_no_finding_returns_same_object():
    text = "nichts Geheimes hier"
    assert si.intake(text, "s1").text is text


def test_numbering_per_scope_and_same_value_same_placeholder():
    other = "ghp" + "_" + "FAKE9fake8FAKE7fake6FAKE5fake4FAKE3f"
    r1 = si.intake(f"a {FAKE_GH} b {other} c {FAKE_GH}", "s1")
    assert r1.text == "a [GEHEIMNIS:github-token-1] b [GEHEIMNIS:github-token-2] c [GEHEIMNIS:github-token-1]"
    assert len(r1.new) == 2
    r2 = si.intake(f"noch mal {FAKE_GH}", "s1")
    assert r2.text == "noch mal [GEHEIMNIS:github-token-1]" and r2.new == []
    assert si.intake(f"x {FAKE_GH}", "s2").text == "x [GEHEIMNIS:github-token-1]"


def test_intake_is_idempotent():
    once = si.intake(f"t {FAKE_GH}", "s1").text
    assert si.intake(once, "s1").text is once


def test_multimodal_text_parts_redacted():
    msg = [{"type": "text", "text": f"key {FAKE_ANT}"}, {"type": "image_url", "image_url": {"url": "data:x"}}]
    res = si.intake(msg, "s1")
    assert res.text[0]["text"] == "key [GEHEIMNIS:anthropic-key-1]"
    assert res.text[1] is msg[1]
    assert msg[0]["text"].endswith(FAKE_ANT)  # input not mutated


def test_switch_off_leaves_text(monkeypatch):
    monkeypatch.setattr(si, "enabled", lambda: False)
    text = f"t {FAKE_GH}"
    assert si.intake(text, "s1").text is text


def test_enabled_reads_config(monkeypatch):
    monkeypatch.undo()
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"security": {"secret_intake": False}})
    assert si.enabled() is False
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"security": {}})
    assert si.enabled() is True


# -- store / vault --------------------------------------------------------------------------------

def test_save_creates_vault_item_and_wipes_value(tmp_path):
    res = si.intake(f"OPENAI_API_KEY={FAKE_ANT}", "s1", surface="test")
    entry = res.new[0]
    out = si.save(entry.id, vault_factory=_vault(tmp_path))
    assert out["ok"] and out["handle"].startswith("vault_")
    vault = VaultStore(tmp_path / "vault")
    meta = vault.get_meta(out["handle"])
    assert meta.kind == "secret" and "OPENAI_API_KEY" in meta.label and "GEHEIMNIS:anthropic-key-1" in meta.label
    assert vault.resolve_secret(out["handle"]) == {"value": FAKE_ANT}
    assert entry.value is None and entry.status == "saved"
    assert si.save(entry.id, vault_factory=_vault(tmp_path))["ok"] is False  # once only


def test_discard_wipes_value(tmp_path):
    entry = si.intake(f"x {FAKE_GH}", "s1").new[0]
    assert si.discard(entry.id)["ok"] is True
    assert entry.value is None and entry.status == "discarded"
    assert si.save(entry.id, vault_factory=_vault(tmp_path))["ok"] is False
    assert not (tmp_path / "vault").exists()


def test_expiry_wipes_value(tmp_path):
    entry = si.intake(f"x {FAKE_GH}", "s1").new[0]
    si.store().purge(now=entry.expires + 1)
    assert entry.value is None and entry.status == "expired"
    assert si.save(entry.id, vault_factory=_vault(tmp_path))["reason"] == "abgelaufen"


def test_apply_choice_texts_never_contain_value(tmp_path):
    e = si.intake(f"a {FAKE_GH} b {FAKE_AWS}", "s1").new
    ids = ",".join(x.id for x in e)
    text = si.apply_choice(f"save:{ids}", vault_factory=_vault(tmp_path))
    assert "[GEHEIMNIS:github-token-1]" in text and "[GEHEIMNIS:aws-key-1]" in text
    assert FAKE_GH not in text and FAKE_AWS not in text
    assert len(VaultStore(tmp_path / "vault").list_items()) == 2


def test_offer_candidates_once():
    si.intake(f"x {FAKE_GH}", "s1")
    assert [e.placeholder for e in si.offer_candidates("s1", "x [GEHEIMNIS:github-token-1]")] == ["[GEHEIMNIS:github-token-1]"]
    assert si.offer_candidates("s1", "x [GEHEIMNIS:github-token-1]") == []
    assert si.offer_candidates("s2", "x [GEHEIMNIS:github-token-1]") == []


def test_card_has_no_value():
    entries = si.intake(f"x {FAKE_GH}", "s1").new
    assert FAKE_GH not in si.card_text(entries)
    assert all(FAKE_GH not in c["value"] + c["label"] for c in si.card_choices(entries))


def test_audit_lines_have_no_cleartext(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    e = si.intake(f"x {FAKE_GH} y password={FAKE_PW}", "telegram:dm:42").new
    si.save(e[0].id, vault_factory=_vault(tmp_path))
    si.discard(e[1].id)
    text = caplog.text
    assert "erkannt" in text and "gespeichert" in text and "verworfen" in text
    assert FAKE_GH not in text and FAKE_PW not in text and "telegram:dm:42" not in text
    assert all(FAKE_GH not in repr(x) for x in e)


def test_vault_secret_kind_requires_value(tmp_path):
    from agent.vault_store import VaultError
    with pytest.raises(VaultError):
        VaultStore(tmp_path / "vault").add_item("secret", "leer", {"value": "  "})


def test_saved_secret_is_listed_by_handle_but_never_page_filled():
    import json
    from agent.vault_store import get_vault_store
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list
    meta = get_vault_store().add_item("secret", "GitHub-Token · GEHEIMNIS:github-token-1", {"value": FAKE_GH})
    listed = json.loads(browser_vault_list())
    entry = next(i for i in listed["items"] if i["handle"] == meta.id)
    assert entry["kind"] == "secret" and entry["available"] is False and "GEHEIMNIS:github-token-1" in entry["label"]
    assert FAKE_GH not in json.dumps(listed)
    filled = json.loads(browser_vault_fill(meta.id))
    assert filled["success"] is False and filled["error_type"] == "not_fillable"
    assert FAKE_GH not in json.dumps(filled)


def test_value_registered_for_egress_redaction():
    from agent.redact import redact_registered_vault_values as redact_vault_values
    si.intake(f"x password={FAKE_PW}", "s1")
    assert FAKE_PW not in redact_vault_values(f"tool echoed {FAKE_PW}")
