"""Bilder im TUI-Transcript: ein Bildpfad wird zu einer Terminalsequenz.

Warum eigenes Modul und nicht ``agent/pet/render.py``: das Maskottchen ist ein FIXIERTES
Overlay, das Ink per ``position="absolute"`` an eine Ecke heftet und über
kitty-Unicode-Platzhalter (``U=1``) malt, damit Ink jede Zelle weiter vermessen kann. Ein
Transcript-Bild hat die umgekehrten Anforderungen: es gehört in den Textfluss und muss mit
ihm wegscrollen. Deshalb hier die DIREKTE Platzierung (``a=T`` ohne ``U``), die das Bild in
den Puffer schreibt.

Live gemessen (spikes/004): ein so geschriebenes Bild überlebt Inks Zell-Diff-Repaint und
scrollt mit dem Text. Das ist die Voraussetzung, auf der dieses Modul steht — ohne sie
würde der nächste Repaint das Bild zerschneiden.

SIXEL fehlt hier bewusst. Hermes' Encoder braucht dafür rund 35 s je Bildschirmfoto
(gemessen, spikes/003); ein Transcript, das eine halbe Minute steht, ist unbrauchbar. kitty
und iTerm2 sind im Kern base64 und kosten Millisekunden.
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Optional, Tuple

# Bildendungen, die wir zu zeigen versuchen. Alles andere bleibt ein Pfad im Text — ein PDF
# als Bild anzuzeigen ist nicht möglich, und ein stiller Fehlschlag wäre schlechter als der
# Pfad, den der Nutzer anklicken kann.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

# Obergrenze für die Anzeige. Ein Bildschirmfoto ist schnell 4K; als base64 wären das
# mehrere MB durch die PTY, und die Zeit dafür merkt der Nutzer. Auf Terminalbreite
# herunterzurechnen kostet nichts an Information, die in Textzellen ohnehin darstellbar wäre.
MAX_COLS = 80
MAX_ROWS = 24

# Nominelle Zellengröße in Pixeln, wie in agent/pet/render.py. Terminals melden ihre echte
# Zellengröße nur auf Anfrage (CSI 16 t), und darauf zu warten kann eine Pipe blockieren.
CELL_W = 8
CELL_H = 16


def _pil():
    """Pillow, oder None. Ohne Pillow bleibt der Pfad als Text stehen — kein Fehler."""
    try:
        from PIL import Image

        return Image
    except ImportError:
        return None


def image_cell_box(width: int, height: int,
                   max_cols: int = MAX_COLS, max_rows: int = MAX_ROWS) -> Tuple[int, int]:
    """Zellenrechteck (Spalten, Zeilen) für ein Bild dieser Pixelgröße, gedeckelt.

    ``max_cols`` ist die Breite des Terminals, das zusieht, nicht eine Konstante: auf einem
    Telefon hat ein Pane gut halb so viele Spalten wie ein Fenster am Rechner. Ein zu breit
    kodiertes Bild wird dort vom Emulator rechts abgeschnitten — gemessen: bei 32 Spalten
    blieben von 311904 Pixeln noch 52364, also ein Sechstel.

    Das Seitenverhältnis bleibt erhalten: wird die Breite gedeckelt, sinkt die Höhe im
    gleichen Verhältnis mit (sonst quetscht das Terminal das Bild in die Breite).
    """
    max_cols = max(1, max_cols)
    max_rows = max(1, max_rows)

    cols = max(1, -(-width // CELL_W))
    rows = max(1, -(-height // CELL_H))

    scale = min(1.0, max_cols / cols, max_rows / rows)
    if scale < 1.0:
        # Runden, nicht abschneiden: `int()` machte aus 2.5 Zeilen 2, und das Terminal
        # streckt das Bild dann auf ein falsches Seitenverhältnis. Der Deckel bleibt
        # hart — Aufrunden darf nie über die Terminalmaße hinausschießen.
        cols = max(1, min(max_cols, int(cols * scale + 0.5)))
        rows = max(1, min(max_rows, int(rows * scale + 0.5)))

    return cols, rows


def _fit(image, cols: int, rows: int, Image):
    """Bild auf das Zellenrechteck herunterrechnen; kleinere Bilder bleiben unangetastet."""
    target_w, target_h = cols * CELL_W, rows * CELL_H
    if image.width <= target_w and image.height <= target_h:
        return image
    # Pillow ≥ 10 hat LANCZOS nach Image.Resampling verschoben, ältere kennen nur das Attribut.
    resample = getattr(Image, "LANCZOS", None) or Image.Resampling.LANCZOS
    scale = min(target_w / image.width, target_h / image.height)
    return image.resize((max(1, int(image.width * scale)),
                         max(1, int(image.height * scale))), resample)


def _png_b64(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def encode_kitty(payload: str, cols: int, rows: int) -> str:
    """kitty-Grafik, direkt platziert (kein ``U=1``), in 4096-Byte-Stücken.

    ``q=2`` unterdrückt die Bestätigungsantworten des Terminals — die landen sonst in Inks
    stdin und erscheinen als Buchstabensalat in der Eingabezeile.
    """
    chunks = [payload[i:i + 4096] for i in range(0, len(payload), 4096)] or [""]
    out = []
    for i, chunk in enumerate(chunks):
        more = 1 if i < len(chunks) - 1 else 0
        if i == 0:
            head = f"a=T,f=100,q=2,c={cols},r={rows},m={more}"
        else:
            head = f"m={more}"
        out.append(f"\x1b_G{head};{chunk}\x1b\\")
    return "".join(out)


def encode_iterm(payload: str, cols: int, rows: int) -> str:
    """iTerm2 Inline Image. ``size=`` ist PFLICHT — ohne die Angabe verwerfen Terminals
    (und xterm.js) die Sequenz still, ohne jede Fehlermeldung."""
    raw_len = len(base64.b64decode(payload))
    return (f"\x1b]1337;File=inline=1;size={raw_len};"
            f"preserveAspectRatio=1;width={cols};height={rows}:{payload}\x07")


def render_image(path: str, protocol: str,
                 max_cols: int = MAX_COLS, max_rows: int = MAX_ROWS) -> Optional[Tuple[str, int, int]]:
    """``(sequenz, cols, rows)`` für ein Bild, oder None wenn es nicht darstellbar ist.

    ``max_cols``/``max_rows`` sind die Maße des Terminals, das zusieht — der Renderer kennt
    sie, dieser Prozess nicht (bei einer angehängten Sitzung läuft er woanders). Ohne sie
    wird auf einem schmalen Pane rechts abgeschnitten.

    None heißt immer: der Aufrufer soll den Pfad als Text stehen lassen. Gründe dafür sind
    gewöhnlich (kein Pillow, Datei weg, kein bildfähiges Terminal) und kein Fehlerfall.
    """
    if protocol not in {"kitty", "iterm"}:
        return None
    Image = _pil()
    if Image is None:
        return None

    p = Path(path).expanduser()
    if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
        return None

    try:
        with Image.open(p) as im:
            im.load()
            frame = im.convert("RGBA")
    except Exception:
        # Kaputte oder abgeschnittene Datei: Pfad stehen lassen, nicht die Ausgabe stören.
        return None

    cols, rows = image_cell_box(frame.width, frame.height, max_cols, max_rows)
    payload = _png_b64(_fit(frame, cols, rows, Image))
    seq = encode_kitty(payload, cols, rows) if protocol == "kitty" else encode_iterm(payload, cols, rows)
    return seq, cols, rows


def graphics_protocol() -> str:
    """``kitty`` | ``iterm`` | ``none`` für das Terminal dieser Sitzung.

    Nutzt die vorhandene Pet-Erkennung und übersetzt deren ``sixel``/``unicode`` zu ``none``:
    Halbblöcke sind für ein Maskottchen ein sinnvoller Ersatz, für ein Bildschirmfoto nicht,
    und SIXEL ist zu teuer (siehe Modul-Docstring).

    ``HERMES_TUI_IMAGE_PROTOCOL`` überschreibt das Ergebnis — nötig, weil die Erkennung nur
    Umgebungsvariablen liest und ein Terminal hinter tmux/ssh sich nicht immer zu erkennen
    gibt.
    """
    forced = os.environ.get("HERMES_TUI_IMAGE_PROTOCOL", "").strip().lower()
    if forced in {"kitty", "iterm", "none"}:
        return forced

    try:
        from agent.pet.render import detect_terminal_graphics
    except Exception:
        return "none"
    detected = detect_terminal_graphics()
    return detected if detected in {"kitty", "iterm"} else "none"
