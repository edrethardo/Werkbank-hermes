"""Secret intake for user prompts (WB-1212): find secrets in what the USER sends, replace each one
with a placeholder before anything persists, mirrors or reaches the model, and hold the value in
process memory for a short while so the user can save it to the vault without the model.

    "Hier ist mein Token ghp_…, bitte einrichten"  →  "Hier ist mein Token [GEHEIMNIS:github-token-1], bitte einrichten"

Contract:

* :func:`intake` is the one entry point the surfaces call (gateway ``BasePlatformAdapter.handle_message``,
  the Werkbank plugin before its Telegram mirror, the classic CLI before staging the turn, and
  ``run_conversation`` as the backstop for every other host). Only the secret spans change; every
  other byte of the text stays as it was. A text without findings comes back as the same object.
* Placeholders are ``[GEHEIMNIS:<art>-<n>]``; ``n`` counts per scope (session) and art, and the same
  value in the same scope keeps its placeholder. Placeholders never match a detector, so running
  intake twice (plugin, then gateway, then backstop) is a no-op the second time.
* The value lives only in :class:`_Store` (process memory) for ``TTL_SECONDS``; then it is wiped. It is
  never logged — the audit line names art, placeholder and scope hash only. Every intake value is
  also registered with the model-egress redaction (``register_vault_redaction_value``), so a tool
  result that echoes it later is scrubbed too; save/discard/expiry removes that registration again.
* Text joining a RUNNING turn goes through :func:`intake_turn_text` inside ``AIAgent.steer`` /
  ``redirect`` (TUI, classic CLI, ACP, API server and gateway all end there); the gateway also
  re-scans the text it prepared after ``handle_message`` (STT transcript, document notes).
* Fail-open: a detector exception lets the text pass unchanged (warning logged); the next stage
  (gateway → steer/redirect → ``run_conversation`` backstop) scans again.
* :func:`save` writes a ``secret`` vault item (label suggested from art + key name + placeholder) and
  wipes the in-memory value; :func:`discard` wipes it. Both run without the model (button / CLI prompt).
* Switch: ``security.secret_intake`` (default on). Off → :func:`intake` returns the text unchanged.

Detection reuses the provider-prefix catalogue of ``agent/redact.py`` and adds a narrow set of
shapes (Telegram bot token, PEM private key, 3-part JWT, bearer header, DSN / URL password,
``password=`` / ``token:`` assignments with an opaque value). Commit SHAs, UUIDs and bare hashes
are NOT findings on their own; see ``tests/agent/test_secret_intake.py`` for the false-alarm corpus.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes.secret_intake")

TTL_SECONDS = 600.0
PLACEHOLDER_RE = re.compile(r"\[GEHEIMNIS:[a-z0-9-]+\]")   # numbered (intake) and bare (mask_text) forms
_MAX_SCAN_CHARS = 400_000   # longer pastes are scanned in full anyway by redact on egress; keep intake bounded

# -- detection ------------------------------------------------------------------------------------

# (literal prefix, art) — first match wins, so longer prefixes come first.
_PREFIX_ARTS: Tuple[Tuple[str, str], ...] = (
    ("sk-ant-", "anthropic-key"), ("sk-or-", "openrouter-key"), ("sk-proj-", "openai-key"),
    ("sk_live_", "stripe-key"), ("sk_test_", "stripe-key"), ("rk_live_", "stripe-key"),
    ("sk-", "api-key"), ("sk_", "api-key"),
    ("github_pat_", "github-token"), ("ghp_", "github-token"), ("gho_", "github-token"),
    ("ghu_", "github-token"), ("ghs_", "github-token"), ("ghr_", "github-token"),
    ("xapp-", "slack-token"), ("xox", "slack-token"), ("AIza", "google-key"), ("AKIA", "aws-key"),
    ("hf_", "huggingface-token"), ("npm_", "npm-token"), ("pypi-", "pypi-token"),
    ("gl", "gitlab-token"), ("GR1348941", "gitlab-token"), ("SG.", "sendgrid-key"),
    ("xai-", "xai-key"), ("gsk_", "groq-key"), ("ntn_", "notion-token"),
)

_KEYWORD_RE = re.compile(
    r"passwor[dt]|passwd|kennwort|pw|pass"
    r"|(?:api|auth|access|refresh|session|secret|bearer|private|client)[ _.\-]?(?:key|token|secret)"
    r"|apikey|token|secret|geheimnis|credential",
    re.IGNORECASE,
)
# ``pwd`` is the working directory (``PWD=/data/…``), never a password key; ``pw``/``pass`` only count
# as a whole word of the key (``DB_PASS``, ``pw:``) — the word-bounded check below enforces that.
_PASSWORD_KEYWORD_RE = re.compile(r"passwor[dt]|passwd|kennwort|pw|pass", re.IGNORECASE)
# key, optional closing quote, ``=`` or ``:``, optional opening quote, the value (no whitespace/quote/delimiter)
_ASSIGN_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<key>[A-Za-z][A-Za-z0-9_.\-]{0,60})[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"(?P<val>[^\s\"'`,;<>(){}\[\]]{6,})"
)
_BEARER_RE = re.compile(r"\bBearer[ \t]+(?P<val>[A-Za-z0-9._~+/=\-]{20,})")
_JWT3_RE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_NOT_A_VALUE_RE = re.compile(
    r"^(?:\$|%|\*{3}|\.{3}|x{3,}|os\.(?:getenv|environ)|process\.env|env\.|none$|null$|true$|false$|"
    r"required$|optional$|string$|example|your[_-]|changeme$|placeholder|redacted|«redacted)",
    re.IGNORECASE,
)
_WORDISH_SEGMENT_RE = re.compile(r"[a-z]+|[0-9]+")
# Values that are code, paths, file names or placeholders — not secrets (review corpus WB-1212):
_DOTTED_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")      # settings.DB_PASSWORD
_PATH_RE = re.compile(r"(?:/|~|\.{1,2}/)")                                                 # /x, ~/x, ./x, ../x
_FILENAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*\.[A-Za-z][A-Za-z0-9]{0,5}")          # test_x.py, db.txt
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z]{3,}")                        # MY_APP_SECRET, CHANGEME
_PLACEHOLDER_WORD_RE = re.compile(r"(?i)(?:replace|change|insert|enter|your|dein|todo|fixme|placeholder)")
_SNAKE_IDENT_SEGMENT_RE = re.compile(r"[a-z]{2,}|[a-z]{0,3}[0-9]{1,4}")                      # tokenizer_v2_large_model


def _value_is_code_or_placeholder(value: str, after: str) -> bool:
    """True for values a human writes in code/config prose, not as a secret: a call or attribute
    expression, a path, a file name with an extension, an ENV-variable name or an upper-case
    placeholder (``REPLACE_ME…``, ``CHANGEME``), ``xxx…``. *after* is the text right after the value."""
    if after.startswith("("):
        return True                                   # get_token(), getpass.getpass()
    if _PATH_RE.match(value) or _DOTTED_IDENT_RE.fullmatch(value) or _FILENAME_RE.fullmatch(value):
        return True
    if _ENV_NAME_RE.fullmatch(value):
        return True
    if re.fullmatch(r"(?i)x{3,}", value):
        return True
    if value.isupper() and _PLACEHOLDER_WORD_RE.search(value):
        return True
    return False


def _is_snake_identifier(value: str) -> bool:
    """``tokenizer_v2_large_model``: lower-case words joined by ``_``/``-`` — a name, not a token."""
    segments = re.split(r"[_-]", value)
    return len(segments) >= 2 and all(_SNAKE_IDENT_SEGMENT_RE.fullmatch(s) for s in segments)


def _redact_mod():
    from agent import redact
    return redact


def _prefix_art(token: str) -> str:
    for prefix, art in _PREFIX_ARTS:
        if token.startswith(prefix):
            return art
    return "api-key"


def _looks_random(token: str) -> bool:
    """Vendor-prefixed tokens must still look generated: ``sk-learn-upgrade`` (all segments plain
    lowercase words or numbers) is a branch name, not a key."""
    if len(token) < 16:
        return False
    segments = [s for s in re.split(r"[-_.]", token) if s]
    return not all(_WORDISH_SEGMENT_RE.fullmatch(s) for s in segments)


def _assignment_value_is_secret(key: str, value: str, after: str = "") -> bool:
    r = _redact_mod()
    if PLACEHOLDER_RE.search(value) or _NOT_A_VALUE_RE.match(value):
        return False
    if _value_is_code_or_placeholder(value, after):
        return False
    if value.endswith(("://",)) or "://" in value:
        return False  # a URL as the value: its own userinfo rule decides
    if r._has_word_bounded_keyword(key, _PASSWORD_KEYWORD_RE):
        # A human password: any non-trivial value, but not a plain lowercase word (``password: required``).
        return len(value) >= 6 and not re.fullmatch(r"[a-zäöüß]+", value)
    return len(value) >= 12 and not _is_snake_identifier(value) and r._looks_like_opaque_credential(value)


@dataclass(frozen=True)
class Finding:
    """One secret span in a text. ``value`` is the raw secret — never log or persist a Finding."""

    start: int
    end: int
    art: str
    hint: str = ""          # key name the value was assigned to (``OPENAI_API_KEY``), for the vault label

    def __repr__(self) -> str:  # never the value
        return f"Finding(art={self.art!r}, start={self.start}, end={self.end})"


def find_secrets(text: str) -> List[Finding]:
    """Non-overlapping secret spans in *text*, in order. Pure; no state, no logging."""
    if not isinstance(text, str) or not text:
        return []
    r = _redact_mod()
    scan = text[:_MAX_SCAN_CHARS]
    found: List[Finding] = []

    def add(start: int, end: int, art: str, hint: str = "") -> None:
        if end - start < 6:
            return
        if PLACEHOLDER_RE.fullmatch(scan[start:end]):
            return
        found.append(Finding(start, end, art, hint))

    for m in r._PRIVATE_KEY_RE.finditer(scan):
        add(m.start(), m.end(), "private-key")
    for m in r._TELEGRAM_RE.finditer(scan):
        add(m.start(2), m.end(3), "telegram-bot-token")
    for m in _JWT3_RE.finditer(scan):
        add(m.start(), m.end(), "jwt")
    for m in r._PREFIX_RE.finditer(scan):
        token = m.group(1)
        if _looks_random(token):
            add(m.start(1), m.end(1), _prefix_art(token))
    for m in _BEARER_RE.finditer(scan):
        if _looks_random(m.group("val")):
            add(m.start("val"), m.end("val"), "bearer-token")
    for m in r._DB_CONNSTR_RE.finditer(scan):
        add(m.start(2), m.end(2), "db-passwort")
    for m in r._URL_USERINFO_RE.finditer(scan):
        add(m.start(3), m.end(3), "url-passwort")
    for m in _ASSIGN_RE.finditer(scan):
        key, value = m.group("key"), m.group("val")
        if not r._has_word_bounded_keyword(key, _KEYWORD_RE):
            continue
        if not _assignment_value_is_secret(key, value, scan[m.end("val"):m.end("val") + 1]):
            continue
        art = "passwort" if r._has_word_bounded_keyword(key, _PASSWORD_KEYWORD_RE) else "token"
        add(m.start("val"), m.end("val"), art, key)

    # Nothing inside an earlier placeholder is a finding (``GEHEIMNIS:github-token-1`` reads like
    # ``key:value``): intake must be idempotent across the plugin → gateway → backstop chain.
    taken = [(m.start(), m.end()) for m in PLACEHOLDER_RE.finditer(scan)]
    if taken:
        found = [f for f in found if not any(f.start < e and s < f.end for s, e in taken)]
    # First rule wins on overlap (the specific shapes come first); keep the assignment's key hint.
    found.sort(key=lambda f: f.start)
    kept: List[Finding] = []
    for f in found:
        if kept and f.start < kept[-1].end:
            prev = kept[-1]
            if f.hint and not prev.hint and (f.start, f.end) == (prev.start, prev.end):
                kept[-1] = Finding(prev.start, prev.end, prev.art, f.hint)
            continue
        kept.append(f)
    return kept


# -- in-memory store ------------------------------------------------------------------------------

@dataclass
class PendingSecret:
    """A secret waiting for the user's decision. ``value`` is wiped on save/discard/expiry."""

    id: str
    scope: str
    art: str
    placeholder: str
    hint: str
    surface: str
    home: str
    created: float
    expires: float
    value: Optional[str] = field(default=None, repr=False)
    offered: bool = False
    status: str = "pending"          # pending | saved | discarded | expired
    handle: Optional[str] = None

    def label(self) -> str:
        return suggest_name(self)


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: Dict[str, PendingSecret] = {}
        self._counters: Dict[Tuple[str, str], int] = {}
        self._timer: Optional[threading.Timer] = None

    # all callers hold no lock
    def _purge_locked(self, now: float) -> List[PendingSecret]:
        expired = [e for e in self._by_id.values() if e.status == "pending" and e.expires <= now]
        for e in expired:
            _unregister_egress(e.value, e.home)
            e.value = None
            e.status = "expired"
        # forget finished entries after another TTL so the dict stays bounded
        for key in [k for k, e in self._by_id.items() if e.status != "pending" and e.expires + TTL_SECONDS <= now]:
            self._by_id.pop(key, None)
        return expired

    def purge(self, now: Optional[float] = None) -> int:
        with self._lock:
            expired = self._purge_locked(time.time() if now is None else now)
        for e in expired:
            _audit("verfallen", e)
        return len(expired)

    def _arm_timer(self) -> None:
        if self._timer is not None and self._timer.is_alive():
            return
        timer = threading.Timer(TTL_SECONDS + 1.0, self._tick)
        timer.daemon = True
        timer.name = "secret-intake-expiry"
        self._timer = timer
        timer.start()

    def _tick(self) -> None:
        self.purge()
        with self._lock:
            self._timer = None
            more = any(e.status == "pending" for e in self._by_id.values())
        if more:
            self._arm_timer()

    def placeholder_for(self, scope: str, art: str, value: str, *, hint: str, surface: str,
                        home: str) -> Tuple[PendingSecret, bool]:
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            for e in self._by_id.values():
                if e.scope == scope and e.status == "pending" and e.value == value:
                    return e, False
            n = self._counters.get((scope, art), 0) + 1
            self._counters[(scope, art)] = n
            entry = PendingSecret(
                id=_secrets.token_hex(6), scope=scope, art=art, placeholder=f"[GEHEIMNIS:{art}-{n}]",
                hint=hint, surface=surface, home=home, created=now, expires=now + TTL_SECONDS, value=value)
            self._by_id[entry.id] = entry
        self._arm_timer()
        return entry, True

    def get(self, entry_id: str) -> Optional[PendingSecret]:
        self.purge()
        with self._lock:
            return self._by_id.get(entry_id)

    def take_unoffered(self, scope: str, text: str) -> List[PendingSecret]:
        self.purge()
        with self._lock:
            out = [e for e in self._by_id.values()
                   if e.scope == scope and e.status == "pending" and not e.offered and e.placeholder in text]
            for e in out:
                e.offered = True
        return sorted(out, key=lambda e: text.find(e.placeholder))

    def finish(self, entry_id: str, status: str, handle: Optional[str] = None) -> Optional[PendingSecret]:
        with self._lock:
            e = self._by_id.get(entry_id)
            if e is None or e.status != "pending":
                return None
            _unregister_egress(e.value, e.home)
            e.value = None
            e.status = status
            e.handle = handle
            return e

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            for e in self._by_id.values():
                e.value = None
            self._by_id.clear()
            self._counters.clear()


