"""Render $OPENJARVIS_HOME/config.toml from environment variables.

Runs on every container start.  Only the keys below are managed; anything
else already in config.toml (e.g. settings changed from the web UI) is kept.

Environment variables (all optional):

  JARVIS_ENGINE              engine.default                      (cloud)
  JARVIS_MODEL               intelligence.default_model          (gpt-4o-mini)
  JARVIS_ASSISTANT_NAME      channel.whatsapp_baileys.assistant_name (Jarvis)
  JARVIS_CHANNEL_AGENT       channel.default_agent               (orchestrator)
  JARVIS_TOOLS               tools.enabled, comma-separated      (see _TOOLS)
  WHATSAPP_ENABLED           true/false: start the WhatsApp channel (true)
  WHATSAPP_PROVIDER          personal (linked device) | meta (official Business API)
  WHATSAPP_MODE              self | own                          (self)
  WHATSAPP_ALLOWED_NUMBERS   numbers allowed in "own" mode, comma-separated
  WHATSAPP_NOTIFY            all | contacts | none: forward incoming
                             messages to your own chat           (all)
  WHATSAPP_CONFIRM_SENDS     true/false: "sí" needed to send     (true)
"""

from __future__ import annotations

import os
from pathlib import Path

import tomlkit

_TOOLS = (
    "think,calculator,web_search,"
    "whatsapp_read_messages,whatsapp_find_contact,whatsapp_send_message,"
    "email_list,email_read,email_send,"
    "reminder_create,reminder_list,reminder_cancel,remember_fact,forget_fact"
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes", "on", "si", "sí")


def _table(doc, *path: str):
    node = doc
    for key in path:
        if key not in node:
            node[key] = tomlkit.table()
        node = node[key]
    return node


def main() -> None:
    home = Path(_env("OPENJARVIS_HOME", str(Path.home() / ".openjarvis")))
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    _table(doc, "engine")["default"] = _env("JARVIS_ENGINE", "cloud")
    _table(doc, "intelligence")["default_model"] = _env("JARVIS_MODEL", "gpt-4o-mini")

    analytics = _table(doc, "analytics")
    analytics["enabled"] = False
    analytics["choice_recorded"] = True

    whatsapp_on = _truthy(_env("WHATSAPP_ENABLED", "true"))
    # personal = Baileys linked device; meta = official WhatsApp Business API
    provider = _env("WHATSAPP_PROVIDER", "personal").lower()
    wa_channel = (
        "whatsapp_cloud" if provider in ("meta", "business") else "whatsapp_baileys"
    )
    channel = _table(doc, "channel")
    channel["enabled"] = whatsapp_on
    channel["default_channel"] = wa_channel if whatsapp_on else ""
    channel["default_agent"] = _env("JARVIS_CHANNEL_AGENT", "orchestrator")
    _table(doc, "tools")["enabled"] = _env("JARVIS_TOOLS", _TOOLS)

    wa = _table(doc, "channel", "whatsapp_baileys")
    wa["assistant_name"] = _env("JARVIS_ASSISTANT_NAME", "Jarvis")
    wa["assistant_has_own_number"] = _env("WHATSAPP_MODE", "self").lower() == "own"
    wa["allowed_numbers"] = _env("WHATSAPP_ALLOWED_NUMBERS")
    wa["auth_dir"] = str(home / "whatsapp_auth")
    notify = _env("WHATSAPP_NOTIFY", "all").lower()
    wa["notify"] = notify if notify in ("all", "contacts", "none") else "all"
    wa["confirm_sends"] = _truthy(_env("WHATSAPP_CONFIRM_SENDS", "true"))

    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    mode = "own" if wa["assistant_has_own_number"] else "self"
    print(
        f"config.toml listo: engine={doc['engine']['default']} "
        f"model={doc['intelligence']['default_model']} "
        f"whatsapp={'on (' + mode + ')' if whatsapp_on else 'off'}"
    )


# Variables a user may set from the "Conecta tu IA" wizard step.  They live
# in <home>/ai.json (written by server/ai_routes.py) and override the
# service's environment, so a customer's own API key never has to go
# through Railway.
_AI_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "JARVIS_MODEL",
    "JARVIS_ENGINE",
)


def apply_ai_overrides() -> None:
    import json

    home = Path(_env("OPENJARVIS_HOME", str(Path.home() / ".openjarvis")))
    try:
        data = json.loads((home / "ai.json").read_text(encoding="utf-8"))
    except Exception:
        return
    for name in _AI_OVERRIDES:
        value = data.get("env", {}).get(name)
        if isinstance(value, str) and value:
            os.environ[name] = value


def serve() -> None:
    """Render the config and replace this process with ``jarvis serve``.

    Also how the server restarts itself after the AI settings change
    (``os.execv`` back into this script), without a container restart.
    """
    apply_ai_overrides()
    main()
    os.execvp(
        "jarvis",
        [
            "jarvis",
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            _env("PORT", "8000"),
            "--engine",
            _env("JARVIS_ENGINE", "cloud"),
            "--model",
            _env("JARVIS_MODEL", "gpt-4o-mini"),
        ],
    )


if __name__ == "__main__":
    import sys

    if "--serve" in sys.argv[1:]:
        serve()
    else:
        main()
