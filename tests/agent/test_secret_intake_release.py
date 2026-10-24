"""WB-1261: third card answer „Kein Geheimnis“ — the original text goes to the agent, nothing is
saved; only the sender, bound to card/session/placeholder, within the TTL, once, audited without the
value. Plus the false alarm behind the owner's report: a document upload's cache path
(``…/cache/documents/doc_<id>_<name>.PDF``) was taken for a bare pasted ``api-key``.

Every value below is synthetic (same SHAPE as the report, no real number) or a runtime-assembled fake.
"""

import logging

import pytest

from agent import secret_intake as si
from agent.vault_store import VaultStore

FAKE_GH = "ghp" + "_" + "FAKE0fake1FAKE2fake3FAKE4fake5FAKE6f"
FAKE_BARE = "Zq8" + "FAKEk2Lm9Xp4Rt7Wv1Bn6Hj3Cs5Df8Gy0Qa4Ne"     # 41 chars, mixed classes
SCOPE = "telegram:dm:4711"


@pytest.fixture(autouse=True)
def _clean_store(monkeypatch):
    si.store().clear()
    si._RELEASED.clear()
    monkeypatch.setattr(si, "enabled", lambda: True)
    yield
    si.store().clear()
    si._RELEASED.clear()


def _vault(tmp_path):
    return lambda home: VaultStore(tmp_path / "vault")


def _entry(text=None, *, sender="42", scope=SCOPE):
    res = si.intake(text or f"nimm {FAKE_BARE} bitte", scope, surface="test", sender=sender)
    assert len(res.new) == 1
    return res, res.new[0]


# -- release ------------------------------------------------------------------------------------

def test_card_has_third_button_kein_geheimnis():
    _, e = _entry()
    choices = si.card_choices([e])
    assert [c["value"].split(":")[0] for c in choices] == ["save", "drop", "keep"]
    assert "Kein Geheimnis" in choices[2]["label"] and FAKE_BARE not in str(choices)
    # a surface that cannot tell who tapped gets no third button (it could never pass the sender check)
    assert [c["value"].split(":")[0] for c in si.card_choices([e], release=False)] == ["save", "drop"]


def test_keep_gives_the_original_to_the_agent_and_saves_nothing(tmp_path):
    res, e = _entry()
    got = []
    text = si.apply_choice(f"keep:{e.id}", vault_factory=_vault(tmp_path), actor="42", scope=SCOPE,
                           on_release=lambda ph, val: got.append((ph, val)))
    assert got == [(e.placeholder, FAKE_BARE)]
    assert "Kein Geheimnis" in text and FAKE_BARE not in text
    assert e.status == "released" and e.value is None
    assert not (tmp_path / "vault").exists()                     # Tresor leer
    # the follow-up / next turn carrying the original is not redacted again (no new card, same text)
    follow = f"Freigegeben: {e.placeholder} steht für {FAKE_BARE}"
    again = si.intake(follow, SCOPE, surface="test", sender="42")
    assert again.text is follow and again.new == []
    assert si.mask_text(follow) == follow
    # and the model-egress scrub no longer hides it
    from agent.redact import redact_registered_vault_values as scrub
    assert scrub(FAKE_BARE) == FAKE_BARE


def test_released_signature_is_not_the_value():
    _, e = _entry()
    si.release(e.id, actor="42", scope=SCOPE)
    dump = repr(si._RELEASED)
    assert FAKE_BARE not in dump and FAKE_BARE[:12] not in dump
    # a different value of the same art still triggers a card
    other = "Xp4" + "FAKEq7Rt2Lm9Wv1Bn6Hj3Cs5Df8Gy0Qa4NeZk"
    assert si.intake(f"key {other}", SCOPE, surface="test", sender="42").new


@pytest.mark.parametrize("actor,scope", [("99", SCOPE), (None, SCOPE), ("", SCOPE), ("42", "telegram:dm:9999")])
def test_stranger_unknown_actor_or_other_session_is_refused(actor, scope, tmp_path):
    _, e = _entry()
    got = []
    text = si.apply_choice(f"keep:{e.id}", actor=actor, scope=scope, on_release=lambda *a: got.append(a))
    assert got == [] and FAKE_BARE not in text and "⛔" in text
    assert e.status == "pending" and e.value == FAKE_BARE        # refusal consumes nothing
    assert si.save(e.id, vault_factory=_vault(tmp_path))["ok"] is True   # owner can still decide


def test_entry_without_known_sender_cannot_be_released():
    _, e = _entry(sender="")
    assert si.release(e.id, actor="42", scope=SCOPE)["ok"] is False
    assert e.status == "pending"


def test_offer_fills_the_sender_of_a_pre_redacted_message():
    """Werkbank redacts in the plugin (no sender); the gateway offers the card for its event."""
    res = si.intake(f"x {FAKE_BARE}", SCOPE, surface="werkbank")
    (e,) = si.offer_candidates(SCOPE, res.text, sender="42")
    assert e.sender == "42"
    assert si.release(e.id, actor="42", scope=SCOPE)["ok"] is True


