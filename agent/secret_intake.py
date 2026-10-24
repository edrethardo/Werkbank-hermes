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
  also registered with the model-egress redaction (``register_intake_redaction_value``, one reference
  per open entry), so a tool result that echoes it later is scrubbed too. Discard/expiry releases the
  reference; the value leaves the scrub list only when no other open entry holds it and no vault fill
  registered it. A SAVED value stays scrubbed for the profile's lifetime (durable set; after a restart
  the profile's ``secret`` vault items are loaded on the first scrub).
* Text joining a RUNNING turn goes through :func:`intake_turn_text` inside ``AIAgent.steer`` /
  ``redirect`` (TUI, classic CLI, ACP, API server and gateway all end there); the gateway also
  re-scans the text it prepared after ``handle_message`` (STT transcript, document notes).
* Fail-open: a detector exception lets the text pass unchanged (warning logged); the next stage
  (gateway → steer/redirect → ``run_conversation`` backstop) scans again.
* :func:`save` writes a ``secret`` vault item (label suggested from art + key name + placeholder) and
  wipes the in-memory value; :func:`discard` wipes it. Both run without the model (button / CLI prompt).
* :func:`release` (WB-1261, card answer „Kein Geheimnis“) hands the original value back to the
  caller (the gateway injects it into the session as a follow-up turn, the CLI puts it back into the
  turn it is about to stage) and saves nothing. Only the sender of the message (``sender`` given to
  :func:`intake`; adopted later by :func:`offer_candidates` only for Werkbank-plugin entries), only in
  the entry's own scope, only while pending (TTL), once. (sender, art, keyed process-local HMAC of
  the value) land in an exception list so the same value from the same sender raises no new card (and
  the follow-up turn is not redacted again); another sender or a scan without one still redacts it.
  The cleartext is never kept. Audit lines name placeholder/art, never the value or the actor.
* Switch: ``security.secret_intake`` (default on). Off → :func:`intake` returns the text unchanged.

Detection reuses the provider-prefix catalogue of ``agent/redact.py`` and adds a narrow set of
shapes (Telegram bot token, PEM private key, 3-part JWT, bearer header, DSN / URL password,
``password=`` / ``token:`` assignments with an opaque value — also behind a ``word:`` lead-in and as
URL query parameters — and a bare pasted key: ≥ 40 chars, mixed case + digits, high entropy).
Path / file name / ENV name / snake-case exemptions need a clear structure and lose against an
entropy counter-check. Commit SHAs, UUIDs and hex hashes are NOT findings on their own; a bare
base64 digest (44/88 chars) is — it cannot be told from ``openssl rand -base64 32``. See
``tests/agent/test_secret_intake.py`` for the corpus.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets as _secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes.secret_intake")

TTL_SECONDS = 600.0
# Open (pending) entries per process. Past it the oldest open entry is retired early (value wiped,
# egress ref released, like expiry), so the store — and with it the intake scrub set in
# ``agent.redact`` (≤ 2 forms per open entry) — never grows unbounded within one TTL window.
MAX_PENDING = 256
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
# key, optional closing quote, ``=`` or ``:``, optional opening quote, the value (no whitespace/quote/delimiter).
# A lead-in like ``Hinweis:`` / ``login:`` / ``env:`` swallows ``password=…`` as its value; the value
# is then scanned again (:func:`_assign_matches`) so the assignment behind it is still seen.
_ASSIGN_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<key>[A-Za-z][A-Za-z0-9_.\-]{0,60})[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"(?P<val>[^\s\"'`,;<>(){}\[\]]{6,})"
)
_MAX_LEAD_IN_DEPTH = 4           # ``a:b:c:password=…``; bounded so ``a=a=a=…`` stays linear-ish
_INNER_ASSIGN_RE = re.compile(r"(?P<key>[A-Za-z][A-Za-z0-9_.\-]{0,60})[\"']?[ \t]*[:=]")
# URL query parameter: the value ends at ``&`` / ``#`` (a plain assignment would run on into the next one).
_URL_QUERY_RE = re.compile(r"[?&](?P<key>[A-Za-z0-9_.\-]{1,60})=(?P<val>[^&#\s\"'<>`]{6,})")
_BEARER_RE = re.compile(r"\bBearer[ \t]+(?P<val>[A-Za-z0-9._~+/=\-]{20,})")
_JWT3_RE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
# A bare pasted key: a whitespace/quote-delimited run of key characters, no key name, no prefix.
_BARE_RUN_RE = re.compile(r"(?<![^\s\"'`(\[{<>:=,;])(?P<val>[A-Za-z0-9_\-+/.~]{40,512}={0,3})(?![^\s\"'`)\]}<>,;.!?])")
_IDENT_WORD_RE = re.compile(r"[A-Z]?[a-z]{3,}")   # word runs of an identifier; rare in random keys
_PEM_BLOCK_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL)
_PUBKEY_LEAD_RE = re.compile(r"(?:ssh|ecdsa|sk)-[A-Za-z0-9@.\-]+[ \t]+$")   # ``ssh-ed25519 AAAA…`` is public
_BARE_MIN_ENTROPY = 4.3          # bits/char; random base62 of 40+ chars ≈ 4.8–5.2, prose/CamelCase ≈ 3.6–4.2
_NOT_A_VALUE_RE = re.compile(
    r"^(?:\$|%|\*{3}|\.{3}|x{3,}|os\.(?:getenv|environ)|process\.env|env\.|none$|null$|true$|false$|"
    r"required$|optional$|string$|example|your[_-]|changeme$|placeholder|redacted|«redacted)",
    re.IGNORECASE,
)
_WORDISH_SEGMENT_RE = re.compile(r"[a-z]+|[0-9]+")
_PLACEHOLDER_WORD_RE = re.compile(r"(?i)(?:replace|change|insert|enter|your|dein|todo|fixme|placeholder)")

