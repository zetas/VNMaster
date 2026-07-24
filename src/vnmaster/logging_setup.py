"""Project-wide logging configuration.

A single `configure_logging` call at the start of each entrypoint installs a
rotating file handler and a stderr handler. Module code uses `get_logger`.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def configure_logging(log_file: Path, level: int = logging.INFO) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    formatter = logging.Formatter(_DEFAULT_FORMAT)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