_STORE = _Store()


def store() -> _Store:
    return _STORE


# -- policy / audit -------------------------------------------------------------------------------

def enabled() -> bool:
    """``security.secret_intake`` of the active profile; default on. Unreadable config → on."""
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly().get("security") or {}).get("secret_intake", True)
    except Exception:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return raw is not False


def _scope_tag(scope: str) -> str:
    return hashlib.sha256(scope.encode("utf-8", "replace")).hexdigest()[:10]


def _audit(action: str, entry: PendingSecret, extra: str = "") -> None:
    """One audit line, never the value: action, art, placeholder, surface, hashed scope."""
    logger.info("secret_intake %s art=%s platzhalter=%s quelle=%s scope=%s%s", action, entry.art,
                entry.placeholder, entry.surface, _scope_tag(entry.scope), f" {extra}" if extra else "")


# -- public API -----------------------------------------------------------------------------------

@dataclass
class IntakeResult:
    text: Any
    new: List[PendingSecret]

    @property
    def changed(self) -> bool:
        return bool(self.new) or self._replaced

    _replaced: bool = False


def _home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return ""


def redact_text(text: str, scope: str, *, surface: str = "unbekannt") -> Tuple[str, List[PendingSecret]]:
    """Replace every finding in *text*; returns ``(text, entries created now)``. *text* object is
    returned unchanged when nothing was found."""
    findings = find_secrets(text)
    if not findings:
        return text, []
    home = _home()
    out: List[str] = []
    pos = 0
    created: List[PendingSecret] = []
    for f in findings:
        value = text[f.start:f.end]
        entry, is_new = _STORE.placeholder_for(scope, f.art, value, hint=f.hint, surface=surface, home=home)
        if is_new:
            created.append(entry)
            _register_egress(value)
            _audit("erkannt", entry)
        out.append(text[pos:f.start])
        out.append(entry.placeholder)
        pos = f.end
    out.append(text[pos:])
    return "".join(out), created


