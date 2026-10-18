"""Nebius Token Factory provider profile."""

from typing import Any

from agent.reasoning_effort import NEBIUS_EFFORTS, clamp_effort
from providers import register_provider
from providers.base import ProviderProfile

# Conservative allowlist of model families that expose reasoning effort.
_REASONING_MARKERS = (
    "deepseek-r1", "deepseek-v4", "deepseek-reasoner", "gpt-oss", "glm-5", "kimi-k2", "kimi-k3", "minimax-m2", "minimax-m3", "qwen3",
)


class NebiusTokenFactoryProfile(ProviderProfile):
    """Nebius Token Factory - top-level reasoning_effort."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None,
        supports_reasoning: bool = False, **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        model_name = (model or "").strip().rsplit("/", 1)[-1].lower()
        if not supports_reasoning and not any(marker in model_name for marker in _REASONING_MARKERS):
            return {}, {}
        rc = reasoning_config if isinstance(reasoning_config, dict) else {}
        # Unset/blank effort defaults to medium (reasoning ON).
        effort = str(rc.get("effort", "medium") or "medium").strip().lower()
        if rc.get("enabled", True) is False or effort in {"none", "off", "disabled"}:
            return {}, {}
        # Canonical clamp: nearest weaker supported level, never escalate.
        return {}, {"reasoning_effort": clamp_effort(effort, NEBIUS_EFFORTS) or "medium"}


nebius_token_factory = NebiusTokenFactoryProfile(
    name="nebius-token-factory",
    aliases=("nebius", "nebius-tokenfactory", "nebius-tf", "token-factory", "tokenfactory"),
    display_name="Nebius Token Factory", description="Nebius Token Factory — OpenAI-compatible inference",
    signup_url="https://tokenfactory.nebius.com/",
    env_vars=("NEBIUS_API_KEY", "NEBIUS_TOKEN_FACTORY_API_KEY", "NEBIUS_BASE_URL"),
    base_url="https://api.tokenfactory.nebius.com/v1",
    models_url="https://api.tokenfactory.nebius.com/v1/models?verbose=true", auth_type="api_key",
    default_aux_model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    # Offline floor only: the picker is live-first for this provider (_LIVE_FIRST_PICKER_PROVIDERS),
    # so these ids matter when no key is set or /v1/models fails. Keep them to ids the live API serves.
    fallback_models=(
        "moonshotai/Kimi-K3", "moonshotai/Kimi-K2.6", "deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4.1-Flash",
        "zai-org/GLM-5.3", "Qwen/Qwen3.5-397B-A17B", "MiniMaxAI/MiniMax-M3", "nvidia/Nemotron-3-Ultra-550b-a55b",
        "NousResearch/Hermes-4-405B", "openai/gpt-oss-120b",
    ),
)

register_provider(nebius_token_factory)
