# Hermes plugin `werkbank`

The Werkbank board's window into the Hermes gateway session of a project (WB-1128): an ordered
event stream with replay, a snapshot, sending and stopping — over HTTP on `127.0.0.1`, protected by a
key. Protocol: [`PROTOCOL.md`](PROTOCOL.md).

It is a bundled platform plugin **without** a messaging adapter: it registers no platform, only the
core hook `on_gateway_session_event` (`gateway/session_events.py`, WB-1126) and its own server. It
starts only inside `hermes gateway`; the CLI, the TUI and `hermes plugins` load it and do nothing.

## Configuration (profile `config.yaml`)

```yaml
plugins:
  entries:
    werkbank:                   # read by this plugin AND by the board (bruecke.read_port)
      enabled: true             # default false: without it no port is opened
      port: 8767                # default 8767; NOT 8765 (the board's own port, refused)
      mirror_to_telegram: true  # default true: "aus Werkbank: <text>" line in the session's topic
    platforms/werkbank:         # the core's gate for this plugin (its plugin id)
      allow_gateway_injection: true   # needed for sending AND stopping
```

Why two entries: the core checks `allow_gateway_injection` under the plugin's **id**, and a bundled
platform plugin's id is its path, `platforms/werkbank` (`hermes_cli/plugins_manifest.manifest_key`).
The plugin's own settings (and the board's port lookup) live under `werkbank`. Without the
`platforms/werkbank` gate, `messages` answers `503` and `stop` `{"stopped": false}`.

Keep `port` a plain number (the board reads it without a YAML library). A changed setting needs a
**gateway restart**.

## Files

* `<profile home>/plugin-data/werkbank/key` — the shared key (dir 0700, file 0600). The board reads
  it from there (same user).
* `<profile home>/plugin-data/werkbank/messages.json` — which SessionDB row is which board message
  (HMAC of the text keyed with the plugin key, the `client_message_id`; no text; 0600).

## Security notes

* Binds `127.0.0.1` only; every request needs the key (`hmac.compare_digest`), else 401.
* Only sessions the board resolved (`/v1/resolve`) are buffered; only messaging sessions of this
  plugin's profile resolve. Sending and stopping go through `PluginContext.inject_message` /
  `interrupt_session`, which refuse other profiles' sessions (WB-1127).
* Chat, prompt and tool texts stay in memory (ring buffer, 5000 events per session); nothing of
  them is logged, the HTTP handler writes no access log.
* Freigaben/Rückfragen (`approvals`, `clarify`) answer `501` until WB-1129.

## Modules

`__init__.py` (gateway-only start), `config.py`, `keyfile.py`, `buffer.py` (ring buffer, epochs),
`resolve.py` (`session_id → session_key`), `host.py` (gateway access, feature detection for
WB-1127), `snapshot.py`, `idmap.py`, `server.py`. Tests: `tests/plugins/platforms/werkbank/`
(`scripts/run_tests.sh tests/plugins/platforms/werkbank/`); the interop tests use the board's
`bruecke.py` and `hermes_view.js` from `WERKBANK_REPO` (default `/data/projects/agent_ticket`).