# -- exemptions with clear structure (review corpus WB-1212). Each one is a SHAPE a human writes in
# code/config prose; an entropy counter-check (:func:`_is_strong`) overrules every one except a call.
# Real file extensions only — ``Hunter2.Abc9`` / ``Sommer2024.de`` are passwords, ``db.txt`` is a file.
_FILE_EXTENSIONS = frozenset("""
    py pyc pyi txt md rst yaml yml json jsonl toml ini cfg conf env pem key pub crt cer csr p12 pfx der
    log csv tsv sh bash zsh fish ps1 bat js mjs cjs ts tsx jsx html htm css scss xml sql sqlite db
    gz tgz zip tar bz2 xz 7z rar pdf png jpg jpeg gif svg webp ico mp3 mp4 wav ogg docx xlsx pptx odt
    lock go rs java kt c h cc cpp hpp rb php pl lua swift dart vue ipynb gpg asc age kdbx service timer
    socket desktop plist properties gradle dockerfile tf tfvars hcl nix
""".split())
_FILENAME_RE = re.compile(r"(?P<stem>[A-Za-z0-9_][A-Za-z0-9_.\-]*)\.(?P<ext>[A-Za-z][A-Za-z0-9]{0,9})")
# Dotted expression: only when it starts with a typical code root or carries an identifier shape.
_DOTTED_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_CODE_ROOTS = frozenset("""
    self cls this settings config conf cfg os env environ request req app ctx context args kwargs opts
    options params data st secrets keyring getpass process window document module vars props state
    current_app django flask g creds credentials auth client session sys
""".split())
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
# ENV name: segments of letters with an optional short number (``AWS_S3_BUCKET``, ``DB_PASSWORD``)
_ENV_SEGMENT_RE = re.compile(r"[A-Z]{2,}[0-9]{0,2}|[A-Z][0-9]{1,2}|[0-9]{1,3}")
_ENV_WORD_PARTS = (
    "KEY", "TOKEN", "SECRET", "PASS", "PWD", "API", "USER", "HOST", "PORT", "URL", "URI", "HOME", "PATH",
    "NAME", "DIR", "FILE", "CONFIG", "ENV", "AUTH", "CLIENT", "ACCESS", "DB", "DATABASE", "SERVICE", "APP",
)
_SNAKE_WORD_RE = re.compile(r"[a-z]{4,}")
_VERSION_SEGMENT_RE = re.compile(r"v?[0-9]{1,4}|[a-z]{1,3}")


def _char_classes(value: str) -> Tuple[bool, bool, bool, bool]:
    """(lower, upper, digit, special) — special excludes the structure characters ``._-/~``."""
    return (any(c.islower() for c in value), any(c.isupper() for c in value),
            any(c.isdigit() for c in value), any(not c.isalnum() and c not in "._-/~" for c in value))


def _shannon(value: str) -> float:
    if not value:
        return 0.0
    n = len(value)
    return -sum((k / n) * math.log2(k / n) for k in Counter(value).values())


def _is_strong(value: str) -> bool:
    """Entropy counter-check: mixed upper/lower case with a digit, or digit + special character, or
    a high per-character Shannon entropy for its length — a generated or chosen password, never a
    name. Overrules the path/file/ENV/snake exemptions."""
    lower, upper, digit, special = _char_classes(value)
    if lower and upper and digit:
        return True
    if digit and special and (lower or upper):
        return True
    return len(value) >= 16 and _shannon(value) >= 4.0 and digit