def mask_text(text: str) -> str:
    """*text* with every finding replaced by ``[GEHEIMNIS:<art>]`` — no store, no audit, nothing kept.
    For copies that must never hold the value but need no vault offer (input history files)."""
    findings = find_secrets(text)
    if not findings:
        return text
    out, pos = [], 0
    for f in findings:
        out.append(text[pos:f.start])
        out.append(f"[GEHEIMNIS:{f.art}]")
        pos = f.end
    out.append(text[pos:])
    return "".join(out)


def intake(message: Any, scope: str, *, surface: str = "unbekannt") -> IntakeResult:
    """Redact a user message (str, or a multimodal content list whose ``text`` parts are redacted).
    Returns the message unchanged (same object) when the switch is off or nothing was found."""
    if not enabled():
        return IntakeResult(message, [])
    scope = str(scope or "global")
    if isinstance(message, str):
        new_text, created = redact_text(message, scope, surface=surface)
        res = IntakeResult(new_text, created)
        res._replaced = new_text is not message
        return res
    if isinstance(message, list):
        changed = False
        created_all: List[PendingSecret] = []
        parts = []
        for part in message:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                new_text, created = redact_text(part["text"], scope, surface=surface)
                if new_text is not part["text"]:
                    part = {**part, "text": new_text}
                    changed = True
                created_all.extend(created)
            parts.append(part)
        res = IntakeResult(parts if changed else message, created_all)
        res._replaced = changed
        return res
    return IntakeResult(message, [])


