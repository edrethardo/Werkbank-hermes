"""Bundled plugin ``key-prompt``: an API key typed on a dashboard page, never in the chat.

Acceptance encoded here (all with fake key strings, never a real credential):

* ``request_api_key`` hands the agent a one-shot link and nothing else; unknown providers and a
  missing ``dashboard.public_url`` are refused,
* the request record on disk carries no secret and is private (0700 dir, 0600 file),
* ``/p/schluessel/?r=<id>`` shows provider name, purpose, a masked field and the profile list,
* submitting stores the key through ``hermes_cli.auth_commands.add_api_key_credential`` into the
  chosen profile's credential pool; the answer and the agent's status never contain the value,
* the value is registered for output redaction,
* single use, expiry, cancel, session binding, CSRF/content-type/origin checks, no value in the
  query string, rate limits, value-free audit lines,
* the page sits behind the dashboard auth gate like every ``/p/<slug>`` page.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "key-prompt"
FAKE = "fake-nvapi-TEST-0000-not-a-real-key"


def _load_plugin():
    name = "hermes_plugins.key_prompt_under_test"
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(PLUGIN)]
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    sys.modules[name] = pkg
    mods = {}
    for sub in ("store", "keys", "tools", "page"):
        spec = importlib.util.spec_from_file_location(f"{name}.{sub}", PLUGIN / f"{sub}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        mods[sub] = mod
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN)])
    init = importlib.util.module_from_spec(spec)
    sys.modules[name] = init
    spec.loader.exec_module(init)
    mods["init"] = init
    return mods


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    work = home / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("{}\n")
    (work / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    return home


@pytest.fixture
def kp(home):
    mods = _load_plugin()
    mods["tools"].configure("https://dash.example", 300)
    mods["page"]._RATE.clear()
    yield mods
    mods["tools"].configure("", 300)


@pytest.fixture
def client(kp):
    app = FastAPI()
    app.include_router(kp["page"].build_router(), prefix="/p/schluessel")
    return TestClient(app, base_url="https://dash.example")


def _request(kp, provider="nvidia", session="sess-A", **extra) -> dict:
    out = kp["tools"].request_api_key({"provider": provider, **extra}, session_id=session)
    return json.loads(out)


def _rid(res: dict) -> str:
    return res["link"].split("?r=", 1)[1]


def _csrf(client, rid) -> str:
    html = client.get(f"/p/schluessel/?r={rid}").text
    marker = 'const csrf = "'
    return html.split(marker, 1)[1].split('"', 1)[0]


def _submit(client, rid, csrf, **body):
    payload = {"r": rid, "csrf": csrf, "op": "save", "value": FAKE, "profile": "default", **body}
    return client.post("/p/schluessel/submit", json=payload)


def _pool(home: Path, provider="nvidia") -> list:
    data = json.loads((home / "auth.json").read_text())
    return (data.get("credential_pool") or {}).get(provider) or []


def _status(kp, rid, session="sess-A") -> dict:
    return json.loads(kp["tools"].api_key_request_status({"request_id": rid}, session_id=session))


# --- (a) the agent's tool -------------------------------------------------------------------------

def test_request_returns_only_a_one_shot_link(kp):
    res = _request(kp, label="nim", purpose="NVIDIA NIM für Delegation")
    assert res["link"].startswith("https://dash.example/p/schluessel/?r=")
    assert res["expires_in"] == 300
    assert set(res) <= {"link", "request_id", "expires_in", "instructions"}
    rid = _rid(res)
    assert kp["store"].valid_request_id(rid) and len(rid) >= 20


def test_request_refuses_unknown_provider_and_missing_public_url(kp):
    assert "error" in _request(kp, provider="no-such-provider")
    kp["tools"].configure("", 300)
    assert "error" in _request(kp)


def test_request_needs_a_session(kp):
    out = json.loads(kp["tools"].request_api_key({"provider": "nvidia"}))
    assert "error" in out


def test_allowlist_is_the_api_key_rows_of_the_provider_registry(kp):
    from hermes_cli.auth import PROVIDER_REGISTRY
    allowed = kp["keys"].allowed_providers()
    assert allowed["nvidia"] == PROVIDER_REGISTRY["nvidia"].name
    assert all(PROVIDER_REGISTRY[p].auth_type == "api_key" for p in allowed if p in PROVIDER_REGISTRY)
    oauth_only = [p for p, c in PROVIDER_REGISTRY.items() if c.auth_type != "api_key"]
    assert oauth_only and not set(oauth_only) & set(allowed)


def test_record_on_disk_is_private_and_secret_free(kp, client, home):
    rid = _rid(_request(kp))
    path = kp["store"].request_path(rid)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert _submit(client, rid, _csrf(client, rid)).status_code == 200
    assert FAKE not in path.read_text()


# --- (b) the page ---------------------------------------------------------------------------------

def test_page_shows_provider_purpose_masked_field_and_profiles(kp, client):
    rid = _rid(_request(kp, purpose="Für den Subagenten <b>"))
    resp = client.get(f"/p/schluessel/?r={rid}")
    assert resp.status_code == 200
    html = resp.text
    assert "NVIDIA NIM" in html
    assert "Für den Subagenten &lt;b&gt;" in html  # escaped
    assert 'type="password"' in html
    assert '<option value="default" selected' in html and '<option value="work"' in html
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-frame-options"] == "DENY"


def test_page_for_unknown_or_bad_id_says_gone(kp, client):
    for rid in ("x" * 30, "../../etc", ""):
        html = client.get("/p/schluessel/", params={"r": rid}).text
        assert "abgelaufen" in html and 'type="password"' not in html


# --- (c)/(d) storing, status, redaction -----------------------------------------------------------

def test_submit_stores_in_default_pool_and_never_echoes_the_value(kp, client, home):
    rid = _rid(_request(kp, label="nim-dashboard"))
    resp = _submit(client, rid, _csrf(client, rid))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "stored", "label": "nim-dashboard"}
    assert FAKE not in resp.text
    [entry] = _pool(home)
    assert entry["access_token"] == FAKE and entry["label"] == "nim-dashboard"
    assert entry["source"] == "manual" and entry["auth_type"] == "api_key"

    st = _status(kp, rid)
    assert st["status"] == "stored" and st["label"] == "nim-dashboard"
    assert st["provider"] == "nvidia" and st["profile"] == "default"
    assert FAKE not in json.dumps(st)
    # the page also never shows it again
    assert FAKE not in client.get(f"/p/schluessel/?r={rid}").text


def test_stored_value_is_registered_for_redaction(kp, client):
    from agent.redact import clear_vault_redaction_values, redact_sensitive_text
    clear_vault_redaction_values()
    rid = _rid(_request(kp))
    _submit(client, rid, _csrf(client, rid))
    clear_vault_redaction_values()  # the gateway is a different process: status re-registers
    assert _status(kp, rid)["status"] == "stored"
    assert FAKE not in redact_sensitive_text(f"echo {FAKE}")


def test_submit_into_a_named_profile(kp, client, home):
    rid = _rid(_request(kp))
    assert _submit(client, rid, _csrf(client, rid), profile="work").status_code == 200
    assert _pool(home) == []
    work = json.loads((home / "profiles" / "work" / "auth.json").read_text())
    assert [e["access_token"] for e in work["credential_pool"]["nvidia"]] == [FAKE]
    assert _status(kp, rid)["profile"] == "work"


def test_unknown_profile_is_refused_and_request_stays_usable(kp, client, home):
    rid = _rid(_request(kp))
    csrf = _csrf(client, rid)
    assert _submit(client, rid, csrf, profile="nope").status_code == 400
    assert _status(kp, rid)["status"] == "pending"
    assert _submit(client, rid, csrf).status_code == 200


def test_empty_value_is_refused(kp, client, home):
    rid = _rid(_request(kp))
    assert _submit(client, rid, _csrf(client, rid), value="   ").status_code == 400
    assert _pool(home) == []


def test_single_use(kp, client, home):
    rid = _rid(_request(kp))
    csrf = _csrf(client, rid)
    assert _submit(client, rid, csrf).status_code == 200
    again = _submit(client, rid, csrf, value="fake-second")
    assert again.status_code == 410
    assert [e["access_token"] for e in _pool(home)] == [FAKE]


def test_cancel(kp, client, home):
    rid = _rid(_request(kp))
    resp = _submit(client, rid, _csrf(client, rid), op="cancel", value="")
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert _status(kp, rid)["status"] == "cancelled"
    assert _submit(client, rid, "x").status_code == 410
    assert _pool(home) == []


def test_expiry(kp, client, home, monkeypatch):
    rid = _rid(_request(kp))
    csrf = _csrf(client, rid)
    real = kp["store"]._now
    monkeypatch.setattr(kp["store"], "_now", lambda: real() + 301)
    assert _status(kp, rid)["status"] == "expired"
    assert _submit(client, rid, csrf).status_code == 410
    assert "abgelaufen" in client.get(f"/p/schluessel/?r={rid}").text
    assert _pool(home) == []


def test_status_is_bound_to_the_requesting_session(kp):
    rid = _rid(_request(kp, session="sess-A"))
    assert "error" in _status(kp, rid, session="sess-B")
    assert _status(kp, rid, session="sess-A")["status"] == "pending"


def test_status_wait_returns_once_answered(kp, client, monkeypatch):
    rid = _rid(_request(kp))
    _submit(client, rid, _csrf(client, rid))
    out = json.loads(kp["tools"].api_key_request_status({"request_id": rid, "wait_seconds": 30},
                                                        session_id="sess-A"))
    assert out["status"] == "stored"


# --- security edges -------------------------------------------------------------------------------

def test_wrong_csrf_is_refused(kp, client, home):
    rid = _rid(_request(kp))
    assert _submit(client, rid, "not-the-token").status_code == 403
    assert _pool(home) == []


def test_repeated_bad_csrf_burns_the_request(kp, client, home):
    rid = _rid(_request(kp))
    csrf = _csrf(client, rid)
    for _ in range(kp["page"].MAX_BAD_ATTEMPTS):
        _submit(client, rid, "wrong")
    assert _submit(client, rid, csrf).status_code == 410
    assert _status(kp, rid)["status"] == "cancelled"


def test_form_post_and_cross_site_origin_are_refused(kp, client, home):
    rid = _rid(_request(kp))
    csrf = _csrf(client, rid)
    form = client.post("/p/schluessel/submit",
                       data={"r": rid, "csrf": csrf, "op": "save", "value": FAKE})
    assert form.status_code == 415
    evil = client.post("/p/schluessel/submit", headers={"Origin": "https://evil.example"},
                       json={"r": rid, "csrf": csrf, "op": "save", "value": FAKE})
    assert evil.status_code == 403
    site = client.post("/p/schluessel/submit", headers={"Sec-Fetch-Site": "cross-site"},
                       json={"r": rid, "csrf": csrf, "op": "save", "value": FAKE})
    assert site.status_code == 403
    assert _pool(home) == []


def test_value_in_query_string_is_refused(kp, client, home):
    rid = _rid(_request(kp))
    csrf = _csrf(client, rid)
    resp = client.post(f"/p/schluessel/submit?value={FAKE}",
                       json={"r": rid, "csrf": csrf, "op": "save", "value": FAKE})
    assert resp.status_code == 400
    assert _pool(home) == []


def test_post_rate_limit(kp, client):
    rid = _rid(_request(kp))
    codes = [_submit(client, rid, "wrong").status_code for _ in range(kp["page"].POST_LIMIT + 1)]
    assert codes[-1] == 429


def test_creation_rate_limit_per_session(kp):
    for _ in range(kp["store"].MAX_PENDING_PER_SESSION):
        assert "link" in _request(kp, session="sess-R")
    assert "error" in _request(kp, session="sess-R")
    assert "link" in _request(kp, session="sess-other")


def test_audit_lines_never_contain_the_value(kp, client, home):
    rid = _rid(_request(kp, label="nim"))
    _submit(client, rid, _csrf(client, rid))
    rid2 = _rid(_request(kp))
    _submit(client, rid2, _csrf(client, rid2), op="cancel", value="")
    log = kp["store"].audit_path().read_text()
    events = [json.loads(line)["event"] for line in log.splitlines()]
    assert events == ["created", "stored", "created", "cancelled"]
    assert FAKE not in log and rid not in log  # only an id prefix
    assert stat.S_IMODE(os.stat(kp["store"].audit_path()).st_mode) == 0o600


# --- wiring ---------------------------------------------------------------------------------------

class _Ctx:
    def __init__(self, config=None):
        self.pages, self.tools, self._config = [], [], config or {}
        self.profile_name = "default"

    def register_dashboard_page(self, slug, title, router):
        self.pages.append((slug, title, router))

    def register_tool(self, **kw):
        self.tools.append(kw)

    def get_config(self, key, default=None):
        return self._config.get(key, default)


def test_register_wires_page_and_tools(kp):
    ctx = _Ctx({"public_url": "https://dash.example/"})
    kp["init"].register(ctx)
    assert [p[0] for p in ctx.pages] == ["schluessel"]
    names = {t["name"] for t in ctx.tools}
    assert names == {"request_api_key", "api_key_request_status"}
    schema = next(t["schema"] for t in ctx.tools if t["name"] == "request_api_key")
    assert schema["parameters"]["required"] == ["provider"]
    # the agent can never pass a key value through the tool
    assert set(schema["parameters"]["properties"]) == {"provider", "label", "purpose"}


def test_page_is_behind_the_dashboard_gate(kp, monkeypatch, tmp_path):
    from hermes_cli import dashboard_pages as dp
    import hermes_cli.web_server as ws
    dp.clear_pages()
    try:
        app = FastAPI()
        app.state.auth_required = True
        monkeypatch.setattr(ws.app.state, "auth_required", True, raising=False)
        dp.register_page(dp.DashboardPage(slug="schluessel", title="Schlüssel",
                                          router=kp["page"].build_router(), plugin="key-prompt"))
        dp.mount_pages(app)
        rid = _rid(_request(kp))
        c = TestClient(app)
        assert c.get(f"/p/schluessel/?r={rid}").status_code == 401
        assert c.post("/p/schluessel/submit", json={"r": rid}).status_code == 401
    finally:
        dp.clear_pages()


def test_bundled_plugin_is_opt_in_and_loads_through_the_plugin_manager(home):
    """Real discovery: off by default; once in ``plugins.enabled`` both tools and the page exist."""
    import hermes_yaml as yaml
    from hermes_cli import dashboard_pages as dp
    from hermes_cli import plugins as pmod
    from tools.registry import registry

    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    assert "key-prompt" in mgr._plugins and not mgr._plugins["key-prompt"].enabled

    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["key-prompt"]},
        "dashboard": {"public_url": "https://dash.example"}}))
    dp.clear_pages()
    mgr = pmod.PluginManager()
    try:
        mgr.discover_and_load()
        loaded = mgr._plugins["key-prompt"]
        assert loaded.enabled, loaded.error
        assert dp.get_page("schluessel") is not None
        names = {"request_api_key", "api_key_request_status"}
        assert all(registry.get_entry(n, scope=mgr.scope_key) is not None for n in names)
    finally:
        mgr.unload_all() if hasattr(mgr, "unload_all") else None
        dp.clear_pages()
