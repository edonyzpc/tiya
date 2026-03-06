from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


LOGGER_NAME = "tiya"
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def configure_logging(log_path: Optional[Path] = None) -> logging.Logger:
    logger = get_logger()
    desired_path = str(log_path) if log_path else None
    current_path = getattr(logger, "_tiya_log_path", None)
    if logger.handlers and current_path == desired_path:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S")

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    setattr(logger, "_tiya_log_path", desired_path)
    return logger


def _level_for_message(msg: str) -> tuple[int, str]:
    lowered = msg.lower()
    if lowered.startswith("[error]"):
        return logging.ERROR, msg[7:].strip()
    if lowered.startswith("[warn]"):
        return logging.WARNING, msg[6:].strip()
    if lowered.startswith("[debug]"):
        return logging.DEBUG, msg[7:].strip()
    if lowered.startswith("[info]"):
        return logging.INFO, msg[6:].strip()
    return logging.INFO, msg


def log(msg: str) -> None:
    logger = get_logger()
    if not logger.handlers:
        configure_logging()
    level, body = _level_for_message(msg)
    logger.log(level, body)
