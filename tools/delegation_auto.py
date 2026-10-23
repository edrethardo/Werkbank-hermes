"""``model_profile: "auto"`` for delegate_task (WB-1260): subagent model by difficulty, trial first.

Config (``delegation.auto``; absent/empty = feature off, everything byte-identical)::

    auto:
      leicht: haiku            # stage -> delegation.models profile
      mittel: schnell
      schwer: stark
      bevorzugt: [nemotron]    # trial profiles tried first (in order) ...
      fuer: [leicht, mittel]   # ... for these stages only
      trial_parallel: 2        # concurrent trial children per process; overflow -> stage profile
      cooldown_minuten: 15     # after a trial 429/5xx/auth/timeout: no trial for this long
      standard: false          # true = tasks without model_profile are treated as "auto"

Difficulty: the task's ``schwierigkeit`` (leicht|mittel|schwer) or the deterministic heuristic
``classify`` (keywords + goal/context length; never a model call). A task gets a trial profile only
when it is not ``vertraulich: true``, its stage is in ``fuer``, the trial is healthy (credential
pool status, last 429/5xx, cooldown), side tasks of that child would NOT follow it onto the trial
(``auxiliary.exclude_main_providers``, WB-1216) and a ``trial_parallel`` slot is free. A trial run
failing with auth/429/5xx/timeout before its first tool call restarts ONCE on the stage profile.

``vertraulich`` (own flag, or inherited from a vertraulich delegating agent) is the central guard: such a
task never runs on a trial route — WB-1216's trial routes plus every ``bevorzugt`` route, whatever
``exclude_main_providers`` says (``trial_route_check``) — and its fallback chain loses every trial entry.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("tools.delegate_tool")

STUFEN: Tuple[str, ...] = ("leicht", "mittel", "schwer")
AUTO_PROFILE = "auto"
DEFAULT_TRIAL_PARALLEL = 2
DEFAULT_COOLDOWN_MINUTES = 15
# A slot whose child never reported back (unit never ran, process hiccup) stops counting after this.
_SLOT_MAX_AGE_SECONDS = 6 * 3600

# ── Heuristic (documented in docs/notes/delegation-auto-profile.md) ─────────
# Order: hard keyword → schwer; very long goal+context → schwer; light keyword with short text, or a
# very short text → leicht; everything else → mittel. Whole-word, case-insensitive matches only.
_SCHWER_WORDS = (
    "review", "reviews", "security", "sicherheit", "sicherheitslücke", "architektur", "architecture", "audit",
    "threat", "vulnerability", "exploit", "concurrency", "race", "migration", "refactor", "refactoring",
    "design", "entwurf", "proof", "beweis",
)
_LEICHT_WORDS = (
    "list", "liste", "auflisten", "zusammenfassen", "zusammenfassung", "summarize", "summary", "translate",
    "übersetze", "übersetzen", "rename", "umbenennen", "count", "zähle", "zählen", "format", "formatiere",
    "lookup", "nachschlagen", "grep", "find", "finde", "suche", "search", "fasse",
)
LONG_TEXT_SCHWER = 6000  # chars of goal + context
SHORT_TEXT_LEICHT = 80
LEICHT_KEYWORD_MAX = 1500


def _word_re(words) -> "re.Pattern[str]":
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.IGNORECASE)


_SCHWER_RE = _word_re(_SCHWER_WORDS)
_LEICHT_RE = _word_re(_LEICHT_WORDS)


def classify(goal: Any, context: Any = None) -> Tuple[str, str]:
    """``(stufe, grund)`` for a task without ``schwierigkeit``. Pure function of the text."""
    goal_s, ctx_s = str(goal or ""), str(context or "")
    length = len(goal_s) + len(ctx_s)
    hard = _SCHWER_RE.search(goal_s)
    if hard:
        return "schwer", f"Heuristik: Schlüsselwort '{hard.group(1).lower()}'"
    if length >= LONG_TEXT_SCHWER:
        return "schwer", f"Heuristik: Ziel+Kontext {length} Zeichen ≥ {LONG_TEXT_SCHWER}"
    light = _LEICHT_RE.search(goal_s)
    if light and length < LEICHT_KEYWORD_MAX:
        return "leicht", f"Heuristik: Schlüsselwort '{light.group(1).lower()}', {length} Zeichen"
    if length < SHORT_TEXT_LEICHT:
        return "leicht", f"Heuristik: kurz ({length} Zeichen < {SHORT_TEXT_LEICHT})"
    return "mittel", f"Heuristik: Standard ({length} Zeichen)"


# ── Config ──────────────────────────────────────────────────────────────────


def get_auto_config(cfg: Optional[dict]) -> Optional[Dict[str, Any]]:
    """``delegation.auto`` as a dict, or None when absent/empty/not a map (feature off)."""
    raw = (cfg or {}).get("auto")
    return raw if isinstance(raw, dict) and raw else None


def _str_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x or "").strip()]


def trial_profiles(auto_cfg: Dict[str, Any]) -> List[str]:
    return _str_list(auto_cfg.get("bevorzugt"))


def trial_stages(auto_cfg: Dict[str, Any]) -> List[str]:
    raw = auto_cfg.get("fuer")
    return [s.lower() for s in _str_list(raw)] if raw is not None else ["leicht", "mittel"]


def trial_parallel(auto_cfg: Dict[str, Any]) -> int:
    raw = auto_cfg.get("trial_parallel", DEFAULT_TRIAL_PARALLEL)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_TRIAL_PARALLEL


def cooldown_seconds(auto_cfg: Dict[str, Any]) -> float:
    raw = auto_cfg.get("cooldown_minuten", DEFAULT_COOLDOWN_MINUTES)
    try:
        return max(0.0, float(raw)) * 60.0
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_MINUTES * 60.0


def standard_auto(auto_cfg: Optional[Dict[str, Any]]) -> bool:
    from utils import is_truthy_value
    return bool(auto_cfg) and is_truthy_value(auto_cfg.get("standard"), default=False)


def validate_auto_config(auto_cfg: Dict[str, Any], profiles: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Error text when the stage mapping or ``bevorzugt`` names a profile that does not exist."""
    for stufe in STUFEN:
        name = str(auto_cfg.get(stufe) or "").strip()
        if not name:
            return f"delegation.auto.{stufe} is not set; map each of {', '.join(STUFEN)} to a delegation.models profile."
        if name not in profiles:
            return (f"delegation.auto.{stufe} names unknown profile {name!r}. "
                    f"Configured profiles (delegation.models): {', '.join(profiles) or '(none)'}.")
    for name in trial_profiles(auto_cfg):
        if name not in profiles:
            return f"delegation.auto.bevorzugt names unknown profile {name!r}."
    return None