def intake_turn_text(text: Any, scope: str, *, surface: str = "steer") -> Any:
    """Intake for text folded into a RUNNING turn (``AIAgent.steer`` / ``redirect``): every surface
    (TUI, classic CLI, ACP, API server, gateway) hands its mid-turn text to those verbs, so the
    replacement sits there as the last stage. Keeps a :class:`agent.folded_messages.FoldedText`
    a FoldedText and redacts each part's own ``text`` too (the Werkbank chat draws the parts).
    Fails open — the original text — only when the detector itself raises (logged)."""
    if not isinstance(text, str) or not text:
        return text
    try:
        if not enabled():
            return text
        from agent.folded_messages import FoldedText, folded_parts
        scope = str(scope or "global")
        new_text = redact_text(str(text), scope, surface=surface)[0]
        parts = folded_parts(text)
        new_parts = [{**p, "text": redact_text(p["text"], scope, surface=surface)[0]}
                     if isinstance(p.get("text"), str) and p["text"] else p for p in parts]
        if new_text == str(text) and new_parts == parts:
            return text                                   # nothing found: the same object
        return FoldedText(new_text, new_parts) if parts else new_text
    except Exception:
        logger.warning("secret_intake: turn text scan failed (text passes unchanged)", exc_info=False)
        return text


