"""Personal email for the assistant: Gmail (IMAP/SMTP) and Outlook (Graph).

Unlike :mod:`openjarvis.connectors`, which bulk-index mail into the knowledge
store, this package talks to the mailbox live: list/search/read on demand,
notify about new mail, and send (only after the user confirms).
"""

from openjarvis.mail.service import MailService, get_mail_service

__all__ = ["MailService", "get_mail_service"]