def _is_path(value: str) -> bool:
    """``/home/u/projekt``, ``~/secrets/db.txt``, ``./run``, ``../config/pw.yaml``, ``config/x.json``:
    ``/``-separated segments without special characters; an absolute or home path needs at least two
    segments (``/9fK$2Lq!m`` and ``~Secr3t!x`` are not paths). No segment may look generated."""
    m = re.match(r"(~/|\.{1,2}/|/)?", value)
    lead = m.group(1) or ""
    body = value[len(lead):]
    segments = body.split("/")
    if not segments or any(not _PATH_SEGMENT_RE.fullmatch(seg) for seg in segments if seg) or not all(segments[:-1]):
        return False
    segments = [seg for seg in segments if seg]
    if not segments:
        return False
    if lead in ("/", "~/"):
        if len(segments) < 2 and not (lead == "~/" and _is_filename(segments[0])):
            return False
    elif lead == "":
        if len(segments) < 2:
            return False
    return not any(_is_strong(seg) and not _is_filename(seg) for seg in segments)


def _is_filename(value: str) -> bool:
    m = _FILENAME_RE.fullmatch(value)
    return bool(m) and m.group("ext").lower() in _FILE_EXTENSIONS and not _is_strong(m.group("stem"))


def _is_code_expression(value: str) -> bool:
    """``settings.DB_PASSWORD``, ``self.config.api_key``, ``request.form.get`` — not ``correct.horse…``."""
    if not _DOTTED_IDENT_RE.fullmatch(value):
        return False
    parts = value.split(".")
    if parts[0] in _CODE_ROOTS:
        return True
    return any("_" in p for p in parts) and not _is_strong(value)


def _is_env_name(value: str) -> bool:
    """``MY_APP_SECRET``, ``OPENAI_API_KEY``, ``AWS_S3_BUCKET``; a single word only with a typical part.
    ``Q7ZK_9XQ2_PLMW_88AA`` / ``KX7_P9Q2`` / ``QWERTZUIOPX`` are values."""
    if not re.fullmatch(r"[A-Z0-9_]+", value) or not value[0].isalpha():
        return False
    segments = value.split("_")
    if not all(_ENV_SEGMENT_RE.fullmatch(seg) for seg in segments):
        return False
    if len(segments) >= 2:
        return sum(1 for seg in segments if re.fullmatch(r"[A-Z]{2,}", seg)) * 2 >= len(segments)
    return any(part in value for part in _ENV_WORD_PARTS)


def _is_snake_identifier(value: str) -> bool:
    """``tokenizer_v2_large_model``: lower-case words joined by ``_``/``-`` with at most a version
    number — a name, not a token. ``xq7_zk9_pw3_mf2_ab4`` has no word and four letter-digit segments."""
    segments = re.split(r"[_-]", value)
    if len(segments) < 2 or not all(re.fullmatch(r"[a-z0-9]+", s) for s in segments):
        return False
    if not any(_SNAKE_WORD_RE.fullmatch(s) for s in segments):
        return False
    mixed = [s for s in segments if not _SNAKE_WORD_RE.fullmatch(s) and not _VERSION_SEGMENT_RE.fullmatch(s)]
    return not mixed


def _value_is_code_or_placeholder(value: str, after: str) -> bool:
    """True for values a human writes in code/config prose, not as a secret: a call, a code
    expression, a path, a file name with a real extension, an ENV-variable name, a snake-case name or
    an upper-case placeholder (``REPLACE_ME…``), ``xxx…``. *after* is the text right after the value."""
    if after.startswith("("):
        return True                                   # get_token(), getpass.getpass()
    if re.fullmatch(r"(?i)x{3,}", value):
        return True
    if value.isupper() and _PLACEHOLDER_WORD_RE.search(value):
        return True
    if _is_code_expression(value):
        return True
    if _is_strong(value) and not _is_path(value):
        return False
    return _is_path(value) or _is_filename(value) or _is_env_name(value) or _is_snake_identifier(value)


# A name part of a file reference (WB-1261): a word in ONE case form (``folio``, ``Depot``, ``PDF``),
# a number, a lower-case hex id (``doc_3f9a1c2b7d4e``), a short letter prefix + number (``DE0000…``,
# ``ABC123``, ``v2``) or a number + short unit (``24h``). Mixed case inside a part (``QxZkPw``) is never
# a word — a block-wise key ``AbCdEf-12345-….txt`` keeps its blocks and stays a key.
_FILE_NAME_PART_RE = re.compile(
    r"[a-z]+|[A-Z][a-z]*|[A-Z]+|[0-9]+|[0-9a-f]{4,32}|[A-Z]{1,4}[0-9]+|[a-z]{1,10}[0-9]+|[0-9]+[a-z]{1,3}")