def task_stage(task: Dict[str, Any]) -> Tuple[Optional[str], str, Optional[str]]:
    """``(stufe, quelle, error)`` from ``schwierigkeit`` or the heuristic."""
    raw = task.get("schwierigkeit")
    if raw is not None:
        stufe = str(raw).strip().lower()
        if stufe not in STUFEN:
            return None, "", f"'schwierigkeit' must be one of {', '.join(STUFEN)} (got {raw!r})."
        return stufe, "Angabe schwierigkeit", None
    stufe, grund = classify(task.get("goal"), task.get("context"))
    return stufe, grund, None


def is_confidential(task: Dict[str, Any], parent_agent: Any = None) -> bool:
    """``vertraulich: true`` on the task, or inherited: the delegating agent is itself the child of a
    vertraulich task (``_delegate_confidential``), so its children — the grandchildren — are vertraulich too."""
    from utils import is_truthy_value
    if getattr(parent_agent, "_delegate_confidential", None) is True:
        return True
    return isinstance(task, dict) and is_truthy_value(task.get("vertraulich"), default=False)


# ── Process-wide trial state (cooldown + parallel slots) ────────────────────


def _now() -> float:
    return time.time()


_lock = threading.Lock()
_failures: Dict[str, Tuple[float, str]] = {}
_slots: Dict[str, Dict[int, float]] = {}
_tokens = itertools.count(1)


def reset_state() -> None:
    """Tests only: forget cooldowns and slots."""
    with _lock:
        _failures.clear()
        _slots.clear()


def record_trial_failure(profile: str, grund: str) -> None:
    with _lock:
        _failures[profile] = (_now(), str(grund or "Fehler"))
    logger.info("delegation.auto: trial profile %s failed (%s); cooldown starts", profile, grund)


