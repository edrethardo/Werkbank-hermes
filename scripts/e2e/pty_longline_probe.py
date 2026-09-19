"""Überlebt eine 20-KB-Sequenz OHNE Zeilenumbruch die PTY-Line-Discipline?

Die Bildsequenz ist eine einzige sehr lange "Zeile". Der Text daneben hat
Zeilenumbrüche und kommt an — das ist genau das Muster, das ein PTY-Puffer-
oder Line-Discipline-Limit erzeugt.

Gemessen wird unabhängig von Browser, Ink und Modell: ein PTY-Paar, die ECHTE
Sequenz in den Slave, alles vom Master lesen, Bytes zählen.

    .venv/bin/python scripts/e2e/pty_longline_probe.py
"""
from __future__ import annotations

import os
import pty
import select
import sys
import time
from pathlib import Path


def drain(master: int, budget: float = 3.0) -> bytes:
    out = bytearray()
    end = time.time() + budget
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.15)
        if not r:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        out.extend(chunk)
        end = time.time() + 0.4
    return bytes(out)


def main() -> int:
    seq = Path("/tmp/probe_seq_kitty.txt").read_bytes()
    longest = max(len(line) for line in seq.split(b"\n"))
    print(f"Sequenz            : {len(seq)} Bytes, längste Zeile {longest}")

    master, slave = pty.openpty()

    # Schreiben wie ein Programm auf seinem stdout.
    with os.fdopen(os.dup(slave), "wb", buffering=0) as w:
        w.write(b"\r\nVORHER\r\n")
        w.write(seq)
        w.write(b"\r\nNACHHER\r\n")

    got = drain(master)
    os.close(slave)
    os.close(master)

    print(f"Gelesen            : {len(got)} Bytes")
    print(f"'VORHER' da        : {b'VORHER' in got}")
    print(f"'NACHHER' da       : {b'NACHHER' in got}")
    print(f"Sequenz vollständig: {seq in got}")

    if seq not in got:
        # Wo bricht sie ab?
        head = seq[:64]
        i = got.find(head)
        print(f"Sequenzanfang bei  : {i}")
        if i >= 0:
            tail = got[i:]
            common = 0
            for a, b in zip(seq, tail):
                if a != b:
                    break
                common += 1
            print(f"identisch bis      : {common} von {len(seq)} Bytes")
            print(f"danach im Strom    : {tail[common:common + 60]!r}")

    return 0 if seq in got else 1


if __name__ == "__main__":
    sys.exit(main())