def test_expired_is_refused():
    _, e = _entry()
    si.store().purge(now=e.expires + 1)
    got = []
    text = si.apply_choice(f"keep:{e.id}", actor="42", scope=SCOPE, on_release=lambda *a: got.append(a))
    assert got == [] and "⌛" in text


@pytest.mark.parametrize("first", ["save", "drop", "keep"])
def test_once_only_a_second_answer_has_no_effect(first, tmp_path):
    _, e = _entry()
    got = []
    kw = dict(vault_factory=_vault(tmp_path), actor="42", scope=SCOPE, on_release=lambda *a: got.append(a))
    si.apply_choice(f"{first}:{e.id}", **kw)
    status = e.status
    for second in ("save", "drop", "keep"):
        if second != first:
            assert "⌛" in si.apply_choice(f"{second}:{e.id}", **kw)
    assert e.status == status
    assert len(got) == (1 if first == "keep" else 0)
    items = VaultStore(tmp_path / "vault").list_items() if (tmp_path / "vault").exists() else []
    assert len(items) == (1 if first == "save" else 0)


def test_release_audit_has_no_value(caplog):
    caplog.set_level(logging.DEBUG)
    _, e = _entry()
    si.release(e.id, actor="99", scope=SCOPE)
    si.release(e.id, actor="42", scope=SCOPE)
    text = caplog.text
    assert "freigabe_abgewiesen" in text and "freigegeben" in text and e.placeholder in text
    assert FAKE_BARE not in text and SCOPE not in text and "actor=42" not in text


def test_save_and_drop_paths_unchanged(tmp_path):
    a = si.intake(f"a {FAKE_GH}", SCOPE, surface="test").new[0]
    b = si.intake(f"b {FAKE_BARE}", SCOPE, surface="test").new[0]
    assert "gespeichert" in si.apply_choice(f"save:{a.id}", vault_factory=_vault(tmp_path))
    assert "verworfen" in si.apply_choice(f"drop:{b.id}")
    assert (a.status, b.status) == ("saved", "discarded")
    # a saved / discarded value is NOT learned as harmless
    assert si.intake(f"c {FAKE_GH}", SCOPE, surface="test").new
    assert si.intake(f"d {FAKE_BARE}", SCOPE, surface="test").new


# -- the false alarm (measured: bare-run rule, ``_bare_key_is_secret``) --------------------------

_HOME = "/home/someuser/.hermes"


def _doc_note(name, home=_HOME, cache_id="3f9a1c2b7d4e"):
    path = f"{home}/cache/documents/doc_{cache_id}_{name}"
    return (f"[The user sent a document: '{name}'. It is saved at: {path}. Its text is not inlined "
            f"here (it's a binary format such as PDF or DOCX).]\n\nbitte ansehen")


@pytest.mark.parametrize("name", [
    "bip_folio_mobile_20261024_0000001.PDF",            # shape of the report, invented digits
    "bip_folio_mo_20261024_0000001.PDF",
    "Depot_DE00000000000000000001_Auszug.PDF",           # IBAN-shaped part, all zeros
    "Kundennr_0000000001_Beleg_20261024.pdf",
    "Scan-2026-10-24-ABC123-0000001.JPG",
])
def test_document_cache_path_is_not_an_api_key(name):
    for home, cid in ((_HOME, "3f9a1c2b7d4e"), ("/root/.hermes", "a1b2c3d4e5f6")):
        assert si.find_secrets(_doc_note(name, home, cid)) == []


def test_bare_file_name_alone_is_not_an_api_key():
    assert si.find_secrets("bip_folio_mobile_DE00000000000000000001.PDF") == []


@pytest.mark.parametrize("text,value", [
    (FAKE_BARE, FAKE_BARE),
    (f"/tmp/{FAKE_BARE}.txt", f"/tmp/{FAKE_BARE}.txt"),          # a key used as a file name stays a key
    (f"{FAKE_BARE}.PDF", f"{FAKE_BARE}.PDF"),
    (f"doc_3f9a1c2b7d4e_{FAKE_BARE}.pdf", f"doc_3f9a1c2b7d4e_{FAKE_BARE}.pdf"),
])
def test_real_keys_still_found_next_to_file_shapes(text, value):
    found = si.find_secrets(text)
    assert [text[f.start:f.end] for f in found] == [value] and found[0].art == "api-key"


def test_real_key_in_a_document_note_message_still_found():
    text = _doc_note("bip_folio_mobile_20261024_0000001.PDF") + f"\nund der Key {FAKE_BARE}"
    found = si.find_secrets(text)
    assert [text[f.start:f.end] for f in found] == [FAKE_BARE]


@pytest.mark.parametrize("text", [
    "IBAN DE00 0000 0000 0000 0000 01",
    "IBAN: DE00000000000000000001 BIC: ABCDDEFFXXX",
    "Depotnummer: 0000000001 Kundennummer: 0000000002",
    "Beleg-Nr. 2026102400000000000000000000000000000000000001",
    "Referenz 00000000000000000000000000000000000000000001/2026",
])
def test_bank_document_numbers_stay_quiet(text):
    assert si.find_secrets(text) == []