def trial_cooldown_reason(profile: str, cooldown_s: float) -> Optional[str]:
    with _lock:
        rec = _failures.get(profile)
    if not rec:
        return None
    age = _now() - rec[0]
    if age >= cooldown_s:
        return None
    return f"Trial {profile} im Cooldown ({rec[1]} vor {int(age)} s, Cooldown {int(cooldown_s)} s)"


def _purge(profile: str) -> Dict[int, float]:
    held = _slots.setdefault(profile, {})
    cutoff = _now() - _SLOT_MAX_AGE_SECONDS
    for token in [t for t, at in held.items() if at < cutoff]:
        held.pop(token, None)
    return held


def try_acquire_slot(profile: str, limit: int) -> Optional[int]:
    with _lock:
        held = _purge(profile)
        if len(held) >= limit:
            return None
        token = next(_tokens)
        held[token] = _now()
        return token


def release_slot(profile: Optional[str], token: Optional[int]) -> None:
    if not profile or token is None:
        return
    with _lock:
        _slots.get(profile, {}).pop(token, None)


def running_trial_children(profile: str) -> int:
    with _lock:
        return len(_purge(profile))


# ── Trial health ────────────────────────────────────────────────────────────


def _pool_refusal_impl(provider: str, model: Optional[str], cooldown_s: float) -> Optional[str]:
    """Credential-pool verdict through its status API only (never reads a secret): no entry, nothing
    available for this model, or a 429/5xx recorded within the cooldown → reason; healthy → None."""
    try:
        from agent.credential_pool import load_pool
        pool = load_pool(provider)
    except Exception as exc:  # unreadable pool = not healthy
        return f"Credential-Pool {provider} nicht lesbar ({type(exc).__name__})"
    if not pool.has_credentials():
        return f"Credential-Pool {provider}: kein Eintrag"
    if not pool.has_available(model=model):
        return f"Credential-Pool {provider}: kein Eintrag verfügbar (erschöpft/Cooldown)"
    now = _now()
    for entry in pool.entries():
        code = getattr(entry, "last_error_code", None)
        at = getattr(entry, "last_status_at", None)
        if isinstance(code, int) and (code == 429 or code >= 500) and isinstance(at, (int, float)) \
                and now - at < cooldown_s:
            return f"Credential-Pool {provider}: letzter Fehler {code} vor {int(now - at)} s"
    return None


def pool_refusal(provider: str, model: Optional[str], cooldown_s: float) -> Optional[str]:
    return _pool_refusal_impl(provider, model, cooldown_s)


def side_tasks_refusal(provider: Optional[str], base_url: Optional[str]) -> Optional[str]:
    """The trial child's side tasks (compression, title, review) must not follow it onto the trial."""
    from agent import aux_provider_policy as policy
    if policy.main_route_excluded(provider, base_url or ""):
        return None
    return (f"auxiliary.exclude_main_providers schließt {provider or '?'} nicht aus — Hilfsaufgaben des Kinds "
            "liefen sonst aufs Trial (WB-1216)")


def route_is_trial(provider: Optional[str], base_url: Optional[str]) -> bool:
    """A route WB-1216 treats as trial (side tasks never go there)."""
    from agent import aux_provider_policy as policy
    return bool(provider or base_url) and policy.main_route_excluded(provider, base_url or "")


# Provider ids that host many unrelated models: a trial profile on one of them marks only its own model
# (or its endpoint host) as trial, never the whole provider.
_SHARED_PROVIDERS = frozenset({"openrouter", "custom", "nous", "auto"})


def _host(url: Any) -> str:
    from urllib.parse import urlparse
    raw = str(url or "").strip()
    if not raw:
        return ""
    return (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "").lower()


