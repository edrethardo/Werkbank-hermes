"""NVIDIA NIM model list (WB-1216).

Only ids confirmed against the live endpoint in the WB-1215 smoke
(``docs/notes/nvidia-nim-smoke-2026-10.md``, ``GET /v1/models`` on 19.10.2026)
may be offered. ``nvidia/llama-3.3-70b-instruct`` is no longer served; Nemotron 3
Super is the default (fast, tool round-trip and streaming confirmed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/nemotron-3-ultra-550b-a55b"
SUPER_VL = "nvidia/nemotron-3.5-super-vl-120b-a12b"
CONFIRMED_PILOTS = (SUPER, ULTRA, SUPER_VL)

_SMOKE = Path(__file__).resolve().parents[3] / "docs" / "notes" / "nvidia-nim-smoke-2026-10.json"


@pytest.fixture
def nvidia_profile():
    import model_tools  # noqa: F401 — plugin discovery registers the profile
    import providers

    profile = providers.get_provider_profile("nvidia")
    assert profile is not None
    return profile


def _served_ids() -> set[str]:
    return set(json.loads(_SMOKE.read_text(encoding="utf-8"))["models"]["ids"])


def test_fallback_models_are_exactly_the_confirmed_pilots_super_first(nvidia_profile):
    assert tuple(nvidia_profile.fallback_models) == CONFIRMED_PILOTS


def test_retired_llama_ids_are_gone(nvidia_profile):
    assert "nvidia/llama-3.3-70b-instruct" not in nvidia_profile.fallback_models
    assert not any("llama" in m for m in nvidia_profile.fallback_models)


def test_static_catalog_defaults_to_super():
    from hermes_cli.models import _PROVIDER_MODELS, get_default_model_for_provider

    assert _PROVIDER_MODELS["nvidia"][0] == SUPER
    assert get_default_model_for_provider("nvidia") == SUPER


def test_every_offered_id_was_served_by_the_endpoint(nvidia_profile):
    """Picker rows (curated list + plugin floor) must all appear in the measured /v1/models."""
    from hermes_cli.models import _PROVIDER_MODELS

    served = _served_ids()
    assert len(served) == 81  # the smoke's GET /v1/models count
    offered = set(_PROVIDER_MODELS["nvidia"]) | set(nvidia_profile.fallback_models)
    assert offered - served == set()


def test_keyless_picker_offers_the_confirmed_pilots(monkeypatch):
    from hermes_cli import models

    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr(models, "_api_key_credentials", lambda _p: (None, None))
    rows = models.provider_model_ids("nvidia")
    assert rows is not None
    assert list(rows)[:3] == list(CONFIRMED_PILOTS)
