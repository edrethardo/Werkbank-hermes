# Protocol: Hermes plugin `werkbank` ↔ Werkbank board (v1)

Contract of plan section 2 (Werkbank repo, `docs/superpowers/plans/2026-10-13-hermes-chat-werkbank.md`)
as built in WB-1128, including the later agreements (port 8767, `session_key` on every event, the
WB-1126 event types, the WB-1132 field names and snapshot obligations). The board's reference fake is
`agent_ticket/tests/fixtures/hermes_werkbank_fake.py`; its client is `src/werkbank/plugins/chat/bruecke.py`.

## Transport

* HTTP/1.0 on `127.0.0.1:<port>` only (the host is not configurable). Port: profile `config.yaml`
  `plugins.entries.werkbank.port`, default **8767** (8765 is the board's own port and is refused).
* Every request: `Authorization: Bearer <key>`. Key file `<profile home>/plugin-data/werkbank/key`
  (directory 0700, file 0600, created on the first start, reused after). Compared with
  `hmac.compare_digest`; missing or wrong → `401 {"error": "unauthorized"}` before anything else.
* Before auth: at most 64 open connections; a new one beyond that closes the OLDEST connection that
  has not authenticated yet (never the new one), so a client sending its headers at once gets in
  during a reconnect flood. Authenticated connections have their own quota (48 = 32 streams + 16;
  beyond it `503 {"error": "busy"}`). Request line and headers (a POST body too) must arrive within 10 s; `Host` must be `127.0.0.1` or `localhost`
  (with or without this port), else `421` (DNS-rebinding defense in depth).
* Errors are JSON `{"error": "<code>", "grund": "<short German reason>"}`. No answer and no log line
  carries the key, a chat text, a tool text or a request body.

## Event (one JSON object per SSE `data:` line)

```json
{"v": 1, "session_key": "agent:main:telegram:dm:…", "session_id": "…", "epoch": 91926958000,
 "seq": 118, "type": "tool.end", "id": "call_…", "parent_id": "turn_…", "origin": "telegram",
 "platform": "telegram", "core_seq": 4711, "ts": "2026-10-13T19:20:01.123456Z", "payload": {…}}
```

* `session_key` is set on **every** event, `session.epoch` included.
* `seq` runs 1, 2, 3, … gap-free per `(session_key, epoch)`. `core_seq` is the core's own sequence
  (`gateway/session_events.py`), informational.
* `epoch` is a number (≤ 12 digits). It changes — buffer emptied, `seq` back to 0 — on
  `overflow` (the core dropped events), a gap in `core_seq` (`luecke`), a changed `session_id`
  (`session_id`: `/new`, compression) and on every plugin restart (epochs are drawn from a process
  counter seeded from the clock, 1000 per second, so an old epoch never matches a new one).
* `origin`: the sender — `telegram`, …, or `werkbank` for messages the board sent (the core's
  plugin id `platforms/werkbank` is reported as `werkbank`).

### Types and payloads (all from the core, passed through unchanged unless noted)

| type | id | payload |
|---|---|---|
| `user.message` | client_message_id, else platform message id | `delivery` (`turn`\|`nachgeschoben`\|`eingereiht`), `text`, `client_message_id`, `message_id`, `internal`, `media_count`, optional `merged_into`, `interrupt`, `synthetic` (`goal_kickoff`\|`goal_continuation`), `injected_by` (the injecting plugin's id for a plugin injection, also without `client_message_id` — WB-1182), `attachments` (WB-1174, plugin: a board message's uploads `[{id, name, type, size, kind}]`) |
| `turn.start` | turn id | `message_id`, `run_generation` |
| `text.delta` | – (parent = turn) | `text` |
| `text.interim` | – | `text`, `already_streamed` |
| `tool.start` | tool call id | `tool_name`, `args` (≤ 4 KB, redacted), `truncated` |
| `tool.progress` | tool call id | `kind`, `tool_name`, … |
| `tool.end` | tool call id | `tool_name`, `output` (≤ 4 KB, redacted), `truncated`, `is_error` |
| `subagent.start\|progress\|end` | subagent id (parent = turn/parent) | `goal`, `status`, `depth`, `model`, `provider` (WB-1309: those the child was built with), `child_session_id`, … |
| `status` | – | `kind`, `message` |
| `approval.request` | request id | `request_id`, `command`/`description` (redacted like the Telegram card), `pattern_key(s)`, `allow_permanent`, `allow_session`, `smart_denied`, `surface`, `delegated_child`, for a child `child_session_id` and (plugin, from `subagent.start`) `subagent_id` |
| `approval.resolved\|expired\|cancelled` | request id | `request_id`, `choice`, `decided_by` / `cause` |
| `clarify.request\|resolved\|expired` | clarify id | `clarify_id`, `question`, `choices`, `multi_select` / `answer`, `decided_by` |
| `turn.done` | turn id | `final_response` (redacted), `completed`, `api_calls`, `model`, `session_id`; `message_uid`, `row_id` of the answer's SessionDB row once it is written; WB-1309: `provider`, `context_tokens`, `context_length`, `compression_threshold` when known (also on `turn.stopped` and a failed turn's `error`) |
| `turn.stopped` | turn id | as `turn.done` plus `reason` |
| `error` | turn id | `stage`/`failure_reason`, `error_type`, `message` |
| `command.reply` | the board's `client_message_id` | WB-1256: the reply of a slash command the board sent — `command` (name), `text` (redacted, ≤ 20 000 chars, may be empty when the command opened a card instead), `request` (the command text as sent, redacted, ≤ 2 000 chars — the stream can be faster than the board's POST), `client_message_id`, `refused` (the gateway's authorization refused the session) |
| `choice.request` | choice id `ch_<16 hex>` | WB-1256: a card a command opened (`/model`, `/reasoning`, `/fast`, the destructive-command confirmation of `/new` …) — `choice_id`, `kind` (`model`\|`choice`\|`confirm`), `title`, `options: [{label, is_current, group?}]` (no values: the board answers by index), `client_message_id`, `request` (the command text as sent, like on `command.reply`) |
| `choice.resolved\|expired` | choice id | `choice_id`, `index`, `label`, `text` (the command's answer to the pick), `decided_by` / `cause` (`ersetzt`: a newer card of the session, `abgelaufen`: 30 min) |
| `session.epoch` | – | `grund` (`epoch` wrong epoch asked, `after` outside the ring, `neu` the epoch changed while streaming), `ursache` (`start`\|`overflow`\|`luecke`\|`session_id`) — generated by the plugin; `epoch`/`seq` are the NEW position |

`overflow` from the core is never passed on: it becomes a new epoch. `session.state` (plan) is not
produced; its fields are in the snapshot's `session`.

## Endpoints

* `GET /v1/resolve?session_id=…` → `{session_key, session_id, platform, live}`; `404` when the
  gateway does not know it (or it is not a messaging session of this profile), `400` for a bad id,
  `503` while the gateway is not up. A `session_id` the plugin once resolved still resolves to its
  key after it moved (answer carries the current id). **Only resolved sessions are buffered**
  (allowlist, at most 64, least recently used dropped); `/v1/s/{key}/…` of any other key → `404`.
* `GET /v1/s/{key}/snapshot` → see below.
* `GET /v1/s/{key}/events?epoch=E&after=N` → `text/event-stream`. Ring buffer of 5000 events per
  session. Wrong `E`, or `N` before the oldest kept event or beyond the newest → exactly ONE
  `session.epoch`, then the stream ends. Otherwise every event with `seq > N`, then live; a
  heartbeat comment `: hb` every 15 s; an epoch change ends the stream with `session.epoch`
  (`grund: neu`), so does dropping the session from the buffers (only when every buffered session
  has an open stream). At most 32 open streams (`429`).
* `POST /v1/s/{key}/messages {text, client_message_id}` → `{accepted: true}`. `400` bad text
  (empty, > 200 000 chars) or id (`[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`); `409` this id was already
  accepted; `503` the gateway refused (not running, `allow_gateway_injection` missing, foreign
  profile). Never retried by the plugin. Sent with
  `ctx.inject_message(text, session_key=…, client_message_id=…)` (WB-1127). The resulting
  `user.message` carries the id (`id` and `payload.client_message_id`) — with a core before
  WB-1127 the plugin assigns it to the next `user.message` it injected. While a turn runs the
  gateway steers or queues it (`delivery: nachgeschoben|eingereiht`).
  With `mirror_to_telegram` (default on) the session's chat (the Telegram topic) first gets the
  text as the owner's message — header `👤 <mirror_name> · Werkbank` (unset: `Du`), the text as an
  escaped blockquote (nothing formats, links or mentions; control, bidi and zero-width characters
  removed, ZWJ/ZWNJ kept between two visible characters; ≤ 3500 UTF-16 units) — and Hermes' answer
  to the turn replies to that message (WB-1148). The `200` waits for that mirror at most 5 s. No
  mirror when the gateway cannot take the message (not running, draining, injection not allowed);
  a mirror whose message is refused anyway is deleted, a late one as soon as it lands.
* **Slash commands (WB-1256).** A `text` whose first word is `/name` (the gateway's own rule,
  `MessageEvent.get_command`: `/name@bot` counts, `/home/…` does not) is sent with
  `inject_message(…, as_command=True)`: the gateway runs it through the command path the session's
  owner gets in the chat (authorization of the session's origin, the `allow_admin_from` slash gate,
  busy policy, the unknown-command notice) — never as text for the model, never mirrored to the
  topic. The reply comes back as `command.reply` (also kept for the snapshot, see below), a picker as
  `choice.request`. A command never carries `attachments` (`400`); a core without `as_command`
  answers `501` and nothing is sent. A command the gateway rewrites into a turn (`/plan`, a skill)
  runs as a turn: `user.message` + `turn.*` as usual. `/v1/s/{key}/stop` stays the stop button.
* `POST /v1/s/{key}/choices/{choice_id} {index}` (WB-1256) → `{resolved: true, index, label, text}`:
  picks option `index` (an integer of the card's `options`, else `400`, nothing picked) of an open
  card of THIS session — a card of another session or one no longer open is `404` (the card stays
  open for its own session). The pick runs the command's own applier (the same one a Telegram tap
  runs) and closes the card exactly once (`choice.resolved`). One open card per session: a newer
  card replaces it (`choice.expired`, `cause: ersetzt`).
* `POST /v1/s/{key}/subagents/steer {delegation_id, task_index, text}` (WB-1338, `subagents.py`) →
  `{steered: true}`: queues `text` into a RUNNING subagent of THIS session (its live record's
  `delegation_id`, transcript `task-<task_index>.log`, owner = this session) via
  `steer_subagent` — it lands at the child's next iteration boundary. A child of another session or
  an unknown one: `404`; a finished child: `409` (`grund` „Subagent beendet …“); bad ids/text: `400`.
  Lookup, ownership check and the steer run under ONE delegate-registry lock (no recycled id gets
  the text). The gateway log gets only the status, never the text; the text itself becomes part of
  the child's conversation like every steer (its session/transcript, or `missed_steer` in the
  delegate result when the child finished before the next boundary).
* `GET /v1/commands` (WB-1256) → `{commands: [{name, args, description}]}`: the gateway's slash
  commands (registry, gateway-available, plus plugin commands) for the board's completion.
* `POST /v1/sessions {next_to, title, text}` (WB-1342, `host.new_session`) →
  `{session_key, session_id, thread_id}`: a NEW chat for the board's „+“ (new project). Next to the
  chat of the session `next_to` (a key of THIS profile) the adapter opens a sibling thread named
  `title` (≤ 60 characters, one line; Telegram DM topic, Slack/Matrix/Discord thread — the rules of
  `/branch`), the session store creates that thread's session, and its first turn starts with
  `text` through `inject_message(origin=…)` WITHOUT a client message id — the start instruction is
  an internal plugin row, no owner bubble. Unknown `next_to`: `404`; a chat that cannot host a
  thread (or a core without the pieces): `501`; thread or session not created: `502`; gateway not
  taking messages, or the start turn refused (`grund` names the session that exists then): `503`.
  A `503` AFTER thread and session were created carries them: `{angelegt: true, session_id,
  session_key, thread_id}` — the board keeps that chat as an open draft („Start nicht angenommen –
  schreib im Chat einfach los“) instead of leaving an orphaned topic.
  One call opens one thread — the board never retries it on its own. The text is never logged.
* `POST /v1/s/{key}/uploads {name, data}` (WB-1174, `uploads.py`) → `{upload_id, name, type, size, kind}`.
  ONE file, `data` base64; the request body may be up to 28 MiB (every other POST: 512 KiB), refused
  `413` from `Content-Length` before it is read; the file ≤ 20 MiB (`413`). The bytes decide the type,
  never the name or a declared type: JPEG/PNG/GIF/WebP → `kind: image` (`type` = its image type);
  HEIC/HEIF → `kind: document`, `type: application/octet-stream` (the vision path cannot decode HEIC
  without `pillow_heif`); PDF; docx/xlsx/pptx (ZIP head + that extension); UTF-8 text without NUL for
  `.txt .md .csv .json .log .yaml .yml`. Anything else `415` (SVG, executables, archives). `name` is
  cleaned (base name only, control/bidi/format characters removed, only `[\w .()-]`, ≤ 80 characters
  and ≤ 200 UTF-8 bytes, the extension of the detected type). `400` bad base64 / empty, `429` more than
  20 uploads of this session not sent yet, `501` uploads not set up, `500 storage failed` with the errno
  name in `grund` when the cache cannot take the file (disk full, permissions). Stored in the profile's media cache like a Telegram
  download (`get_hermes_dir("cache/images"|"cache/documents", home=<plugin home>)`; directory 0700, file
  0600 with `O_EXCL`); the gateway's hourly housekeeping removes files older than 24 h, the plugin
  deletes an upload not bound to a message after 1 h. The index lives in the plugin process only.
* `GET /v1/s/{key}/uploads/{upload_id}` → the file of an upload of THIS session (else `404`), an image
  with its own type, everything else `application/octet-stream`; `X-Content-Type-Options: nosniff`.
* `DELETE /v1/s/{key}/uploads/{upload_id}` (no body, else `400`) → `{deleted: upload_id}`: an upload of
  THIS session not sent yet goes, file included (the board chip's ✕; frees the `429` quota). Sent → `409`,
  unknown or another session's → `404`.
* `POST /v1/s/{key}/messages {text, client_message_id, attachments: [upload_id, …]}` (WB-1174): 1–10
  upload ids of this session, not sent yet, no repeats (else `400`, nothing sent); `text` may then be
  empty. Sent with `inject_message(…, media_urls=[paths], media_types=[types])` — the turn gets them like
  a Telegram photo / document (all images: a PHOTO event, else DOCUMENT; the content is never inlined
  into the text). A core without `media_urls` → `501`, nothing sent (text alone still works). The mirror
  lists the files under the text (`📷 name (size)` / `📎 …`) and, once the message is accepted, posts each
  image as a photo replying to the mirror. A refused message frees its uploads again. Without
  `attachments` the request and its handling are exactly the text message above.
* `POST /v1/s/{key}/stop` → `{stopped: bool}` via `ctx.interrupt_session(key)` (`false`: nothing
  running or refused). Without that API (core before WB-1127): `501` with `grund`.
* `POST /v1/s/{key}/approvals/{request_id} {choice: once|session|always|deny}` →
  `{resolved: 1|0, choice, decided_by}` (WB-1129, `approvals.py`). Only a `request_id` pending in
  THIS session (`list_gateway_approvals(key)`) — a foreign or invented id is `404` and resolves
  nothing (an id this session answered a moment ago gives `200 {resolved: 0}` with the outcome).
  Resolved with the exact id (`resolve_gateway_approval(…, request_id=…, decided_by="werkbank")`):
  of a Telegram tap and a board click exactly one wins; the loser gets `resolved: 0` plus the
  winner's `choice`/`decided_by`, the Telegram card shows the same outcome. `400` for a scope the
  Telegram card does not offer — the same rule as its buttons (`ea_choices`): `session` only with
  `allow_session`, `always` only with `allow_session` AND `allow_permanent`, once/deny only on a
  `smart_denied` — and for `session`/`always` on a subagent's request (`delegated_child`): a child
  is `once`/`deny` only. The plugin resolves only the named id; the core's coalescing of an
  identical pending command (same text and pattern keys) applies as it does for a Telegram tap.
* `POST /v1/s/{key}/clarify/{clarify_id} {answer}` → `{resolved: bool}`. Only a `clarify_id` of
  THIS session (else `404`); the answer is read like a typed Telegram reply (number or label of a
  choice, free text for an open prompt, `skip`; C0 control characters except newline/tab, and
  DEL, removed first; at most 20 000 characters), else `400`. `resolved: false` when it was answered
  already. `clarify.resolved` carries `decided_by`.
* `GET /v1/s/{key}/tools/{tool_call_id}/output` → `{tool_call_id, output}` (full text, redacted,
  ≤ 1 MB) from the SessionDB; `404` when unknown.

## Snapshot

```json
{"epoch": 91926958000, "seq": 25, "session_key": "…", "session_id": "…",
 "state": "arbeitet|wartet", "turn_id": "turn_…|null",
 "messages": [{"id", "client_message_id", "message_id", "db_id", "role": "user|assistant|tool|system",
               "text", "origin", "ts", "tool_call_id", "tool_name", "is_error", "truncated",
               "tool_calls?": [{"id", "name"}], "delivery?", "from_buffer?",
               "kind?": "system", "herkunft?": "<plugin id>"}],
 "pending_approvals": [approval.request payload], "pending_clarify": [clarify.request payload + awaiting_text],
 "pending_choices": [choice.request payload],
 "subagents": [{"id", "parent_id", "status", "goal?", "model?", "provider?", …}],
 "session": {"model", "context_percent", "tool_count", "errors", "provider?", "context_tokens?",
             "context_length?", "compression_threshold?"}}
```

* **Fits the stream:** `epoch`/`seq` are read first, the history after; continue with
  `events?epoch=<epoch>&after=<seq>`. Something written in between may come once more as an event;
  the board de-duplicates by id.
* **Message ids = stream ids.** `id` is the id the stream uses for the same message:
  `client_message_id`, else the platform message id (`platform_message_id` of the row), only when
  both are missing `db:<row id>`. `db_id` is the row id. A board message's row carries its id
  itself: the gateway stores `client_message_id` and `hermes_plugin_id` in the row's
  `display_metadata` (`gateway/run_plugin_injection.client_message_metadata`; context compression
  keeps both). `origin` of such a row is `werkbank` only when `hermes_plugin_id` is this plugin
  (`platforms/werkbank`), another plugin's id for its rows. Rows are never matched by text or time.
* **One session only:** buffered entries carry the session id they arrived under; a snapshot uses
  those of its `session_id` and its compression ancestors — never those of a session `/new` replaced.
* **Complete up to `seq`:** every `user.message` announced at or before `seq` (accepted, steered,
  queued) is in `messages` even when its SessionDB row is not written yet (`from_buffer: true`,
  with `delivery`); a steered message that never gets an own row stays there while the plugin
  keeps it (200 per session). Messages accepted but not announced yet arrive later as
  `user.message` with a higher `seq`. A buffered message counts as written only when a row with its
  id exists — a row without an id (e.g. a turn that failed before the agent persisted) and the
  buffered message are BOTH shown rather than paired by guesswork.
* **Folded messages (WB-1175).** A message steered or redirected into the running turn shares a
  row with others and its `content` carries the gateway's origin preamble (a redirect also the
  interruption scaffold in `api_content`). The core names them on the row:
  `display_metadata.folded_messages = [{ref, text, client_message_id?, hermes_plugin_id?,
  synthetic?} | {ref, internal: true}]` (`agent/folded_messages.py`, set by
  `gateway/session_events.folded_part`; `ref` = the id the `user.message` was announced under).
  Such a row becomes one user message per part — `id` = `ref` (without one `db:<row id>#<n>`),
  `text` = the sender's own words, same `db_id`; an internal part none. That confirms the buffered
  entry by its id. A row without the marker (an older core) stays as it is — never matched by text.
* **Not the owner's words (WB-1175), by markers only:** a compaction handoff
  (`_compressed_summary`) shows only the human part the core's `user_originated_turn_view` returns,
  a standalone handoff nothing; a user row with `display_kind` `hidden`, `internal_notification`,
  `async_delegation_complete` or `process_complete` is not shown unless it names a sender's
  `client_message_id` (a board message is injected as an internal event).
* **A plugin's own words (WB-1182): a system line.** A plugin injection WITHOUT a
  `client_message_id` (the Werkbank wake order `werkbank_admin_wake`: `ctx.inject_message(text,
  origin=…)`) still names its plugin: the gateway stores `hermes_plugin_id` on the row (via
  `client_message_metadata`), sets `injected_by` on its `user.message`, and a folded part keeps
  `hermes_plugin_id` with the plugin's text. Such a gateway-internal row (or folded part) becomes
  `{role: "system", kind: "system", herkunft: <plugin id>, origin: <plugin id>, text, id: db:<row>}`
  — never a user message, never by its text. A board before WB-1182 skips `role: system`. Internal
  rows without a plugin stay hidden; the board's own messages (with a client id) stay user messages.
* **Where a buffered message stands (WB-1175):** before the first history row whose id lies above
  its mark (the session's highest row id when it was announced) — row ids only grow, the clock is
  never read; without a mark at the end.
* **Final answer after `turn.done`:** the board re-syncs once after `turn.done`. When the turn's
  assistant row is not in the SessionDB yet, the snapshot carries the `final_response` of that
  `turn.done` as an assistant message (`id` = turn id, `from_buffer: true`) until an answer row of
  THAT turn exists. When `turn.done` names the row (`message_uid`), only a row with that uid counts
  (also after compression copied it). Otherwise by row ids, which only grow (`messages.id` is
  AUTOINCREMENT): the plugin notes the highest row id of the session at each `turn.start` (and when it
  first observes a session); the answer row of a turn is the first assistant text row (no tool calls)
  above that mark and not above the next `turn.start`'s mark. A turn begun before the plugin observed
  the session uses the observation mark. Never the clock, never the text — the same answer twice
  stays twice. Then it is shown once (as `db:<id>`). `turn.done` names only a row this turn wrote
  (the gateway's `history_offset`) and only one with visible text; an empty assistant row never
  stands for the buffered answer.
* **Buffered entries are a bridge, never a second history.** An entry leaves the plugin buffer for
  good once its row was confirmed: seen in a snapshot (by id / uid / the row-id rule above) or, for
  an answer, reported written by `turn.done` (`row_id` at or below the session's highest row id,
  `SELECT MAX(id)` via `SessionDB.max_message_row_id`). A compression (in place or by rotation) or
  `/undo` that later takes the row out of the active history never brings the buffered copy back.
  Independent of the window: an entry whose row (`row_id`) or mark lies below the first row of the
  active history is not shown; only answers of turns after the newest confirmed answer are shown
  (in practice the last one or two); a new session id drops the answers of the old one.
* **Remaining limits (honest):**
  - An answer whose `turn.done` carries no uid (older core, empty row) is matched by row marks; if
    it was never seen in a snapshot and a compression / `/undo` happens, it is dropped, not shown
    twice. Without a watermark (store unreadable) entries cannot be checked against row ids: an
    entry may then be missing until its row is read, never doubled by guesswork.
  - A message queued during a turn that compresses in place may be missing from a snapshot taken
    between the compression and its own row write (its mark lies below the new rows); the stream's
    `user.message` still carries it.
  - A row observed between the agent's flush and `turn.done` carries no uid in the buffer yet; the
    row itself is in the history, so nothing is lost.
  - Under a local connection flood a legitimate request can be delayed: unauthenticated
    connections are capped (`max_pending`, the oldest is hung up) and must send their headers
    within `HEADER_DEADLINE_S` (10 s); a client that sends its headers slowly may be dropped and
    has to reconnect.
  - `/retry` does NOT carry the board's id over: it rewinds the last user turn and sends its text as
    a new message without `client_message_id` (`gateway/slash_commands_session._handle_retry_command`).
    The snapshot then shows a plain row (`db:<id>`, origin of the session's platform); the board's
    message id is gone with the rewound row.
  - Rows written by a core before `hermes_plugin_id`: a `client_message_id` starting with `wb-`
    (the board's own ids) counts as origin `werkbank`.
* **Command replies (WB-1256)** have no SessionDB row: the last 20 `command.reply` texts of the
  session (its id or its compression lineage) are added as `{role: "system", kind: "system",
  herkunft: "befehl", command, id: "cmd:<client_message_id>", text, from_buffer: true}` where their
  mark says they arrived. `pending_choices`: the open cards (`choice.request` payload shape).
* A user message whose `client_message_id` has bound uploads carries `attachments` (WB-1174, same
  shape as on `user.message`; only while the plugin process knows them).
* History: newest 200 SessionDB rows via the store's read API (`get_messages`), tool output capped
  at 4 KB like `tool.end` (`truncated`). `pending_approvals` from `list_gateway_approvals`, commands
  redacted like the event; `pending_clarify` from `get_pending_for_session` (the oldest open one).
* `state: "arbeitet"` while a turn runs (open `turn.start` in the buffer or a running agent),
  else `"wartet"`. `session` counts tools/errors since the board first resolved the session in this
  plugin process. WB-1309: `provider`, `context_tokens` (prompt size of the last API call — what
  `/usage` shows, lower after a compression), `context_length` (window of the active model) and
  `compression_threshold` are those of the LAST turn end that named them (they survive epochs, like
  `model`); `context_percent` = `context_tokens / context_length`, `null` without both numbers.
  Nothing is estimated: an unknown number is absent.

### Weitere Restgrenzen (Review 4)
- Ohne uid gilt eine Antwort als bestätigt, sobald eine Assistant-Textzeile über ihrer Startmarke liegt. Schreibt eine In-place-Kompression alte Antworten mit neuen IDs neu, kann eine noch ungeschriebene Antwort ohne uid verworfen werden; sie fehlt, bis ihre Zeile geschrieben ist.
- Das Ausblenden gepufferter Nutzernachrichten unter der kleinsten History-Zeile greift auch bei Rotation (Nachricht unter alter Session-ID gepuffert, Zeile in der neuen).
- Ist die Antwortzeile leer oder kein Text, kommt `turn.done` ohne uid; die gepufferte Antwort verschwindet, sobald eine spätere bestätigt ist. Der Puffer ist Brücke, keine zweite History.
- Wird ein Puffer mitten im Snapshot-Bau verdrängt und neu angelegt, kann eine neue, noch ungeschriebene Antwort kurz fehlen, bis ihre Zeile geschrieben ist.
