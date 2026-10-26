"""Speech-to-text for WhatsApp voice notes.

Uses OpenAI's transcription API when the instance has an ``OPENAI_API_KEY``
(the user's own key), otherwise a local Whisper model (faster-whisper on CPU,
int8) that runs inside the user's own instance: nothing leaves the server.

The local model is downloaded once into ``<home>/models`` and unloaded after
a few idle minutes, so it only uses memory while transcribing.

Environment: ``JARVIS_WHISPER_MODEL`` (default ``small``),
``JARVIS_VOICE_LANGUAGE`` (default ``es``; empty = autodetect).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

IDLE_UNLOAD_S = 10 * 60

_lock = threading.Lock()
_backend: Any = None
_unload_timer: Optional[threading.Timer] = None


class VoiceError(Exception):
    """Spanish, user-facing."""


def _format(mimetype: str) -> str:
    mt = (mimetype or "").split(";")[0].strip().lower()
    return {
        "audio/ogg": "ogg",
        "audio/opus": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/aac": "aac",
        "audio/amr": "amr",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/webm": "webm",
    }.get(mt, "ogg")


def _language() -> Optional[str]:
    lang = os.environ.get("JARVIS_VOICE_LANGUAGE", "es").strip()
    return lang or None


def _local_backend() -> Any:
    from openjarvis.core.paths import get_config_dir

    os.environ.setdefault("HF_HOME", str(get_config_dir() / "models"))
    try:
        from openjarvis.speech.faster_whisper import FasterWhisperBackend
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise VoiceError("La transcripción de audios no está instalada.") from exc
    return FasterWhisperBackend(
        model_size=os.environ.get("JARVIS_WHISPER_MODEL", "small"),
        device="cpu",
        compute_type="int8",
    )


def _get_backend() -> Any:
    global _backend
    if os.environ.get("OPENAI_API_KEY"):
        from openjarvis.speech.openai_whisper import OpenAIWhisperBackend

        return OpenAIWhisperBackend()
    if _backend is None:
        _backend = _local_backend()
    return _backend


def _schedule_unload() -> None:
    global _unload_timer
    if _unload_timer is not None:
        _unload_timer.cancel()
    _unload_timer = threading.Timer(IDLE_UNLOAD_S, _unload)
    _unload_timer.daemon = True
    _unload_timer.start()


def _unload() -> None:
    global _backend
    with _lock:
        _backend = None
    logger.info("Local speech model unloaded (idle)")


def transcribe(audio: bytes, mimetype: str = "audio/ogg") -> str:
    """Text of a voice note.  Raises :class:`VoiceError`."""
    if not audio:
        raise VoiceError("El audio está vacío.")
    with _lock:
        backend = _get_backend()
        try:
            result = backend.transcribe(
                audio, format=_format(mimetype), language=_language()
            )
        except VoiceError:
            raise
        except Exception as exc:
            logger.exception("Transcription failed")
            raise VoiceError("No he podido entender el audio.") from exc
        finally:
            if backend is _backend:
                _schedule_unload()
    return (getattr(result, "text", "") or "").strip()


def duration_label(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def prefetch() -> None:
    """Download the local model (disk only, not RAM) so the first voice note
    is not slow.  Runs in a background thread at start-up."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    try:
        from openjarvis.core.paths import get_config_dir

        os.environ.setdefault("HF_HOME", str(get_config_dir() / "models"))
        from faster_whisper import download_model

        download_model(os.environ.get("JARVIS_WHISPER_MODEL", "small"))
        logger.info("Speech model ready")
    except Exception:
        logger.debug("Speech model prefetch skipped", exc_info=True)


__all__ = ["VoiceError", "duration_label", "prefetch", "transcribe"]
