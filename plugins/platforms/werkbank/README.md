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

Which SessionDB row is which board message needs no plugin file: the gateway stores the
`client_message_id` on the turn's own user row (`display_metadata`, core change marked
`# fork: werkbank-chat` in `gateway/run_plugin_injection.py` / `gateway/run_turn.py`).

## Security notes

* Binds `127.0.0.1` only; every request needs the key (`hmac.compare_digest`), else 401.
* Only sessions the board resolved (`/v1/resolve`) are buffered; only messaging sessions of this
  plugin's profile resolve. Sending and stopping go through `PluginContext.inject_message` /
  `interrupt_session`, which refuse other profiles' sessions (WB-1127).
* Chat, prompt and tool texts stay in memory (ring buffer, 5000 events per session); nothing of
  them is logged, the HTTP handler writes no access log.
* Before auth: at most 64 connections (when full the oldest unauthenticated one is closed, not the
  new one; authenticated ones have an own quota of 48), 10 s for request line + headers, `Host` must be loopback (`421`).
* The mirror line is plain text: the board text goes into a literal code block, without control,
  bidi or zero-width characters, capped at 3500 UTF-16 units.
* Freigaben/Rückfragen (`approvals`, `clarify`, WB-1129, `approvals.py`): only the exact id pending in
  that session (else `404`, nothing resolved); a subagent's request `once`/`deny` only. A Telegram card
  answered from the board is edited to „erledigt in der Werkbank: …“ — the Telegram plugin subscribes
  to the session stream only while this plugin is enabled. Needs a gateway restart.
* **Known residual risk on multi-user hosts:** any local user can connect to `127.0.0.1:<port>`
  (the key stops them), and while the gateway is down another local user could bind the port
  first and receive the board's requests — including the key in `Authorization`. On a shared host
  keep the port firewalled per user or wait for a Unix-socket transport (not built).

## Modules

`__init__.py` (gateway-only start), `config.py`, `keyfile.py`, `buffer.py` (ring buffer, epochs),
`resolve.py` (`session_id → session_key`), `host.py` (gateway access, feature detection for
WB-1127), `snapshot.py`, `server.py`. Tests: `tests/plugins/platforms/werkbank/`
(`scripts/run_tests.sh tests/plugins/platforms/werkbank/`); the interop tests use the board's
`bruecke.py` and `hermes_view.js` from `WERKBANK_REPO` (default `/data/projects/agent_ticket`).
