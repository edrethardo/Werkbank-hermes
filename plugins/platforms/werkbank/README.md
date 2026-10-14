# Hermes plugin `werkbank`

The Werkbank board's window into the Hermes gateway session of a project (WB-1128): an ordered
event stream with replay, a snapshot, sending and stopping — over HTTP on `127.0.0.1`, protected by a
key. Protocol: [`PROTOCOL.md`](PROTOCOL.md).

It is a bundled platform plugin **without** a messaging adapter: it registers no platform, only the
core hook `on_gateway_session_event` (`gateway/session_events.py`, WB-1126) and its own server. It
starts only inside `hermes gateway`; the CLI, the TUI and `hermes plugins` load it and do nothing.

"Inside the gateway" means `start_gateway()` ran in the process (`gateway.run.gateway_process_started()`,
`# fork: werkbank-chat`), not merely that `gateway.run` was imported: `hermes gateway status` imports it
and resolves every platform plugin, and used to grab the port while the gateway booted. Per process and
Hermes home there is exactly one server (state in `sys.modules["hermes_werkbank_runtime"]`, shared by
every copy the loader imports); every plugin context that loads holds it, and it stops when the last one
unloads. A forced plugin re-discovery (`reload-plugins`, plugin go-live) re-loads it in the same pass.

## Configuration (profile `config.yaml`)

```yaml
plugins:
  entries:
    werkbank:                   # read by this plugin AND by the board (bruecke.read_port)
      enabled: true             # default false: without it no port is opened
      port: 8767                # default 8767; NOT 8765 (the board's own port, refused)
      mirror_to_telegram: true  # default true: the board text, as the owner's message, in the session's topic
      mirror_name: Aaron        # header name; default: the chat owner's Telegram first name, else "Du"
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
* The mirror (WB-1148, `mirror.py`) reads as the owner's message: `👤 <Name> · Werkbank`, then the
  text as a blockquote (expandable when long), built as finished MarkdownV2 that the Telegram adapter
  sends unchanged (metadata `telegram_preformatted_markdown_v2`). Every MarkdownV2 special character
  is escaped; a word joiner after `@ # $ / .` keeps Telegram from making mentions, commands, hashtags
  or links; control, bidi and zero-width characters are removed; ≤ 3500 UTF-16 units of text and one
  message. It is sent BEFORE the turn is injected; its message id becomes the turn's reply anchor
  (`inject_message(reply_to_message_id=…)`), so Hermes' answer replies to it. Mirror failed or slower
  than 5 s: the answer goes out without a reply. Message refused by the gateway: the mirror is deleted.
  Other platforms: header plus a literal code block.
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