def offer_candidates(scope: str, text: Any) -> List[PendingSecret]:
    """Pending entries of *scope* named in *text* that have not been offered to the user yet; marks
    them offered. A Werkbank message redacted in the plugin gets its card from the gateway this way."""
    if isinstance(text, list):
        text = "\n".join(str(p.get("text") or "") for p in text if isinstance(p, dict))
    if not isinstance(text, str) or "[GEHEIMNIS:" not in text:
        return []
    return _STORE.take_unoffered(str(scope or "global"), text)


_ART_NAMES = {
    "github-token": "GitHub-Token", "openai-key": "OpenAI-Key", "anthropic-key": "Anthropic-Key",
    "openrouter-key": "OpenRouter-Key", "stripe-key": "Stripe-Key", "slack-token": "Slack-Token",
    "google-key": "Google-API-Key", "aws-key": "AWS-Key", "huggingface-token": "Hugging-Face-Token",
    "npm-token": "npm-Token", "pypi-token": "PyPI-Token", "gitlab-token": "GitLab-Token",
    "telegram-bot-token": "Telegram-Bot-Token", "private-key": "Privater Schlüssel", "jwt": "JWT",
    "bearer-token": "Bearer-Token", "db-passwort": "Datenbank-Passwort", "url-passwort": "URL-Passwort",
    "passwort": "Passwort", "token": "Token", "api-key": "API-Key",
}


