"""Dashboard page ``/p/schluessel``: the one place an API key is typed.

Mounted through ``ctx.register_dashboard_page`` so it inherits the dashboard's login gate and
Host guard — there is no auth of its own here. On top of that, the submit endpoint only takes
same-origin JSON (a cross-site HTML form cannot send ``application/json``), checks Origin and
``Sec-Fetch-Site``, and needs the per-request CSRF token embedded in the page. The key travels
only in the POST body; a query string on the submit is refused, and no response repeats it.
"""

from __future__ import annotations

import hmac
import html
import json
import time
import urllib.parse
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import keys, store

_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
             "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}

#: Submits per rolling minute for the whole page (all requests together).
POST_LIMIT = 20
#: Wrong CSRF tokens before a request is burned.
MAX_BAD_ATTEMPTS = 5
MAX_VALUE_LEN = 4096
_RATE: deque = deque()

_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>API-Schlüssel</title>
<style>
 body{font:17px -apple-system,system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:24px;
      max-width:520px}
 h1{font-size:20px;margin:0 0 16px}
 .box{background:#222;padding:12px;border-radius:8px;white-space:pre-wrap;word-break:break-word;
      font-size:15px;margin-bottom:8px}
 label{display:block;margin-top:12px;color:#bbb;font-size:15px}
 input,select{width:100%;box-sizing:border-box;font-size:18px;padding:12px;border-radius:10px;
       border:1px solid #555;background:#000;color:#fff;margin:6px 0}
 .row{display:flex;gap:12px;margin-top:12px}
 button{flex:1;font-size:18px;padding:14px;border-radius:10px;border:0}
 .ok{background:#2d7d46;color:#fff}.no{background:#444;color:#eee}
 #msg{margin-top:16px;font-size:16px}
</style></head><body>
<h1>🔑 API-Schlüssel</h1>
__BODY__
<script>
const rid = __RID__;
const csrf = __CSRF__;
async function send(op){
  const field = document.getElementById("key");
  const body = {r: rid, csrf, op};
  if (op === "save") {
    body.value = field.value;
    body.profile = document.getElementById("profile").value;
    if (!body.value.trim()) return;
  }
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  let data = {};
  try {
    const res = await fetch(location.pathname.replace(/\\/?$/, "/") + "submit",
      {method: "POST", credentials: "same-origin", cache: "no-store",
       headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    data = await res.json().catch(() => ({}));
  } finally {
    field.value = "";
    body.value = "";
  }
  const msg = document.getElementById("msg");
  if (data.ok) {
    document.getElementById("form").remove();
    msg.textContent = data.status === "stored"
      ? "✓ Gespeichert als „" + (data.label || "") + "“. Du kannst das Fenster schließen."
      : "Abgebrochen.";
  } else if (data.retry) {
    document.querySelectorAll("button").forEach(b => b.disabled = false);
    msg.textContent = data.error || "Bitte prüfen und erneut senden.";
  } else {
    document.getElementById("form").remove();
    msg.textContent = data.error || "Anfrage ist abgelaufen oder schon beantwortet.";
  }
}
</script>
</body></html>"""

_FORM = """<p>Der Agent bittet um einen Schlüssel für</p><div class="box"><b>{provider_name}</b> ({provider})</div>
{purpose}
<div id="form">
 <form onsubmit="send('save');return false" autocomplete="off">
  <label for="key">Schlüssel</label>
  <input id="key" type="password" autocomplete="off" autocapitalize="off" spellcheck="false"
         autofocus placeholder="Schlüssel einfügen" aria-label="API-Schlüssel">
  <label for="profile">Profil</label>
  <select id="profile">{options}</select>
  <div class="box" style="font-size:14px;color:#aaa">Eintrag: {label}</div>
  <div class="row"><button class="ok" type="submit">Speichern</button>
   <button class="no" type="button" onclick="send('cancel')">Abbrechen</button></div>
 </form>
 <p style="color:#999;font-size:14px">Gültig noch ca. {minutes} min, nur einmal. Der Schlüssel wird
 direkt im Credential-Pool des gewählten Profils gespeichert; der Agent sieht ihn nie, nur
 „gespeichert“ oder „abgebrochen“.</p>
</div>
<div id="msg"></div>"""

_GONE = """<p>Diese Anfrage ist abgelaufen oder wurde schon beantwortet.</p>
<p style="color:#999">Lass den Agenten eine neue Anfrage stellen, dann kommt ein neuer Link.</p>"""


def _json(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_NO_STORE)


def _rate_limited() -> bool:
    now = time.monotonic()
    while _RATE and _RATE[0] < now - 60:
        _RATE.popleft()
    if len(_RATE) >= POST_LIMIT:
        return True
    _RATE.append(now)
    return False


def _cross_origin(request: Request) -> bool:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("origin", "")
    if not origin:
        return False
    try:
        netloc = urllib.parse.urlparse(origin).netloc.lower()
    except ValueError:
        return True
    allowed = {request.headers.get("host", "").lower(),
               request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower()}
    from .tools import _settings
    if _settings.get("public_url"):
        allowed.add(urllib.parse.urlparse(_settings["public_url"]).netloc.lower())
    return not netloc or netloc not in (allowed - {""})


def _render_form(rec: dict) -> str:
    providers = keys.allowed_providers()
    options = "".join(
        f'<option value="{html.escape(n)}"{" selected" if n == "default" else ""}>{html.escape(n)}</option>'
        for n in keys.profile_names())
    purpose = rec.get("purpose") or ""
    return _FORM.format(
        provider=html.escape(rec["provider"]),
        provider_name=html.escape(str(providers.get(rec["provider"]) or rec["provider"])),
        purpose=f'<p>Zweck:</p><div class="box">{html.escape(purpose)}</div>' if purpose else "",
        options=options,
        label=html.escape(rec.get("label") or "(automatisch)"),
        minutes=max(1, round((float(rec["expires"]) - store._now()) / 60)))


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/")
    async def page(request: Request, r: str = "") -> HTMLResponse:
        rec = store.load(r)
        if rec is None or store.effective_status(rec) != store.PENDING \
                or keys.normalize_provider(rec.get("provider")) is None:
            body, rid, csrf = _GONE, '""', '""'
        else:
            body, rid, csrf = _render_form(rec), json.dumps(r), json.dumps(rec["csrf"])
        return HTMLResponse(
            _PAGE.replace("__BODY__", body).replace("__RID__", rid).replace("__CSRF__", csrf),
            headers=_NO_STORE)

    @router.post("/submit")
    async def submit(request: Request) -> JSONResponse:
        if _rate_limited():
            return _json({"ok": False, "error": "Zu viele Versuche, bitte kurz warten."}, 429)
        if any(k != "token" for k in request.query_params.keys()):
            return _json({"ok": False, "error": "Keine Parameter in der Adresse."}, 400)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return _json({"ok": False}, 415)
        if _cross_origin(request):
            return _json({"ok": False}, 403)
        try:
            data = await request.json()
        except Exception:
            return _json({"ok": False}, 400)
        if not isinstance(data, dict):
            return _json({"ok": False}, 400)
        rid, op = str(data.get("r") or ""), data.get("op")
        rec = store.load(rid)
        if rec is None or store.effective_status(rec) != store.PENDING:
            return _json({"ok": False}, 410)
        token = str(data.get("csrf") or "")
        if not rec.get("csrf") or not hmac.compare_digest(token.encode(), rec["csrf"].encode()):
            bad = int(rec.get("bad_attempts", 0)) + 1
            store.update(rid, bad_attempts=bad)
            if bad >= MAX_BAD_ATTEMPTS and store.claim(rid) is not None:
                store.finish(rid, store.CANCELLED, reason="too many bad attempts")
            return _json({"ok": False}, 403)

        if op == "cancel":
            if store.claim(rid) is None:
                return _json({"ok": False}, 410)
            store.finish(rid, store.CANCELLED, reason="owner")
            return _json({"ok": True, "status": store.CANCELLED})
        if op != "save":
            return _json({"ok": False}, 400)

        value = data.get("value")
        value = value.strip() if isinstance(value, str) else ""
        profile = str(data.get("profile") or "default")
        if not value or len(value) > MAX_VALUE_LEN or any(ord(c) < 32 for c in value):
            return _json({"ok": False, "retry": True, "error": "Ungültiger Schlüssel."}, 400)
        if profile not in keys.profile_names():
            return _json({"ok": False, "retry": True, "error": "Unbekanntes Profil."}, 400)
        provider = keys.normalize_provider(rec.get("provider"))
        if provider is None or store.claim(rid) is None:
            return _json({"ok": False}, 410)
        try:
            entry_id, label = keys.save_api_key(provider, value, label=rec.get("label") or "",
                                                profile=profile)
        except Exception as exc:
            store.release(rid)
            store.audit("failed", rec, profile=profile, reason=exc.__class__.__name__)
            return _json({"ok": False, "retry": True,
                          "error": "Speichern fehlgeschlagen, bitte erneut versuchen."}, 400)
        finally:
            value = data["value"] = ""
        store.finish(rid, store.STORED, label=label, entry_id=entry_id, profile=profile)
        return _json({"ok": True, "status": store.STORED, "label": label})

    return router
