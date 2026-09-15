---
title: "TUI Delivery Socket"
description: "The local socket through which a board hands a ticket to a live TUI session, and the permission decision behind it"
---

# TUI Delivery Socket

`tui_gateway/delivery_socket.py` opens a local Unix socket so a process on the same machine — a
ticket board, a watcher, a script — can hand a prompt to a session the TUI gateway is *already
holding*, instead of spawning a new one.

It belongs to `tui_gateway` and nowhere else: the dashboard does not hold the sessions, and the
messaging gateway is a different process with a different job. The process that owns the live
session is the only one that can deliver into it.

## Wire contract

It follows `gateway/control_socket.py` exactly where it can, and shares that module's path
helpers (`resolve_local_socket_path`, `resolve_client_socket_path_for`) rather than re-deriving
them:

| Property | Value |
| --- | --- |
| Path | `$HERMES_HOME/tui-delivery.sock` (temp-dir fallback + `tui-delivery.sock.path` pointer when the home exceeds `sun_path`) |
| Framing | one JSON line in, one JSON line out, then the server closes |
| Request cap | 64 KiB; anything larger is refused, never buffered |
| Verbs | `identify`, `ticket.deliver` |
| Transport | Unix socket only — never a TCP port |

```bash
printf '{"verb":"ticket.deliver","id":1,"params":{"text":"WB-536: pick this up"}}\n' \
  | socat - UNIX-CONNECT:$HERMES_HOME/tui-delivery.sock
```

From Python: `delivery_socket.deliver_ticket(home, text, session_id="")`. It never raises; a
missing socket, a disabled gate, or a refusal all come back as `{"ok": false, "error": ...}`.

## The permission decision

This is the part that cost more thought than the bind.

The gateway's control socket answers *status* verbs: anything that can open the file learns what
the gateway is doing. `ticket.deliver` is a different kind of grant — it puts words in the agent's
mouth and spends the user's tokens. So the "file permissions are the auth boundary" answer was
re-taken here rather than inherited, and it comes with one deliberate difference:

- **Filesystem permissions are the boundary.** The socket is `0o600`, bound under a restrictive
  umask so it is never group- or world-connectable even for the instant between `bind` and
  `chmod`, and it sits inside the profile's own `HERMES_HOME`. There is no token and no peer
  allowlist: a process running as the user can already read that home, drive the PTY, and edit
  `config.yaml`, so a secret stored beside the socket would be a boundary in appearance only.
- **It is off unless configured.** `tui.delivery_socket` defaults to `false`. Escalating from
  "read my status" to "type for me" is the user's call, not a shipped default. This gate is the
  reason the verb is safe to ship at all.
- **Never a port.** A listening TCP port has no owner, and "localhost only" is not a permission.

```yaml
# ~/.hermes/config.yaml
tui:
  delivery_socket: true
```

## Not a second writer

Delivery reuses `prompt.submit` with `queued=True` — the same composer a typed message goes
through, exactly as `methods_bot_relay.py` already does for a relayed DM. There is no second
composer path, and adding one would be a regression.

That single flag is the whole delivery contract:

- `session_auto_continue._handle_busy_submit` forces queue mode for a `queued=True` submit, so a
  delivered prompt runs as the **next** turn and never steers or interrupts a turn in flight.
- Consecutive text-only arrivals merge losslessly into one queue slot (existing `_enqueue_prompt`
  behaviour); the order of the delivered words is preserved either way.
- The delivering connection binds **no** transport. `prompt.submit`'s re-bind therefore finds
  nothing to attach, and the PTY/WebSocket client keeps sole write ownership of the session. A
  second writer arises only when someone resumes the same durable session id over a second
  WebSocket — which this socket does not do.

Two rules for anyone extending this: do not add a bespoke composer path, and do not bind a
transport for a delivery.

## Addressing

An empty `session_id` resolves only when exactly one session is live. Several live sessions, or an
unknown id, is a refusal — delivering a ticket into the wrong chat is worse than not delivering
it. `identify` lists the ids currently deliverable.

Tests: `tests/tui_gateway/test_delivery_socket.py`.
