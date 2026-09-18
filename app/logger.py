"""应用日志：同时输出到控制台（journalctl 可见）和滚动日志文件。"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

import config

FMT = "%(asctime)s %(levelname)-7s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    logger = logging.getLogger("pxe")
    if _configured:
        return logger

    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    logger.propagate = False
    formatter = logging.Formatter(FMT, datefmt=DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    try:
        directory = os.path.dirname(config.LOG_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.LOG_FILE,
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as exc:  # noqa: BLE001
        logger.warning("无法写入日志文件 %s（%s），仅输出到控制台", config.LOG_FILE, exc)

    _configured = True
    return logger


log = setup_logging()