# Credential containers: a key-like name with one of these is never the file-reference exception
# (``<key>.env`` / ``.pem`` / ``.p12`` / ``client_<key>.json``); the bare-key rule decides as without it.
_CREDENTIAL_EXTENSIONS = frozenset({"env", "key", "pem", "p12", "pfx", "json", "jks", "keystore", "ppk"})


def _is_file_reference(value: str) -> bool:
    """``/home/u/.hermes/cache/documents/doc_3f9a1c2b7d4e_bip_folio_mobile_20261024_0000001.PDF``
    or the file name alone: the last ``/`` segment carries a real extension (any case) and every name
    part — split at ``/ _ - . space`` — is a plain word, number, hex id or prefixed number. A pasted
    key used as a file name (``/tmp/<key>.txt``) keeps its mixed-case segment and stays a key."""
    head, _, last = value.rstrip(".").rpartition("/")   # sentence dot after the path (``saved at: ….PDF.``)
    m = _FILENAME_RE.fullmatch(last)
    if not m or m.group("ext").lower() not in _FILE_EXTENSIONS:
        return False
    if m.group("ext").lower() in _CREDENTIAL_EXTENSIONS:
        return False                                  # only called for an already key-like value
    parts = [p for p in re.split(r"[/_.\- ]", head + "/" + m.group("stem")) if p]
    return bool(parts) and all(_FILE_NAME_PART_RE.fullmatch(p) for p in parts)


def _bare_key_is_secret(value: str) -> bool:
    """A long pasted key alone: ≥ 40 chars, lower + upper + digit, high entropy, no path or URL shape,
    not mostly words. Commit SHAs, hex hashes and UUIDs never pass: hex carries at most 4.0 bits/char
    (UUID with dashes ≈ 4.1), below ``_BARE_MIN_ENTROPY``, and lacks mixed case. A file reference
    (cache path of an uploaded document, a file name with a real extension and plain name parts) is
    not a key either (WB-1261: an upload ``….PDF`` was offered as ``api-key``)."""
    if value.startswith(("//", "~", "./", "../")) or ("/" in value and _is_path(value)):
        return False                                  # path / URL-ish
    if _is_file_reference(value):
        return False                                  # document cache path / file name
    if re.match(r"(?i)(?:sha|md)[0-9]{1,3}[-:]", value):
        return False                                  # SRI ``sha384-…`` / digest ``sha256:…``
    lower, upper, digit, _ = _char_classes(value)
    if not (lower and upper and digit) or sum(c.isdigit() for c in value) < 2:
        return False
    if sum(len(w) for w in _IDENT_WORD_RE.findall(value)) * 5 >= len(value) * 3:
        return False                                  # ``LongVariableName2026Version3`` — mostly words
    return _shannon(value) >= _BARE_MIN_ENTROPY


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
    if len(value) < 12:
        return False
    return r._looks_like_opaque_credential(value) or _is_strong(value) or _letter_digit_segments(value) >= 3


def _letter_digit_segments(value: str) -> int:
    """``xq7_zk9_pw3_mf2_ab4`` / ``Q7ZK_9XQ2_PLMW_88AA``: segments mixing letters and digits."""
    return sum(1 for seg in re.split(r"[_.\-]", value)
               if re.search(r"[A-Za-z]", seg) and re.search(r"[0-9]", seg))


