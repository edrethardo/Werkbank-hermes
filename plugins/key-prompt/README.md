# key-prompt

API key entry without shell and without chat. The agent calls `request_api_key(provider)` and sends
the owner the returned one-shot link (`/p/schluessel/?r=<id>`, 5 min, once). The owner types the
key into a masked field on that dashboard page; the dashboard stores it in the chosen profile's
credential pool via `hermes_cli.auth_commands.add_api_key_credential` (the non-interactive core of
`hermes auth add <provider> --type api-key`). `api_key_request_status` tells the agent
stored / cancelled / expired + label — never the value.

Files: `store.py` (value-free request records, single-use claim, rate limits, audit),
`keys.py` (provider allowlist, profile list, store + redaction), `tools.py` (agent tools),
`page.py` (dashboard page). Docs: `website/docs/user-guide/features/built-in-plugins.md#key-prompt`.
Tests: `tests/plugins/test_key_prompt_plugin.py`.
