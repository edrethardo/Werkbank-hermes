"""Two starts at the same moment must end with ONE key (live finding 2026-10-14: the gateway
registered the plugin from two scopes at once; the listener kept one key, the file held the
other, and every board request got 401)."""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.platforms.werkbank import keyfile
import plugins.platforms.werkbank as wb


def test_parallel_ensure_key_returns_the_key_on_disk(tmp_path):
    for round_ in range(20):
        home = tmp_path / f"h{round_}"
        barrier = threading.Barrier(8)
        keys = []

        def go():
            barrier.wait()
            keys.append(keyfile.ensure_key(home))

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        on_disk = keyfile.key_path(home).read_text().strip()
        assert set(keys) == {on_disk}, round_
        assert not [p for p in keyfile.key_dir(home).iterdir() if p.name.startswith(".key.")]


def test_unusable_key_is_still_replaced(tmp_path):
    keyfile.key_dir(tmp_path).mkdir(parents=True, mode=0o700)
    keyfile.key_path(tmp_path).write_text("short\n")
    keyfile.key_path(tmp_path).chmod(0o600)
    key = keyfile.ensure_key(tmp_path)
    assert len(key) >= keyfile.MIN_KEY_CHARS
    assert keyfile.key_path(tmp_path).read_text().strip() == key


def test_register_twice_in_one_process_starts_one_server(tmp_path, monkeypatch):
    calls = []
    monkeypatch.delitem(__import__("sys").modules, wb.RUNTIME_MODULE, raising=False)
    monkeypatch.setattr(wb, "in_gateway_process", lambda: True)
    monkeypatch.setattr(wb, "plugin_home", lambda ctx: tmp_path)
    from plugins.platforms.werkbank import config
    monkeypatch.setattr(config, "read", lambda home: SimpleNamespace(enabled=True, problem=None))

    class Srv:
        closing = False            # a property on the real server, not a method
        port = 1

        def stop(self):
            self.closing = True

    def launch(ctx, home, settings):
        calls.append(home)
        return wb.Instance(Srv(), SimpleNamespace(on_event=lambda e: None), SimpleNamespace(), str(home.resolve()))

    monkeypatch.setattr(wb, "_launch", launch)
    ctx = SimpleNamespace(register_hook=lambda *a: None, on_unload=lambda cb: None)
    wb.register(ctx)
    wb.register(ctx)
    assert calls == [tmp_path]
    wb.runtime().instances[str(tmp_path.resolve())].server.stop()   # stopped: a later register starts again
    wb.register(ctx)
    assert calls == [tmp_path, tmp_path]
