"""Built-in integrations; importing this package registers them all."""

import importlib
import logging

logger = logging.getLogger(__name__)

# Order = order in the catalogue (grouped by category in the UI).
_MODULES = [
    "agenda",
    "google",
    "notion",
    "todoist",
    "trello",
    "slack",
    "holded",
    "hubspot",
    "airtable",
    "github",
    "mcp_server",
    "weather",
    "news",
    "granola",
    "oura",
    "strava",
    "spotify",
    "dropbox",
]

for _name in _MODULES:
    try:
        importlib.import_module(f"{__name__}.{_name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}.{_name}":
            logger.warning("Integration %s unavailable: %s", _name, exc)
    except Exception:
        logger.exception("Integration %s failed to load", _name)
