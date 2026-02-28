"""Centralized logging with IBAN masking."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_IBAN_PATTERN = re.compile(r"\b([A-Z]{2}\d{2})[\dA-Z]{4,}([\dA-Z]{4})\b")


class _MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _mask(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: (_mask(v) if isinstance(v, str) else v) for k, v in record.args.items()}
            else:
                record.args = tuple((_mask(a) if isinstance(a, str) else a) for a in record.args)
        return True


def _mask(text: str) -> str:
    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        full = m.group(0)
        if len(full) < 8:
            return full
        return full[:4] + "*" * (len(full) - 8) + full[-4:]
    return _IBAN_PATTERN.sub(_replace, text)


def setup_logger(name: str = "byMCP") -> logging.Logger:
    log_dir = Path.home() / ".byMCP" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    console.addFilter(_MaskingFilter())

    fh = RotatingFileHandler(
        log_dir / "server.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d – %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    fh.addFilter(_MaskingFilter())

    logger.addHandler(console)
    logger.addHandler(fh)
    return logger


logger = setup_logger()