def _assign_matches(scan: str):
    """``_ASSIGN_RE`` matches, plus the matches inside a value that itself starts with an assignment
    (lead-in ``Hinweis: password=…``, ``login:password=…``, ``a:token:…``), up to a fixed depth."""
    stack = [(0, len(scan), 0)]
    while stack:
        lo, hi, depth = stack.pop()
        for m in _ASSIGN_RE.finditer(scan, lo, hi):
            yield m
            if depth < _MAX_LEAD_IN_DEPTH and _INNER_ASSIGN_RE.match(m.group("val")):
                stack.append((m.start("val"), m.end("val"), depth + 1))


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
    for m in _assign_matches(scan):
        key, value = m.group("key"), m.group("val")
        if m.start("key") > 0 and scan[m.start("key") - 1] in "?&":
            continue                                  # URL query: _URL_QUERY_RE splits at ``&``
        if not r._has_word_bounded_keyword(key, _KEYWORD_RE):
            continue
        inner = _INNER_ASSIGN_RE.match(value)
        if inner and r._has_word_bounded_keyword(inner.group("key"), _KEYWORD_RE):
            continue                                  # ``secret: token=…``: the inner assignment decides
        if not _assignment_value_is_secret(key, value, scan[m.end("val"):m.end("val") + 1]):
            continue
        art = "passwort" if r._has_word_bounded_keyword(key, _PASSWORD_KEYWORD_RE) else "token"
        add(m.start("val"), m.end("val"), art, key)
    for m in _URL_QUERY_RE.finditer(scan):
        key, value = m.group("key"), m.group("val")
        if key.lower() not in r._SENSITIVE_QUERY_PARAMS and not r._has_word_bounded_keyword(key, _KEYWORD_RE):
            continue
        if PLACEHOLDER_RE.search(value) or _NOT_A_VALUE_RE.match(value):
            continue
        if r._has_word_bounded_keyword(key, _PASSWORD_KEYWORD_RE):
            ok = not re.fullmatch(r"[a-zäöüß]+", value)
        else:
            ok = len(value) >= 8 and (r._looks_like_opaque_credential(value) or _is_strong(value))
        if ok:
            art = "passwort" if r._has_word_bounded_keyword(key, _PASSWORD_KEYWORD_RE) else "token"
            add(m.start("val"), m.end("val"), art, key)
    pem_spans = [(m.start(), m.end()) for m in _PEM_BLOCK_RE.finditer(scan)] if "-----BEGIN " in scan else []
    for m in _BARE_RUN_RE.finditer(scan):
        value = m.group("val")
        if scan[max(0, m.start() - 7):m.start()].lower() == "base64,":
            continue                                  # data URI payload (inline image)
        if _PUBKEY_LEAD_RE.search(scan[max(0, m.start() - 64):m.start()]):
            continue                                  # SSH public key
        if any(s <= m.start() < e for s, e in pem_spans):
            continue                                  # certificate / public key block body
        if _bare_key_is_secret(value):
            add(m.start("val"), m.end("val"), "api-key")

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
    status: str = "pending"          # pending | saved | discarded | released | expired
    handle: Optional[str] = None
    sender: str = field(default="", repr=False)   # user id of the message's sender (release check)

    def label(self) -> str:
        return suggest_name(self)


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: Dict[str, PendingSecret] = {}
        self._counters: Dict[Tuple[str, str], int] = {}
        self._timer: Optional[threading.Timer] = None

    # all callers hold no lock
    def _purge_locked(self, now: float, reserve: int = 0) -> List[PendingSecret]:
        """Expire entries past TTL; with *reserve*, also retire the oldest open entries so at most
        ``MAX_PENDING - reserve`` stay open (room for the entries about to be created)."""
        expired = [e for e in self._by_id.values() if e.status == "pending" and e.expires <= now]
        if reserve:
            pending = sorted((e for e in self._by_id.values() if e.status == "pending" and e.expires > now),
                             key=lambda e: e.created)
            expired += pending[:max(0, len(pending) - max(0, MAX_PENDING - reserve))]
        for e in expired:
            _release_egress(e.value, e.home)
            e.value = None
            e.status = "expired"
            e.expires = min(e.expires, now)   # retired early: forgotten one TTL from now, like expiry
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
                        home: str, sender: str = "") -> Tuple[PendingSecret, bool]:
        now = time.time()
        entry, is_new = None, False
        with self._lock:
            retired = self._purge_locked(now)
            for e in self._by_id.values():
                if e.scope == scope and e.status == "pending" and e.value == value:
                    entry = e
                    break
            if entry is not None and sender and not entry.sender and entry.surface == _WERKBANK_SURFACE:
                entry.sender = sender
            if entry is None:
                retired += self._purge_locked(now, reserve=1)   # bound: retire the oldest open entry
                n = self._counters.get((scope, art), 0) + 1
                self._counters[(scope, art)] = n
                entry = PendingSecret(
                    id=_secrets.token_hex(6), scope=scope, art=art, placeholder=f"[GEHEIMNIS:{art}-{n}]",
                    hint=hint, surface=surface, home=home, created=now, expires=now + TTL_SECONDS, value=value,
                    sender=sender)
                self._by_id[entry.id] = entry
                _register_egress(value, home)   # under the lock: retire/expiry can't release it first
                is_new = True
        for r in retired:
            _audit("verfallen", r)
        if not is_new:
            return entry, False
        self._arm_timer()
        return entry, True

    def get(self, entry_id: str) -> Optional[PendingSecret]:
        self.purge()
        with self._lock:
            return self._by_id.get(entry_id)

    def take_unoffered(self, scope: str, text: str, sender: str = "") -> List[PendingSecret]:
        self.purge()
        with self._lock:
            out = [e for e in self._by_id.values()
                   if e.scope == scope and e.status == "pending" and not e.offered and e.placeholder in text]
            for e in out:
                e.offered = True
                if sender and not e.sender and e.surface == _WERKBANK_SURFACE:
                    e.sender = sender            # pre-redacted (Werkbank plugin): the card's event names it
        return sorted(out, key=lambda e: text.find(e.placeholder))

    def finish(self, entry_id: str, status: str, handle: Optional[str] = None,
               check: Optional[Callable[[PendingSecret], bool]] = None) -> Optional[PendingSecret]:
        with self._lock:
            e = self._by_id.get(entry_id)
            if e is None or e.status != "pending" or e.expires <= time.time():
                return None
            if check is not None and not check(e):
                return None
            if status == "saved":
                _register_durable_egress(e.value, e.home)   # saved: scrubbed for the profile's lifetime
            _release_egress(e.value, e.home)
            e.value = None
            e.status = status
            e.handle = handle
            return e

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            for e in self._by_id.values():
                if e.status == "pending":
                    _release_egress(e.value, e.home)
                e.value = None
            self._by_id.clear()
            self._counters.clear()


