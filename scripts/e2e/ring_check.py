"""Wie viel kitty-Bildsequenz steckt noch im Replay-Ring des Panes?

Der Punkt, an dem meine Sonde unschluessig wurde: sie behauptete, den 512-KiB-Ring
leergerollt zu haben, ohne das je zu pruefen. Diese Messung liest den Ring direkt.
"""
import asyncio
import json
import sys

import websockets

PANE = sys.argv[1]
URI = "ws://127.0.0.1:9161/ws?token=testtoken123"


async def main():
    async with websockets.connect(URI, max_size=None) as ws:
        await ws.send(json.dumps({"type": "attach", "pane": PANE}))
        total = b""
        # Der Ring kommt als EIN Schwung direkt nach `opened`. Danach laeuft die
        # TUI weiter und sendet endlos Frames — deshalb wird nach dem ersten
        # Byte-Schwung abgebrochen, nicht auf Stille gewartet (das kam nie).
        first = True
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, bytes):
                total += msg
                if first:
                    first = False
                    # Ein einzelner Replay-Schwung reicht; alles Weitere ist live.
                    break
        print("Replay-Bytes:          ", len(total))
        print("kitty-Chunks (ESC _G): ", total.count(b"\x1b_G"))
        print("Bildanfaenge (a=T,f=100):", total.count(b"a=T,f=100"))


asyncio.run(asyncio.wait_for(main(), timeout=40))
