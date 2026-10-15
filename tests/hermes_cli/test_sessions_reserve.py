"""``hermes sessions reserve``: a trusted launcher reserves an EMPTY session and gets its id (WB-1090).

The Werkbank sandbox handshake (WB-1064) binds a worker grant to a session id BEFORE any
model tool runs, then starts exactly that session with ``--resume <id>``. The id must come
from Hermes' own store (never a title, guess or model output), the insert must be exclusive
(never adopt an existing row) and the reserved row must be resumable while empty.

Real SessionDB in the conftest-isolated HERMES_HOME; never the live state.db, no gateway.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading

import pytest

from hermes_state import SessionDB

ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6,}$")


def _profile_store():
    """A store INSIDE the profile tree (a reservation must name its owning profile)."""
    from hermes_constants import get_default_hermes_root

    path = get_default_hermes_root() / "profiles" / "wbtest" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=_profile_store())
    yield d
    d.close()


def test_reserve_creates_empty_open_row(db):
    sid = db.reserve_session()
    assert ID_RE.match(sid)
    row = db.get_session(sid)
    assert row is not None
    assert row["source"] == "cli"
    assert row["ended_at"] is None
    assert db.message_count(sid) == 0


def test_reserve_never_adopts_an_existing_row(db, monkeypatch):
    taken = "20261015_120000_aaaaaa"
    db.create_session(taken, source="telegram", model="m-1")
    ids = iter([taken, "20261015_120000_bbbbbb"])
    import hermes_state_ids
    monkeypatch.setattr(hermes_state_ids, "new_session_id", lambda *a, **k: next(ids))
    sid = db.reserve_session()
    assert sid == "20261015_120000_bbbbbb"
    kept = db.get_session(taken)
    assert kept["source"] == "telegram" and kept["model"] == "m-1"


def test_reserve_gives_up_when_every_id_collides(db, monkeypatch):
    taken = "20261015_120000_aaaaaa"
    db.create_session(taken, source="cli")
    import hermes_state_ids
    monkeypatch.setattr(hermes_state_ids, "new_session_id", lambda *a, **k: taken)
    with pytest.raises(RuntimeError):
        db.reserve_session()


def test_concurrent_reservations_are_distinct(tmp_path):
    path = _profile_store()
    SessionDB(db_path=path).close()
    out, errors = [], []

    def worker():
        d = SessionDB(db_path=path)
        try:
            for _ in range(5):
                out.append(d.reserve_session())
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            d.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert len(out) == 20 and len(set(out)) == 20


def test_reserved_session_is_resumable_while_empty(db):
    from hermes_cli.oneshot import _load_resume_target

    sid = db.reserve_session()
    resolved, history, meta = _load_resume_target(db, sid)
    assert resolved == sid and history == [] and meta["id"] == sid


def _main(monkeypatch, capsys, argv):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(sys, "argv", ["hermes", *argv])
    try:
        main_mod.main()
        code = 0
    except SystemExit as e:
        code = e.code or 0
    return code, capsys.readouterr()


def test_cli_reserve_json_writes_the_profile_store(monkeypatch, capsys):
    from hermes_constants import get_hermes_home

    code, out = _main(monkeypatch, capsys, ["sessions", "reserve", "--json"])
    assert code == 0, out.err
    rep = json.loads(out.out)
    assert ID_RE.match(rep["session_id"])
    store = get_hermes_home() / "state.db"
    assert rep["state_db"] == str(store)
    con = sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
    try:  # exactly what Werkbank host_proof._known_to_hermes asks
        assert con.execute("SELECT 1 FROM sessions WHERE id = ?", (rep["session_id"],)).fetchone()
        assert con.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?",
                           (rep["session_id"],)).fetchone()[0] == 0
    finally:
        con.close()


def test_cli_reserve_plain_prints_only_the_id(monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, ["sessions", "reserve"])
    assert code == 0
    assert ID_RE.match(out.out.strip())


def test_reserve_refuses_a_store_outside_the_profile_tree(tmp_path):
    """No owning profile -> no reservation: a NULL-profile row cannot be attributed later."""
    d = SessionDB(db_path=tmp_path / "elsewhere" / "state.db")
    try:
        assert d._own_profile_name() is None
        with pytest.raises(RuntimeError, match="profile"):
            d.reserve_session()
    finally:
        d.close()
    con = sqlite3.connect(tmp_path / "elsewhere" / "state.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        con.close()


def test_reserve_row_names_its_profile(db):
    sid = db.reserve_session()
    assert db.get_session(sid)["profile_name"] == "wbtest"


def test_cli_reserve_without_profile_fails(monkeypatch, capsys):
    import hermes_state_sessions

    monkeypatch.setattr(hermes_state_sessions.SessionSessionsMixin, "_own_profile_name", lambda self: None)
    code, out = _main(monkeypatch, capsys, ["sessions", "reserve", "--json"])
    assert code != 0
    assert out.out.strip() == ""
    assert "profile" in out.err


def test_reserved_empty_session_survives_orphan_reap_and_empty_sweep(db):
    """Between ``reserve`` and ``--resume`` the row is empty; once something ends it (orphan
    reap, crash) the empty sweep must still leave it alone: the launcher already bound its id."""
    sid = db.reserve_session()
    other = db.create_session("20261015_120000_cccccc", source="cli")
    db.end_session(other, "test")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET ended_at = ?, end_reason = 'startup_orphan_reap' WHERE id = ?",
        (1.0, sid)))
    assert db.count_empty_sessions() == 1
    assert db.delete_empty_sessions() == 1
    assert db.get_session(sid) is not None
    assert db.get_session(other) is None