_STORE = _Store()
# The one path whose entries are created without a sender on purpose (the Werkbank plugin redacts the
# board text before the gateway event exists); only there may the card's event name the sender later.
# Any other sender-less entry (reply quote, edit, STT, backstop) stays unreleasable (WB-1261 F4).
_WERKBANK_SURFACE = "werkbank"


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


# The classic CLI's user is the local owner: its intake entries and „Kein Geheimnis“ answers carry this
# marker, and the CLI agent's own backstop scans with it (WB-1261).
CLI_SENDER = "cli-lokal"


def agent_sender(agent: Any) -> str:
    """Sender for scans inside an agent run: the gateway user it runs for (``agent._user_id``), the
    local owner for the classic CLI, else "" (no release applies)."""
    uid = str(getattr(agent, "_user_id", None) or "")
    if uid:
        return uid
    return CLI_SENDER if str(getattr(agent, "platform", "") or "") == "cli" else ""


def _unreleased(text: str, findings: List[Finding], home: str, sender: str = "") -> List[Finding]:
    """*findings* minus the values *sender* released as „Kein Geheimnis“ (same sender, art, value).
    No sender known → no release applies (the value is a secret again: the safe direction)."""
    if not findings or not sender or not _RELEASED.get(home):
        return findings
    return [f for f in findings if not _is_released(sender, f.art, text[f.start:f.end], home)]


def redact_text(text: str, scope: str, *, surface: str = "unbekannt",
                sender: str = "") -> Tuple[str, List[PendingSecret]]:
    """Replace every finding in *text*; returns ``(text, entries created now)``. *text* object is
    returned unchanged when nothing was found."""
    findings = find_secrets(text)
    if not findings:
        return text, []
    home = _home()
    findings = _unreleased(text, findings, home, sender)
    if not findings:
        return text, []
    out: List[str] = []
    pos = 0
    created: List[PendingSecret] = []
    for f in findings:
        value = text[f.start:f.end]
        entry, is_new = _STORE.placeholder_for(scope, f.art, value, hint=f.hint, surface=surface, home=home,
                                               sender=sender)
        if is_new:
            created.append(entry)
            _audit("erkannt", entry)
        out.append(text[pos:f.start])
        out.append(entry.placeholder)
        pos = f.end
    out.append(text[pos:])
    return "".join(out), created


def mask_text(text: str, *, sender: str = "") -> str:
    """*text* with every finding replaced by ``[GEHEIMNIS:<art>]`` — no store, no audit, nothing kept.
    For copies that must never hold the value but need no vault offer (input history files). Values
    *sender* released stay; without a sender every finding is masked."""
    findings = _unreleased(text, find_secrets(text), _home(), str(sender or ""))
    if not findings:
        return text
    out, pos = [], 0
    for f in findings:
        out.append(text[pos:f.start])
        out.append(f"[GEHEIMNIS:{f.art}]")
        pos = f.end
    out.append(text[pos:])
    return "".join(out)


def intake(message: Any, scope: str, *, surface: str = "unbekannt", sender: str = "") -> IntakeResult:
    """Redact a user message (str, or a multimodal content list whose ``text`` parts are redacted).
    Returns the message unchanged (same object) when the switch is off or nothing was found."""
    if not enabled():
        return IntakeResult(message, [])
    scope = str(scope or "global")
    if isinstance(message, str):
        new_text, created = redact_text(message, scope, surface=surface, sender=sender)
        res = IntakeResult(new_text, created)
        res._replaced = new_text is not message
        return res
    if isinstance(message, list):
        changed = False
        created_all: List[PendingSecret] = []
        parts = []
        for part in message:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                new_text, created = redact_text(part["text"], scope, surface=surface, sender=sender)
                if new_text is not part["text"]:
                    part = {**part, "text": new_text}
                    changed = True
                created_all.extend(created)
            parts.append(part)
        res = IntakeResult(parts if changed else message, created_all)
        res._replaced = changed
        return res
    return IntakeResult(message, [])


