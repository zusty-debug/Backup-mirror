from __future__ import annotations

import logging
import re


class SecretRedactionFilter(logging.Filter):
    """Best-effort redaction for common Telegram secret shapes."""

    _patterns = (
        re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b"),  # Bot token
        re.compile(r"\b[0-9a-fA-F]{32}\b"),  # API hash
        re.compile(r"\b\+?\d{10,15}\b"),  # phone-like numbers
    )

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        for pattern in self._patterns:
            rendered = pattern.sub("[REDACTED]", rendered)
        record.msg = rendered
        record.args = ()
        return True


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    root.addHandler(handler)
