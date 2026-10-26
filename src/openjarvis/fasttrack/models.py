"""Models offered to Jarvis users, and how to describe the active one.

Each entry says which engine runs it and which credential it needs (a shared
variable in the Railway project).  ``custom`` entries let the admin type any
model id the provider supports.
"""

from __future__ import annotations

from typing import Any, Dict, List

PROVIDERS: Dict[str, Dict[str, str]] = {
    "anthropic": {"label": "Anthropic", "credential": "ANTHROPIC_API_KEY"},
    "openai": {"label": "OpenAI", "credential": "OPENAI_API_KEY"},
    "openrouter": {
        "label": "Open source (OpenRouter)",
        "credential": "OPENROUTER_API_KEY",
    },
    "ollama": {"label": "Servidor propio (Ollama)", "credential": "OLLAMA_HOST"},
}

MODELS: List[Dict[str, Any]] = [
    {
        "id": "claude-sonnet-5-5",
        "label": "Claude Sonnet 5.5",
        "provider": "anthropic",
        "note": "Recomendado: el más fiable usando herramientas",
    },
    {
        "id": "claude-haiku-5-5",
        "label": "Claude Haiku 5.5",
        "provider": "anthropic",
        "note": "Rápido y económico",
    },
    {
        "id": "claude-opus-5-5",
        "label": "Claude Opus 5.5",
        "provider": "anthropic",
        "note": "Máxima calidad, el más caro",
    },
    {
        "id": "gpt-5.4",
        "label": "GPT-5.4",
        "provider": "openai",
        "note": "Modelo principal de OpenAI",
    },
    {
        "id": "gpt-5-mini",
        "label": "GPT-5 mini",
        "provider": "openai",
        "note": "Económico",
    },
    {
        "id": "openrouter/meta-llama/llama-3.3-70b-instruct",
        "label": "Llama 3.3 70B",
        "provider": "openrouter",
        "note": "Open source (Meta)",
    },
    {
        "id": "openrouter/qwen/qwen3-235b-a22b",
        "label": "Qwen3 235B",
        "provider": "openrouter",
        "note": "Open source (Alibaba)",
    },
    {
        "id": "openrouter/mistralai/mistral-large",
        "label": "Mistral Large",
        "provider": "openrouter",
        "note": "Open weights (Mistral, europeo)",
    },
    {
        "id": "qwen2.5:7b",
        "label": "Qwen2.5 7B en servidor propio",
        "provider": "ollama",
        "note": "Requiere un servidor Ollama con GPU (OLLAMA_HOST)",
    },
]

DEFAULT_MODEL = "claude-sonnet-5-5"


def provider_for(model_id: str, engine: str = "") -> str:
    """Provider key for *model_id* (also for ids not in the catalog)."""
    if (engine or "").lower() == "ollama":
        return "ollama"
    for entry in MODELS:
        if entry["id"] == model_id:
            return entry["provider"]
    if model_id.startswith("openrouter/"):
        return "openrouter"
    if "claude" in model_id:
        return "anthropic"
    return "openai"


def engine_for(provider: str) -> str:
    return "ollama" if provider == "ollama" else "cloud"


def describe(model_id: str, engine: str = "") -> Dict[str, Any]:
    """Human description of the active model for the user-facing page."""
    provider = provider_for(model_id, engine)
    label = next((m["label"] for m in MODELS if m["id"] == model_id), "")
    if not label:
        label = model_id.split("/")[-1] if model_id else "—"
    return {
        "model": model_id,
        "label": label,
        "provider": provider,
        "provider_label": PROVIDERS[provider]["label"],
        "open_source": provider in ("openrouter", "ollama"),
    }


__all__ = [
    "DEFAULT_MODEL",
    "MODELS",
    "PROVIDERS",
    "describe",
    "engine_for",
    "provider_for",
]