def intake_turn_text(text: Any, scope: str, *, surface: str = "steer", sender: str = "") -> Any:
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
        sender = str(sender or "")
        new_text = redact_text(str(text), scope, surface=surface, sender=sender)[0]
        parts = folded_parts(text)
        new_parts = [{**p, "text": redact_text(p["text"], scope, surface=surface, sender=sender)[0]}
                     if isinstance(p.get("text"), str) and p["text"] else p for p in parts]
        if new_text == str(text) and new_parts == parts:
            return text                                   # nothing found: the same object
        return FoldedText(new_text, new_parts) if parts else new_text
    except Exception:
        logger.warning("secret_intake: turn text scan failed (text passes unchanged)", exc_info=False)
        return text


def offer_candidates(scope: str, text: Any, *, sender: str = "") -> List[PendingSecret]:
    """Pending entries of *scope* named in *text* that have not been offered to the user yet; marks
    them offered. A Werkbank message redacted in the plugin gets its card from the gateway this way;
    *sender* (the card event's user) fills an entry that was created without one."""
    if isinstance(text, list):
        text = "\n".join(str(p.get("text") or "") for p in text if isinstance(p, dict))
    if not isinstance(text, str) or "[GEHEIMNIS:" not in text:
        return []
    return _STORE.take_unoffered(str(scope or "global"), text, str(sender or ""))


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


# -- release („Kein Geheimnis“, WB-1261) ----------------------------------------------------------
# Exception list: (sender tag, art, HMAC-SHA256 of the value under a random per-process key) per profile
# home — a release counts only for the sender who gave it (WB-1261 F6); the sender is kept as an HMAC too.
# Not reversible without the key, the key never leaves process memory, nothing is persisted —
# a restart forgets the list (the value would raise a card again, the safe direction).
_RELEASE_KEY = _secrets.token_bytes(32)
_RELEASED: Dict[str, "OrderedDict[Tuple[str, str], None]"] = {}
_RELEASED_MAX = 512
_RELEASED_LOCK = threading.Lock()


def _release_sig(sender: str, art: str, value: str) -> Tuple[str, str, str]:
    import hmac

    def mac(label: bytes, data: str) -> str:
        return hmac.new(_RELEASE_KEY, label + b"\0" + data.encode("utf-8", "replace"), hashlib.sha256).hexdigest()
    return mac(b"sender", sender), art, mac(b"value", value)


def _remember_released(sender: str, art: str, value: str, home: str) -> None:
    from collections import OrderedDict
    if not sender:
        return
    with _RELEASED_LOCK:
        bucket = _RELEASED.setdefault(home, OrderedDict())
        sig = _release_sig(sender, art, value)
        bucket.pop(sig, None)
        bucket[sig] = None
        while len(bucket) > _RELEASED_MAX:
            bucket.popitem(last=False)


def _is_released(sender: str, art: str, value: str, home: str) -> bool:
    if not sender:
        return False
    with _RELEASED_LOCK:
        return _release_sig(sender, art, value) in (_RELEASED.get(home) or {})


def release(entry_id: str, *, actor: Optional[str], scope: Optional[str]) -> Dict[str, Any]:
    """„Kein Geheimnis“: hand the pending value back for the agent and save nothing. Allowed only for
    the message's sender (*actor* must equal the entry's ``sender``; unknown on either side → refused),
    only in the entry's own *scope* (session of the card), only while pending and within the TTL, once.
    Returns ``{"ok": True, "placeholder", "art", "value"}`` — the value is for the agent only, never log
    it — or ``{"ok": False, "reason": "abgelaufen" | "nicht_berechtigt", "placeholder"}``. A refusal
    consumes nothing: the entry stays open for the sender's own answer."""
    entry = _STORE.get(entry_id)
    if entry is None or entry.status != "pending" or entry.value is None:
        return {"ok": False, "reason": "abgelaufen", "placeholder": getattr(entry, "placeholder", None)}
    actor = str(actor or "")

    def allowed(e: PendingSecret) -> bool:
        return bool(actor) and bool(e.sender) and actor == e.sender and str(scope or "") == e.scope

    captured: Dict[str, Any] = {}

    def check(e: PendingSecret) -> bool:
        if not allowed(e):
            return False
        captured["value"] = e.value
        _remember_released(e.sender, e.art, e.value or "", e.home)   # before the egress ref is released
        return True

    done = _STORE.finish(entry_id, "released", check=check)
    if done is None:
        fresh = _STORE.get(entry_id)
        if fresh is not None and fresh.status == "pending" and not allowed(fresh):
            _audit("freigabe_abgewiesen", fresh)
            return {"ok": False, "reason": "nicht_berechtigt", "placeholder": fresh.placeholder}
        return {"ok": False, "reason": "abgelaufen", "placeholder": entry.placeholder}
    _audit("freigegeben", done)
    return {"ok": True, "placeholder": done.placeholder, "art": done.art, "value": captured.get("value")}


