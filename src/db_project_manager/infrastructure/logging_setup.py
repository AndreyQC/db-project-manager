"""Centralized loguru setup.

A single configure() call wires console + file sinks. Importing this module
does NOT configure logging on its own (avoids side effects at import time,
unlike the POC logger that ran setup_logger on import).
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_CONFIGURED = False

_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"


def configure(
    *,
    level: str = "INFO",
    console: bool = True,
    logs_dir: str | Path = "logs",
    file_name: str = "app.log",
) -> None:
    """Configure loguru sinks. Safe to call once; subsequent calls reconfigure.

    Args:
        level: Minimum level for console and file sinks.
        console: Whether to emit to stderr.
        logs_dir: Directory for log files.
        file_name: Log file name.
    """
    global _CONFIGURED
    logger.remove()

    if console:
        logger.add(sys.stderr, format=_CONSOLE_FORMAT, level=level)

    logs_path = Path(logs_dir)
    logs_path.mkdir(parents=True, exist_ok=True)
    logger.add(logs_path / file_name, format=_FILE_FORMAT, level="DEBUG", rotation="500 MB", retention="10 days")
    logger.add(logs_path / "error.log", format=_FILE_FORMAT, level="ERROR", rotation="100 MB", retention="30 days")

    _CONFIGURED = True


def get_logger():
    """Return the configured loguru logger."""
    if not _CONFIGURED:
        configure()
    return logger
