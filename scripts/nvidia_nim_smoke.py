#!/usr/bin/env python3
"""Live smoke probe for NVIDIA Build (NIM) through Hermes' own provider resolution (WB-1215).

Credentials come ONLY from Hermes (``resolve_runtime_provider("nvidia")`` → credential
pool / profile ``.env``). The key is never printed or written: every string that leaves
this script passes ``_scrub`` (exact key value + any NVIDIA key-prefix pattern).

Run with Hermes' interpreter so the bootstrapped dependency env is used, e.g.::

    PY=$(hermes --print-runtime-command | python3 -c 'import json,sys;print(json.load(sys.stdin)[0])')
    $PY -I -c "import sys,runpy; sys.path.insert(0,'<hermes checkout>'); \
        sys._hermes_pin_default_home=True; import hermes_bootstrap; \
        runpy.run_path('scripts/nvidia_nim_smoke.py', run_name='__main__')" models|probe|errors ...

Each sub-command appends to the JSON result file (``--out``); calls are counted.
No load tests: one request per check, small prompts without customer/personal data.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PILOTS = (
    "deepseek-ai/deepseek-v4.1-flash",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
)
VISION_MODELS = ("nvidia/nemotron-3.5-super-vl-120b-a12b",)
HEADER_KEEP = re.compile(r"(ratelimit|retry-after|x-request-id|nvcf|content-type|^server$|^date$|x-.*limit)", re.I)
_KEY_PREFIX = "nv" + "api-"  # split so `git grep` for key prefixes stays empty
_KEY_RE = re.compile(re.escape(_KEY_PREFIX) + r"[A-Za-z0-9_\-]+")

FAKE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_test_temperature",
        "description": "Returns a fake test temperature for a fictional test city. Test tool, no real data.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "Name of the fictional city"}},
            "required": ["city"],
        },
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Probe:
    def __init__(self, out: Path):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from providers import get_provider_profile

        rt = resolve_runtime_provider(requested="nvidia")
        self._key = rt.get("api_key") or ""
        if not self._key:
            raise SystemExit("NVIDIA_API_KEY/pool credential not resolved by Hermes - stop (Rückfrage an Owner).")
        self.base_url = rt["base_url"].rstrip("/")
        self.source = rt.get("source")
        self.profile = get_provider_profile("nvidia")
        from agent.auxiliary_client import build_nvidia_nim_headers
        import openai

        self.headers = build_nvidia_nim_headers(self.base_url)
        self.openai = openai
        self.client = openai.OpenAI(api_key=self._key, base_url=self.base_url,
                                    default_headers=self.headers, max_retries=0, timeout=180)
        self.out = out
        self.data = json.loads(out.read_text()) if out.exists() else {"calls": 0, "events": []}
        self.data.setdefault("meta", {}).update({
            "base_url": self.base_url, "credential_source": self.source,
            "extra_headers_sent": sorted(self.headers), "openai_sdk": openai.__version__,
        })

    # --- bookkeeping -------------------------------------------------------
    def _scrub(self, obj):
        text = json.dumps(obj, ensure_ascii=False, default=str)
        if self._key:
            text = text.replace(self._key, "***")
        return json.loads(_KEY_RE.sub("<redacted-key>", text))

    def record(self, kind: str, model: str | None, **fields):
        self.data["calls"] += 1
        ev = self._scrub({"n": self.data["calls"], "at": _now(), "kind": kind, "model": model, **fields})
        self.data["events"].append(ev)
        self.out.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(ev, ensure_ascii=False)[:1500])
        return ev

    @staticmethod
    def _hdrs(h) -> dict:
        return {k.lower(): v for k, v in dict(h).items() if HEADER_KEEP.search(k)}

    def _err(self, exc) -> dict:
        d = {"error_type": type(exc).__name__, "error": str(exc)[:800]}
        resp = getattr(exc, "response", None)
        if resp is not None:
            d["status"] = resp.status_code
            d["headers"] = self._hdrs(resp.headers)
            try:
                d["body"] = resp.text[:800]
            except Exception:  # streamed body
                pass
        return d

    # --- probes --------------------------------------------------------------
    def models(self):
        import httpx

        t0 = time.monotonic()
        r = httpx.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self._key}", **self.headers}, timeout=60)
        dt = round(time.monotonic() - t0, 3)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:500]}
        ids = sorted(m.get("id") for m in body.get("data", []))
        pilots = {}
        for p in PILOTS:
            stem = p.split("/")[-1].split("-")[0]  # e.g. "nemotron", "deepseek"
            pilots[p] = {"present": p in ids, "similar": [i for i in ids if stem in i and i != p][:25]}
        sample = {m.get("id"): m for m in body.get("data", []) if m.get("id") in PILOTS}
        self.data["models"] = {"at": _now(), "status": r.status_code, "count": len(ids), "ids": ids,
                               "pilot_entries_raw": sample, "pilots": pilots}
        self.record("GET /models", None, status=r.status_code, latency_s=dt, count=len(ids),
                    headers=self._hdrs(r.headers), pilots={k: v["present"] for k, v in pilots.items()},
                    entry_fields=sorted({k for m in body.get("data", []) for k in m}))

    def model_detail(self, model: str):
        import httpx

        t0 = time.monotonic()
        r = httpx.get(f"{self.base_url}/models/{model}", headers={"Authorization": f"Bearer {self._key}", **self.headers}, timeout=60)
        self.record("GET /models/{id}", model, status=r.status_code, latency_s=round(time.monotonic() - t0, 3),
                    headers=self._hdrs(r.headers), body=r.text[:1500])

    def chat(self, model: str):
        msgs = [{"role": "user", "content": "Smoke test. Reply with exactly the word: PONG"}]
        t0 = time.monotonic()
        try:
            raw = self.client.chat.completions.with_raw_response.create(model=model, messages=msgs, max_tokens=512, temperature=0)
            dt = round(time.monotonic() - t0, 3)
            c = raw.parse()
            m = c.choices[0].message
            self.record("chat", model, ok=True, latency_s=dt, status=raw.http_response.status_code,
                        response_model=c.model, content=(m.content or "")[:200],
                        reasoning_chars=len(getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or ""),
                        finish_reason=c.choices[0].finish_reason,
                        usage=c.usage.model_dump() if c.usage else None, headers=self._hdrs(raw.headers))
        except Exception as exc:
            self.record("chat", model, ok=False, latency_s=round(time.monotonic() - t0, 3), **self._err(exc))

    def stream(self, model: str):
        msgs = [{"role": "user", "content": "Smoke test. Count from 1 to 5, separated by spaces, nothing else."}]
        t0 = time.monotonic()
        try:
            raw = self.client.chat.completions.with_raw_response.create(
                model=model, messages=msgs, max_tokens=512, temperature=0, stream=True,
                stream_options={"include_usage": True})
            hdrs = self._hdrs(raw.headers)
            first = first_content = None
            chunks, text, rmodel, usage, finish, reasoning = 0, "", None, None, None, 0
            for ch in raw.parse():
                chunks += 1
                if first is None:
                    first = round(time.monotonic() - t0, 3)
                rmodel = rmodel or ch.model
                if ch.usage:
                    usage = ch.usage.model_dump()
                for choice in ch.choices or []:
                    d = choice.delta
                    if d and d.content:
                        if first_content is None:
                            first_content = round(time.monotonic() - t0, 3)
                        text += d.content
                    rc = getattr(d, "reasoning_content", None) or getattr(d, "reasoning", None) if d else None
                    reasoning += len(rc or "")
                    finish = choice.finish_reason or finish
            self.record("stream", model, ok=True, ttfb_chunk_s=first, ttft_content_s=first_content,
                        total_s=round(time.monotonic() - t0, 3), chunks=chunks, response_model=rmodel,
                        content=text[:200], reasoning_chars=reasoning, finish_reason=finish, usage=usage, headers=hdrs)
        except Exception as exc:
            self.record("stream", model, ok=False, latency_s=round(time.monotonic() - t0, 3), **self._err(exc))

    def tools(self, model: str, strip: bool = True):
        msgs = [
            {"role": "system", "content": "You are a test assistant. Use the provided tool when asked for a temperature."},
            {"role": "user", "content": "What is the temperature in the fictional test city Testhausen? Use the tool."},
        ]
        t0 = time.monotonic()
        try:
            c = self.client.chat.completions.create(model=model, messages=msgs, tools=[FAKE_TOOL], tool_choice="auto",
                                                    max_tokens=1024, temperature=0)
            dt1 = round(time.monotonic() - t0, 3)
            m = c.choices[0].message
            calls = m.tool_calls or []
            ev1 = dict(ok=bool(calls), latency_s=dt1, response_model=c.model, finish_reason=c.choices[0].finish_reason,
                       tool_calls=[{"name": tc.function.name, "arguments": tc.function.arguments} for tc in calls],
                       content=(m.content or "")[:200], usage=c.usage.model_dump() if c.usage else None)
            self.record("tools step1 (call)", model, **ev1)
            if not calls:
                return
        except Exception as exc:
            self.record("tools step1 (call)", model, ok=False, latency_s=round(time.monotonic() - t0, 3), **self._err(exc))
            return
        tc = calls[0]
        msgs.append({"role": "assistant", "content": m.content or "",
                     "tool_calls": [{"id": tc.id, "type": "function",
                                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}]})
        # Hermes stores tool results WITH name/tool_name; the NVIDIA profile must strip them.
        msgs.append({"role": "tool", "tool_call_id": tc.id, "name": tc.function.name, "tool_name": tc.function.name,
                     "content": json.dumps({"city": "Testhausen", "temperature_c": 17.5, "note": "fake test value"})})
        sent = self.profile.prepare_messages(msgs) if strip else msgs
        stripped = "name" not in sent[-1] and "tool_name" not in sent[-1]
        t1 = time.monotonic()
        kind = "tools step2 (result, prepare_messages)" if strip else "tools step2 (result, UNSTRIPPED name/tool_name)"
        try:
            c2 = self.client.chat.completions.create(model=model, messages=sent, tools=[FAKE_TOOL], max_tokens=1024, temperature=0)
            m2 = c2.choices[0].message
            self.record(kind, model, ok=True, stripped=stripped, latency_s=round(time.monotonic() - t1, 3),
                        response_model=c2.model, content=(m2.content or "")[:300],
                        mentions_17_5=("17.5" in (m2.content or "") or "17,5" in (m2.content or "")),
                        finish_reason=c2.choices[0].finish_reason, usage=c2.usage.model_dump() if c2.usage else None)
        except Exception as exc:
            self.record(kind, model, ok=False, stripped=stripped, latency_s=round(time.monotonic() - t1, 3), **self._err(exc))

    def context(self, model: str):
        """No giant prompt: ask for an absurd max_tokens and read the server's own limit from the 400 text."""
        msgs = [{"role": "user", "content": "Smoke test. Reply: OK"}]
        t0 = time.monotonic()
        try:
            c = self.client.chat.completions.create(model=model, messages=msgs, max_tokens=10_000_000, temperature=0)
            self.record("context (max_tokens=1e7)", model, ok=True, accepted=True, latency_s=round(time.monotonic() - t0, 3),
                        content=(c.choices[0].message.content or "")[:100], usage=c.usage.model_dump() if c.usage else None)
        except Exception as exc:
            err = self._err(exc)
            nums = [int(n) for n in re.findall(r"\b(\d{4,7})\b", err.get("body", "") + " " + err.get("error", ""))]
            self.record("context (max_tokens=1e7)", model, ok=False, accepted=False,
                        latency_s=round(time.monotonic() - t0, 3), numbers_in_error=nums, **err)

    def vision(self, model: str, image: Path):
        b64 = base64.b64encode(image.read_bytes()).decode()
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "Smoke test. Describe this image in one short sentence (main colours and any visible text)."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]
        t0 = time.monotonic()
        try:
            c = self.client.chat.completions.create(model=model, messages=msgs, max_tokens=512, temperature=0)
            self.record("vision", model, ok=True, image=str(image), image_bytes=image.stat().st_size,
                        latency_s=round(time.monotonic() - t0, 3), response_model=c.model,
                        content=(c.choices[0].message.content or "")[:300], usage=c.usage.model_dump() if c.usage else None)
        except Exception as exc:
            self.record("vision", model, ok=False, latency_s=round(time.monotonic() - t0, 3), **self._err(exc))

    def errors(self, model: str):
        bad = self.openai.OpenAI(api_key="invalid-test-key-wb1215", base_url=self.base_url,
                                 default_headers=self.headers, max_retries=0, timeout=60)
        t0 = time.monotonic()
        try:
            bad.chat.completions.create(model=model, messages=[{"role": "user", "content": "Smoke test: OK"}], max_tokens=8)
            self.record("error: fake key", model, ok=True, note="UNEXPECTED: accepted")
        except Exception as exc:
            self.record("error: fake key", model, ok=False, latency_s=round(time.monotonic() - t0, 3), **self._err(exc))
        t0 = time.monotonic()
        unknown = "nvidia/does-not-exist-wb1215"
        try:
            self.client.chat.completions.create(model=unknown, messages=[{"role": "user", "content": "Smoke test: OK"}], max_tokens=8)
            self.record("error: unknown model", unknown, ok=True, note="UNEXPECTED: accepted")
        except Exception as exc:
            self.record("error: unknown model", unknown, ok=False, latency_s=round(time.monotonic() - t0, 3), **self._err(exc))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["models", "detail", "probe", "tools-unstripped", "vision", "errors"])
    ap.add_argument("models", nargs="*")
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "nvidia-nim-smoke.json")
    ap.add_argument("--image", type=Path, default=Path(__file__).resolve().parents[1] / "assets" / "banner.png")
    ap.add_argument("--checks", default="chat,stream,tools,context")
    a = ap.parse_args(argv)
    p = Probe(a.out)
    if a.cmd == "models":
        p.models()
    for m in a.models:
        if a.cmd == "detail":
            p.model_detail(m)
        elif a.cmd == "probe":
            for check in a.checks.split(","):
                getattr(p, check)(m)
        elif a.cmd == "tools-unstripped":
            p.tools(m, strip=False)
        elif a.cmd == "vision":
            p.vision(m, a.image)
        elif a.cmd == "errors":
            p.errors(m)
    print(f"calls_total={p.data['calls']} out={a.out}")


if __name__ == "__main__":
    main()
