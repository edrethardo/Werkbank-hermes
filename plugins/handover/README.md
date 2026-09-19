# handover

`/handover` — carry context across a **session boundary**.

```
/handover                  # summarize this session, save the briefing
/handover <focus>          # steer the summary
/handover show             # show the pending briefing
/handover discard          # throw it away
```

Enable with `hermes plugins enable handover`.

## Scope — read this before adding anything

This plugin does **not** free context, and must not start doing so. In-session compaction is
`/compress` (`agent/context_compressor.py`): structured template, `focus` argument with budget
priorisation, forced secret redaction, iterative updates across repeated compactions. Anything
this plugin did to the current context would be a worse second implementation — and, worse,
an additive one: an extra summarizing call whose output compaction then summarizes again.

The one thing compaction cannot do is survive a session boundary:
`ContextCompressor.on_session_reset()` drops `_previous_summary` on `/new` and `/reset`. That
gap is this plugin's entire reason to exist. If a change here would also make sense inside one
session, it belongs in the compressor, not here.

## Shape

| File | Role |
|---|---|
| `handover_core.py` | Transcript normalization, caps, prompt, record shape, injection text. Pure except `redact()`. |
| `__init__.py` | Host wiring: hooks, plugin state, LLM call, slash command. |

## Invariants

- **Never touch the current session.** No history mutation, no injected `/new` or `/compress`,
  no session rotation. `/handover` writes a record and returns.
- **The briefing rides the user message, never the system prompt.** `pre_llm_call` returns
  `{"context": ...}`, which the host appends to the turn's user message. Putting it in the
  system prompt would break per-conversation prompt caching.
- **The writing session never receives its own briefing.** Delivery is keyed on
  `source_session_id`; that session still holds the real history.
- **Delivery is one-shot.** The record is cleared before the briefing is returned; a
  redelivered briefing would grow every later turn.
- **Persisted text is force-redacted.** The record goes to disk and is replayed into a later
  session, so `redact(force=True)` runs before storing — not display-side, not optional.
- **A failed summarization must not lose the handover.** The mechanical digest in
  `fallback_briefing()` is the floor, and `/handover show` labels it.
- **Every cap is a budget.** `_truncate()` never returns more than `limit` characters — the
  omission marker fits inside the cap, not on top of it.

User-facing docs: `website/docs/user-guide/features/built-in-plugins.md`.
Tests: `tests/plugins/test_handover_plugin.py`.