def trial_markers(auto_cfg: Optional[Dict[str, Any]], profiles: Dict[str, Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    """``(provider, host, model)`` of every ``delegation.auto.bevorzugt`` profile — trial no matter what
    ``auxiliary.exclude_main_providers`` says. A ``nvidia`` profile without ``base_url`` is the hosted NIM."""
    from agent import aux_provider_policy as policy
    markers: List[Tuple[str, str, str]] = []
    for name in trial_profiles(auto_cfg or {}):
        prof = profiles.get(name)
        if not isinstance(prof, dict):
            continue
        provider = policy._canonical(prof.get("provider") if isinstance(prof.get("provider"), str) else None)
        host = _host(prof.get("base_url"))
        if not host and provider == "nvidia":
            host = policy.HOSTED_NIM_HOST
        model = str(prof.get("model") or "").strip().lower()
        if provider or host:
            markers.append((provider, host, model))
    return markers


def trial_route_check(auto_cfg: Optional[Dict[str, Any]], profiles: Dict[str, Dict[str, Any]]):
    """Predicate ``(provider, base_url, model=None) -> bool``: the WB-1216 trial routes plus everything a
    ``bevorzugt`` profile points at (same endpoint host; same provider — on shared providers same model)."""
    from agent import aux_provider_policy as policy
    from utils import base_url_host_matches
    markers = trial_markers(auto_cfg, profiles)

    def _is_trial(provider: Any, base_url: Any, model: Any = None) -> bool:
        provider = provider if isinstance(provider, str) else None
        base_url = base_url if isinstance(base_url, str) else None
        if route_is_trial(provider, base_url):
            return True
        canonical = policy._canonical(provider)
        model_s = str(model or "").strip().lower()
        for m_provider, m_host, m_model in markers:
            if m_host and base_url and base_url_host_matches(base_url, m_host):
                return True
            if not m_provider or canonical != m_provider:
                continue
            if m_provider in _SHARED_PROVIDERS or m_provider.startswith("custom"):
                if m_model and model_s == m_model:
                    return True
                continue
            return True
        return False
    return _is_trial


def drop_trial_fallbacks(chain: Any, is_trial) -> Optional[List[Dict[str, Any]]]:
    """The fallback chain without trial entries (``None`` when nothing is left) — a vertraulich child must
    not reach the trial through its fallback either."""
    if not isinstance(chain, list):
        return chain
    kept = [e for e in chain if not (isinstance(e, dict) and is_trial(e.get("provider"), e.get("base_url"), e.get("model")))]
    if len(kept) != len(chain):
        logger.info("delegation.auto: vertraulich — %d trial entr(y/ies) removed from the child's fallback chain",
                    len(chain) - len(kept))
    return kept or None


# ── Fallback trigger ────────────────────────────────────────────────────────

_FALLBACK_REASONS = frozenset({
    "auth", "auth_permanent", "billing", "rate_limit", "upstream_rate_limit", "overloaded", "server_error", "timeout",
})
_HTTP_RE = re.compile(r"\b(401|403|429|5\d\d)\b")
_TIMEOUT_RE = re.compile(r"time(?:d)?[ -]?out", re.IGNORECASE)


def _made_tool_call(entry: Dict[str, Any]) -> bool:
    """Conservative: unknown counts as 'made one' unless no API call completed at all."""
    trace = entry.get("tool_trace")
    if isinstance(trace, list):
        return bool(trace)
    return entry.get("api_calls", 0) not in (0, None)


def fallback_reason(entry: Dict[str, Any]) -> Optional[str]:
    """Reason text when a trial run's entry qualifies for the one stage-profile restart, else None:
    status failed/error/timeout, cause auth/429/5xx/timeout, and no tool call before the failure."""
    if not isinstance(entry, dict) or entry.get("status") not in ("failed", "error", "timeout"):
        return None
    if _made_tool_call(entry):
        return None
    reason = entry.get("failure_reason")
    if isinstance(reason, str) and reason:
        return reason if reason in _FALLBACK_REASONS else None
    if entry.get("status") == "timeout":
        return "timeout"
    text = str(entry.get("error") or "")
    m = _HTTP_RE.search(text)
    if m:
        return f"HTTP {m.group(1)}"
    if _TIMEOUT_RE.search(text):
        return "timeout"
    return None


# ── Run path (called from ``_Batch.run_child`` on a worker thread) ──────────

_rebuild_lock = threading.Lock()


def _annotate(entry: Dict[str, Any], child: Any, auto: Optional[Dict[str, Any]]) -> None:
    if not isinstance(entry, dict):
        return
    model, provider = getattr(child, "model", None), getattr(child, "provider", None)
    entry["model"] = model if isinstance(model, str) and model else entry.get("model")
    entry["provider"] = provider if isinstance(provider, str) and provider else None
    profile = getattr(child, "_delegate_model_profile", None)
    if isinstance(profile, str):
        entry["model_profile"] = profile
    if auto:
        entry["schwierigkeit"] = auto.get("stufe")
        entry["auto_grund"] = auto.get("quelle")
        if auto.get("hinweis"):
            entry["auto_hinweis"] = auto["hinweis"]


def _replace_child(batch: Any, i: int, task: Dict[str, Any], new_child: Any) -> None:
    """Point the batch's (index, task, child) row at the child that produced the entry (hooks, memory)."""
    children = getattr(batch, "children", None)
    if isinstance(children, list):
        for k, row in enumerate(children):
            if row[0] == i:
                children[k] = (i, task, new_child)


def _unit_stopped(batch: Any) -> bool:
    return getattr(batch, "stop_requested", None) is True


def _note_unit_model(batch: Any) -> None:
    """An async unit's completion label follows the children that actually run (fallback child)."""
    unit_id = getattr(batch, "unit_id", None)
    if getattr(batch, "detached", None) is not True or not isinstance(unit_id, str):
        return
    try:
        from tools.async_delegation import update_unit_model
        from tools.delegate_tool_dispatch import _unit_model_label
        update_unit_model(unit_id, _unit_model_label(batch, [c for (_, _, c) in batch.children]))
    except Exception as exc:  # label only; never fail the run
        logger.debug("delegation.auto: could not update the unit model label: %s", exc)


def _close_unrun(child: Any) -> None:
    try:
        child.close()
    except Exception as exc:
        logger.debug("delegation.auto: closing an unstarted fallback child failed: %s", exc)


def run_auto_child(batch: Any, i: int, task: Dict[str, Any], child: Any) -> Dict[str, Any]:
    """Run one child; a trial child that failed with auth/429/5xx/timeout before its first tool call
    restarts ONCE on its stage profile (``fallback_von``). Releases the trial slot when done.

    Inside an async unit the fallback child takes the trial child's place in ``batch.children`` (the unit's
    ``interrupt_fn`` reads it live), keeps its ``subagent_id`` (ids already handed to the model stay valid),
    stays out of the parent's interrupt fan-out like its siblings, and is never started once the unit was
    stopped."""
    from tools.delegate_tool import _run_single_child
    auto = getattr(child, "_delegate_auto", None)
    auto = auto if isinstance(auto, dict) else None
    trial = (auto or {}).get("trial")
    slot = [(auto or {}).get("slot")]

    def _release():
        if slot[0] is not None:
            release_slot(trial, slot[0])
            slot[0] = None

    def _run(c):
        return _run_single_child(task_index=i, goal=task["goal"], child=c, parent_agent=batch.parent_agent,
                                 **batch.owner_kwargs())
    try:
        entry = _run(child)
        reason = fallback_reason(entry) if trial and callable((auto or {}).get("rebuild")) else None
        if reason:
            record_trial_failure(trial, reason)
            _release()
        if reason and _unit_stopped(batch):
            logger.info("delegation.auto: task %d stopped; no fallback after trial failure (%s)", i, reason)
            reason = None
        if reason:
            kw: Dict[str, Any] = {}
            sid = getattr(child, "_subagent_id", None)
            if isinstance(sid, str) and sid:
                kw["subagent_id"] = sid
            if getattr(batch, "detached", None) is True:
                kw["attach_to_parent"] = False
            try:
                with _rebuild_lock:  # construction is not thread-safe; siblings may fall back at once
                    new_child = auto["rebuild"](**kw)
            except Exception as exc:
                logger.warning("delegation.auto: fallback child for task %d could not be built: %s", i, exc)
                entry["fallback_fehler"] = str(exc)
            else:
                old_child = child
                _replace_child(batch, i, task, new_child)
                if _unit_stopped(batch):  # stop landed while building: put the trial child back, never start
                    _replace_child(batch, i, task, old_child)
                    _close_unrun(new_child)
                else:
                    child = new_child
                    _note_unit_model(batch)
                    entry = _run(child)
                    if isinstance(entry, dict):
                        entry["fallback_von"] = trial
                        entry["fallback_grund"] = reason
        _annotate(entry, child, auto)
        return entry
    finally:
        _release()
