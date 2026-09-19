"""Transcript-Bilder: Encoder-Verhalten und Protokollwahl.

Invarianten, keine Momentaufnahmen: geprüft wird, wie die Teile ZUSAMMENHÄNGEN (Sequenz
passt zum Protokoll, Deckelung greift, Nicht-Bilder werden abgelehnt), nicht ob eine
bestimmte Byte-Länge herauskommt.
"""

from __future__ import annotations

import base64

import pytest

from tui_gateway.transcript_images import (
    MAX_COLS,
    MAX_ROWS,
    encode_iterm,
    encode_kitty,
    graphics_protocol,
    image_cell_box,
    render_image,
)

PIL = pytest.importorskip("PIL.Image", reason="Pillow fehlt — der Encoder ist dann absichtlich inaktiv")


def _png(tmp_path, name="shot.png", size=(120, 90), color=(255, 0, 255, 255)):
    from PIL import Image

    p = tmp_path / name
    Image.new("RGBA", size, color).save(p)
    return p


def test_iterm_sequence_carries_size_or_terminals_drop_it(tmp_path):
    """``size=`` ist bei IIP Pflicht — fehlt es, verwerfen Terminals die Sequenz STILL.

    Genau dieser Fehler kostete beim Bau eine Messrunde: kein Fehler, kein Log, nur nichts.
    """
    payload = base64.b64encode(_png(tmp_path).read_bytes()).decode()
    seq = encode_iterm(payload, 10, 5)

    assert seq.startswith("\x1b]1337;File=")
    assert ";size=" in seq
    assert seq.endswith("\x07")
    # Die angegebene Größe muss die der ROHBYTES sein, nicht die der base64-Zeichen.
    declared = int(seq.split(";size=")[1].split(";")[0])
    assert declared == len(base64.b64decode(payload))


def test_kitty_chunks_stay_within_protocol_limit_and_mark_continuation(tmp_path):
    """Lange Nutzlasten werden gestückelt; nur das letzte Stück trägt ``m=0``."""
    payload = "A" * 10000
    seq = encode_kitty(payload, 10, 5)
    frames = [f for f in seq.split("\x1b_G") if f]

    assert len(frames) >= 3, "10000 Zeichen müssen in mehrere Stücke zerfallen"
    for f in frames:
        head, _, body = f.partition(";")
        assert len(body.replace("\x1b\\", "")) <= 4096
    assert "m=1" in frames[0]
    assert "m=0" in frames[-1]
    # q=2 unterdrückt die Terminal-Antworten; ohne das landen sie in Inks stdin.
    assert "q=2" in frames[0]


def test_cell_box_is_capped_so_a_4k_screenshot_cannot_flood_the_pty():
    """Ein Bildschirmfoto darf das Transcript nicht sprengen."""
    cols, rows = image_cell_box(3840, 2160)

    assert cols <= MAX_COLS and rows <= MAX_ROWS
    # Kleine Bilder bleiben klein — die Deckelung ist eine Obergrenze, keine feste Größe.
    small_cols, small_rows = image_cell_box(16, 16)
    assert small_cols < cols and small_rows < rows


def test_a_narrow_terminal_gets_an_image_that_fits_it(tmp_path):
    """Die Breite des ZUSEHENDEN Terminals deckelt das Bild, nicht eine Konstante.

    Der Grund ist das Telefon: ein i3-Pane dort hat gut 32 Spalten. Ein auf 80 Spalten
    kodiertes Bild wird vom Emulator rechts abgeschnitten — gemessen blieben von 311904
    gemalten Pixeln noch 52364. Das Gateway kann die Maße nicht selbst kennen (bei einer
    angehängten Sitzung läuft es auf einer anderen Maschine), also reisen sie mit.
    """
    shot = _png(tmp_path, size=(2000, 1500))

    weit = render_image(str(shot), "kitty")
    schmal = render_image(str(shot), "kitty", max_cols=32, max_rows=20)

    assert weit is not None and schmal is not None
    assert schmal[1] <= 32 and schmal[2] <= 20
    # Und es ist wirklich schmaler geworden, nicht bloß anders deklariert.
    assert schmal[1] < weit[1]
    assert len(schmal[0]) < len(weit[0])


