"""External tools a user connects to their Jarvis (Notion, Google, Slack...)."""

from openjarvis.integrations.base import Integration, IntegrationError, registry
from openjarvis.integrations.manager import IntegrationManager, get_manager

__all__ = [
    "Integration",
    "IntegrationError",
    "IntegrationManager",
    "get_manager",
    "registry",
]
