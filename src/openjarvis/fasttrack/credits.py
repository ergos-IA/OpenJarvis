"""Per-instance AI credit meter.

Subscribes to the server's ``INFERENCE_END`` events, prices every model call
(see :mod:`openjarvis.fasttrack.plans`) and keeps a monthly balance in
``<home>/credits.json``.  For plans with included AI:

* warns the user on WhatsApp at 80 % and 100 % of the allowance;
* when it runs out, **Esencial** pauses (Jarvis answers that the credits are
  used up) and **Pro** switches to the cheaper fallback model until the next
  month or a top-up.

Plans where the customer brings their own AI ("own") are only counted.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from openjarvis.core.paths import get_config_dir
from openjarvis.fasttrack import plans

logger = logging.getLogger(__name__)

MODEL_CHANGE_COOLDOWN_S = 24 * 3600


def _period(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(now or time.time()))


class CreditMeter:
    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        plan_name: Optional[str] = None,
        notifier: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._path = Path(path or get_config_dir() / "credits.json")
        self._plan_name = plan_name
        self._notifier = notifier
        self._lock = threading.Lock()
        self._system: Any = None
        self._base_model = ""
        self._state = self._load()

    # -- state ----------------------------------------------------------------

    @property
    def plan(self) -> Dict[str, Any]:
        return plans.plan(self._plan_name)

    def _load(self) -> Dict[str, Any]:
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        state.setdefault("period", _period())
        state.setdefault("used", 0.0)
        state.setdefault("topup", 0.0)
        state.setdefault("warned", [])
        state.setdefault("downgraded", False)
        state.setdefault("model_changed_at", 0.0)
        state.setdefault("lifetime", 0.0)
        return state

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state), encoding="utf-8")
        tmp.replace(self._path)

    def _roll(self, now: Optional[float] = None) -> None:
        """New month: top-ups not spent carry over, the allowance resets."""
        current = _period(now)
        if self._state["period"] == current:
            return
        monthly = self.plan["credits"]
        over = max(0.0, self._state["used"] - monthly)
        self._state.update(
            period=current,
            used=0.0,
            topup=max(0.0, self._state["topup"] - over),
            warned=[],
            downgraded=False,
        )
        self._restore_model()
        self._save()

    # -- figures --------------------------------------------------------------

    def total(self) -> float:
        return self.plan["credits"] + self._state["topup"]

    def remaining(self) -> float:
        return max(0.0, self.total() - self._state["used"])

    def exhausted(self) -> bool:
        return self.plan["included_ai"] and self.remaining() <= 0

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._roll()
            p = self.plan
            total = self.total()
            used = self._state["used"]
            wait = max(
                0.0,
                self._state["model_changed_at"] + MODEL_CHANGE_COOLDOWN_S - time.time(),
            )
            return {
                "plan": p["key"],
                "plan_label": p["label"],
                "included_ai": p["included_ai"],
                "period": self._state["period"],
                "monthly": p["credits"],
                "topup": round(self._state["topup"], 1),
                "used": round(used, 1),
                "remaining": round(self.remaining(), 1),
                "percent": round(100 * used / total, 1) if total else 0,
                "exhausted": self.exhausted(),
                "downgraded": self._state["downgraded"],
                "models": p["models"],
                "model_change_in_s": int(wait) if p["included_ai"] else 0,
                "lifetime": round(self._state["lifetime"], 1),
            }

    # -- metering ---------------------------------------------------------------

    def record(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        cost = plans.credits_for(
            model, int(prompt_tokens or 0), int(completion_tokens or 0)
        )
        messages = []
        with self._lock:
            self._roll()
            self._state["used"] += cost
            self._state["lifetime"] += cost
            p = self.plan
            if p["included_ai"]:
                total = self.total()
                pct = 100 * self._state["used"] / total if total else 100
                for threshold in (80, 100):
                    if pct >= threshold and threshold not in self._state["warned"]:
                        self._state["warned"].append(threshold)
                        messages.append(self._warning(threshold))
                if (
                    self.exhausted()
                    and p["on_empty"] == "downgrade"
                    and not self._state["downgraded"]
                ):
                    self._state["downgraded"] = True
                    self._apply_fallback()
            self._save()
        for text in messages:
            self._notify(text)
        return cost

    def _warning(self, threshold: int) -> str:
        p = self.plan
        if threshold == 80:
            return (
                f"⚠️ Has usado el 80 % de los créditos de IA de tu plan {p['label']} "
                "este mes. Puedes añadir una recarga desde tu página de Jarvis."
            )
        if p["on_empty"] == "downgrade":
            return (
                f"🔋 Has agotado los créditos de IA de tu plan {p['label']}. "
                "Sigo funcionando en modo ahorro (modelo ligero) hasta el mes que "
                "viene o hasta que añadas una recarga."
            )
        return (
            f"🔋 Has agotado los créditos de IA de tu plan {p['label']} este mes. "
            "Vuelvo el día 1, o antes si añades una recarga desde tu página de Jarvis."
        )

    def blocked_reply(self) -> Optional[str]:
        """Text to answer instead of running the AI, or None to go ahead."""
        with self._lock:
            self._roll()
            if self.exhausted() and self.plan["on_empty"] == "pause":
                return (
                    "🔋 Has agotado los créditos de IA de este mes. Añade una recarga "
                    "desde tu página de Jarvis o espera al día 1."
                )
        return None

    def add_topup(self, credits: float) -> Dict[str, Any]:
        with self._lock:
            self._roll()
            self._state["topup"] += float(credits)
            if not self.exhausted():
                self._state["warned"] = [w for w in self._state["warned"] if w != 100]
                if self._state["downgraded"]:
                    self._state["downgraded"] = False
                    self._restore_model()
            self._save()
        return self.status()

    # -- model ----------------------------------------------------------------

    def can_change_model(self) -> bool:
        if not self.plan["included_ai"]:
            return True
        return time.time() - self._state["model_changed_at"] >= MODEL_CHANGE_COOLDOWN_S

    def mark_model_changed(self) -> None:
        with self._lock:
            self._state["model_changed_at"] = time.time()
            self._save()

    def _apply_fallback(self) -> None:
        fallback = self.plan.get("fallback")
        if self._system is not None and fallback:
            self._system.model = fallback
            logger.info("Credits exhausted: switched to %s", fallback)

    def _restore_model(self) -> None:
        if self._system is not None and self._base_model:
            self._system.model = self._base_model

    # -- wiring -----------------------------------------------------------------

    def attach(self, bus: Any = None, system: Any = None) -> None:
        if system is not None:
            self._system = system
            self._base_model = getattr(system, "model", "") or ""
            if self._state.get("downgraded") and self._state["period"] == _period():
                self._apply_fallback()
        if bus is not None and not getattr(bus, "_jarvis_credit_meter", False):
            from openjarvis.core.events import EventType

            bus.subscribe(EventType.INFERENCE_END, self._on_inference)
            bus._jarvis_credit_meter = True

    def _on_inference(self, event: Any) -> None:
        data = getattr(event, "data", event) or {}
        usage = data.get("usage") or {}
        try:
            self.record(
                data.get("model", ""),
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )
        except Exception:
            logger.exception("Credit metering failed")

    def _notify(self, text: str) -> None:
        if self._notifier is not None:
            self._notifier(text)
            return
        try:
            from openjarvis.channels.whatsapp_baileys import get_active_channel

            channel = get_active_channel()
            own = channel.self_jid() if channel is not None else ""
            if own:
                channel.send(own, text)
        except Exception:
            logger.debug("Credit warning not delivered", exc_info=True)


_meter: Optional[CreditMeter] = None
_meter_lock = threading.Lock()


def get_meter() -> CreditMeter:
    global _meter
    with _meter_lock:
        if _meter is None:
            _meter = CreditMeter()
        return _meter


def reset_meter() -> None:
    global _meter
    with _meter_lock:
        _meter = None


__all__ = ["CreditMeter", "MODEL_CHANGE_COOLDOWN_S", "get_meter", "reset_meter"]
