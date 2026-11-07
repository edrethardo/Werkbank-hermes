"""WB-1471: shared helpers for the chat-view tests (core and one test file per block)."""

from __future__ import annotations

import pytest

from werkbank_testkit import KEY_SESSION

from plugins.platforms.werkbank import ansichten, medien, outbox as outbox_mod

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def tabelle(**extra):
    args = {"art": "tabelle", "titel": "Läufe",
            "daten": {"spalten": [{"id": "name", "titel": "Name"}, {"id": "dauer", "typ": "zahl"},
                                  {"id": "stand", "typ": "status"}],
                      "zeilen": [["a", 1.5, "ok"], {"name": "b", "dauer": 2, "stand": "fehler"}]}}
    args.update(extra)
    return args


def fehler_von(args, aufl=None):
    view, fehler = ansichten.pruefen(args, aufl)
    assert view is None, view
    return fehler


def aufl(projekt=None, box=None, key=KEY_SESSION):
    return ansichten.Aufloeser(key, str(projekt) if projekt else None, box, None)


@pytest.fixture
def projekt(tmp_path):
    root = tmp_path / "projekt"
    (root / "daten").mkdir(parents=True)
    (root / "daten" / "tab.csv").write_text("name,dauer\nx,1\ny,2\n", encoding="utf-8")
    (root / "bild.png").write_bytes(PNG)
    outside = tmp_path / "geheim"
    outside.mkdir()
    (outside / "x.csv").write_text("a\n1\n", encoding="utf-8")
    return root


@pytest.fixture
def box(tmp_path):
    return outbox_mod.Outbox(tmp_path / "home", werkstatt=medien.Werkstatt(ffmpeg=None, ffprobe=None))
