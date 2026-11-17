import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime

from .config import config, BASE_DIR


def setup_logger(name: str = "wechat_publisher") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(config.log_level)

    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_format)
    logger.addHandler(console_handler)

    log_file = log_dir / f"publisher_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = TimedRotatingFileHandler(
        log_file, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(log_format)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger()