def suggest_name(entry: PendingSecret) -> str:
    base = entry.hint or _ART_NAMES.get(entry.art, entry.art)
    day = datetime.fromtimestamp(entry.created).strftime("%Y-%m-%d")
    return f"{base} · {entry.placeholder[1:-1]} · {day}"


def save(entry_id: str, label: Optional[str] = None, *, vault_factory: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Store the pending value as a ``secret`` vault item of the profile it arrived in. Returns
    ``{"ok", "handle", "label", "placeholder"}`` or ``{"ok": False, "reason"}`` — never the value."""
    entry = _STORE.get(entry_id)
    if entry is None or entry.status != "pending" or entry.value is None:
        return {"ok": False, "reason": "abgelaufen", "placeholder": getattr(entry, "placeholder", None)}
    name = (label or "").strip() or suggest_name(entry)
    try:
        if vault_factory is not None:
            vault = vault_factory(entry.home)
        else:
            from agent.vault_store import VaultStore, get_vault_store
            vault = VaultStore(Path(entry.home) / "vault") if entry.home else get_vault_store()
        meta = vault.add_item("secret", name, {"value": entry.value})
    except Exception as exc:  # never echo the exception text: it may carry the payload
        logger.warning("secret_intake speichern fehlgeschlagen art=%s (%s)", entry.art, type(exc).__name__)
        return {"ok": False, "reason": "tresor_fehler", "placeholder": entry.placeholder}
    done = _STORE.finish(entry_id, "saved", handle=meta.id)
    if done is None:
        return {"ok": False, "reason": "abgelaufen", "placeholder": entry.placeholder}
    _audit("gespeichert", done, f"handle={meta.id}")
    return {"ok": True, "handle": meta.id, "label": name, "placeholder": done.placeholder}


def discard(entry_id: str) -> Dict[str, Any]:
    done = _STORE.finish(entry_id, "discarded")
    if done is None:
        entry = _STORE.get(entry_id)
        return {"ok": False, "reason": "abgelaufen", "placeholder": getattr(entry, "placeholder", None)}
    _audit("verworfen", done)
    return {"ok": True, "placeholder": done.placeholder}


def apply_choice(value: str, *, vault_factory: Optional[Callable[[str], Any]] = None) -> str:
    """Answer of a card / prompt: ``save:<id>[,<id>…]`` or ``drop:<id>[,<id>…]``. Returns the
    user-facing result text (German, no values). Runs without the model."""
    action, _, ids = str(value or "").partition(":")
    lines: List[str] = []
    for entry_id in [i for i in ids.split(",") if i]:
        if action == "save":
            res = save(entry_id, vault_factory=vault_factory)
            if res.get("ok"):
                lines.append(f"🔐 {res['placeholder']} im Tresor gespeichert als „{res['label']}“ "
                             f"(Handle {res['handle']}).")
            elif res.get("reason") == "tresor_fehler":
                lines.append(f"⚠️ {res.get('placeholder') or 'Geheimnis'}: Tresor nicht beschreibbar, nichts gespeichert.")
            else:
                lines.append(f"⌛ {res.get('placeholder') or 'Geheimnis'}: schon abgelaufen oder entschieden.")
        elif action == "drop":
            res = discard(entry_id)
            lines.append(f"🗑 {res.get('placeholder') or 'Geheimnis'} verworfen."
                         if res.get("ok") else f"⌛ {res.get('placeholder') or 'Geheimnis'}: schon abgelaufen oder entschieden.")
    return "\n".join(lines) or "Nichts zu tun."


def card_text(entries: List[PendingSecret]) -> str:
    """Body of the suggestion card (no values; Markdown-neutral wording)."""
    minutes = int(TTL_SECONDS // 60)
    head = ("🔐 In deiner Nachricht war ein Geheimnis. Der Agent sieht nur den Platzhalter:"
            if len(entries) == 1 else
            f"🔐 In deiner Nachricht waren {len(entries)} Geheimnisse. Der Agent sieht nur die Platzhalter:")
    rows = [f"• {e.placeholder} → Name: {suggest_name(e)}" for e in entries]
    tail = f"Im Tresor speichern? Ohne Entscheidung wird der Wert in {minutes} Minuten verworfen."
    return "\n".join([head, *rows, tail])


def card_choices(entries: List[PendingSecret]) -> List[Dict[str, str]]:
    ids = ",".join(e.id for e in entries)
    return [{"value": f"save:{ids}", "label": "💾 Im Tresor speichern"},
            {"value": f"drop:{ids}", "label": "🗑 Verwerfen"}]


def _unregister_egress(value: Optional[str], home: str) -> None:
    """Counterpart of :func:`_register_egress` when the entry finishes (save/discard/expiry). A saved
    value is re-registered by the vault fill path whenever it is used; a tool result echoing a
    discarded/expired value after that is no longer scrubbed (documented trade-off)."""
    if not value:
        return
    try:
        from agent.redact import unregister_vault_redaction_value
        unregister_vault_redaction_value(value, home or None)
    except Exception:
        logger.debug("secret_intake: egress unregistration failed", exc_info=False)


def _register_egress(value: str) -> None:
    try:
        from agent.redact import register_vault_redaction_value
        register_vault_redaction_value(value)
    except Exception:
        logger.debug("secret_intake: egress registration failed", exc_info=False)


__all__ = [
    "Finding", "IntakeResult", "PendingSecret", "PLACEHOLDER_RE", "TTL_SECONDS", "apply_choice",
    "card_choices", "card_text", "discard", "enabled", "find_secrets", "intake", "intake_turn_text",
    "offer_candidates",
    "redact_text", "save", "store", "suggest_name",
]