def test_narrow_cap_keeps_the_aspect_ratio(tmp_path):
    """Nur die Breite zu deckeln würde das Bild im Terminal in die Breite quetschen."""
    breit, hoch = image_cell_box(1600, 400)
    schmal_breit, schmal_hoch = image_cell_box(1600, 400, max_cols=20, max_rows=24)

    assert schmal_breit <= 20
    # Relativ vergleichen, nicht absolut: bei wenigen Zeilen dominiert die Ganzzahl-
    # Rundung (2 statt 2.5 Zeilen sind schon 20 % Abweichung), ohne dass das Bild
    # gequetscht wäre. Ein Viertel Toleranz fängt die Rundung, nicht aber den Fehler,
    # den der Test sucht: nur die Breite zu deckeln ergäbe hier Faktor 4.
    assert abs(schmal_breit / schmal_hoch - breit / hoch) / (breit / hoch) < 0.25


def test_render_downscales_a_large_image_instead_of_sending_it_whole(tmp_path):
    """Die Nutzlast eines großen Bildes muss kleiner sein als das Original."""
    big = _png(tmp_path, "big.png", size=(2000, 1500))
    rendered = render_image(str(big), "kitty")

    assert rendered is not None
    seq, cols, rows = rendered
    assert cols <= MAX_COLS and rows <= MAX_ROWS
    # Roh wären 2000x1500 RGBA deutlich über einem MB; die Sequenz muss klar darunter liegen.
    assert len(seq) < 400_000


@pytest.mark.parametrize("protocol,marker", [("kitty", "\x1b_G"), ("iterm", "\x1b]1337;")])
def test_render_matches_the_requested_protocol(tmp_path, protocol, marker):
    rendered = render_image(str(_png(tmp_path)), protocol)

    assert rendered is not None
    assert rendered[0].startswith(marker)


@pytest.mark.parametrize("case", ["missing", "not_an_image", "unknown_protocol"])
def test_unrenderable_input_returns_none_rather_than_raising(tmp_path, case):
    """None heißt: Pfad als Text stehen lassen. Diese Fälle sind normal, keine Fehler."""
    if case == "missing":
        assert render_image(str(tmp_path / "weg.png"), "kitty") is None
    elif case == "not_an_image":
        doc = tmp_path / "bericht.pdf"
        doc.write_bytes(b"%PDF-1.4 nicht wirklich")
        assert render_image(str(doc), "kitty") is None
    else:
        assert render_image(str(_png(tmp_path)), "sixel") is None


def test_corrupt_image_is_declined_quietly(tmp_path):
    """Eine abgeschnittene Datei darf die Ausgabe nicht stören."""
    broken = tmp_path / "kaputt.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    assert render_image(str(broken), "kitty") is None


def test_protocol_override_wins_over_detection(monkeypatch):
    """Hinter tmux/ssh erkennt die Umgebungsvariablen-Heuristik nichts — der Override ist
    dort der einzige Weg, und er muss auch greifen, wenn die Erkennung 'none' sagt."""
    monkeypatch.setenv("HERMES_TUI_IMAGE_PROTOCOL", "kitty")
    assert graphics_protocol() == "kitty"

    monkeypatch.setenv("HERMES_TUI_IMAGE_PROTOCOL", "none")
    assert graphics_protocol() == "none"


def test_sixel_and_unicode_detection_map_to_none(monkeypatch):
    """SIXEL ist zu teuer (~35 s je Screenshot) und Halbblöcke taugen für ein Foto nicht —
    beides muss als 'kein Bild' durchkommen, nicht als Protokoll."""
    monkeypatch.delenv("HERMES_TUI_IMAGE_PROTOCOL", raising=False)
    import tui_gateway.transcript_images as ti

    for detected in ("sixel", "unicode"):
        monkeypatch.setattr(ti, "graphics_protocol", ti.graphics_protocol)
        monkeypatch.setitem(
            __import__("sys").modules,
            "agent.pet.render",
            type("m", (), {"detect_terminal_graphics": staticmethod(lambda d=detected: d)}),
        )
        assert ti.graphics_protocol() == "none"
