"""Placeholder engine for a Jarvis whose owner has not connected an AI yet.

A self-service "Conecta tu IA" instance starts without any provider key: the
user adds it in the setup page.  Instead of refusing to start (which would
also take the setup page down), ``jarvis serve`` runs with this engine and
every request gets a short explanation until the key is saved and the
server restarts with the real engine.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Sequence

from openjarvis.core.types import Message
from openjarvis.engine._stubs import InferenceEngine

SETUP_MESSAGE = (
    "Todavía no tengo una IA conectada, así que aún no puedo ayudarte. "
    "Entra en tu página de Jarvis, ve al paso «Tu IA» y conecta tu cuenta "
    "(hay un tutorial paso a paso). En cuanto lo hagas, vuelve a escribirme."
)


class SetupPendingEngine(InferenceEngine):
    engine_id = "setup"

    def __init__(self, model: str = "") -> None:
        self._model = model or "pendiente"

    def generate(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return {
            "content": SETUP_MESSAGE,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "model": model,
            "finish_reason": "stop",
        }

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        yield SETUP_MESSAGE

    def list_models(self) -> List[str]:
        return [self._model]

    def health(self) -> bool:
        # The server (and its setup page) is healthy; only the AI is missing.
        return True


__all__ = ["SETUP_MESSAGE", "SetupPendingEngine"]
