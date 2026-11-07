"""WB-1471: ``ansicht_zeigen`` — the frame of the chat views (Chat-Ansichten, Schritt 1).

Blocks are files (``ansichten/<art>.py``, found on their own; tests per block in
``test_werkbank_ansicht_<art>.py``). Here: the block contract and discovery, the frame's limits
(200 KB inline, unknown art/fields), references (chat files of THIS session, artefacts, files in the
project folder without a symlink on the way), precise German messages, and the way into the chat:
the ``ansicht`` event in the plugin buffer and the view in the snapshot at the place of its tool row —
also after a reload. Everything lands under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from ansichten_testkit import aufl, box, fehler_von, projekt, tabelle  # noqa: F401 (fixtures)
from werkbank_testkit import FakeHost, KEY_SESSION, SID, call, core_event, events_path, running_server, session_path, sse

from plugins.platforms.werkbank import ansichten, outbox as outbox_mod


# -- the block contract ----------------------------------------------------------------------------
def test_blocks_are_found_by_file_name():
    mods = ansichten.bausteine()
    assert list(mods) == ["gruppe", "tabelle"]
    for art, mod in mods.items():
        assert mod.ART == art and callable(mod.pruefen) and isinstance(mod.KURZ, str) and mod.KURZ
    assert ansichten.arten(in_gruppe=True) == ["tabelle"]
    assert ansichten.schema()["parameters"]["properties"]["art"]["enum"] == ["gruppe", "tabelle"]


def test_a_new_block_is_a_new_file_only(tmp_path, monkeypatch):
    (tmp_path / "probe.py").write_text(
        'ART = "probe"\nKURZ = "art=probe: daten={wert}."\n'
        'from . import erwartet\n'
        'def pruefen(args, feld, k):\n'
        '    wert = (args.get("daten") or {}).get("wert")\n'
        '    if not isinstance(wert, int):\n'
        '        raise erwartet(feld + "daten.wert", "Zahl", wert)\n'
        '    return {"wert": wert}\n', encoding="utf-8")
    (tmp_path / "kaputt.py").write_text('ART = "anders"\ndef pruefen(a, f, k): return {}\n', encoding="utf-8")
    monkeypatch.setattr(ansichten, "__path__", list(ansichten.__path__) + [str(tmp_path)])
    monkeypatch.setattr(ansichten, "_BAUSTEINE", None)
    try:
        assert list(ansichten.bausteine()) == ["gruppe", "probe", "tabelle"]     # kaputt: ART ≠ name
        view, fehler = ansichten.pruefen({"art": "probe", "titel": "P", "daten": {"wert": 3}})
        assert fehler == [] and view == {"v": 1, "art": "probe", "titel": "P", "wert": 3}
        assert fehler_von({"art": "probe", "daten": {"wert": "x"}}) == ["daten.wert: erwartet Zahl, gefunden Text „x“"]
        assert "art=probe" in ansichten.beschreibung()
        inner = {"art": "gruppe", "daten": {"tabs": [{"titel": "t", "ansicht": {"art": "probe", "daten": {"wert": 1}}}]}}
        assert ansichten.pruefen(inner)[1] == []                # IN_GRUPPE defaults to True
    finally:
        ansichten._BAUSTEINE = None


# -- the frame -------------------------------------------------------------------------------------
def test_unknown_art_names_the_choices():
    assert fehler_von({"art": "grafik"}) == ["art: erwartet gruppe oder tabelle, gefunden Text „grafik“"]


def test_unknown_fields_are_refused_with_the_allowed_ones():
    assert fehler_von(tabelle(farbe="rot")) == ["farbe: unbekanntes Feld, erlaubt: art, titel, daten, quelle, optionen"]


def test_at_most_200_kb_inline():
    args = {"art": "tabelle", "daten": {"spalten": ["n"], "zeilen": [["x" * 1900] for _ in range(110)]}}
    fehler = fehler_von(args)
    assert len(fehler) == 1 and "KB inline, erlaubt 200 KB" in fehler[0] and "quelle" in fehler[0]


# -- references ------------------------------------------------------------------------------------
def test_a_project_file_is_copied_into_the_outbox(projekt, box):  # noqa: F811
    view, fehler = ansichten.pruefen({"art": "tabelle", "quelle": "daten/tab.csv"}, aufl(projekt, box))
    assert fehler == []
    ref = view["quelle"]
    assert outbox_mod.ID_RE.fullmatch(ref["datei"]) and ref["format"] == "csv" and ref["name"] == "tab.csv"
    found = box.path(KEY_SESSION, ref["datei"], "original")
    assert found and found[0].read_bytes() == (projekt / "daten" / "tab.csv").read_bytes()
    assert box.for_refs(KEY_SESSION, ["h:1", "turn_1"]) == []          # never an attachment of an answer


@pytest.mark.parametrize("ref", ["../geheim/x.csv", "daten/../../geheim/x.csv"])
def test_no_way_out_of_the_project_folder(projekt, box, ref):  # noqa: F811
    fehler = fehler_von({"art": "tabelle", "quelle": ref}, aufl(projekt, box))
    assert fehler == [f"quelle: „{ref}“ liegt nicht im Projektordner {projekt} (kein „..“)"]


def test_an_absolute_path_outside_is_refused(projekt, box, tmp_path):  # noqa: F811
    ref = str(tmp_path / "geheim" / "x.csv")
    assert fehler_von({"art": "tabelle", "quelle": ref}, aufl(projekt, box)) == [
        f"quelle: „{ref}“ liegt nicht im Projektordner {projekt}"]
    inside = str(projekt / "daten" / "tab.csv")
    assert ansichten.pruefen({"art": "tabelle", "quelle": inside}, aufl(projekt, box))[1] == []


def test_a_symlinked_file_is_not_followed(projekt, box, tmp_path):  # noqa: F811
    os.symlink(tmp_path / "geheim" / "x.csv", projekt / "link.csv")
    assert fehler_von({"art": "tabelle", "quelle": "link.csv"}, aufl(projekt, box)) == [
        "quelle: „link.csv“ ist ein Symlink — Symlinks werden nicht verfolgt"]


def test_a_symlinked_directory_is_not_followed(projekt, box, tmp_path):  # noqa: F811
    os.symlink(tmp_path / "geheim", projekt / "aussen")
    assert fehler_von({"art": "tabelle", "quelle": "aussen/x.csv"}, aufl(projekt, box)) == [
        "quelle: „aussen“ ist ein Symlink — Symlinks werden nicht verfolgt"]
    assert box.for_refs(KEY_SESSION, ["ansicht"]) == []                # nothing was copied


def test_a_swap_between_check_and_copy_is_caught(projekt, box, tmp_path, monkeypatch):  # noqa: F811
    """The outbox opens the path again: a directory swapped for a symlink in between would copy
    ANOTHER file — the copy's source key (real path, size, mtime) must be the checked file's."""
    echt = box.ingest

    def getauscht(session_key, media, refs):
        return echt(session_key, [(str(tmp_path / "geheim" / "x.csv"), False)], refs)

    monkeypatch.setattr(box, "ingest", getauscht)
    assert fehler_von({"art": "tabelle", "quelle": "daten/tab.csv"}, aufl(projekt, box)) == [
        "quelle: „tab.csv“ hat sich während der Prüfung geändert; noch einmal aufrufen"]


def test_no_project_folder_no_project_files(box):  # noqa: F811
    fehler = fehler_von({"art": "tabelle", "quelle": "daten/tab.csv"}, aufl(None, box))
    assert fehler[0].startswith("quelle: „daten/tab.csv“ — diese Sitzung hat keinen Projektordner")


def test_chat_files_only_of_this_session(projekt, box):  # noqa: F811
    view, _ = ansichten.pruefen({"art": "tabelle", "quelle": "daten/tab.csv"}, aufl(projekt, box))
    fid = view["quelle"]["datei"]
    assert ansichten.pruefen({"art": "tabelle", "quelle": fid}, aufl(projekt, box))[1] == []
    other = aufl(projekt, box, key="agent:main:telegram:dm:1:2")
    assert fehler_von({"art": "tabelle", "quelle": fid}, other) == [f"quelle: {fid} ist keine Datei dieses Chats"]


def test_artefacts_by_board_path_and_no_foreign_urls(box):  # noqa: F811
    view, fehler = ansichten.pruefen({"art": "tabelle", "quelle": "https://board.example:8443/api/project-outbox/"
                                      "agent_ticket/artifact-" + "a" * 64 + ".csv"}, aufl(None, box))
    assert fehler == []
    assert view["quelle"]["artefakt"] == "/api/project-outbox/agent_ticket/artifact-" + "a" * 64 + ".csv"
    assert fehler_von({"art": "tabelle", "quelle": "/api/project-outbox/../x.csv"}, aufl(None, box))
    assert fehler_von({"art": "tabelle", "quelle": "https://evil.example/data.csv"}, aufl(None, box))[0].startswith(
        "quelle: „https://evil.example/data.csv“ ist kein erlaubter Verweis")
    assert fehler_von({"art": "tabelle", "quelle": "/api/hermes-chat/datei/x"}, aufl(None, box))


# -- the tool, the event and the snapshot ----------------------------------------------------------
@pytest.fixture
def chat(tmp_path, projekt, box):  # noqa: F811
    srv = running_server(tmp_path, FakeHost(), outbox=box)
    srv.ablage = ansichten.Ablage(tmp_path / "home")
    srv.server.ansichten = srv.ablage
    status, _ = call(srv.port, "GET", f"/v1/resolve?session_id={SID}&projekt={projekt}", srv.key)
    assert status == 200
    inst = SimpleNamespace(server=srv.server, buffers=srv.buffers, host=srv.host, ansichten=srv.ablage)
    srv.inst = inst
    try:
        yield srv
    finally:
        srv.server.stop()


def _emit_into(srv):
    def emit(key, sid, platform, type_, id=None, parent_id=None, payload=None):   # noqa: A002
        srv.buffers.on_event(core_event(None, type_, payload, id_=id, parent_id=parent_id))
        return 1
    return emit


def test_resolve_remembers_the_project_folder(chat, projekt):  # noqa: F811
    assert chat.ablage.angehaengt(KEY_SESSION)
    assert chat.ablage.projekt(KEY_SESSION) == str(projekt)
    again = ansichten.Ablage(chat.ablage.root.parents[2])           # a gateway restart reads the file
    assert again.angehaengt(KEY_SESSION) and again.projekt(KEY_SESSION) == str(projekt)
    assert not again.angehaengt("agent:main:telegram:dm:1:2")
    assert oct(os.stat(chat.ablage.root / "sitzungen.json").st_mode & 0o777) == "0o600"


def test_the_tool_answers_ok_and_id_and_the_event_reaches_the_stream(chat):
    answer = json.loads(ansichten.ausfuehren(tabelle(), inst=chat.inst, session_key=KEY_SESSION,
                                             tool_call_id="call_7", emit=_emit_into(chat)))
    assert answer["ok"] is True and ansichten.ID_RE.fullmatch(answer["id"]) and set(answer) == {"ok", "id"}
    status, events = sse(chat.port, chat.key, events_path(epoch=chat.buffers.get(KEY_SESSION).epoch), 1)
    assert status == 200
    ev = events[0]
    assert ev["type"] == "ansicht" and ev["id"] == answer["id"] and ev["parent_id"] == "call_7"
    assert ev["payload"]["ansicht"]["id"] == answer["id"] and ev["payload"]["ansicht"]["zeilen"][0] == ["a", 1.5, "ok"]
    stored = chat.ablage.lade(KEY_SESSION, answer["id"])
    assert stored["ansicht"] == ev["payload"]["ansicht"]
    path = chat.ablage.root / stored["session"] / (answer["id"] + ".json")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_an_invalid_call_shows_nothing_and_says_why(chat):
    answer = json.loads(ansichten.ausfuehren({"art": "tabelle"}, inst=chat.inst, session_key=KEY_SESSION,
                                             tool_call_id="c", emit=_emit_into(chat)))
    assert answer["error"].startswith("Ansicht nicht gezeigt. Aufruf: erwartet daten oder quelle")
    assert "ok" not in answer
    assert chat.buffers.get(KEY_SESSION).seq == 0                  # no event
    assert list(chat.ablage.root.glob("*/av_*.json")) == []


def test_a_session_the_board_never_attached_gets_no_tool(chat):
    answer = json.loads(ansichten.ausfuehren(tabelle(), session_key="agent:main:telegram:dm:9:9", tool_call_id="c"))
    assert answer == {"error": "ansicht_zeigen geht nur in einer Sitzung, die im Werkbank-Chat offen ist."}


def _snapshot(srv):
    status, body = call(srv.port, "GET", session_path("snapshot"), srv.key)
    assert status == 200
    return body["messages"]


def test_the_view_stands_at_its_tool_row_after_a_reload(chat):
    answer = json.loads(ansichten.ausfuehren(tabelle(), inst=chat.inst, session_key=KEY_SESSION,
                                             tool_call_id="call_7", emit=_emit_into(chat)))
    # the turn is written: assistant row with the call, the tool row with {ok, id}, the final text
    chat.host.messages = [
        {"id": 1, "role": "user", "content": "zeig", "timestamp": 1.0},
        {"id": 2, "role": "assistant", "content": "", "timestamp": 2.0,
         "tool_calls": [{"id": "call_7", "function": {"name": "ansicht_zeigen"}}]},
        {"id": 3, "role": "tool", "content": json.dumps(answer), "tool_call_id": "call_7",
         "tool_name": "ansicht_zeigen", "timestamp": 3.0},
        {"id": 4, "role": "assistant", "content": "Hier die Tabelle.", "timestamp": 4.0},
    ]
    messages = _snapshot(chat)
    tool = next(m for m in messages if m["role"] == "tool")
    assert tool["ansicht"]["id"] == answer["id"] and tool["ansicht"]["art"] == "tabelle"
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert not any(m.get("from_buffer") for m in messages)


def test_a_reload_during_the_turn_shows_the_view_from_the_store(chat):
    chat.host.messages = [{"id": 1, "role": "user", "content": "zeig", "timestamp": 1.0}]
    answer = json.loads(ansichten.ausfuehren(tabelle(), inst=chat.inst, session_key=KEY_SESSION,
                                             tool_call_id="call_8", emit=_emit_into(chat)))
    messages = _snapshot(chat)
    assert [m["role"] for m in messages] == ["user", "tool"]
    assert messages[1]["from_buffer"] is True and messages[1]["tool_call_id"] == "call_8"
    assert messages[1]["ansicht"]["id"] == answer["id"]


def test_a_failed_tool_row_shows_no_view(chat):
    chat.host.messages = [{"id": 3, "role": "tool", "content": json.dumps({"error": "x"}),
                           "tool_call_id": "c", "tool_name": "ansicht_zeigen"}]
    assert "ansicht" not in _snapshot(chat)[0]


def test_verfuegbar_only_for_attached_sessions(chat, monkeypatch):
    from plugins.platforms import werkbank as plugin
    rt = plugin.runtime()
    monkeypatch.setattr(rt, "instances", {"x": chat.inst})
    monkeypatch.setattr(ansichten, "_current", lambda: (KEY_SESSION, "c"))
    assert ansichten.verfuegbar() is True
    monkeypatch.setattr(ansichten, "_current", lambda: ("agent:main:telegram:dm:9:9", "c"))
    assert ansichten.verfuegbar() is False
    monkeypatch.setattr(ansichten, "_current", lambda: ("", ""))
    assert ansichten.verfuegbar() is False


def test_registered_in_the_werkbank_toolset_with_a_gate():
    seen = {}

    class Ctx:
        def register_tool(self, **kw):
            seen.update(kw)

    ansichten.registriere(Ctx())
    assert seen["name"] == "ansicht_zeigen" and seen["toolset"] == "werkbank"
    assert seen["check_fn"] is ansichten.verfuegbar and seen["schema"]["parameters"]["required"] == ["art"]
    assert seen["description"] == ansichten.beschreibung() == seen["schema"]["description"]
    from tools.registry import _NO_CACHE_CHECK_FNS
    assert ansichten.verfuegbar in _NO_CACHE_CHECK_FNS


def _tokens(text):
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return int(len(text) / 3.2) + 1          # German without a tokenizer: a cautious estimate


def test_the_description_is_short():
    text = ansichten.beschreibung()
    print(f"WB1471-TOKENS {_tokens(text)} chars={len(text)}")
    assert _tokens(text) <= 300


def test_a_started_plugin_holds_the_store_and_registers_the_tool(tmp_path):
    from plugins.platforms import werkbank as plugin
    seen = []

    class Ctx:
        plugin_id = "platforms/werkbank"

        def __init__(self):
            self.unload = []

        def register_hook(self, name, callback):
            pass

        def on_unload(self, callback):
            self.unload.append(callback)

        def register_tool(self, **kw):
            seen.append(kw["name"])

    ctx = Ctx()
    settings = SimpleNamespace(port=0, mirror_to_telegram=False, mirror_name="")
    srv = plugin.start(ctx, tmp_path, settings, host=FakeHost(), port=0)
    try:
        assert srv is not None and isinstance(srv.ansichten, ansichten.Ablage)
        assert seen == ["ansicht_zeigen"]
        status, _ = call(srv.port, "GET", f"/v1/resolve?session_id={SID}&projekt={tmp_path}",
                         srv._wanted.decode()[len("Bearer "):])
        assert status == 200 and srv.ansichten.projekt(KEY_SESSION) == str(tmp_path)
    finally:
        for fn in ctx.unload:
            fn()