def apply_choice(value: str, *, vault_factory: Optional[Callable[[str], Any]] = None,
                 actor: Optional[str] = None, scope: Optional[str] = None,
                 on_release: Optional[Callable[[str, str], None]] = None,
                 on_refused: Optional[Callable[[str], None]] = None) -> str:
    """Answer of a card / prompt: ``save:<id>[,<id>…]``, ``drop:<id>[,<id>…]`` or ``keep:<id>[,<id>…]``
    („Kein Geheimnis“: *on_release* gets ``(placeholder, original)`` per released entry; *actor* /
    *scope* identify who tapped on which session's card; *on_refused* gets the placeholder of each
    „Kein Geheimnis“ refused as ``nicht_berechtigt``). Returns the user-facing result text
    (German, no values). Runs without the model."""
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
        elif action == "keep":
            res = release(entry_id, actor=actor, scope=scope)
            ph = res.get("placeholder") or "Geheimnis"
            if res.get("ok"):
                if on_release is not None:
                    on_release(res["placeholder"], res["value"])
                lines.append(f"✅ {ph}: Kein Geheimnis. Der Agent bekommt den Originaltext; nichts gespeichert.")
            elif res.get("reason") == "nicht_berechtigt":
                if on_refused is not None:
                    on_refused(ph)
                lines.append(f"⛔ {ph}: freigeben kann nur, wer die Nachricht geschickt hat.")
            else:
                lines.append(f"⌛ {ph}: schon abgelaufen oder entschieden.")
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


def card_choices(entries: List[PendingSecret], *, release: bool = True) -> List[Dict[str, str]]:
    """Card buttons. *release* adds „Kein Geheimnis“ — only where the surface reports who tapped
    (the sender check could never pass otherwise)."""
    ids = ",".join(e.id for e in entries)
    out = [{"value": f"save:{ids}", "label": "💾 Im Tresor speichern"},
           {"value": f"drop:{ids}", "label": "🗑 Verwerfen"}]
    if release:
        out.append({"value": f"keep:{ids}", "label": "✅ Kein Geheimnis"})
    return out


def _release_egress(value: Optional[str], home: str) -> None:
    """Counterpart of :func:`_register_egress` when the entry finishes (save/discard/expiry): drops
    this entry's reference. The value leaves the scrub list only when no other open entry holds it
    and no vault fill registered it (``agent.redact`` keeps provenance). A discarded/expired value
    echoed by a tool after that is no longer scrubbed (documented trade-off); a saved one is."""
    if not value:
        return
    try:
        from agent.redact import release_intake_redaction_value
        release_intake_redaction_value(value, home or None)
    except Exception:
        logger.debug("secret_intake: egress release failed", exc_info=False)


def _register_egress(value: str, home: str = "") -> None:
    try:
        from agent.redact import register_intake_redaction_value
        register_intake_redaction_value(value, home or None)
    except Exception:
        logger.debug("secret_intake: egress registration failed", exc_info=False)


def _register_durable_egress(value: Optional[str], home: str) -> None:
    if not value:
        return
    try:
        from agent.redact import register_durable_redaction_value
        register_durable_redaction_value(value, home or None)
    except Exception:
        logger.debug("secret_intake: durable egress registration failed", exc_info=False)


def saved_secret_values(home: str) -> List[str]:
    """Values of the profile's saved ``secret`` vault items (server-side only, never logged): the
    model-egress scrub loads them once per profile so a saved token stays redacted after a restart.
    An absent vault file is not created."""
    from agent.vault_store import VaultStore
    vault_dir = Path(home) / "vault"
    if not (vault_dir / "vault.json.enc").exists():
        return []
    vault = VaultStore(vault_dir)
    out: List[str] = []
    for meta in vault.list_items():
        if meta.kind != "secret":
            continue
        value = str(vault.resolve_secret(meta.id).get("value") or "")
        if value:
            out.append(value)
    return out


__all__ = [
    "Finding", "IntakeResult", "PendingSecret", "PLACEHOLDER_RE", "TTL_SECONDS", "apply_choice",
    "card_choices", "card_text", "discard", "enabled", "find_secrets", "intake", "intake_turn_text",
    "offer_candidates",
    "redact_text", "release", "save", "store", "suggest_name", "CLI_SENDER", "agent_sender",
]
